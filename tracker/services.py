from django.db import transaction, connection
from django.db.models import Avg, Min, Max, StdDev, Window, F, Sum, Count, Q, Subquery, OuterRef
from django.db.models.functions import TruncMonth, Lag, Coalesce
from django.core.cache import cache
from django.utils import timezone
from decimal import Decimal
from datetime import datetime, timedelta
from math import radians, cos, sin, asin, sqrt
import re
from django_q.tasks import async_task
from rapidfuzz import fuzz, process
from .models import Store, Product, Category, Receipt, ReceiptItem, PriceHistory, ProductMapping
from .enrichment import ProductEnrichmentService

class ReceiptService:
    @staticmethod
    def generate_readable_name(raw_name):
        """
        Translates cryptic receipt abbreviations into human-readable Portuguese.
        Example: "CR LEITE ITALAC 200G" -> "Creme Leite Italac 200g"
        """
        abbreviations = {
            'CR': 'Creme',
            'CD': 'Creme Dental',
            'FGO': 'Frango',
            'DET': 'Detergente',
            'MAC': 'Macarrão',
            'EXT': 'Extrato',
            'INTEG': 'Integral',
            'INTE': 'Integral',
            'ZER': 'Zero',
            'BCO': 'Branco',
            'LIMP': 'Limpeza',
            'ABAC': 'Abacaxi',
            'IOG': 'Iogurte',
            'CHOC': 'Chocolate',
            'SAB': 'Sabonete',
            'AMAC': 'Amaciante',
            'COND': 'Condicionador',
            'DESN': 'Desnatado',
            'REFRI': 'Refrigerante',
            'SUC': 'Suco',
            'CERV': 'Cerveja',
            'VIN': 'Vinho',
            'MOL': 'Molho',
            'ACUC': 'Açúcar',
            'FAR': 'Farinha',
            'MAN': 'Manteiga',
            'QUEI': 'Queijo',
            'PRES': 'Presunto',
            'LING': 'Linguiça',
            'SALS': 'Salsicha',
            'BISC': 'Biscoito',
            'BOL': 'Bolacha',
            'TRIGO': 'Farinha de Trigo',
            'KG': 'kg',
            'UN': 'un',
            'LT': 'L'
        }
        
        words = raw_name.upper().split()
        expanded_words = []
        for word in words:
            # Check if word is exactly an abbreviation
            expanded = abbreviations.get(word, word.capitalize())
            expanded_words.append(expanded)
            
        return ' '.join(expanded_words)

    @staticmethod
    @transaction.atomic
    def save_scraped_data(data, url, user):
        """
        Processes and saves a receipt and its items.
        Uses GTIN and ProductMapping (internal store codes) for 100% accuracy.
        """
        store, _ = Store.objects.get_or_create(
            cnpj=data['store']['cnpj'],
            defaults={
                'name': data['store']['name'],
                'address_city': data['store']['city'],
                'address_neighborhood': data['store']['neighborhood'],
                'address_street': data['store']['street']
            }
        )

        receipt = Receipt.objects.create(
            store=store,
            user=user,
            url=url,
            **data['receipt']
        )

        for i in data['items']:
            cat, _ = Category.objects.get_or_create(name=i.get('category', 'Geral'))
            gtin, name = i.get('code_gtin'), i['name'].strip()
            internal_code = i.get('internal_code')
            
            prod = None
            
            # 1. Try GTIN (Universal)
            if gtin:
                prod = Product.objects.filter(code_gtin=gtin).first()
            
            # 2. Try Store Mapping (Internal Code Anchor)
            if not prod and internal_code:
                mapping = ProductMapping.objects.filter(store=store, internal_code=internal_code).first()
                if mapping:
                    prod = mapping.product
            
            # 3. Try Name (Fuzzy Fallback)
            if not prod:
                prod = Product.objects.filter(name__iexact=name).first()

            # 4. Try Semantic Similarity Match (New)
            is_fuzzy_match = False
            if not prod:
                prod = ProductMatchingService.find_best_match(
                    name=name,
                    category_name=i.get('category'),
                    brand=i.get('brand')
                )
                if prod: is_fuzzy_match = True
            
            # 5. Create if still not found
            if not prod:
                readable = ReceiptService.generate_readable_name(name)
                prod = Product.objects.create(
                    name=name,
                    display_name=readable,
                    category=cat,
                    brand=i.get('brand', ''),
                    ncm=i.get('ncm', ''),
                    code_gtin=gtin
                )
                # Async Enrichment: Don't block the receipt saving process
                async_task('tracker.tasks.async_enrich_product', prod.id, q_options={'name': f"Enrich: {prod.name[:20]}"})
            else:
                # Update product metadata ONLY if not manually edited by user
                if not prod.is_manually_edited:
                    changed = False
                    if gtin and not prod.code_gtin:
                        prod.code_gtin = gtin
                        changed = True
                    # Only upgrade category if it was the default 'Geral'
                    if prod.category and prod.category.name == 'Geral' and cat.name != 'Geral':
                        prod.category = cat
                        changed = True
                    if changed:
                        prod.save()
            
            # 5. Stabilize identification for future scrapes
            if internal_code:
                mapping, created = ProductMapping.objects.get_or_create(
                    user=user,
                    store=store, 
                    internal_code=internal_code, 
                    defaults={'product': prod, 'is_confirmed': not is_fuzzy_match}
                )
                # If mapping existed but was unconfirmed, and we found a new exact match, we don't change it here
                # but if we just created it from a fuzzy match, it stays is_confirmed=False

            item = ReceiptItem.objects.create(
                receipt=receipt,
                product=prod,
                quantity=i['quantity'],
                unit_type=i['unit_type'],
                unit_price=i['unit_price'],
                total_price=i['total_price'],
                normalized_price=i.get('normalized_price')
            )

            # Populate Time-Series History
            PriceHistory.objects.create(
                user=user,
                product=prod,
                store=store,
                date=receipt.issue_date,
                unit_price=i['unit_price'],
                normalized_price=item.normalized_price
            )

        cache.clear()
        return receipt

class AnalyticsService:
    @staticmethod
    def get_inflation_heatmap(user):
        """
        Calculates Store Competitiveness Index for a specific user.
        """
        end_date = timezone.now()
        start_date = end_date - timedelta(days=180)
        
        # 1. Calculate Global Average per Product (Market Baseline)
        # We still use Global averages for the baseline, but store deltas are personal
        global_avgs = PriceHistory.objects.filter(date__gte=start_date).values('product_id').annotate(
            avg_price=Avg('normalized_price')
        )
        avg_map = {item['product_id']: float(item['avg_price']) for item in global_avgs}
        
        # 2. Fetch prices per store for THIS user
        raw_stats = PriceHistory.objects.filter(user=user, date__gte=start_date).select_related('store').values(
            'store__name', 'store__id', 'store__latitude', 'store__longitude', 'product_id', 'normalized_price'
        )
        
        store_scores = {}
        for row in raw_stats:
            s_id = row['store__id']
            p_id = row['product_id']
            if s_id not in store_scores:
                store_scores[s_id] = {
                    'name': row['store__name'],
                    'lat': row['store__latitude'],
                    'lon': row['store__longitude'],
                    'deltas': []
                }
            
            global_avg = avg_map.get(p_id)
            if global_avg and global_avg > 0 and row['normalized_price']:
                # Calculate % difference from market average
                price = float(row['normalized_price'])
                delta_pct = ((price - global_avg) / global_avg) * 100
                store_scores[s_id]['deltas'].append(delta_pct)
        
        data = []
        for s_id, s_data in store_scores.items():
            if s_data['deltas']:
                avg_delta = sum(s_data['deltas']) / len(s_data['deltas'])
                data.append({
                    'name': s_data['name'],
                    'lat': s_data['lat'],
                    'lon': s_data['lon'],
                    'value': round(avg_delta, 2), # e.g. -5.2 means 5.2% cheaper than average
                    'count': len(s_data['deltas'])
                })
        
        # Sort: Most expensive first
        data.sort(key=lambda x: x['value'], reverse=True)
        return data

    @staticmethod
    def get_product_candlesticks(user, product_id):
        """
        Returns OHLC (Open, High, Low, Close) data for a product per month.
        """
        raw_data = PriceHistory.objects.filter(user=user, product_id=product_id).order_by('date')
        if not raw_data.exists(): return []

        grouped = {}
        for row in raw_data:
            m = row.date.strftime('%Y-%m')
            if m not in grouped: grouped[m] = {'prices': [], 'date': row.date}
            grouped[m]['prices'].append(float(row.unit_price))
        
        chart_data = []
        for m, d in grouped.items():
            prices = d['prices']
            chart_data.append({
                'date': m,
                'open': prices[0],
                'close': prices[-1],
                'low': min(prices),
                'high': max(prices)
            })
        return chart_data

    @staticmethod
    def get_category_radar(user, store_ids=None):
        """
        Compares store competitiveness across categories for a specific user.
        Metric: Average normalized price per category.
        Includes dynamic max bounds for ECharts.
        """
        qs = PriceHistory.objects.filter(
            user=user,
            date__gte=timezone.now()-timedelta(days=120)
        )
        
        if store_ids:
            qs = qs.filter(store_id__in=store_ids)
            
        data = qs.values('store__name', 'product__category__name').annotate(
            avg_price=Avg('normalized_price')
        ).order_by('store__name')
        
        radar_data = {}
        stores = set()
        category_maxes = {}
        
        for row in data:
            s, c, p = row['store__name'], row['product__category__name'], float(row['avg_price'] or 0)
            if c not in radar_data: radar_data[c] = {}
            radar_data[c][s] = p
            stores.add(s)
            
            if c not in category_maxes or p > category_maxes[c]:
                category_maxes[c] = p * 1.15
            
        indicators = []
        for cat, m_val in category_maxes.items():
            indicators.append({'name': cat, 'max': round(m_val, 2)})
            
        return {
            'indicators': indicators, 
            'stores': list(stores), 
            'data': radar_data,
            'categories': list(category_maxes.keys())
        }

    @staticmethod
    def get_pareto_analysis(user):
        """
        Performs ABC Analysis (Pareto Principle).
        Identifies the top 20% of items that cause 80% of spending.
        """
        start_date = timezone.now() - timedelta(days=180)
        product_stats = ReceiptItem.objects.filter(
            receipt__user=user, 
            receipt__issue_date__gte=start_date
        ).annotate(
            best_name=Coalesce('product__display_name', 'product__name')
        ).values(
            'best_name', 'product__category__name'
        ).annotate(
            total_spend=Sum('total_price'),
            avg_unit_price=Avg('unit_price')
        ).order_by('-total_spend')

        if not product_stats:
            return {'a_items': [], 'b_items': [], 'c_items': []}

        total_portfolio_spend = sum(item['total_spend'] for item in product_stats)
        cumulative_spend = 0
        
        a_spend, b_spend, c_spend = 0, 0, 0
        a_items, b_items, c_items = [], [], []
        
        for item in product_stats:
            spend = float(item['total_spend'])
            
            data = {
                'name': item['best_name'],
                'category': item['product__category__name'],
                'spend': spend,
                'avg_price': float(item['avg_unit_price'])
            }
            
            class_pct = (cumulative_spend / float(total_portfolio_spend)) * 100
            
            if class_pct < 80:
                a_items.append(data)
                a_spend += spend
            elif class_pct < 95:
                b_items.append(data)
                b_spend += spend
            else:
                c_items.append(data)
                c_spend += spend
            
            cumulative_spend += spend
                
        return {
            'a_items': a_items,
            'b_items': b_items,
            'c_items': c_items,
            'a_count': len(a_items),
            'b_count': len(b_items),
            'c_count': len(c_items),
            'a_spend': round(a_spend, 2),
            'b_spend': round(b_spend, 2),
            'c_spend': round(c_spend, 2),
            'total_items': len(product_stats)
        }

    @staticmethod
    def get_spending_forecast(user):
        """
        Projects end-of-month spending based on current daily burn rate.
        """
        today = timezone.now()
        first_day = today.replace(day=1)
        days_passed = today.day
        last_day = (today.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        total_days = last_day.day
        
        current_spend = Receipt.objects.filter(
            user=user, 
            issue_date__gte=first_day, 
            issue_date__lte=today
        ).aggregate(sum=Sum(F('total_amount') - F('discount')))['sum'] or Decimal('0')
        
        if current_spend == 0 or days_passed < 2: return None
        
        daily_burn = float(current_spend) / days_passed
        projected_total = daily_burn * total_days
        
        return {
            'current': float(current_spend),
            'projected': round(projected_total, 2),
            'days_left': total_days - days_passed,
            'burn_rate': round(daily_burn, 2)
        }

    @staticmethod
    def get_variant_suggestions(user):
        """
        Finds products that share a brand and similar name but aren't linked.
        Suggests them as potential shrinkflation candidates.
        """
        # Find products without a parent and not being a parent themselves
        lone_products = Product.objects.filter(
            parent__isnull=True,
            variants__isnull=True,
            receiptitems__receipt__user=user
        ).distinct()

        suggestions = []
        # Group by brand to limit search space
        brands = lone_products.values_list('brand', flat=True).distinct()
        
        for brand in brands:
            if not brand or brand == 'Generic': continue
            
            prods = list(lone_products.filter(brand=brand))
            for i in range(len(prods)):
                for j in range(i + 1, len(prods)):
                    p1, p2 = prods[i], prods[j]
                    
                    # Fuzzy match: check if first 10 chars match (e.g. 'Chocolate ')
                    # or if they share significant name tokens
                    n1 = (p1.display_name or p1.name).upper()
                    n2 = (p2.display_name or p2.name).upper()
                    
                    if n1[:8] == n2[:8]:
                        # Ensure weights exist and are different
                        w1 = p1.weight_grams or 0
                        w2 = p2.weight_grams or 0
                        
                        if w1 > 0 and w2 > 0 and w1 != w2:
                            suggestions.append({
                                'p1': p1,
                                'p2': p2,
                                'reason': f"Same brand & similar name, but different weights ({w1}g vs {w2}g)"
                            })
        
        return suggestions[:5] # Limit to top 5 suggestions

    @staticmethod
    def get_shrinkflation_report(user):
        """
        Detects products whose volume has decreased over time for this user.
        Compares variants linked to the same canonical parent.
        """
        # Find all 'Master' products that have variants AND the user has purchased at least one version
        masters = Product.objects.filter(
            variants__isnull=False,
            variants__receiptitems__receipt__user=user
        ).distinct()
        
        report = []
        for master in masters:
            variants = master.variants.all().order_by('id') # Simplified timeline for now
            if variants.count() < 2: continue
            
            # Compare first and last variant
            v_old = variants.first()
            v_new = variants.last()
            
            # Logic: Try to extract weight from names (e.g. 395g vs 350g)
            def extract_weight(name):
                # Look for numbers (including decimals) followed by weight units
                # Pattern: matches '395g', '1,15kg', '2 L', '500 ml'
                matches = re.findall(r'(\d+[\.,]?\d*)\s*(G|KG|ML|L)', name.upper())
                if matches:
                    # Take the last match (usually where the size is in the name)
                    val_str, unit = matches[-1]
                    val = float(val_str.replace(',', '.'))
                    return val if unit in ['G', 'ML'] else val * 1000
                return 0

            w_old = extract_weight(v_old.name)
            w_new = extract_weight(v_new.name)
            
            if w_new < w_old and w_new > 0:
                shrink_pct = ((w_old - w_new) / w_old) * 100
                
                # Financial Impact: Compare average normalized price (Price per 1kg/1L)
                p_old = PriceHistory.objects.filter(product=v_old).aggregate(Avg('normalized_price'))['normalized_price__avg'] or 0
                p_new = PriceHistory.objects.filter(product=v_new).aggregate(Avg('normalized_price'))['normalized_price__avg'] or 0
                
                cost_impact = 0
                if p_old > 0:
                    cost_impact = ((float(p_new) - float(p_old)) / float(p_old)) * 100

                report.append({
                    'master': master.display_name or master.name,
                    'old_variant': v_old.display_name or v_old.name,
                    'new_variant': v_new.display_name or v_new.name,
                    'old_weight': w_old,
                    'new_weight': w_new,
                    'shrink_pct': round(shrink_pct, 1),
                    'cost_impact': round(cost_impact, 1)
                })
        
        return report

    @staticmethod
    def get_price_benchmark(user, product_id, current_price):
        """
        Benchmarks a price against the history of the product.
        Returns: { 'label': 'Great Deal', 'color': '#10b981', 'diff': -15 }
        """
        history = PriceHistory.objects.filter(user=user, product_id=product_id).values_list('unit_price', flat=True)
        if len(history) < 3:
            return None # Not enough data to benchmark
            
        prices = sorted([float(p) for p in history])
        p25 = prices[int(len(prices) * 0.25)]
        p75 = prices[int(len(prices) * 0.75)]
        
        current = float(current_price)
        avg = sum(prices) / len(prices)
        diff_pct = ((current - avg) / avg) * 100
        
        if current <= p25:
            return {'label': 'Great Deal', 'class': 'badge-success', 'icon': '🔥', 'diff': round(diff_pct, 1)}
        elif current >= p75:
            return {'label': 'Expensive', 'class': 'badge-danger', 'icon': '🚩', 'diff': round(diff_pct, 1)}
        else:
            return {'label': 'Fair Price', 'class': 'badge-info', 'icon': '⚖️', 'diff': round(diff_pct, 1)}

    @staticmethod
    def get_health_analysis(user):
        """
        Aggregates NOVA groups, Eco-scores and nutritional density.
        Weighted by Volume (Quantity x Weight).
        """
        # Fetch items with products and weights
        items = ReceiptItem.objects.filter(receipt__user=user).select_related('product')
        
        stats = {
            'nova': {
                1: {'count': 0, 'items': []},
                2: {'count': 0, 'items': []},
                3: {'count': 0, 'items': []},
                4: {'count': 0, 'items': []},
                'unknown': {'count': 0, 'items': []}
            },
            'eco': {
                'a': {'count': 0, 'items': []}, 'b': {'count': 0, 'items': []},
                'c': {'count': 0, 'items': []}, 'd': {'count': 0, 'items': []},
                'e': {'count': 0, 'items': []}, 'unknown': {'count': 0, 'items': []}
            },
            'nutrients': {'sugar': 0, 'salt': 0, 'fat': 0, 'total_volume_kg': 0}
        }
        
        seen_products = set()
        for item in items:
            p = item.product
            m = p.metadata or {}
            p_name = p.display_name or p.name
            
            # 1. Bucket Distribution (Unique Products)
            if p.id not in seen_products:
                # NOVA
                nova = m.get('nova_group')
                try:
                    nova_int = int(nova)
                    if nova_int in [1, 2, 3, 4]:
                        stats['nova'][nova_int]['count'] += 1
                        stats['nova'][nova_int]['items'].append(p_name)
                    else:
                        stats['nova']['unknown']['count'] += 1
                        stats['nova']['unknown']['items'].append(p_name)
                except (TypeError, ValueError):
                    stats['nova']['unknown']['count'] += 1
                    stats['nova']['unknown']['items'].append(p_name)
                
                # Eco-Score
                eco = str(m.get('ecoscore', 'unknown')).lower()
                if eco in stats['eco']:
                    stats['eco'][eco]['count'] += 1
                    stats['eco'][eco]['items'].append(p_name)
                else:
                    stats['eco']['unknown']['count'] += 1
                    stats['eco']['unknown']['items'].append(p_name)
                
                seen_products.add(p.id)

            # 2. Nutritional Density (Weighted by Volume)
            nutri = m.get('nutrition', {})
            if nutri:
                # Weight in KG for this specific purchase
                weight_kg = (float(p.weight_grams or 0) / 1000.0) * float(item.quantity)
                # If weight not found, assume 1kg/unit for heuristic density
                if weight_kg == 0: weight_kg = float(item.quantity) 

                stats['nutrients']['sugar'] += float(nutri.get('sugars_100g', 0) or 0) * weight_kg
                stats['nutrients']['salt'] += float(nutri.get('salt_100g', 0) or 0) * weight_kg
                stats['nutrients']['fat'] += float(nutri.get('fat_100g', 0) or 0) * weight_kg
                stats['nutrients']['total_volume_kg'] += weight_kg
        
        if stats['nutrients']['total_volume_kg'] > 0:
            vol = stats['nutrients']['total_volume_kg']
            stats['nutrients']['sugar'] = round(stats['nutrients']['sugar'] / vol, 1)
            stats['nutrients']['salt'] = round(stats['nutrients']['salt'] / vol, 1)
            stats['nutrients']['fat'] = round(stats['nutrients']['fat'] / vol, 1)
            
        return stats

    @staticmethod
    def get_budget_drift(user):
        """
        Calculates the change in cost for a 'Standard Basket' 
        (top 10 items by frequency) across different stores.
        """
        top_items = Product.objects.filter(receiptitems__receipt__user=user).annotate(
            freq=Count('receiptitems')
        ).order_by('-freq')[:10]
        
        stores = Store.objects.filter(receipts__user=user).distinct()
        drift_report = []
        
        for store in stores:
            current_total = 0
            previous_total = 0
            count = 0
            
            for prod in top_items:
                prices = PriceHistory.objects.filter(product=prod, store=store).order_by('-date')[:2]
                if len(prices) >= 2:
                    current_total += float(prices[0].unit_price)
                    previous_total += float(prices[1].unit_price)
                    count += 1
            
            if count > 0 and previous_total > 0:
                diff = current_total - previous_total
                pct = (diff / previous_total) * 100
                drift_report.append({
                    'store': store.name,
                    'diff': round(diff, 2),
                    'pct': round(pct, 1),
                    'status': 'up' if diff > 0 else 'down' if diff < 0 else 'stable'
                })
        
        return drift_report

class SmartCartService:
    @staticmethod
    def optimize_cart(user, shopping_list_text):
        """
        Solves the 'Basket Splitter' problem.
        Input: Text list (newline separated).
        Output: Optimization plan.
        """
        items_needed = [line.strip() for line in shopping_list_text.split('\n') if line.strip()]
        if not items_needed: return None

        product_map = {}
        for item_name in items_needed:
            match = Product.objects.filter(
                Q(name__icontains=item_name) | Q(display_name__icontains=item_name)
            ).annotate(
                recent_price=Subquery(
                    PriceHistory.objects.filter(product=OuterRef('pk')).order_by('-date').values('unit_price')[:1]
                ),
                store_name=Subquery(
                    PriceHistory.objects.filter(product=OuterRef('pk')).order_by('-date').values('store__name')[:1]
                ),
                store_id=Subquery(
                    PriceHistory.objects.filter(product=OuterRef('pk')).order_by('-date').values('store__id')[:1]
                )
            ).filter(recent_price__isnull=False).first()
            
            if match:
                if match.id not in product_map:
                    product_map[match.id] = {'product': match, 'inputs': [item_name], 'quantity': 1}
                else:
                    product_map[match.id]['inputs'].append(item_name)
                    product_map[match.id]['quantity'] += 1

        store_baskets = {}
        for p_id, p_data in product_map.items():
            prod = p_data['product']
            qty = p_data['quantity']
            prices = PriceHistory.objects.filter(
                product=prod, 
                date__gte=timezone.now()-timedelta(days=120)
            ).values('store__name').annotate(price=Avg('unit_price'))
            
            for p in prices:
                s_name = p['store__name']
                if s_name not in store_baskets: store_baskets[s_name] = {'total': 0, 'items': []}
                item_total = float(p['price']) * qty
                store_baskets[s_name]['total'] += item_total
                store_baskets[s_name]['items'].append({
                    'item': ", ".join(p_data['inputs']), 
                    'product': prod.display_name or prod.name, 
                    'price': float(p['price']),
                    'quantity': qty,
                    'total': item_total,
                    'benchmark': AnalyticsService.get_price_benchmark(user, prod.id, p['price'])
                })

        valid_stores = []
        unique_item_count = len(product_map)
        for s, data in store_baskets.items():
            if len(data['items']) >= unique_item_count * 0.5:
                data['store'] = s
                data['missing_count'] = unique_item_count - len(data['items'])
                valid_stores.append(data)
        
        valid_stores.sort(key=lambda x: x['total'])
        
        # --- NEW: Split-Trip Logic ---
        split_trip = {'items': [], 'total': 0, 'savings': 0}
        best_single_total = valid_stores[0]['total'] if valid_stores else 0
        
        for p_id, p_data in product_map.items():
            prod = p_data['product']
            qty = p_data['quantity']
            # Find the absolute best price ever recorded for this product
            best_price_record = PriceHistory.objects.filter(
                product=prod,
                date__gte=timezone.now()-timedelta(days=120)
            ).values('store__name', 'unit_price', 'date').order_by('unit_price').first()
            
            if best_price_record:
                # Calculate Confidence
                record_date = best_price_record['date']
                if hasattr(record_date, 'date'): record_date = record_date.date()
                
                days_old = (timezone.now().date() - record_date).days
                if days_old <= 7: 
                    confidence = {'label': 'High', 'class': 'badge-success', 'score': 100}
                elif days_old <= 21:
                    confidence = {'label': 'Medium', 'class': 'badge-info', 'score': 70}
                else:
                    confidence = {'label': 'Low', 'class': 'badge-danger', 'score': 30}

                item_total = float(best_price_record['unit_price']) * qty
                split_trip['total'] += item_total
                split_trip['items'].append({
                    'product': prod.display_name or prod.name,
                    'store': best_price_record['store__name'],
                    'price': float(best_price_record['unit_price']),
                    'quantity': qty,
                    'total': item_total,
                    'date': best_price_record['date'],
                    'confidence': confidence
                })
        
        if best_single_total > 0:
            split_trip['savings'] = best_single_total - split_trip['total']
            # Only suggest split trip if savings are significant (> 5% of total)
            split_trip['is_worth_it'] = split_trip['savings'] > (best_single_total * 0.05)
        else:
            split_trip['is_worth_it'] = False

        return {
            'single_store_recommendation': valid_stores[0] if valid_stores else None,
            'alternatives': valid_stores[1:3],
            'split_trip_recommendation': split_trip if split_trip['items'] else None
        }

class ProductMatchingService:
    @staticmethod
    def find_best_match(name, category_name=None, brand=None, threshold=88.0):
        """
        Attempts to find a similar product using fuzzy semantic matching.
        Strategy: Filters by Category and Brand first, then fuzzy matches the Name.
        """
        # 1. Exact Name/GTIN match is already handled by ReceiptService before calling this.
        
        # 2. Scope the search to the same Brand and Category if possible
        candidates = Product.objects.all()
        if category_name and category_name != 'Geral':
            candidates = candidates.filter(category__name__iexact=category_name)
        if brand and brand != 'Generic':
            candidates = candidates.filter(brand__iexact=brand)
        
        # 3. Only look at products that have some history (real purchases)
        candidates = list(candidates.values('id', 'name', 'display_name'))
        if not candidates:
            return None

        # 4. Fuzzy Match logic using rapidfuzz
        # We compare against BOTH raw name and display name
        search_space = []
        for c in candidates:
            # Normalize to uppercase for accurate comparison
            search_space.append((c['id'], (c['display_name'] or c['name']).upper()))
            
        names_only = [s[1] for s in search_space]
        # token_set_ratio handles "Leite Integral" vs "Integral Leite"
        best = process.extractOne(name.upper(), names_only, scorer=fuzz.token_set_ratio)
        
        if best and best[1] >= threshold:
            match_name = best[0]
            match_score = best[1]
            match_id = [s[0] for s in search_space if s[1] == match_name][0]
            
            return Product.objects.get(id=match_id)
            
        return None
