from django import template

from tracker.enrichment import ProductEnrichmentService


register = template.Library()


@register.filter
def safe_product_image(url):
    """Return only normalized image URLs from the approved data providers."""
    return ProductEnrichmentService._safe_image_url(url) or ''
