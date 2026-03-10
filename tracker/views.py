from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.http import require_POST
from django.contrib.auth.decorators import login_required
from django.db import transaction, models
from django.http import JsonResponse
from .scraper import NFCeScraper
from .models import Store, Product, Category, Receipt, ReceiptItem, ScrapeLog
from .services import ReceiptService, AnalyticsService, SmartCartService
from .decorators import receipt_owner_required
from django.db.models import Avg, Sum, Count, F, Q, Min, Max, Window
from django.db.models.functions import TruncMonth, ExtractWeekDay, Rank, Coalesce
from django.contrib import messages
from django.core.paginator import Paginator
from django.core.cache import cache
from django.contrib.admin.views.decorators import staff_member_required
from django_q.tasks import async_task
from .tasks import maintenance_requeue_enrichment, async_enrich_product
from decimal import Decimal
import json
import logging
import hashlib

logger = logging.getLogger(__name__)

def _get_trading_name(full_name):
    # Standardize common corporate names to recognizable trading names
    names = {
        'SDB COMERCIO': 'Fort Atacadista',
        'ANGELONI': 'Angeloni',
        'GIASSI': 'Giassi',
        'BISTEK': 'Bistek',
        'CONDOR': 'Condor',
        'MAGAZINE LUIZA': 'Magalu',
        'WMS BRASIL': 'Carrefour/Big'
    }
    upper_name = full_name.upper()
    for key, val in names.items():
        if key in upper_name: return val
    return full_name.title()

def _get_user_filter(request):
    """
    Returns a list of user IDs to filter by.
    If user is staff (admin), allow multiple IDs from GET params.
    If user is normal, return only their ID.
    """
    if request.user.is_staff:
        user_ids = request.GET.getlist('user_ids')
        if user_ids:
            return [int(uid) for uid in user_ids if uid.isdigit()]
        # If admin and no users selected, show ALL (return None)
        return None
    return [request.user.id]

from django.core.serializers.json import DjangoJSONEncoder

@staff_member_required
def system_maintenance(request):
    """
    Control panel for background tasks and data enrichment.
    """
    from django_q.models import Task, Schedule, OrmQ
    from .models import Product, ProductMapping
    
    # 1. Action Handling
    if request.method == "POST":
        action = request.POST.get('action')
        if action == 'requeue_missing':
            # Run the maintenance task manually
            count = maintenance_requeue_enrichment(batch_size=200)
            messages.success(request, f"Re-enqueued {count} products with missing data.")
        elif action == 'requeue_all':
            # Force re-enrichment of ALL products
            prods = Product.objects.all()
            for p in prods:
                async_task('tracker.tasks.async_enrich_product', p.id, q_options={'name': f"Enrich: {p.name[:20]}"})
            messages.success(request, f"Forced re-enrichment of ALL {prods.count()} products.")
        elif action == 'purge_queue':
            # Clear all pending tasks from the broker
            OrmQ.objects.all().delete()
            messages.success(request, "Task queue has been purged.")
        elif action == 'confirm_mapping':
            m_id = request.POST.get('mapping_id')
            ProductMapping.objects.filter(id=m_id).update(is_confirmed=True)
            return JsonResponse({'status': 'ok'})
        elif action == 'delete_mapping':
            m_id = request.POST.get('mapping_id')
            ProductMapping.objects.filter(id=m_id).delete()
            return JsonResponse({'status': 'ok'})
        return redirect('system_maintenance')

    # 2. Stats
    stats = {
        'total_products': Product.objects.count(),
        'missing_metadata': Product.objects.filter(metadata={}).count(),
        'missing_images': Product.objects.filter(Q(image_url__isnull=True) | Q(image_url='')).count(),
        'successful_tasks': Task.objects.filter(success=True).count(),
        'failed_tasks': Task.objects.filter(success=False).count(),
        'queued_tasks': OrmQ.objects.count(),
        'unconfirmed_mappings': [
            {
                'id': m.id,
                'store_name': m.store.name,
                'internal_code': m.internal_code,
                'product_name': m.product.display_name or m.product.name
            }
            for m in ProductMapping.objects.filter(is_confirmed=False).select_related('product', 'store')
        ],
        'schedules': list(Schedule.objects.all().values('name', 'func', 'schedule_type', 'next_run', 'repeats')),
        # Manually construct task list to calculate time_taken (not a DB field)
        'recent_tasks': [
            {
                'id': t.id,
                'name': t.name,
                'func': t.func,
                'success': t.success,
                'stopped': t.stopped,
                'result': str(t.result) if t.result else '',
                'time_taken': (t.stopped - t.started).total_seconds() if t.stopped and t.started else 0
            }
            for t in Task.objects.all().order_by('-stopped')[:50]
        ],
    }

    # Robust check for AJAX (header or query param)
    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('ajax') == '1':
        return JsonResponse(stats, encoder=DjangoJSONEncoder)
    
    return render(request, 'tracker/maintenance.html', {'stats': stats})

@login_required
def shopping_optimizer(request):
    query = request.GET.get('q', '')
    user_ids = _get_user_filter(request)
    
    items = ReceiptItem.objects.all().select_related('product', 'receipt', 'receipt__store')
    if user_ids:
        items = items.filter(receipt__user_id__in=user_ids)
    
    if query:
        items = items.filter(Q(product__name__icontains=query) | Q(product__display_name__icontains=query))

    raw_deals = items.order_by('product__name', 'unit_price')
    optimized_list = []
    seen_products = set()
    for item in raw_deals:
        norm_name = item.product.name.strip().upper()
        if norm_name not in seen_products:
            optimized_list.append({
                'product': item.product,
                'price': item.unit_price,
                'store_name': _get_trading_name(item.receipt.store.name),
                'store_obj': item.receipt.store,
                'date': item.receipt.issue_date
            })
            seen_products.add(norm_name)
    
    optimized_list.sort(key=lambda x: x['product'].name.strip().upper())
    context = {
        'optimized_list': optimized_list,
        'current_query': query
    }
    return render(request, 'tracker/optimizer.html', context)

@login_required
def category_list(request):
    categories = Category.objects.prefetch_related('products').annotate(product_count=Count('products')).order_by('name')
    all_categories = Category.objects.all().order_by('name')
    return render(request, 'tracker/category_list.html', {
        'categories': categories,
        'all_categories': all_categories,
    })

@login_required
@require_POST
def category_create(request):
    name = request.POST.get('name')
    if name:
        Category.objects.get_or_create(name=name)
        messages.success(request, f"Category '{name}' created.")
    return redirect('category_list')

@login_required
@require_POST
def category_update(request):
    cat_id = request.POST.get('id')
    new_name = request.POST.get('name')
    category = get_object_or_404(Category, id=cat_id)
    if new_name:
        category.name = new_name
        category.save()
        messages.success(request, "Category updated.")
    return redirect('category_list')

@login_required
@require_POST
def category_delete(request, category_id):
    category = get_object_or_404(Category, id=category_id)
    if category.products.exists():
        messages.error(request, "Cannot delete category: It still contains products.")
    else:
        category.delete()
        messages.success(request, "Category deleted.")
    return redirect('category_list')

@login_required
@require_POST
def update_product_category(request):
    product_id = request.POST.get('product_id')
    category_id = request.POST.get('category_id')
    product = get_object_or_404(Product, id=product_id)
    category = get_object_or_404(Category, id=category_id)
    product.category = category
    product.save()
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

@login_required
@require_POST
def update_product_details(request):
    product_id = request.POST.get('product_id')
    new_name = request.POST.get('display_name')
    new_brand = request.POST.get('brand')
    category_id = request.POST.get('category_id')
    
    product = get_object_or_404(Product, id=product_id)
    
    if new_name: product.display_name = new_name
    if new_brand is not None: product.brand = new_brand
    if category_id:
        product.category = get_object_or_404(Category, id=category_id)
    
    parent_id = request.POST.get('parent_id')
    if parent_id:
        product.parent = get_object_or_404(Product, id=parent_id)

    product.is_manually_edited = True
    product.save()
    messages.success(request, f"Updated details for: {product.display_name or product.name}")
    return redirect(request.META.get('HTTP_REFERER', 'dashboard'))

@login_required
@require_POST
def link_product_variant(request):
    child_id = request.POST.get('child_id')
    parent_id = request.POST.get('parent_id')
    child = get_object_or_404(Product, id=child_id)
    parent = get_object_or_404(Product, id=parent_id)
    
    child.parent = parent
    child.save()
    messages.success(request, f"Linked {child.name} as a variant of {parent.name}")
    return redirect('product_history', product_id=child.id)

@login_required
def dashboard(request):
    user_ids = _get_user_filter(request)
    cache_key = f"dashboard_stats_{hashlib.md5(str(user_ids).encode()).hexdigest()}"
    cached_context = cache.get(cache_key)
    
    if cached_context:
        return render(request, 'tracker/dashboard.html', cached_context)

    monthly_raw = Receipt.monthly_stats(user_ids=user_ids)
    spending_labels = [item['month'].strftime('%b %Y') for item in reversed(monthly_raw)]
    spending_data = [float(item['total_spent']) for item in reversed(monthly_raw)]
    discount_data = [float(item['total_discount']) for item in reversed(monthly_raw)]

    category_qs = Category.objects.all()
    if user_ids:
        category_qs = category_qs.filter(products__receiptitems__receipt__user_id__in=user_ids)
    
    category_raw = category_qs.annotate(
        total=Sum('products__receiptitems__total_price')
    ).filter(total__gt=0).order_by('-total')
    
    category_labels = [item.name for item in category_raw]
    category_data = [float(item.total) for item in category_raw]

    weekday_qs = Receipt.objects.all()
    if user_ids: weekday_qs = weekday_qs.filter(user_id__in=user_ids)
    weekday_raw = weekday_qs.annotate(weekday=ExtractWeekDay('issue_date')).values('weekday').annotate(total=Sum(F('total_amount') - F('discount'))).order_by('weekday')
    
    days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
    weekday_labels = [days[i['weekday']-1] for i in weekday_raw]
    weekday_data = [float(i['total']) for i in weekday_raw]

    product_qs = Product.objects.all()
    if user_ids: product_qs = product_qs.filter(receiptitems__receipt__user_id__in=user_ids)
    brand_raw = product_qs.values('brand').annotate(total=Sum('receiptitems__total_price')).filter(total__gt=10).order_by('-total')[:10]
    brand_labels = [i['brand'] for i in brand_raw]
    brand_data = [float(i['total']) for i in brand_raw]

    store_qs = Store.objects.all()
    if user_ids: store_qs = store_qs.filter(receipts__user_id__in=user_ids)
    store_perf_raw = store_qs.annotate(
        avg_receipt=Avg(F('receipts__total_amount') - F('receipts__discount')),
        total_items=Count('receipts__items'),
        avg_item_price=Avg('receipts__items__unit_price')
    ).order_by('avg_item_price')[:10]
    
    store_perf = []
    for s in store_perf_raw:
        s.trading_name = _get_trading_name(s.name); store_perf.append(s)

    receipt_qs = Receipt.objects.all()
    if user_ids: receipt_qs = receipt_qs.filter(user_id__in=user_ids)
    total_spent = receipt_qs.aggregate(sum=Sum(F('total_amount') - F('discount')))['sum'] or Decimal('0.00')
    total_tax = receipt_qs.aggregate(sum=Sum(F('tax_federal')+F('tax_state')+F('tax_municipal')))['sum'] or Decimal('0.00')
    tax_percentage = (total_tax / total_spent * 100) if total_spent > 0 else 0
    
    best_value_products = product_qs.annotate(
        best_name=Coalesce('display_name', 'name'),
        avg_norm_price=Avg('receiptitems__normalized_price')
    ).filter(avg_norm_price__gt=0).order_by('avg_norm_price')[:10]

    context = {
        'spending_labels': json.dumps(spending_labels), 'spending_data': json.dumps(spending_data), 
        'discount_data': json.dumps(discount_data), 'category_labels': json.dumps(category_labels), 
        'category_data': json.dumps(category_data), 'weekday_labels': json.dumps(weekday_labels), 
        'weekday_data': json.dumps(weekday_data), 'brand_labels': json.dumps(brand_labels), 
        'brand_data': json.dumps(brand_data), 'store_perf': store_perf, 'total_spent': total_spent, 
        'total_tax': total_tax, 'tax_percentage': tax_percentage, 'monthly_raw': monthly_raw, 
        'best_value_products': best_value_products
    }
    cache.set(cache_key, context, 600)
    return render(request, 'tracker/dashboard.html', context)

@login_required
def index(request):
    latest_receipts = Receipt.objects.filter(user=request.user).select_related('store').annotate(items_count=Count('items')).order_by('-issue_date')[:10]
    return render(request, 'tracker/index.html', {'latest_receipts': latest_receipts})

@login_required
def receipt_list(request):
    user_ids = _get_user_filter(request)
    sort_by = request.GET.get('sort', '-issue_date')
    if sort_by not in ['issue_date', '-issue_date', 'total_amount', '-total_amount', 'created_at', '-created_at']:
        sort_by = '-issue_date'
    query = request.GET.get('q', '')
    
    receipts = Receipt.objects.all().select_related('store', 'user', 'store__chain').annotate(items_count=Count('items'))
    if user_ids: receipts = receipts.filter(user_id__in=user_ids)

    if query:
        receipts = receipts.filter(Q(store__name__icontains=query) | Q(access_key__icontains=query) | Q(number__icontains=query))
    store_id = request.GET.get('store')
    if store_id: receipts = receipts.filter(store_id=store_id)

    receipts = receipts.order_by(sort_by).prefetch_related(
        models.Prefetch('items', queryset=ReceiptItem.objects.select_related('product'))
    )

    paginator = Paginator(receipts, 15)
    page_obj = paginator.get_page(request.GET.get('page'))
    
    stores = Store.objects.all().distinct().order_by('name')
    if user_ids: stores = stores.filter(receipts__user_id__in=user_ids)
    
    context = {'page_obj': page_obj, 'stores': stores, 'current_sort': sort_by, 'current_query': query, 'current_store': store_id}
    return render(request, 'tracker/receipt_list.html', context)

@login_required
@receipt_owner_required
def receipt_detail(request, receipt_id):
    receipt = get_object_or_404(Receipt.objects.select_related('store', 'user'), id=receipt_id)
    items = receipt.items.select_related('product', 'product__category').all()
    
    # Calculate benchmarks for items
    for item in items:
        item.benchmark = AnalyticsService.get_price_benchmark(request.user, item.product_id, item.unit_price)
        
    item_query = request.GET.get('q', '')
    if item_query: items = items.filter(product__name__icontains=item_query)
    category_summary = items.values('product__category__name').annotate(total=Sum('total_price'), count=Count('id')).order_by('-total')
    all_categories = Category.objects.all().order_by('name')
    return render(request, 'tracker/receipt_detail.html', {
        'receipt': receipt, 'items': items, 'category_summary': category_summary, 'item_query': item_query, 'all_categories': all_categories
    })

@login_required
def product_comparison(request):
    user_ids = _get_user_filter(request)
    query = request.GET.get('q', '')
    category_id = request.GET.get('category')
    sort_by = request.GET.get('sort', 'avg_norm_price')
    
    products = Product.objects.select_related('category').all()
    if user_ids: products = products.filter(receiptitems__receipt__user_id__in=user_ids)
    
    products = products.annotate(
        avg_norm_price=Avg('receiptitems__normalized_price'),
        purchase_count=Count('receiptitems'),
        total_volume=Sum('receiptitems__quantity')
    ).filter(purchase_count__gt=0)
    
    if query: products = products.filter(Q(name__icontains=query) | Q(display_name__icontains=query) | Q(brand__icontains=query))
    if category_id: products = products.filter(category_id=category_id)
    products = products.order_by(sort_by)
    paginator = Paginator(products, 20)
    page_obj = paginator.get_page(request.GET.get('page'))
    categories = Category.objects.all().order_by('name')
    context = {'page_obj': page_obj, 'categories': categories, 'current_query': query, 'current_category': category_id, 'current_sort': sort_by}
    return render(request, 'tracker/product_compare.html', context)

@login_required
def inflation_analysis(request):
    user_ids = _get_user_filter(request)
    
    base_qs = ReceiptItem.objects.all()
    if user_ids: base_qs = base_qs.filter(receipt__user_id__in=user_ids)
    
    monthly_stats = base_qs.annotate(
        month=TruncMonth('receipt__issue_date')
    ).values('month').annotate(avg_price=Avg('normalized_price'), count=Count('id')).order_by('month')

    top_categories = Category.objects.all()
    if user_ids: top_categories = top_categories.filter(products__receiptitems__receipt__user_id__in=user_ids)
    top_categories = top_categories.annotate(item_count=Count('products__receiptitems')).order_by('-item_count')[:5]

    category_datasets = []
    colors = ['#10b981', '#6366f1', '#f59e0b', '#ef4444', '#8b5cf6']
    all_months = sorted(list(set(item['month'] for item in monthly_stats)))
    month_labels = [m.strftime('%b %Y') for m in all_months]

    for idx, cat in enumerate(top_categories):
        cat_stats = ReceiptItem.objects.filter(product__category=cat)
        if user_ids: cat_stats = cat_stats.filter(receipt__user_id__in=user_ids)
        cat_stats = cat_stats.annotate(month=TruncMonth('receipt__issue_date')).values('month').annotate(avg_price=Avg('normalized_price')).order_by('month')
        
        price_map = {s['month']: float(s['avg_price']) for s in cat_stats}
        cat_data = [price_map.get(m, None) for m in all_months]
        category_datasets.append({'label': cat.name, 'data': cat_data, 'borderColor': colors[idx], 'backgroundColor': 'transparent', 'tension': 0.3})

    stats_with_change = []; prev_price = None; labels, data = [], []
    for item in monthly_stats:
        change = ((item['avg_price'] - prev_price) / prev_price * 100) if prev_price else 0
        stats_with_change.append({'month': item['month'], 'avg_price': item['avg_price'], 'change': change, 'count': item['count']})
        labels.append(item['month'].strftime('%b %Y')); data.append(float(item['avg_price']))
        prev_price = item['avg_price']

    context = {'stats': reversed(stats_with_change), 'labels': json.dumps(month_labels), 'global_data': json.dumps(data), 'category_datasets': json.dumps(category_datasets)}
    return render(request, 'tracker/inflation.html', context)

@login_required
def product_history(request, product_id):
    user_ids = _get_user_filter(request)
    product = get_object_or_404(Product, id=product_id)
    history = ReceiptItem.objects.filter(product=product)
    if user_ids: history = history.filter(receipt__user_id__in=user_ids)
    history = history.select_related('receipt', 'receipt__store').order_by('receipt__issue_date')
    
    if not history.exists():
         messages.error(request, "No history available for this selection."); return redirect('dashboard')

    store_ranking_raw = history.values('receipt__store__name').annotate(min_price=Min('unit_price'), avg_price=Avg('unit_price')).order_by('min_price')
    store_ranking = []
    for s in store_ranking_raw:
        s['trading_name'] = _get_trading_name(s['receipt__store__name']); store_ranking.append(s)
    chart_labels = [item.receipt.issue_date.strftime('%d/%m/%Y') for item in history]
    chart_data = [float(item.unit_price) for item in history]
    
    # ECharts Candlestick Data
    candlestick_data = AnalyticsService.get_product_candlesticks(request.user, product.id)
    all_categories = Category.objects.all().order_by('name')
    all_products = Product.objects.exclude(id=product.id).order_by('display_name', 'name')
    
    context = {
        'product': product, 'history': history, 
        'chart_labels': json.dumps(chart_labels), 'chart_data': json.dumps(chart_data), 
        'store_ranking': store_ranking,
        'candlestick_data': json.dumps(candlestick_data),
        'all_categories': all_categories,
        'all_products': all_products
    }
    return render(request, 'tracker/product_history.html', context)

@login_required
@require_POST
@receipt_owner_required
def delete_receipt(request, receipt_id):
    receipt = get_object_or_404(Receipt, id=receipt_id)
    receipt.delete()
    cache.clear() # Clear all to be safe for admin
    messages.success(request, "Receipt deleted successfully."); return redirect('receipt_list')

@login_required
@require_POST
@transaction.atomic
def process_nfce_url(request):
    url = request.POST.get('url')
    if not url: return render(request, 'tracker/index.html', {'error': 'URL is required'})
    try:
        scraper = NFCeScraper(); new_data = scraper.scrape_url(url); access_key = new_data['receipt']['access_key']
        ScrapeLog.objects.create(url=url, status='SUCCESS', access_key=access_key, user=request.user)
        user_receipt = Receipt.objects.filter(access_key=access_key, user=request.user).first()
        if user_receipt:
            diff = _generate_receipt_diff(user_receipt, new_data)
            return render(request, 'tracker/refresh_preview.html', {'receipt': user_receipt, 'new_data': new_data, 'diff': diff, 'is_duplicate': True, 'new_url': url})
        receipt = ReceiptService.save_scraped_data(new_data, url, request.user)
        messages.success(request, f"Successfully processed receipt from {receipt.store.name}")
        return redirect('receipt_detail', receipt_id=receipt.id)
    except Exception as e:
        ScrapeLog.objects.create(url=url, status='FAILED', error_message=str(e), user=request.user)
        logger.error(f"Failed to parse URL {url}: {str(e)}", exc_info=True)
        return render(request, 'tracker/index.html', {'error': f"Failed to parse: {str(e)}"})

@login_required
@require_POST
@receipt_owner_required
def refresh_receipt(request, receipt_id):
    receipt = get_object_or_404(Receipt, id=receipt_id)
    try:
        scraper = NFCeScraper(); new_data = scraper.scrape_url(receipt.url); diff = _generate_receipt_diff(receipt, new_data)
        return render(request, 'tracker/refresh_preview.html', {'receipt': receipt, 'new_data': new_data, 'diff': diff, 'is_duplicate': False})
    except Exception as e:
        messages.error(request, f"Refresh failed: {str(e)}"); return redirect('receipt_detail', receipt_id=receipt.id)

@login_required
@require_POST
@transaction.atomic
def confirm_refresh(request):
    url = request.POST.get('url'); scraper = NFCeScraper(); new_data = scraper.scrape_url(url); access_key = new_data['receipt']['access_key']
    existing = Receipt.objects.filter(access_key=access_key, user=request.user).first()
    if existing: existing.delete()
    receipt = ReceiptService.save_scraped_data(new_data, url, request.user)
    messages.success(request, f"Updated receipt from {receipt.store.name}")
    return redirect('receipt_detail', receipt_id=receipt.id)

def _generate_receipt_diff(existing, new_data):
    existing_items = {f"{i.product.name}": i for i in existing.items.all()}; new_items_list = new_data['items']
    new_total = Decimal(str(new_data['receipt']['total_amount']))
    diff = {'added': [], 'removed': [], 'updated': [], 'total_change': new_total - existing.total_amount, 'tax_change': (Decimal(str(new_data['receipt']['tax_federal'])) + Decimal(str(new_data['receipt']['tax_state']))) - (existing.tax_federal + existing.tax_state)}
    new_names = set()
    for item in new_items_list:
        name = item['name']; new_names.add(name)
        if name in existing_items:
            old = existing_items[name]
            if Decimal(str(item['total_price'])) != old.total_price or Decimal(str(item['quantity'])) != old.quantity:
                diff['updated'].append({'name': name, 'old_total': old.total_price, 'new_total': item['total_price']})
        else: diff['added'].append(item)
    for name, item in existing_items.items():
        if name not in new_names: diff['removed'].append(item)
    return diff

# --- NEW ANALYTICS VIEWS ---

@login_required
def product_search_api(request):
    query = request.GET.get('q', '')
    if len(query) < 2:
        return JsonResponse([], safe=False)
    
    products = Product.objects.filter(
        Q(name__icontains=query) | Q(display_name__icontains=query)
    ).values('id', 'name', 'display_name').distinct()[:10]
    
    results = []
    for p in products:
        results.append({
            'id': p['id'],
            'text': p['display_name'] or p['name']
        })
    return JsonResponse(results, safe=False)

@login_required
def analytics_dashboard(request):
    # Returns the new Ultimate Dashboard view
    context = {}
    return render(request, 'tracker/analytics.html', context)

@login_required
def smart_cart(request):
    context = {}
    if request.method == 'POST':
        shopping_list = request.POST.get('shopping_list', '')
        optimization_result = SmartCartService.optimize_cart(request.user, shopping_list)
        context['result'] = optimization_result
        context['original_list'] = shopping_list
    return render(request, 'tracker/smart_cart.html', context)

@login_required
def api_chart_data(request):
    """
    JSON Endpoint for fetching heavy analytics data asynchronously.
    """
    chart_type = request.GET.get('type')
    
    if chart_type == 'heatmap':
        data = AnalyticsService.get_inflation_heatmap(request.user)
        return JsonResponse({'data': data}, safe=False)
        
    elif chart_type == 'radar':
        data = AnalyticsService.get_category_radar(request.user)
        return JsonResponse(data, safe=False)

    elif chart_type == 'pareto':
        data = AnalyticsService.get_pareto_analysis(request.user)
        return JsonResponse(data, safe=False)

    elif chart_type == 'forecast':
        data = AnalyticsService.get_spending_forecast(request.user)
        return JsonResponse(data, safe=False)

    elif chart_type == 'health':
        data = AnalyticsService.get_health_analysis(request.user)
        return JsonResponse(data, safe=False)

    elif chart_type == 'drift':
        data = AnalyticsService.get_budget_drift(request.user)
        return JsonResponse(data, safe=False)

    elif chart_type == 'shrinkflation':
        data = AnalyticsService.get_shrinkflation_report(request.user)
        suggestions = AnalyticsService.get_variant_suggestions(request.user)
        # Format suggestions for JSON
        s_list = []
        for s in suggestions:
            s_list.append({
                'id1': s['p1'].id, 
                'name1': s['p1'].display_name or s['p1'].name,
                'id2': s['p2'].id,
                'name2': s['p2'].display_name or s['p2'].name,
                'reason': s['reason']
            })
        return JsonResponse({'report': data, 'suggestions': s_list}, safe=False)
        
    return JsonResponse({'error': 'Invalid chart type'}, status=400)

