import requests
from bs4 import BeautifulSoup
import logging
import re
import json
import os
import random
from django.core.files.base import ContentFile
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

class ProductEnrichmentService:
    # Source confidence: higher is better
    CONFIDENCE = {
        'manual': 100,
        'off_gtin': 90,
        'api_gtin': 80,
        'off_name': 60,
        'ml_search': 50,
        'heuristic': 20,
        'none': 0
    }

    @staticmethod
    def _get_headers():
        agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
        ]
        return {
            'User-Agent': random.choice(agents),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7',
        }

    @staticmethod
    def download_local_image(product):
        if not product.image_url or product.local_image:
            return False
        try:
            response = requests.get(product.image_url, headers=ProductEnrichmentService._get_headers(), timeout=15)
            if response.status_code == 200:
                content_size = len(response.content)
                if content_size < 5120: return False
                ext = product.image_url.split('.')[-1].split('?')[0][:4]
                if len(ext) > 4 or '/' in ext: ext = 'jpg'
                filename = f"{product.code_gtin or product.id}.{ext}"
                product.local_image.save(filename, ContentFile(response.content), save=True)
                return True
        except: pass
        return False

    @staticmethod
    def _log_history(product, field, new_value, source):
        if 'history' not in product.metadata: product.metadata['history'] = []
        product.metadata['history'].append({'date': timezone.now().isoformat(), 'field': field, 'source': source, 'value': str(new_value)[:100]})
        product.metadata['history'] = product.metadata['history'][-10:]

    @staticmethod
    def _can_update(product, field, new_source):
        if product.is_manually_edited and field == 'display_name': return False
        current_source = product.metadata.get(f'source_{field}', 'none')
        return ProductEnrichmentService.CONFIDENCE.get(new_source, 0) >= ProductEnrichmentService.CONFIDENCE.get(current_source, 0)

    @staticmethod
    def enrich_product(product):
        if product.name.upper() == 'DEBUG' or len(product.name) < 2: return False
        if not product.metadata: product.metadata = {}
        improved = False
        if product.code_gtin and len(product.code_gtin) >= 8:
            if ProductEnrichmentService._fetch_off(product, 'off_gtin'): improved = True
            if ProductEnrichmentService._fetch_mercadolivre(product, 'api_gtin'): improved = True
        if not improved or not product.image_url:
            if ProductEnrichmentService._search_by_name(product, 'ml_search'): improved = True
        if not product.metadata.get('nova_group'):
            if ProductEnrichmentService._search_off_by_name(product, 'off_name'): improved = True
        
        # ALWAYS try heuristics last, but it can now OVERWRITE if we find it was a bad heuristic before
        if ProductEnrichmentService._apply_heuristics(product): improved = True

        if improved:
            product.save()
            if product.image_url and not product.local_image: ProductEnrichmentService.download_local_image(product)
        return improved

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
        if any(kw in full for kw in nova4): found = 4
        elif any(kw in full for kw in nova3): found = 3
        elif any(kw in full for kw in nova2): found = 2
        elif any(kw in full for kw in nova1): found = 1
        
        # SPECIAL OVERRIDE: Flavored or Sweetened yogurts are Group 4
        if 'iogurte' in full or 'iog' in full:
            if any(kw in full for kw in ['morango', 'coco', 'mel', 'fruta', 'batido', 'desnatado']):
                found = 4
            else:
                found = 1 # Natural/Plain is 1

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
    def _fetch_off(product, source):
        url = f"https://world.openfoodfacts.org/api/v0/product/{product.code_gtin}.json"
        try:
            res = requests.get(url, headers=ProductEnrichmentService._get_headers(), timeout=15).json()
            if res.get('status') == 1:
                p = res['product']
                changed = False
                name_pt = p.get('product_name_pt', p.get('product_name'))
                if name_pt and ProductEnrichmentService._can_update(product, 'display_name', source):
                    product.display_name = name_pt.title()
                    product.metadata['source_display_name'] = source
                    changed = True
                nova = p.get('nova_group')
                if nova and ProductEnrichmentService._can_update(product, 'nova_group', source):
                    product.metadata['nova_group'] = int(nova)
                    product.metadata['source_nova_group'] = source
                    product.metadata['nutrition'] = p.get('nutriments', {})
                    product.metadata['ecoscore'] = p.get('ecoscore_data', {}).get('grades', {}).get('world')
                    changed = True
                if not product.image_url: product.image_url = p.get('image_front_url')
                if changed: return True
        except: pass
        return False

    @staticmethod
    def _fetch_mercadolivre(product, source):
        url = f"https://lista.mercadolivre.com.br/{product.code_gtin}"
        try:
            res = requests.get(url, headers=ProductEnrichmentService._get_headers(), timeout=15)
            soup = BeautifulSoup(res.text, 'html.parser')
            item = soup.select_one('.ui-search-result__content, .ui-search-layout__item')
            if item:
                img = item.select_one('.ui-search-result-image__element, img')
                if img: product.image_url = img.get('data-src', img.get('src'))
                title = item.select_one('.ui-search-item__title')
                if title and ProductEnrichmentService._can_update(product, 'display_name', source):
                    product.display_name = title.text.strip().title()
                    product.metadata['source_display_name'] = source
                return True
        except: pass
        return False

    @staticmethod
    def _search_by_name(product, source):
        search_term = product.display_name or product.name
        search_term = re.sub(r'\s+', ' ', search_term).strip()
        if len(search_term) < 5: return False
        url = f"https://lista.mercadolivre.com.br/{search_term}"
        try:
            res = requests.get(url, headers=ProductEnrichmentService._get_headers(), timeout=15)
            soup = BeautifulSoup(res.text, 'html.parser')
            item = soup.select_one('.ui-search-result__content, .ui-search-layout__item')
            if item:
                img = item.select_one('.ui-search-result-image__element, img')
                if img: product.image_url = img.get('data-src', img.get('src'))
                title = item.select_one('.ui-search-item__title')
                if title and ProductEnrichmentService._can_update(product, 'display_name', source):
                    if not product.display_name or len(product.display_name) < len(title.text):
                        product.display_name = title.text.strip().title()
                        product.metadata['source_display_name'] = source
                return True
        except: pass
        return False

    @staticmethod
    def _search_off_by_name(product, source):
        term = re.sub(r'\(.*?\)', '', (product.display_name or product.name))
        term = re.sub(r'\d+(G|KG|ML|L|UN)', '', term, flags=re.IGNORECASE)
        term = " ".join(re.sub(r'\s+', ' ', term).strip().split()[:3])
        if len(term) < 4: return False
        url = "https://world.openfoodfacts.org/cgi/search.pl"
        try:
            res = requests.get(url, params={'search_terms': term, 'search_simple': 1, 'action': 'process', 'json': 1, 'page_size': 3}, headers=ProductEnrichmentService._get_headers(), timeout=25).json()
            for p in res.get('products', []):
                nova = p.get('nova_group')
                if nova and ProductEnrichmentService._can_update(product, 'nova_group', source):
                    product.metadata['nova_group'] = int(nova)
                    product.metadata['source_nova_group'] = source
                    product.metadata['nutrition'] = p.get('nutriments', {})
                    product.metadata['ecoscore'] = p.get('ecoscore_data', {}).get('grades', {}).get('world')
                    if not product.image_url: product.image_url = p.get('image_front_url')
                    return True
        except: pass
        return False

    @staticmethod
    def _fetch_ncm_info(product): return False
