from django.contrib import admin
from .models import (StoreChain, Store, Category, Product, ProductMapping,
                     PriceHistory, Receipt, ReceiptItem, ScrapeLog)


@admin.register(StoreChain)
class StoreChainAdmin(admin.ModelAdmin):
    list_display = ('name', 'store_count')
    search_fields = ('name',)

    def store_count(self, obj):
        return obj.stores.count()


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ('name', 'chain', 'cnpj', 'address_city')
    list_filter = ('chain', 'address_city')
    search_fields = ('name', 'cnpj')


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ('name', 'ncm_prefix')
    search_fields = ('name',)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('display_name', 'name', 'brand', 'code_gtin', 'ncm',
                    'category', 'weight_grams', 'is_manually_edited')
    list_filter = ('category', 'is_manually_edited')
    search_fields = ('name', 'display_name', 'brand', 'code_gtin')
    actions = ('unlock_enrichment',)

    def unlock_enrichment(self, request, queryset):
        queryset.update(is_manually_edited=False)
    unlock_enrichment.short_description = "Allow enrichment to update again"


@admin.register(ProductMapping)
class ProductMappingAdmin(admin.ModelAdmin):
    list_display = ('store', 'internal_code', 'product', 'is_confirmed', 'user')
    list_filter = ('is_confirmed', 'store')
    search_fields = ('internal_code', 'product__name')


class ReceiptItemInline(admin.TabularInline):
    model = ReceiptItem
    extra = 0
    autocomplete_fields = ('product',)


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ('id', 'store', 'user', 'issue_date', 'total_amount', 'number')
    list_filter = ('store', 'issue_date')
    search_fields = ('access_key', 'number', 'store__name')
    inlines = (ReceiptItemInline,)
    date_hierarchy = 'issue_date'


@admin.register(PriceHistory)
class PriceHistoryAdmin(admin.ModelAdmin):
    list_display = ('product', 'store', 'date', 'unit_price', 'normalized_price')
    list_filter = ('store', 'date')
    search_fields = ('product__name',)
    date_hierarchy = 'date'


@admin.register(ScrapeLog)
class ScrapeLogAdmin(admin.ModelAdmin):
    list_display = ('timestamp', 'status', 'user', 'access_key')
    list_filter = ('status',)
    search_fields = ('url', 'access_key', 'error_message')
    readonly_fields = ('timestamp',)
