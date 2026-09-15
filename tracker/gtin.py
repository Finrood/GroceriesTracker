"""GTIN vs store-internal PLU helpers (Brazil NFCe).

Every NFCe item carries a ``Código:`` value, but that value is NOT always a
GTIN/EAN:

* Packaged goods  -> real GTIN-8/12/13/14 (global, same across stores).
* Weighed produce / deli / bakery -> store-local PLU (e.g. ``231``, ``2904``),
  different per chain and MUST NOT be treated as a global identifier.
* In-store weigh labels -> EAN-13 with prefix 20-29 (``2...``). These look
  like GTINs (13 digits, valid checksum) but encode store+price/weight and
  are NOT globally unique.

Treating PLUs as GTINs caused cross-store false merges (global
``Product.objects.filter(code_gtin=...)`` lookup) and wasted enrichment
lookups. These helpers are the single source of truth used by the scraper,
services and model validation.
"""
import re


def normalize_code(raw):
    """Digits-only normalization; returns '' for empty input."""
    if not raw:
        return ''
    return re.sub(r'\D', '', str(raw))


def _ean_checksum_valid(digits):
    """Mod-10 (EAN/UPC) checksum: valid for lengths 8/12/13/14."""
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return False
    check = int(digits[-1])
    body = digits[:-1]
    total = 0
    # From the right (excluding check digit): 3,1,3,1...
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - (total % 10)) % 10 == check


def is_instore_weigh_code(code):
    """True for in-store weigh labels (EAN-13 starting with 20-29)."""
    d = normalize_code(code)
    return len(d) == 13 and d.startswith('2')


def is_valid_gtin(code):
    """True only for globally-unique GTINs (EAN-8/UPC/ EAN-13/ITF-14).

    Rejects: empty/short PLUs, wrong checksums, and in-store weigh codes.
    """
    d = normalize_code(code)
    if not d or len(d) not in (8, 12, 13, 14):
        return False
    if is_instore_weigh_code(d):
        return False
    return _ean_checksum_valid(d)


def split_scraped_code(raw_code):
    """Split a scraped ``Código:`` into (gtin_or_empty, internal_code).

    ``internal_code`` always keeps the raw digits (store-scoped identity).
    ``gtin`` is only populated when the code is a real global GTIN.
    """
    d = normalize_code(raw_code)
    if not d:
        return '', ''
    if is_valid_gtin(d):
        return d, d
    return '', d
