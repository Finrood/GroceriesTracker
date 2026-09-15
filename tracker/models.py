from django.db import models
from django.db.models import Avg, Sum, Count, F, Q
from django.db.models.functions import TruncMonth, ExtractWeekDay
from django.contrib.auth.models import User
from decimal import Decimal

import re

from .gtin import is_valid_gtin, normalize_code

def normalize_text(text, is_product=False):
    if not text: return text
    # 1. Remove multiple spaces and strip
    text = re.sub(r'\s+', ' ', str(text)).strip()
    # 2. Basic Title Case
    text = text.title()
    
    if is_product:
        # 3. Preservation of units (Smart Casing)
        # Keep KG, L, ML, UN, etc. in correct format
        replacements = {
            r'(\d+)\s*Kg\b': r'\1kg',
            r'(\d+)\s*L\b': r'\1L',
            r'(\d+)\s*Ml\b': r'\1ml',
            r'(\d+)\s*Un\b': r'\1un',
            r'(\d+)\s*G\b': r'\1g',
        }
        for pattern, replacement in replacements.items():
            text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
            
    return text

class StoreChain(models.Model):
    name = models.CharField(max_length=100, unique=True)
    logo_url = models.URLField(blank=True, null=True)

    def __str__(self):
        return self.name


# Corporate-name fragments -> consumer-facing chain brand. Single source of
# truth for trading names (used by Store.display_name, assign_chains and the
# legacy views._get_trading_name wrapper).
CHAIN_ALIASES = {
    'SDB COMERCIO': 'Fort Atacadista',
    'FORT ATACADISTA': 'Fort Atacadista',
    'ANGELONI': 'Angeloni',
    'GIASSI': 'Giassi',
    'BISTEK': 'Bistek',
    'CONDOR': 'Condor',
    'MAGAZINE LUIZA': 'Magalu',
    'WMS BRASIL': 'Carrefour/Big',
    'KOCH HIPERMERCADO': 'Koch',
    'SACOLAO MERCADO': 'Sacolão Mercado',
}


def resolve_trading_name(full_name):
    """Map a fiscal corporate name to its consumer-facing chain brand."""
    upper_name = (full_name or '').upper()
    for key, val in CHAIN_ALIASES.items():
        if key in upper_name:
            return val
    return (full_name or '').title()

class Store(models.Model):
    name = models.CharField(max_length=255)
    chain = models.ForeignKey(StoreChain, on_delete=models.SET_NULL, null=True, blank=True, related_name='stores')
    cnpj = models.CharField(max_length=14, unique=True, db_index=True)
    cnpj_root = models.CharField(max_length=8, db_index=True, blank=True, default='')
    address_city = models.CharField(max_length=100, db_index=True)
    address_neighborhood = models.CharField(max_length=100, blank=True, db_index=True)
    address_street = models.CharField(max_length=255, blank=True)
    latitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)
    longitude = models.DecimalField(max_digits=9, decimal_places=6, null=True, blank=True)

    def save(self, *args, **kwargs):
        if self.cnpj:
            self.cnpj = ''.join(filter(str.isdigit, self.cnpj))
            self.cnpj_root = self.cnpj[:8]
        else:
            self.cnpj_root = ''
        super().save(*args, **kwargs)

    @property
    def display_name(self):
        """Consumer-facing name: chain brand when linked, else alias mapping."""
        if self.chain_id and getattr(self, 'chain', None) is not None:
            # chain may be unfetched; use cached id via query only if needed
            try:
                return self.chain.name
            except StoreChain.DoesNotExist:
                pass
        elif self.chain_id:
            chain = StoreChain.objects.filter(id=self.chain_id).first()
            if chain:
                return chain.name
        return resolve_trading_name(self.name)

    def __str__(self):
        return self.name

class Category(models.Model):
    name = models.CharField(max_length=100, unique=True)
    ncm_prefix = models.CharField(max_length=8, blank=True, null=True, db_index=True)

    class Meta:
        verbose_name_plural = "Categories"

    def save(self, *args, **kwargs):
        self.name = normalize_text(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name

class CanonicalProduct(models.Model):
    """Cross-store identity: groups Product rows that are the same purchasable
    item sold under different store-local codes/names (e.g. produce PLUs).

    Same-GTIN products always share a canonical; PLU-only goods are grouped
    by signature matching (see tracker/canonical.py), auto or via review.
    Analytics (benchmarks, history) read through this layer.
    """
    name = models.CharField(max_length=255, db_index=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True,
                                 blank=True, related_name='canonical_products')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name


class CanonicalSuggestion(models.Model):
    """A proposed merge of two products pending human review."""
    PENDING = 'pending'
    ACCEPTED = 'accepted'
    DISMISSED = 'dismissed'
    STATUS_CHOICES = [(PENDING, 'Pending'), (ACCEPTED, 'Accepted'), (DISMISSED, 'Dismissed')]

    product_a = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='canonical_suggestions_a')
    product_b = models.ForeignKey('Product', on_delete=models.CASCADE, related_name='canonical_suggestions_b')
    score = models.FloatField()
    reason = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['product_a', 'product_b'], name='uniq_canon_sugg_pair')
        ]

    def __str__(self):
        return f"{self.product_a_id}<->{self.product_b_id} ({self.score:.0f})"


class Product(models.Model):
    name = models.CharField(max_length=255, db_index=True)
    display_name = models.CharField(max_length=255, blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    image_url = models.URLField(max_length=1000, blank=True, null=True)
    local_image = models.ImageField(upload_to='products/', blank=True, null=True)
    is_manually_edited = models.BooleanField(default=False)
    weight_grams = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    last_enrichment_attempt = models.DateTimeField(null=True, blank=True, db_index=True)
    # Self-referential FK to link "Coke 2L" (variant) to "Coke" (canonical)
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True, related_name='variants')
    brand = models.CharField(max_length=100, blank=True, db_index=True)
    code_gtin = models.CharField(max_length=14, blank=True, null=True, db_index=True)
    ncm = models.CharField(max_length=8, db_index=True, blank=True)
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, related_name='products')
    canonical = models.ForeignKey(CanonicalProduct, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='products', db_index=True)

    class Meta:
        verbose_name = "Product"
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['code_gtin']),
            models.Index(fields=['canonical']),
        ]

    def save(self, *args, **kwargs):
        self.name = self.name.upper().strip() # Raw name stays upper for scraper matching
        # Never persist store-local PLUs or weigh codes as the global GTIN.
        if self.code_gtin:
            cleaned = normalize_code(self.code_gtin)
            self.code_gtin = cleaned if is_valid_gtin(cleaned) else None
        if self.display_name:
            self.display_name = normalize_text(self.display_name, is_product=True)
        if self.brand:
            self.brand = normalize_text(self.brand)
        
        # Automatic Weight Extraction
        # Multipack-aware: "COCA 12X350ML" must yield 4200 (12x350ml), not 350.
        target_name = self.display_name or self.name
        matches = re.findall(r'(\d+[\.,]?\d*)\s*(G|KG|ML|L)', target_name.upper())
        multi = re.search(r'(\d+)\s*[X]\s*(\d+[\.,]?\d*)\s*(G|KG|ML|L)', target_name.upper())
        extracted = None
        try:
            if multi:
                count = Decimal(multi.group(1))
                val = Decimal(multi.group(2).replace(',', '.'))
                unit = multi.group(3)
                size = val if unit in ['G', 'ML'] else val * 1000
                extracted = (count * size).quantize(Decimal('1'))
            elif matches:
                val_str, unit = matches[-1]
                val = Decimal(val_str.replace(',', '.'))
                extracted = val if unit in ['G', 'ML'] else val * 1000
        except Exception:
            extracted = None

        if extracted is not None:
            self.weight_grams = extracted
        elif not self.is_manually_edited:
            # Name no longer carries a size (e.g. edited to "Nescau"): clear the
            # stale weight instead of silently keeping the old value, which
            # poisoned every future normalized_price for this product.
            self.weight_grams = None

        # A weight change retroactively falsifies every frozen normalized_price
        # (items/history keep the value computed at import). Propagate, unless
        # the caller scoped this save away from weight (update_fields).
        update_fields = kwargs.get('update_fields')
        propagate = update_fields is None or 'weight_grams' in update_fields
        old_weight = None
        if propagate and self.pk:
            old_weight = type(self).objects.filter(pk=self.pk).values_list(
                'weight_grams', flat=True).first()

        super().save(*args, **kwargs)

        if propagate and self.pk and _weights_differ(old_weight, self.weight_grams):
            self.renormalize_items()

    def renormalize_items(self):
        """Recompute frozen normalized prices from the CURRENT weight.

        Only materially stale rows are touched (see needs_renorm); weightless
        products are skipped entirely (unit-price fallback can't beat the
        import-time value). Mirrors into PriceHistory so benchmarks/inflation
        read one consistent definition. Returns rows touched.
        """
        if not self.weight_grams or Decimal(str(self.weight_grams)) <= 0:
            return 0
        touched = 0
        for item in ReceiptItem.objects.filter(product=self).only(
                'id', 'receipt_id', 'unit_price', 'normalized_price'):
            want = normalized_for(item.unit_price, self.weight_grams)
            if needs_renorm(item.normalized_price, want):
                ReceiptItem.objects.filter(id=item.id).update(normalized_price=want)
                PriceHistory.objects.filter(
                    receipt_id=item.receipt_id, product=self,
                    unit_price=item.unit_price).exclude(
                    normalized_price=want).update(normalized_price=want)
                touched += 1
        return touched

    def __str__(self):
        return self.display_name or self.name

def _weights_differ(old, new):
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return Decimal(str(old)) != Decimal(str(new))


def needs_renorm(stored, want):
    """True only for material drift, not Decimal dust.

    Import-time values were quantized to cents by the scraper while the
    formula yields full precision (6.39 vs 6.3933…): rewriting those rows
    churns data for zero analytic gain. A weight correction (1.66 vs 16.61
    after BD 18L -> 1.8L) exceeds the tolerance by orders of magnitude.
    """
    if stored is None or want is None:
        return stored != want
    stored, want = Decimal(str(stored)), Decimal(str(want))
    if stored == want:
        return False
    tolerance = max(Decimal('0.005'), abs(want) * Decimal('0.005'))
    return abs(stored - want) > tolerance


def normalized_for(unit_price, weight_grams):
    """Price per 1kg/1L for a unit price and weight in grams (single formula).

    Used by ReceiptItem.save for new rows and by Product.renormalize_items
    to repair rows frozen under an older (misparsed, since corrected) weight.
    weight_grams is multipack-aware, so pack math agrees with the scraper.
    """
    if weight_grams and Decimal(str(weight_grams)) > 0:
        weight = Decimal(str(weight_grams))
        return (Decimal(str(unit_price)) / weight) * 1000
    return Decimal(str(unit_price))

class ProductMapping(models.Model):
    """
    Maps a store's internal code to a canonical Product.
    Ensures that 'Code 1937' at 'Store A' always maps to 'Tomato'.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='product_mappings', null=True, blank=True)
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    internal_code = models.CharField(max_length=50, db_index=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='store_mappings')
    is_confirmed = models.BooleanField(default=True) # False means it was a fuzzy auto-match needing review

    class Meta:
        unique_together = ('user', 'store', 'internal_code')

class PriceHistory(models.Model):
    """
    Denormalized time-series table for high-performance analytics.
    `receipt` ties each row back to the purchase it came from: deleting a
    receipt now cascades to its history rows, and refreshing a receipt
    (delete + re-create) no longer leaves the old rows behind as unlinked
    ghosts that inflated every average. Nullable only for legacy rows.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='price_history', null=True, blank=True)
    receipt = models.ForeignKey('Receipt', on_delete=models.CASCADE, related_name='price_history', null=True, blank=True)
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='price_history')
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='price_history')
    date = models.DateTimeField(db_index=True)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)
    normalized_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['user', 'product', 'store', 'date']),
            models.Index(fields=['date']),
        ]


class Receipt(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='receipts', null=True, blank=True)
    access_key = models.CharField(max_length=44, db_index=True)
    url = models.URLField(max_length=1000)
    issue_date = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, null=True, blank=True, db_index=True)
    
    series = models.CharField(max_length=10, blank=True)
    number = models.CharField(max_length=20, blank=True, db_index=True)
    
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, db_index=True)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_method = models.CharField(max_length=100, blank=True, db_index=True)
    
    tax_federal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_state = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_municipal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    consumer_cpf = models.CharField(max_length=11, blank=True, null=True, db_index=True)
    store = models.ForeignKey(Store, on_delete=models.CASCADE, related_name='receipts')

    @property
    def paid_amount(self):
        return self.total_amount - self.discount

    def __str__(self):
        return f"NF {self.number} - {self.store.name}"

    class Meta:
        indexes = [
            models.Index(fields=['user', '-issue_date']),
            models.Index(fields=['access_key']),
        ]
        # Access keys are only unique PER USER: two accounts can legitimately
        # import the same public NFCe. The old global unique=True on
        # access_key made the second import crash with IntegrityError.
        constraints = [
            models.UniqueConstraint(fields=['user', 'access_key'], name='uniq_receipt_user_accesskey')
        ]

    @classmethod
    def monthly_stats(cls, user_ids=None):
        qs = cls.objects.all()
        if user_ids:
            qs = qs.filter(user_id__in=user_ids)
        return qs.annotate(
            month=TruncMonth('issue_date')
        ).values('month').annotate(
            total_spent=Sum(F('total_amount') - F('discount')),
            total_discount=Sum('discount'),
            total_tax=Sum(F('tax_federal') + F('tax_state') + F('tax_municipal')),
            receipt_count=Count('id')
        ).order_by('-month')

class ReceiptItem(models.Model):
    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name='receiptitems')
    quantity = models.DecimalField(max_digits=12, decimal_places=3)
    unit_type = models.CharField(max_length=10)
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, db_index=True)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)
    normalized_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True, db_index=True)

    def save(self, *args, **kwargs):
        # Calculate Normalized Price (Price per 1kg or 1L)
        # Only compute when the caller didn't supply one: the scraper's
        # _calculate_normalization understands multipacks ("12X350ML" -> price
        # per litre of the whole pack), and the old unconditional overwrite
        # replaced it with a value based on weight_grams=350, inflating
        # multipack prices ~12x across all analytics.
        if self.normalized_price is None:
            self.normalized_price = normalized_for(self.unit_price, self.product.weight_grams)
        super().save(*args, **kwargs)

    class Meta:
        indexes = [
            models.Index(fields=['product', 'unit_price']),
            models.Index(fields=['normalized_price']),
        ]

    def __str__(self):
        return f"{self.product.name} @ {self.unit_price}"

class ScrapeLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, related_name='scrape_logs', null=True, blank=True)
    url = models.URLField(max_length=1000)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    status = models.CharField(max_length=20, db_index=True)
    error_message = models.TextField(blank=True)
    access_key = models.CharField(max_length=44, blank=True, db_index=True)
