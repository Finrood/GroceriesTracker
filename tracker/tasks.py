from django_q.tasks import async_task
from django.db.models import Q
from django.utils import timezone
from .models import Product
from .enrichment import ProductEnrichmentService
import logging
from datetime import timedelta

logger = logging.getLogger(__name__)

def async_enrich_product(product_id, **kwargs):
    """
    Background task to enrich product metadata and images.
    Accepted **kwargs to prevent 'unexpected keyword argument' errors.
    """
    try:
        product = Product.objects.get(id=product_id)
        current_name = product.display_name or product.name
        logger.info(f"Starting async enrichment for product {product_id}: {current_name}")
        
        # Update attempt timestamp before starting
        product.last_enrichment_attempt = timezone.now()
        product.save(update_fields=['last_enrichment_attempt'])
        
        # Call the existing synchronous service logic in the background
        success = ProductEnrichmentService.enrich_product(product)
        
        if success:
            logger.info(f"Successfully enriched product {product_id}")
            return f"Data found and saved for {product.name}"
        else:
            logger.warning(f"Enrichment task finished without new data for product {product_id}")
            return f"No external data found for GTIN {product.code_gtin or 'N/A'}"
            
    except Product.DoesNotExist:
        error_msg = f"Product {product_id} not found"
        logger.error(error_msg)
        return error_msg
    except Exception as e:
        error_msg = f"Error: {str(e)}"
        logger.error(f"Enrichment task error for product {product_id}: {str(e)}", exc_info=True)
        return error_msg

def maintenance_requeue_enrichment(batch_size=100):
    """
    Periodic maintenance: Re-enqueues products that are missing metadata.
    Bypasses the 7-day rule if the metadata is completely empty.
    """
    # 1. Critical: Missing all metadata (no 7-day rule here)
    critical_products = Product.objects.filter(metadata={}).distinct()[:batch_size]
    
    # 2. Regular: Missing specific fields but have some metadata (7-day rule applies)
    seven_days_ago = timezone.now() - timedelta(days=7)
    regular_products = Product.objects.filter(
        Q(last_enrichment_attempt__isnull=True) | Q(last_enrichment_attempt__lte=seven_days_ago)
    ).filter(
        Q(image_url__isnull=True) | Q(image_url='') |
        Q(metadata__nova_group__isnull=True) |
        Q(metadata__nutrition__isnull=True)
    ).exclude(metadata={}).distinct()[:batch_size]

    incomplete_products = list(critical_products) + list(regular_products)
    incomplete_products = incomplete_products[:batch_size]

    count = 0
    for product in incomplete_products:
        # Use q_options for naming the task in the database
        async_task('tracker.tasks.async_enrich_product', product.id, q_options={'name': f"Maint: {product.name[:20]}"})
        count += 1
    
    logger.info(f"Maintenance: Re-enqueued {count} products for enrichment.")
    return count
