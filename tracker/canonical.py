"""Cross-store canonical identity (Brazil PLU problem).

Packaged goods share a global GTIN across chains, but weighed produce, deli
and bakery items carry store-local PLUs (Fort: 2904, Koch: 8345, Angeloni: 82
for the same banana). This module groups Product rows into CanonicalProduct
buckets so price comparison works across stores even without GTINs.

Pipeline:
  1. Same valid GTIN          -> always the same canonical (strong key).
  2. Signature + gates        -> score >= AUTO_THRESHOLD merges automatically.
  3. Score in [SUGGEST_FLOOR, AUTO_THRESHOLD) + gates -> CanonicalSuggestion
     for human review (accept merges, dismiss blocks re-suggestion).

Gates (all must pass for 2/3): same size_key (weight_grams + unit hint),
same category (when both known), same NCM chapter (when both known),
non-conflicting brands.
"""
import re

from rapidfuzz import fuzz

AUTO_THRESHOLD = 93.0
SUGGEST_FLOOR = 78.0

# Receipt-noise tokens ignored in signatures (pack/grade/promo markers).
STOP_TOKENS = frozenset({
    'PROMOCAO', 'PROMO', 'OFERTA', 'PEDAC', 'PED', 'RESF', 'CONG', 'LV', 'PG',
    'TP', 'CTAMPA', 'ROSCA', 'CP', 'GF', 'PT', 'PC', 'TRADICIONAL', 'LONGA',
    'VIDA', 'COMUM', 'PEROLA', 'BRANCA', 'BRANCO',
})

_SIZE_RE = re.compile(r'\d+(?:[.,]\d+)?\s*(?:KG|G|L|ML|UN)\b', re.IGNORECASE)
_MULTIPACK_RE = re.compile(r'\d+\s*[Xx]\s*\d+(?:[.,]\d+)?\s*(?:KG|G|L|ML|UN)\b', re.IGNORECASE)
_UNIT_RE = re.compile(r'(KG|G|L|ML|UN)\b', re.IGNORECASE)
_PARENS_RE = re.compile(r'\(.*?\)')


def signature_for(name):
    """Normalized comparison key: upper, no parens/sizes/packs/stopwords."""
    t = (name or '').upper()
    t = _PARENS_RE.sub(' ', t)
    t = _MULTIPACK_RE.sub(' ', t)
    t = _SIZE_RE.sub(' ', t)
    toks = [w for w in re.findall(r'[A-Z0-9]+', t)
            if w not in STOP_TOKENS and len(w) > 1]
    return ' '.join(toks)


def unit_hint_for(name):
    """Raw sell-unit hint from the name (KG/G/L/ML/UN or '')."""
    found = _UNIT_RE.findall((name or '').upper())
    return found[-1] if found else ''


def size_key_for(product):
    """(weight_grams or 0, unit hint): equality required for merges."""
    w = product.weight_grams
    return (float(w) if w is not None else 0.0, unit_hint_for(product.name))


def _brand_of(product):
    b = (product.brand or '').strip().upper()
    return '' if b in ('', 'GENERIC', 'GENERICO') else b


def _same_gtin(a, b):
    return bool(a.code_gtin and a.code_gtin == b.code_gtin)


def gates_pass(a, b):
    """Hard requirements shared by auto-merge and suggestions."""
    if size_key_for(a) != size_key_for(b):
        return False
    # Distinct manufacturer SKUs = distinct items, even when names are close
    # (e.g. different nail-polish shades or chocolate flavors). Same GTIN
    # always merges via preview_groups; one-sided GTINs still pass.
    ga, gb = (a.code_gtin or ''), (b.code_gtin or '')
    if ga and gb and ga != gb:
        return False
    if a.category_id and b.category_id and a.category_id != b.category_id:
        return False
    na, nb = (a.ncm or '')[:2], (b.ncm or '')[:2]
    if na and nb and na != nb:
        return False
    ba, bb = _brand_of(a), _brand_of(b)
    if ba and bb and ba != bb:
        return False
    return True


def match_score(a, b):
    return float(fuzz.token_set_ratio(signature_for(a.name), signature_for(b.name)))


def preview_groups(products):
    """Dry-run grouping: returns ({root_id: [members]}, [(a, b, score, reason)])."""
    products = list(products)
    parent = {p.id: p.id for p in products}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    by_id = {p.id: p for p in products}
    # 1. Same valid GTIN always merges.
    gtin_groups = {}
    for p in products:
        if p.code_gtin:
            gtin_groups.setdefault(p.code_gtin, []).append(p.id)
    for ids in gtin_groups.values():
        for other in ids[1:]:
            union(ids[0], other)

    # 2/3. Fuzzy signature with gates.
    sigs = {p.id: signature_for(p.name) for p in products}
    suggestions = []
    for i in range(len(products)):
        for j in range(i + 1, len(products)):
            a, b = products[i], products[j]
            if find(a.id) == find(b.id):
                continue
            sa, sb = sigs[a.id], sigs[b.id]
            if not sa or not sb or not gates_pass(a, b):
                continue
            score = float(fuzz.token_set_ratio(sa, sb))
            if score >= AUTO_THRESHOLD and (sa == sb or _same_gtin(a, b)):
                # Conservative auto-merge: identical signatures (or shared
                # GTIN, already unioned above). Near-matches with different
                # tokens (Italiano vs Salada, Dark vs Branco) go to review:
                # there is no split UI to undo a bad auto-merge.
                union(a.id, b.id)
            elif score >= SUGGEST_FLOOR:
                reason = f"Similar names ({score:.0f}), same size/category"
                suggestions.append((a, b, score, reason))

    groups = {}
    for p in products:
        groups.setdefault(find(p.id), []).append(p)
    return groups, suggestions


def representative_name(members):
    """Shortest display name in the group (usually the cleanest label)."""
    names = [(len(m.display_name or m.name), (m.display_name or m.name)) for m in members]
    return sorted(names)[0][1] if names else 'Grupo'


def attach(product):
    """Attach a single product to a canonical (fast path for new imports).

    Joins the canonical of a same-GTIN sibling, else an exact
    signature+size twin, else creates a fresh singleton canonical.
    Fuzzy grouping stays in the group_canonicals command.
    """
    from .models import CanonicalProduct
    if product.canonical_id:
        return product.canonical
    if product.code_gtin:
        sibling = (type(product).objects
                   .filter(code_gtin=product.code_gtin, canonical__isnull=False)
                   .exclude(id=product.id).first())
        if sibling is not None:
            product.canonical = sibling.canonical
            product.save(update_fields=['canonical'])
            return product.canonical
    sig = signature_for(product.name)
    if sig:
        twin = None
        for cand in (type(product).objects
                     .filter(canonical__isnull=False)
                     .exclude(id=product.id)
                     .select_related('category')[:2000]):
            if (signature_for(cand.name) == sig and gates_pass(product, cand)):
                twin = cand
                break
        if twin is not None:
            product.canonical = twin.canonical
            product.save(update_fields=['canonical'])
            return product.canonical
    canon = CanonicalProduct.objects.create(
        name=(product.display_name or product.name)[:255],
        category=product.category)
    product.canonical = canon
    product.save(update_fields=['canonical'])
    return canon


def merge_canonicals(keeper, absorbed):
    """Move every product from absorbed into keeper; delete absorbed."""
    from .models import Product
    if keeper.id == absorbed.id:
        return keeper
    Product.objects.filter(canonical=absorbed).update(canonical=keeper)
    absorbed.delete()
    return keeper
