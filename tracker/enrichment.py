"""Product enrichment: names, nutrition and images from external sources.

Data sources (see README "Data sources & attribution"):
- Open Food Facts (https://world.openfoodfacts.org): free JSON API, no key,
  ODbL licensed. Primary source for packaged food looked up by GTIN.
- Open Beauty Facts (https://world.openbeautyfacts.org): same platform and
  API shape, used for personal-care / cosmetic GTINs.
- Mercado Livre: official catalog API when MELI_ACCESS_TOKEN is configured,
  otherwise the public search JSON endpoint, otherwise HTML scraping as a
  last resort (datacenter IPs are frequently bot-flagged by ML, so the
  scrape path is expected to fail from servers — failures are logged, not
  hidden).
- Local keyword heuristics for NOVA groups when no API has data.

All JSON traffic goes through _fetch_json(), which retries transient
failures with backoff and returns a machine-readable outcome so background
tasks can distinguish "not found" from "blocked/timeout" in the logs.
"""
import logging
import os
import re
import time
import uuid

import requests
from bs4 import BeautifulSoup
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone
from rapidfuzz import fuzz

logger = logging.getLogger(__name__)

OFF_BASE = "https://world.openfoodfacts.org"
OBF_BASE = "https://world.openbeautyfacts.org"
# Slim payloads: full product documents are huge; we only need these fields.
OPENFACTS_FIELDS = (
    "code,product_name,product_name_pt,generic_name,brands,"
    "nova_group,nutriscore_grade,ecoscore_grade,nutriments,"
    "image_front_url"
)
MELI_SEARCH_URL = "https://api.mercadolibre.com/sites/MLB/search"
MELI_ITEM_URL = "https://api.mercadolibre.com/items/{}"
MELI_CATALOG_URL = "https://api.mercadolibre.com/products/search"
MELI_LIST_URL = "https://lista.mercadolivre.com.br/{}"

# Minimum accepted image side (px). Rejects tracking pixels and tiny icon
# thumbnails while keeping narrow-but-real product shots.
MIN_IMAGE_SIDE_PX = 100
ALLOWED_IMAGE_EXTS = ("jpg", "jpeg", "png", "webp", "gif")

# Title-similarity floors (RapidFuzz token_set_ratio, 0-100). GTIN-identified
# lookups are lenient because the barcode already identifies the product;
# name searches must clear a higher bar so wrong products (and their images)
# are never attached.
TITLE_MATCH_GTIN = 55
TITLE_MATCH_NAME = 65

# Personal-care keywords: GTINs matching these also try Open Beauty Facts
# when Open Food Facts comes up empty.
PERSONAL_CARE_KEYWORDS = [
    'shampoo', 'condicionador', 'sabonete', 'creme dental', 'pasta de dente',
    'pasta dental', 'escova dental', 'escova de dente', 'desodorante',
    'perfume', 'colonia', 'colônia', 'hidratante', 'protetor solar',
    'protetor labial', 'maquiagem', 'batom', 'esmalte', 'base facial',
    'fralda', 'absorvente', 'lenco umedecido', 'lenço umedecido',
    'talco', 'saboneteira', 'aparelho de barbear', 'lamina de barbear',
    'higiene', 'beleza', 'cosmetico', 'cosmético', 'perfumaria',
]


class ProductEnrichmentService:
    # Source confidence: higher is better. image_url participates via
    # metadata['source_image_url'] so a weak source can never overwrite the
    # image found by a stronger one.
    CONFIDENCE = {
        'manual': 100,
        'off_gtin': 90,
        'obf_gtin': 85,
        'meli_catalog': 85,
        'api_gtin': 80,
        'off_name': 60,
        'ml_search': 50,
        'heuristic': 20,
        'none': 0,
    }

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _user_agent():
        contact = getattr(
            settings, 'ENRICHMENT_CONTACT',
            os.environ.get(
                'ENRICHMENT_CONTACT',
                'GroceriesTracker (+https://github.com/Finrood/GroceriesTracker)',
            ),
        )
        return f"GroceriesTracker/1.0 ({contact})"

    @staticmethod
    def _get_headers(html=False):
        return {
            # Open Food Facts *requires* an identifying UA (fake browser UAs
            # risk being blocked as bots); it is also polite everywhere else.
            'User-Agent': ProductEnrichmentService._user_agent(),
            'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                       'image/avif,image/webp,*/*;q=0.8'
                       if html else 'application/json'),
            'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        }

    @staticmethod
    def _fetch_json(url, params=None, source='api', timeout=15, retries=2,
                    extra_headers=None):
        """GET JSON with backoff.

        Returns (payload_or_None, outcome) where outcome is one of:
        ok / not_found / rate_limited / timeout / blocked / bad_payload /
        http_<code> / error. Callers log non-ok outcomes instead of hiding
        them, so ops can tell "product missing" from "we are blocked".
        """
        headers = ProductEnrichmentService._get_headers()
        if extra_headers:
            headers.update(extra_headers)
        outcome = 'error'
        for attempt in range(retries + 1):
            try:
                res = requests.get(url, params=params, headers=headers,
                                   timeout=timeout)
                code = res.status_code
                if code == 200:
                    try:
                        return res.json(), 'ok'
                    except ValueError:
                        return None, 'bad_payload'
                if code == 404:
                    return None, 'not_found'
                if code == 429:
                    outcome = 'rate_limited'
                elif code in (401, 403):
                    return None, 'blocked'
                elif 500 <= code < 600:
                    outcome = f'http_{code}'
                else:
                    return None, f'http_{code}'
            except (requests.Timeout, requests.ConnectTimeout):
                outcome = 'timeout'
            except requests.ConnectionError:
                outcome = 'connection_error'
            except requests.RequestException:
                outcome = 'error'
                break
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
        logger.warning("Enrichment request failed [%s]: %s -> %s",
                       source, url if not params else f"{url} {params}",
                       outcome)
        return None, outcome

    @staticmethod
    def _titles_match(candidate, reference, threshold):
        """True when a marketplace title plausibly names our product."""
        if not candidate or not reference:
            return False
        clean = lambda s: re.sub(r'\s+', ' ', str(s)).strip().lower()
        return fuzz.token_set_ratio(clean(candidate), clean(reference)) >= threshold

    # ------------------------------------------------------------------
    # Local image handling
    # ------------------------------------------------------------------
    @staticmethod
    def download_local_image(product, force=False):
        if not product.image_url:
            return False
        if product.local_image and not force:
            return False
        try:
            response = requests.get(
                product.image_url,
                headers=ProductEnrichmentService._get_headers(),
                timeout=15,
            )
            if response.status_code != 200:
                logger.warning("Image download HTTP %s for product %s",
                               response.status_code, product.id)
                return False
            content = response.content
            if len(content) < 5120 or len(content) > 10 * 1024 * 1024:
                return False
            # Validate the payload is a real, sensibly-sized image before
            # serving it from our origin: a scraped "image" URL can return
            # HTML/JS (stored XSS) or a 1px tracker.
            from io import BytesIO
            from PIL import Image
            try:
                img = Image.open(BytesIO(content))
                img.load()
                if min(img.size) < MIN_IMAGE_SIDE_PX:
                    logger.warning(
                        "Rejected tiny image %s for product %s from %s",
                        img.size, product.id, product.image_url[:100])
                    return False
            except Exception:
                logger.warning("Rejected non-image payload for product %s from %s",
                               product.id, product.image_url[:100])
                return False
            ctype = response.headers.get('Content-Type', '')
            if ctype and not ctype.startswith('image/'):
                return False
            match = re.search(r'\.([a-z0-9]{3,4})(?:[?#]|$)',
                              product.image_url, re.IGNORECASE)
            ext = (match.group(1).lower() if match else 'jpg')
            if ext not in ALLOWED_IMAGE_EXTS:
                ext = 'jpg'
            # GTIN-less products get a random stable name: numeric product IDs
            # change across databases, so id-based filenames orphan easily.
            base = product.code_gtin or f"noid-{uuid.uuid4().hex[:12]}"
            if force and product.local_image:
                product.local_image.delete(save=False)
                product.local_image = None
            filename = f"{base}.{ext}"
            product.local_image.save(filename, ContentFile(content), save=True)
            return True
        except Exception:
            logger.exception("Image download failed for product %s", product.id)
        return False

    @staticmethod
    def prune_orphan_product_images(dry_run=False, media_root=None):
        """Delete media/products/* files referenced by no Product.

        Returns the list of removed (or would-be-removed) filenames.
        MANUAL USE ONLY: never call this from automated maintenance — a test
        or dev database legitimately references nothing and would classify
        the whole production media dir as orphan.
        """
        from pathlib import Path
        from django.conf import settings as dj_settings
        root = Path(media_root or dj_settings.MEDIA_ROOT) / 'products'
        if not root.is_dir():
            return []
        from .models import Product
        referenced = {
            Path(str(v)).name
            for v in Product.objects.exclude(
                local_image__isnull=True).exclude(
                local_image='').values_list('local_image', flat=True)
        }
        removed = []
        for path in sorted(root.iterdir()):
            if path.is_file() and path.name not in referenced:
                removed.append(path.name)
                if not dry_run:
                    path.unlink()
        if removed:
            logger.warning("Pruned %d orphan product images%s: %s",
                           len(removed),
                           ' (dry-run)' if dry_run else '',
                           ', '.join(removed[:10]))
        return removed

    # ------------------------------------------------------------------
    # Metadata bookkeeping
    # ------------------------------------------------------------------
    @staticmethod
    def _log_history(product, field, new_value, source):
        if 'history' not in product.metadata:
            product.metadata['history'] = []
        product.metadata['history'].append({
            'date': timezone.now().isoformat(),
            'field': field,
            'source': source,
            'value': str(new_value)[:100],
        })
        product.metadata['history'] = product.metadata['history'][-10:]

    @staticmethod
    def _can_update(product, field, new_source):
        if product.is_manually_edited and field == 'display_name':
            return False
        current_source = product.metadata.get(f'source_{field}', 'none')
        return (ProductEnrichmentService.CONFIDENCE.get(new_source, 0)
                >= ProductEnrichmentService.CONFIDENCE.get(current_source, 0))

    @staticmethod
    def _set_image(product, url, source):
        """Assign image_url honoring source confidence. Returns True if set."""
        if not url or product.image_url == url:
            return False
        if not ProductEnrichmentService._can_update(product, 'image_url', source):
            logger.info("Keeping %s image for product %s (stronger than %s)",
                        product.metadata.get('source_image_url'),
                        product.id, source)
            return False
        product.image_url = url
        product.metadata['source_image_url'] = source
        ProductEnrichmentService._log_history(product, 'image_url', url, source)
        return True

    @staticmethod
    def _set_display_name(product, title, source):
        if not title:
            return False
        title = re.sub(r'\s+', ' ', str(title)).strip()
        if not title or not ProductEnrichmentService._can_update(
                product, 'display_name', source):
            return False
        if source == 'ml_search' and product.display_name and \
                len(product.display_name) >= len(title):
            return False
        product.display_name = title
        product.metadata['source_display_name'] = source
        ProductEnrichmentService._log_history(product, 'display_name', title, source)
        return True

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------
    @staticmethod
    def enrich_product(product):
        from .gtin import is_valid_gtin
        if product.name.upper() == 'DEBUG' or len(product.name) < 2:
            return False
        if not product.metadata or not isinstance(product.metadata, dict):
            product.metadata = {}
        old_image_url = product.image_url
        had_local = bool(product.local_image)
        improved = False
        # Only real global GTINs hit the GTIN APIs; PLUs/weigh codes would
        # waste lookups and risk wrong-product metadata.
        if product.code_gtin and is_valid_gtin(product.code_gtin):
            if ProductEnrichmentService._fetch_off(product):
                improved = True
            if (not product.metadata.get('nova_group') or not product.image_url) \
                    and ProductEnrichmentService._looks_like_personal_care(product):
                if ProductEnrichmentService._fetch_obf(product):
                    improved = True
            if ProductEnrichmentService._fetch_meli_catalog(product):
                improved = True
            if ProductEnrichmentService._fetch_meli_by_gtin(product):
                improved = True
        if not improved or not product.image_url:
            if ProductEnrichmentService._search_by_name(product):
                improved = True
        if not product.metadata.get('nova_group'):
            if ProductEnrichmentService._search_off_by_name(product):
                improved = True

        # ALWAYS try heuristics last, but it can now OVERWRITE if we find it was a bad heuristic before
        if ProductEnrichmentService._apply_heuristics(product):
            improved = True

        if improved:
            product.save()
            image_changed = (product.image_url != old_image_url)
            if product.image_url and (image_changed or not product.local_image):
                ProductEnrichmentService.download_local_image(
                    product, force=bool(image_changed and had_local))
        return improved

    @staticmethod
    def _looks_like_personal_care(product):
        text = f"{product.display_name or ''} {product.name} " \
               f"{product.category.name if product.category else ''}".lower()
        return any(kw in text for kw in PERSONAL_CARE_KEYWORDS)

    # ------------------------------------------------------------------
    # Open Food / Beauty Facts (official JSON APIs)
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_openfacts_product(product, p, source):
        changed = False
        name = (p.get('product_name_pt') or p.get('product_name')
                or p.get('generic_name'))
        if name and ProductEnrichmentService._set_display_name(
                product, name.title(), source):
            changed = True
        brands = (p.get('brands') or '').split(',')[0].strip()
        if brands and not product.brand:
            product.brand = brands
            ProductEnrichmentService._log_history(
                product, 'brand', brands, source)
            changed = True
        nova = p.get('nova_group')
        if nova and ProductEnrichmentService._can_update(
                product, 'nova_group', source):
            try:
                product.metadata['nova_group'] = int(nova)
            except (TypeError, ValueError):
                pass
            else:
                product.metadata['source_nova_group'] = source
                changed = True
        if p.get('nutriments') and not product.metadata.get('nutrition'):
            product.metadata['nutrition'] = p.get('nutriments', {})
            changed = True
        for api_field, meta_field in (('nutriscore_grade', 'nutriscore'),
                                      ('ecoscore_grade', 'ecoscore')):
            grade = p.get(api_field)
            if grade and not product.metadata.get(meta_field):
                product.metadata[meta_field] = str(grade).lower()
                changed = True
        if ProductEnrichmentService._set_image(
                product, p.get('image_front_url'), source):
            changed = True
        return changed

    @staticmethod
    def _fetch_openfacts(product, base, source, kind):
        url = f"{base}/api/v2/product/{product.code_gtin}.json"
        payload, outcome = ProductEnrichmentService._fetch_json(
            url, params={'fields': OPENFACTS_FIELDS}, source=kind)
        if payload is None:
            return False
        if payload.get('status') != 1 or not payload.get('product'):
            logger.info("%s: product %s not found for GTIN %s",
                        kind, product.id, product.code_gtin)
            return False
        return ProductEnrichmentService._apply_openfacts_product(
            product, payload['product'], source)

    @staticmethod
    def _fetch_off(product, source='off_gtin'):
        return ProductEnrichmentService._fetch_openfacts(
            product, OFF_BASE, source, 'openfoodfacts')

    @staticmethod
    def _fetch_obf(product, source='obf_gtin'):
        return ProductEnrichmentService._fetch_openfacts(
            product, OBF_BASE, source, 'openbeautyfacts')

    # ------------------------------------------------------------------
    # Mercado Livre: official catalog API > public search JSON > scraping
    # ------------------------------------------------------------------
    @staticmethod
    def _meli_token():
        return (getattr(settings, 'MELI_ACCESS_TOKEN', '')
                or os.environ.get('MELI_ACCESS_TOKEN', ''))

    @staticmethod
    def _fetch_meli_catalog(product, source='meli_catalog'):
        """Official ML catalog lookup by GTIN (needs MELI_ACCESS_TOKEN).

        Catalog entries are GTIN-verified by Mercado Livre itself, so this is
        the highest-quality ML source when a token is configured.
        """
        token = ProductEnrichmentService._meli_token()
        if not token:
            return False
        payload, outcome = ProductEnrichmentService._fetch_json(
            MELI_CATALOG_URL,
            params={'site_id': 'MLB',
                    'product_identifier': product.code_gtin,
                    'limit': 1},
            source='meli-catalog',
            extra_headers={'Authorization': f'Bearer {token}'})
        if payload is None:
            return False
        results = payload.get('results', [])
        if not results:
            logger.info("meli-catalog: no entry for GTIN %s", product.code_gtin)
            return False
        entry = results[0]
        name = entry.get('name', '')
        reference = product.display_name or product.name
        if not ProductEnrichmentService._titles_match(
                name, reference, TITLE_MATCH_GTIN):
            logger.warning("meli-catalog: rejecting mismatched entry %r for %r",
                           name[:80], reference[:80])
            return False
        changed = ProductEnrichmentService._set_display_name(
            product, name, source)
        pictures = entry.get('pictures', [])
        if pictures:
            pic = pictures[0].get('secure_url') or pictures[0].get('url')
            if ProductEnrichmentService._set_image(product, pic, source):
                changed = True
        return changed

    @staticmethod
    def _apply_meli_item(product, item, source, reference, threshold):
        """Apply one ML search/catalog item after title verification."""
        title = item.get('title', '')
        if not ProductEnrichmentService._titles_match(
                title, reference, threshold):
            return False
        changed = ProductEnrichmentService._set_display_name(
            product, title, source)
        pic = None
        item_id = item.get('id')
        if item_id:
            # Search thumbnails are low-res; the item endpoint has full-size
            # pictures. One extra request, much better images.
            detail, _ = ProductEnrichmentService._fetch_json(
                MELI_ITEM_URL.format(item_id), source='meli-item')
            if detail:
                pics = detail.get('pictures', [])
                if pics:
                    pic = pics[0].get('secure_url') or pics[0].get('url')
        pic = pic or item.get('thumbnail')
        if ProductEnrichmentService._set_image(product, pic, source):
            changed = True
        return changed

    @staticmethod
    def _fetch_meli_search(query, product, source, threshold):
        """Public ML search JSON (no auth). Returns True if applied."""
        reference = product.display_name or product.name
        payload, outcome = ProductEnrichmentService._fetch_json(
            MELI_SEARCH_URL, params={'q': query, 'limit': 5},
            source='meli-search')
        if payload is None:
            if outcome == 'blocked':
                logger.warning(
                    "meli-search: blocked (HTTP 403) — this IP is flagged by "
                    "Mercado Livre; catalog API token or proxy required")
            return False
        results = payload.get('results', [])
        if not results:
            logger.info("meli-search: no results for %r", query[:60])
            return False
        # Prefer verified titles, breaking ties by units sold.
        matches = [r for r in results
                   if ProductEnrichmentService._titles_match(
                       r.get('title', ''), reference, threshold)]
        if not matches:
            logger.info("meli-search: %d results, none matched %r",
                        len(results), reference[:60])
            return False
        matches.sort(key=lambda r: r.get('sold_quantity') or 0, reverse=True)
        return ProductEnrichmentService._apply_meli_item(
            product, matches[0], source, reference, threshold)

    @staticmethod
    def _scrape_mercadolivre(query, product, source, threshold):
        """Last-resort HTML scraping (ML frequently bot-blocks servers)."""
        url = MELI_LIST_URL.format(query)
        try:
            res = requests.get(
                url,
                headers=ProductEnrichmentService._get_headers(html=True),
                timeout=15,
            )
        except requests.RequestException as exc:
            logger.warning("meli-scrape failed for %r: %s", query[:60], exc)
            return False
        if 'account-verification' in getattr(res, 'url', ''):
            logger.warning("meli-scrape: bot challenge for %r (IP flagged)",
                           query[:60])
            return False
        if res.status_code != 200:
            logger.warning("meli-scrape HTTP %s for %r",
                           res.status_code, query[:60])
            return False
        try:
            soup = BeautifulSoup(res.text, 'html.parser')
            item = soup.select_one(
                '.ui-search-result__content, .ui-search-layout__item')
            if not item:
                logger.info("meli-scrape: no results for %r", query[:60])
                return False
            title_el = item.select_one('.ui-search-item__title')
            title = title_el.text.strip() if title_el else ''
            reference = product.display_name or product.name
            if not ProductEnrichmentService._titles_match(
                    title, reference, threshold):
                logger.info("meli-scrape: rejecting mismatched title %r for %r",
                            title[:80], reference[:60])
                return False
            changed = ProductEnrichmentService._set_display_name(
                product, title, source)
            img = item.select_one(
                '.ui-search-result-image__element, img')
            if img and ProductEnrichmentService._set_image(
                    product, img.get('data-src', img.get('src')), source):
                changed = True
            return changed
        except Exception:
            logger.exception("meli-scrape parse failed for %r", query[:60])
            return False

    @staticmethod
    def _fetch_meli_by_gtin(product, source='api_gtin'):
        if ProductEnrichmentService._fetch_meli_search(
                product.code_gtin, product, source, TITLE_MATCH_GTIN):
            return True
        return ProductEnrichmentService._scrape_mercadolivre(
            product.code_gtin, product, source, TITLE_MATCH_GTIN)

    @staticmethod
    def _search_by_name(product, source='ml_search'):
        search_term = product.display_name or product.name
        search_term = re.sub(r'\s+', ' ', search_term).strip()
        if len(search_term) < 5:
            return False
        if ProductEnrichmentService._fetch_meli_search(
                search_term, product, source, TITLE_MATCH_NAME):
            return True
        return ProductEnrichmentService._scrape_mercadolivre(
            search_term, product, source, TITLE_MATCH_NAME)

    @staticmethod
    def _search_off_by_name(product, source='off_name'):
        term = re.sub(r'\(.*?\)', '', (product.display_name or product.name))
        term = re.sub(r'\d+(G|KG|ML|L|UN)', '', term, flags=re.IGNORECASE)
        term = " ".join(re.sub(r'\s+', ' ', term).strip().split()[:3])
        if len(term) < 4:
            return False
        url = f"{OFF_BASE}/cgi/search.pl"
        payload, outcome = ProductEnrichmentService._fetch_json(
            url,
            params={'search_terms': term, 'search_simple': 1,
                    'action': 'process', 'json': 1, 'page_size': 3},
            source='openfoodfacts-search', timeout=25)
        if payload is None:
            return False
        for p in payload.get('products', []):
            nova = p.get('nova_group')
            if nova and ProductEnrichmentService._can_update(
                    product, 'nova_group', source):
                try:
                    product.metadata['nova_group'] = int(nova)
                except (TypeError, ValueError):
                    continue
                product.metadata['source_nova_group'] = source
                product.metadata['nutrition'] = p.get('nutriments', {})
                ecoscore = p.get('ecoscore_grade')
                if ecoscore:
                    product.metadata['ecoscore'] = str(ecoscore).lower()
                if not product.image_url:
                    ProductEnrichmentService._set_image(
                        product, p.get('image_front_url'), source)
                return True
        return False

    @staticmethod
    def _apply_heuristics(product):
        name = (product.display_name or product.name).lower()
        cat_name = (product.category.name if product.category else 'Geral').lower()
        full = f"{name} {cat_name}"

        # 0. Non-Food Safety (Force None)
        non_food = ['detergente', 'limpador', 'sabão', 'sabonete', 'shampoo', 'condicionador', 'esponja', 'limpol', 'veja', 'ypê', 'desinfetante', 'amaciante']
        if any(kw in full for kw in non_food):
            product.metadata['nova_group'] = None
            product.metadata['source_nova_group'] = 'heuristic_nonfood'
            return True

        # 1. Ultra-Processed (4)
        nova4 = ['refrigerante', 'biscoito', 'bolacha', 'snack', 'nugget', 'hamburguer', 'miojo', 'doce', 'guloseima', 'cão', 'cao', 'cães', 'caes', 'pedigree', 'pet', 'salsicha', 'refresco', 'suco po', 'danoninho', 'batido', 'iogurte com fruta', 'coco', 'morango']
        # 2. Processed (3)
        nova3 = ['linguica', 'linguiça', 'pão frances', 'queijo', 'presunto', 'mortadela', 'macarrao', 'macarrão', 'massa', 'espaguete', 'lasanha', 'extrato', 'conserva', 'milho', 'ervilha']
        # 3. Processed Culinary Ingredients (2)
        nova2 = ['manteiga', 'azeite', 'óleo', 'sal', 'açúcar', 'açucar']
        # 4. Unprocessed (1)
        nova1 = ['hortifruti', 'fruta', 'verdura', 'legume', 'açougue', 'carne', 'ovo', 'arroz', 'feijão', 'tomate', 'cebola', 'batata', 'banana', 'frango', 'peixe', 'tilapia', 'cafe', 'água', 'abobora', 'uva', 'mamão', 'leite']

        found = None
        # Check in order of processing intensity
        if any(kw in full for kw in nova4):
            found = 4
        elif any(kw in full for kw in nova3):
            found = 3
        elif any(kw in full for kw in nova2):
            found = 2
        elif any(kw in full for kw in nova1):
            found = 1

        # SPECIAL OVERRIDE: Flavored or Sweetened yogurts are Group 4
        if 'iogurte' in full or 'iog' in full:
            if any(kw in full for kw in ['morango', 'coco', 'mel', 'fruta', 'batido', 'desnatado']):
                found = 4
            else:
                found = 1  # Natural/Plain is 1

        # MILK OVERRIDE (Re-verify)
        if 'leite' in full and not any(kw in full for kw in ['achocolatado', 'condensado', 'creme']):
            found = 1

        if found:
            # SPECIAL: If current data is heuristic and incorrect, we overwrite it with better heuristic
            current_src = product.metadata.get('source_nova_group', 'none')
            current_val = product.metadata.get('nova_group')

            if current_src == 'heuristic' and current_val != found:
                product.metadata['nova_group'] = found
                product.metadata['ecoscore'] = 'b' if found == 1 else 'd'
                ProductEnrichmentService._log_history(product, 'nova_group', found, 'heuristic_correction')
                return True

            # FORCE OVERRIDE for Milk (Priority over API)
            if 'leite' in full and current_val == 4:
                product.metadata['nova_group'] = 1
                product.metadata['source_nova_group'] = 'heuristic_override'
                ProductEnrichmentService._log_history(product, 'nova_group', 1, 'milk_safety_override')
                return True

            if ProductEnrichmentService._can_update(product, 'nova_group', 'heuristic'):
                product.metadata['nova_group'] = found
                product.metadata['source_nova_group'] = 'heuristic'
                product.metadata['ecoscore'] = 'b' if found == 1 else 'd'
                ProductEnrichmentService._log_history(product, 'nova_group', found, 'heuristic')
                return True
        return False

    @staticmethod
    def _fetch_ncm_info(product):
        return False
