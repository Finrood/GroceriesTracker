from django.db import models
from django.db.models import Avg, Sum, Count, F, Q
from django.db.models.functions import TruncMonth, ExtractWeekDay
from django.contrib.auth.models import User
from decimal import Decimal

import re

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
        super().save(*args, **kwargs)

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

    class Meta:
        verbose_name = "Product"
        indexes = [
            models.Index(fields=['name']),
            models.Index(fields=['code_gtin']),
        ]

    def save(self, *args, **kwargs):
        self.name = self.name.upper().strip() # Raw name stays upper for scraper matching
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

        super().save(*args, **kwargs)

    def __str__(self):
        return self.display_name or self.name

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
            if self.product.weight_grams and self.product.weight_grams > 0:
                # Ensure we are dividing Decimal by Decimal (just in case weight_grams is still float in memory)
                weight = Decimal(str(self.product.weight_grams))
                self.normalized_price = (self.unit_price / weight) * 1000
            else:
                self.normalized_price = self.unit_price
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
