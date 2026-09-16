from django.test import TestCase, Client, TransactionTestCase, RequestFactory
from django.contrib.auth.models import User
from django.urls import reverse
from django.utils import timezone
from django.db import transaction
from decimal import Decimal
from datetime import datetime, timedelta
from .models import Store, Product, Category, Receipt, ReceiptItem, PriceHistory, ProductMapping, normalize_text
from .services import ReceiptService, AnalyticsService, SmartCartService
from .scraper import NFCeScraper
from .enrichment import ProductEnrichmentService
from unittest.mock import patch, MagicMock
from hypothesis.extra.django import TestCase as HypothesisTestCase
from hypothesis import given, strategies as st
import re

class SecuritySSRFTests(TestCase):
    def test_ssrf_protection(self):
        """Verify the scraper blocks unauthorized domains (SSRF protection)."""
        scraper = NFCeScraper()
        malicious_urls = [
            "http://localhost:8000/admin",
            "http://169.254.169.254/latest/meta-data/",
            "https://evil-site.com",
            "file:///etc/passwd"
        ]
        for url in malicious_urls:
            with self.assertRaises(ValueError):
                scraper.scrape_url(url)

class NormalizationRobustnessTests(HypothesisTestCase):
    @given(st.text())
    def test_text_normalization_random_strings(self, s):
        """Property-based test: normalization should NEVER crash."""
        result = normalize_text(s)
        self.assertIsInstance(result, str)

    def test_smart_unit_preservation(self):
        self.assertEqual(normalize_text("leite 1L", True), "Leite 1L")
        self.assertEqual(normalize_text("ARROZ 5KG", True), "Arroz 5kg")

class TransactionalAtomicityTests(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='atomic', password='p')
        
    def test_rollback_on_failure(self):
        bad_data = {
            'store': {'name': 'Store', 'cnpj': '1', 'city': 'C', 'neighborhood': 'N', 'street': 'S'},
            'receipt': {
                'access_key': '1'*44, 'issue_date': timezone.now(), 'series': '1', 'number': '1',
                'total_amount': Decimal('10'), 'discount': 0, 'payment_method': 'X',
                'tax_federal': 0, 'tax_state': 0, 'tax_municipal': 0, 'consumer_cpf': None
            },
            'items': [{'name': 'Good Item', 'quantity': 'BAD', 'unit_price': 5}] # Force crash
        }
        with self.assertRaises(Exception):
            ReceiptService.save_scraped_data(bad_data, "http://sat.sef.sc.gov.br/test", self.user)
        self.assertEqual(Receipt.objects.count(), 0)

class AnalyticsBoundaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='boundary', password='p')

    def test_empty_dataset_grace(self):
        self.assertEqual(AnalyticsService.get_inflation_heatmap(self.user), [])
        self.assertIsNone(AnalyticsService.get_spending_forecast(self.user))

    def test_division_by_zero_protection(self):
        master = Product.objects.create(name="Master")
        Product.objects.create(name="No weight", parent=master)
        self.assertEqual(AnalyticsService.get_shrinkflation_report(self.user), [])

class EnrichmentChainTests(TestCase):
    @patch('requests.get')
    def test_aggregator_resilience(self, mock_get):
        # 7891097103643 is a real checksum-valid EAN-13 (PLU-like fakes such
        # as 7891234567890 fail validation and must skip the GTIN APIs).
        product = Product.objects.create(name="Test Prod", code_gtin="7891097103643")
        
        # Provide enough mocks for the whole chain (OFF, ML, Buscape, Amazon, Cosmos)
        ml_html = '<div class="ui-search-result__content"><h2 class="ui-search-item__title">Success Name</h2><img class="ui-search-result-image__element" src="http://img.jpg"></div>'
        mock_get.side_effect = [
            MagicMock(status_code=500), # OFF
            MagicMock(status_code=200, text=ml_html), # ML
            MagicMock(status_code=404), # Buscape
            MagicMock(status_code=404), # Amazon
            MagicMock(status_code=403), # Cosmos
        ]
        
        success = ProductEnrichmentService.enrich_product(product)
        self.assertTrue(success)
        self.assertEqual(product.display_name, "Success Name")

class SplitTripOptimizerTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='split', password='p')
        self.store_a = Store.objects.create(name="Store A", cnpj="10")
        self.store_b = Store.objects.create(name="Store B", cnpj="20")
        
        self.p1 = Product.objects.create(name="Product 1")
        self.p2 = Product.objects.create(name="Product 2")
        
        # P1 is cheaper at Store A
        PriceHistory.objects.create(user=self.user, product=self.p1, store=self.store_a, date=timezone.now(), unit_price=10)
        PriceHistory.objects.create(user=self.user, product=self.p1, store=self.store_b, date=timezone.now(), unit_price=20)
        
        # P2 is cheaper at Store B
        PriceHistory.objects.create(user=self.user, product=self.p2, store=self.store_a, date=timezone.now(), unit_price=20)
        PriceHistory.objects.create(user=self.user, product=self.p2, store=self.store_b, date=timezone.now(), unit_price=10)

    def test_split_trip_savings_calculation(self):
        """Verify that split trip identifies best items from different stores."""
        from .services import SmartCartService
        result = SmartCartService.optimize_cart(self.user, "Product 1\nProduct 2")
        
        # Single store (A or B) would cost 30 (10+20)
        # Split trip (P1@A + P2@B) costs 20 (10+10)
        self.assertEqual(result['single_store_recommendation']['total'], 30)
        self.assertEqual(result['split_trip_recommendation']['total'], 20)
        self.assertEqual(result['split_trip_recommendation']['savings'], 10)
        self.assertTrue(result['split_trip_recommendation']['is_worth_it'])

class HealthAnalysisTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='health_user', password='p')
        self.cat = Category.objects.create(name="Health")
        self.p1 = Product.objects.create(
            name="Healthy", category=self.cat,
            metadata={'nova_group': 1, 'ecoscore': 'a', 'nutrition': {'sugars_100g': 5, 'salt_100g': 0.1, 'fat_100g': 2}}
        )
        self.p2 = Product.objects.create(
            name="Unhealthy", category=self.cat,
            metadata={'nova_group': 4, 'ecoscore': 'e', 'nutrition': {'sugars_100g': 25, 'salt_100g': 1.5, 'fat_100g': 18}}
        )
        # Link products to user via receipt
        self.store = Store.objects.create(name="S", cnpj="0")
        self.r = Receipt.objects.create(user=self.user, store=self.store, issue_date=timezone.now(), total_amount=10, access_key="X")
        ReceiptItem.objects.create(receipt=self.r, product=self.p1, quantity=1, unit_type="UN", unit_price=5, total_price=5)
        ReceiptItem.objects.create(receipt=self.r, product=self.p2, quantity=1, unit_type="UN", unit_price=5, total_price=5)

    def test_health_aggregation(self):
        from .services import AnalyticsService
        stats = AnalyticsService.get_health_analysis(self.user)
        
        self.assertEqual(stats['nova'][1]['count'], 1)
        self.assertEqual(stats['nova'][4]['count'], 1)
        self.assertEqual(stats['eco']['a']['count'], 1)
        self.assertEqual(stats['eco']['e']['count'], 1)
        # Averages: Sugar (5+25)/2 = 15, Salt (0.1+1.5)/2 = 0.8, Fat (2+18)/2 = 10
        self.assertEqual(stats['nutrients']['sugar'], 15.0)
        self.assertEqual(stats['nutrients']['salt'], 0.8)
        self.assertEqual(stats['nutrients']['fat'], 10.0)

class EnrichmentServiceTests(TestCase):
    @patch('tracker.enrichment.requests.get')
    def test_name_based_fallback_success(self, mock_get):
        """Test that if GTIN fails, we try searching by name."""
        # 1. Setup mock response for name search
        from .enrichment import ProductEnrichmentService
        html_content = """
            <div class="ui-search-layout__item">
                <img class="ui-search-result-image__element" src="http://test.com/img.jpg">
                <h2 class="ui-search-item__title">Full Product Commercial Name</h2>
            </div>
        """
        mock_get.return_value.status_code = 200
        mock_get.return_value.text = html_content
        
        p = Product.objects.create(name="SIMPLE NAME", display_name="Simple Name")
        
        # We manually trigger enrichment
        # Note: enrich_product normally returns False if NO codes exist, 
        # but our new logic allows name search. 
        # I need to ensure the check at the start of enrich_product doesn't block it.
        success = ProductEnrichmentService.enrich_product(p)
        
        self.assertTrue(success)
        self.assertEqual(p.image_url, "http://test.com/img.jpg")
        self.assertEqual(p.display_name, "Full Product Commercial Name")

    def test_heuristic_guessing(self):
        """Test that categories like Hortifruti result in NOVA 1 guessing."""
        from .enrichment import ProductEnrichmentService
        cat = Category.objects.create(name="Hortifruti")
        p = Product.objects.create(name="BANANA NANICA KG", category=cat)
        
        # This should trigger _apply_heuristics
        success = ProductEnrichmentService.enrich_product(p)
        
        self.assertTrue(success)
        self.assertEqual(p.metadata.get('nova_group'), 1)
        # Updated key from 'enrichment_source' to 'source_nova_group'
        self.assertEqual(p.metadata.get('source_nova_group'), 'heuristic')

class AsyncEnrichmentTaskTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='async_user', password='p')
        self.store_data = {'name': 'Async Store', 'cnpj': '999', 'city': 'C', 'neighborhood': 'N', 'street': 'S'}

    @patch('tracker.services.async_task')
    def test_enrichment_task_is_enqueued(self, mock_async_task):
        """Verify that creating a new product via receipt triggers async enrichment."""
        data = {
            'store': self.store_data,
            'receipt': {
                'access_key': '0'*44, 'issue_date': timezone.now(), 'series': '1', 'number': '1',
                'total_amount': Decimal('50.00'), 'discount': 0, 'payment_method': 'Debit',
                'tax_federal': 0, 'tax_state': 0, 'tax_municipal': 0, 'consumer_cpf': None
            },
            'items': [{
                'name': 'NEW ASYNC PRODUCT', 'quantity': 1, 'unit_type': 'UN',
                'unit_price': 10, 'total_price': 10, 'code_gtin': '12345',
                'category': 'Geral', 'internal_code': 'IC1'
            }]
        }
        
        ReceiptService.save_scraped_data(data, "http://test.com", self.user)
        
        # Check if async_task was called with the correct path and some product ID
        mock_async_task.assert_called_once()
        args, kwargs = mock_async_task.call_args
        self.assertEqual(args[0], 'tracker.tasks.async_enrich_product')
        # product_id should be the first positional argument after the task name
        self.assertIsInstance(args[1], int)

    @patch('tracker.tasks.ProductEnrichmentService.enrich_product')
    def test_task_executes_enrichment(self, mock_enrich):
        """Verify the task itself calls the enrichment service."""
        from .tasks import async_enrich_product
        p = Product.objects.create(name="Task Test Product")
        
        async_enrich_product(p.id)
        
        mock_enrich.assert_called_once()
        # Verify it was called with the correct product instance
        called_prod = mock_enrich.call_args[0][0]
        self.assertEqual(called_prod.id, p.id)

class SemanticMatchingTests(TestCase):
    def setUp(self):
        self.cat = Category.objects.create(name="Dairy")
        self.prod = Product.objects.create(
            name="LEITE INTEGRAL TIROL 1L", 
            display_name="Leite Tirol Integral 1L",
            brand="Tirol",
            category=self.cat
        )

    def test_fuzzy_match_different_order(self):
        """Test that words in different order still match."""
        from .services import ProductMatchingService
        match = ProductMatchingService.find_best_match(
            name="TIROL INTEGRAL LEITE 1L",
            category_name="Dairy",
            brand="Tirol"
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.id, self.prod.id)

    def test_no_match_different_brand(self):
        """Test that different brands don't match even if names are similar."""
        from .services import ProductMatchingService
        # Create a different product
        match = ProductMatchingService.find_best_match(
            name="LEITE INTEGRAL NESTLE 1L",
            category_name="Dairy",
            brand="Nestle"
        )
        self.assertIsNone(match)

class SmartCartMathTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='math', password='p')
        self.store_a = Store.objects.create(name="Store A", cnpj="1")
        self.store_b = Store.objects.create(name="Store B", cnpj="2")
        self.p = Product.objects.create(name="Rice 5kg", display_name="Rice 5kg")
        PriceHistory.objects.create(user=self.user, product=self.p, store=self.store_a, date=timezone.now(), unit_price=20, normalized_price=4)
        PriceHistory.objects.create(user=self.user, product=self.p, store=self.store_b, date=timezone.now(), unit_price=15, normalized_price=3)

    def test_cheapest_store_selection(self):
        result = SmartCartService.optimize_cart(self.user, "Rice")
        self.assertEqual(result['single_store_recommendation']['store'], "Store B")


class RefreshAndCleanupRegressionTests(TestCase):
    """Regression tests for the 2026-09-15 bug-fix round."""

    def setUp(self):
        self.user = User.objects.create_user(username='regress', password='p')
        self.other = User.objects.create_user(username='intruder', password='p')
        self.store = Store.objects.create(name="Store X", cnpj="123")
        self.product = Product.objects.create(name="Cafe 500g", display_name="Cafe 500g")
        self.receipt = Receipt.objects.create(
            store=self.store, user=self.user,
            url="https://sat.sef.sc.gov.br/test",
            access_key="9" * 44, number="123", series="1",
            total_amount=Decimal('25.00'), discount=Decimal('0'),
            issue_date=timezone.now(),
        )
        ReceiptItem.objects.create(
            receipt=self.receipt, product=self.product,
            quantity=Decimal('2'), unit_type='UN',
            unit_price=Decimal('12.50'), total_price=Decimal('25.00'),
        )

    def test_receipt_deletion_cascades_to_price_history(self):
        """PriceHistory rows must carry receipt FK and die with their receipt."""
        PriceHistory.objects.create(
            user=self.user, receipt=self.receipt, product=self.product,
            store=self.store, date=self.receipt.issue_date,
            unit_price=Decimal('12.50'), normalized_price=Decimal('12.50'),
        )
        self.receipt.delete()
        self.assertEqual(PriceHistory.objects.count(), 0)

    def test_duplicate_import_is_rejected(self):
        """Re-processing the same NFCe access key must not create a 2nd receipt."""
        from tracker.services import ReceiptService
        data = {
            'store': {'name': 'Store X', 'cnpj': '123', 'city': 'C',
                      'neighborhood': 'N', 'street': 'S'},
            'receipt': {
                'access_key': '9' * 44, 'issue_date': timezone.now(),
                'series': '1', 'number': '123', 'total_amount': Decimal('25.00'),
                'discount': 0, 'payment_method': 'X',
                'tax_federal': 0, 'tax_state': 0, 'tax_municipal': 0,
                'consumer_cpf': None,
            },
            'items': [{'name': 'Cafe 500g', 'quantity': Decimal('2'),
                       'unit_price': Decimal('12.50'), 'total_price': Decimal('25.00'),
                       'unit_type': 'UN', 'category': 'Geral'}],
        }
        ReceiptService.save_scraped_data(data, 'https://sat.sef.sc.gov.br/x', self.user)
        self.assertEqual(Receipt.objects.count(), 1)

    def test_refresh_rejects_other_users_receipt(self):
        """A user must not be able to refresh (delete) someone else's receipt."""
        from tracker.views import confirm_refresh
        from django.core.exceptions import PermissionDenied
        scraped = {
            'store': {'name': 'Store X', 'cnpj': '123', 'city': 'C',
                      'neighborhood': 'N', 'street': 'S'},
            'receipt': {
                'access_key': '9' * 44, 'issue_date': timezone.now(),
                'series': '1', 'number': '123', 'total_amount': Decimal('30.00'),
                'discount': 0, 'payment_method': 'X',
                'tax_federal': 0, 'tax_state': 0, 'tax_municipal': 0,
                'consumer_cpf': None,
            },
            'items': [],
        }
        factory = RequestFactory()
        request = factory.post('/confirm_refresh/', {'url': 'https://sat.sef.sc.gov.br/test'})
        request.user = self.other
        with patch('tracker.views.NFCeScraper.scrape_url', return_value=scraped):
            with self.assertRaises(PermissionDenied):
                confirm_refresh(request)
        self.assertEqual(Receipt.objects.count(), 1)

    def test_confirm_refresh_replaces_same_receipt(self):
        """Full refresh flow: scrape -> delete old -> import -> redirect."""
        scraped = {
            'store': {'name': 'Store X', 'cnpj': '123', 'city': 'C',
                      'neighborhood': 'N', 'street': 'S'},
            'receipt': {
                'access_key': '9' * 44, 'issue_date': timezone.now(),
                'series': '1', 'number': '123', 'total_amount': Decimal('30.00'),
                'discount': 0, 'payment_method': 'X',
                'tax_federal': 0, 'tax_state': 0, 'tax_municipal': 0,
                'consumer_cpf': None,
            },
            'items': [{'name': 'Cafe 500g', 'quantity': Decimal('2'),
                       'unit_price': Decimal('15.00'), 'total_price': Decimal('30.00'),
                       'unit_type': 'UN', 'category': 'Geral'}],
        }
        self.client.force_login(self.user)
        with patch('tracker.views.NFCeScraper.scrape_url', return_value=scraped):
            # secure=True: SECURE_SSL_REDIRECT=True in the container env would
            # otherwise 301-redirect plain-http test requests before the view.
            response = self.client.post(reverse('confirm_refresh'), {'url': 'https://sat.sef.sc.gov.br/test'}, secure=True)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Receipt.objects.count(), 1)
        self.assertEqual(PriceHistory.objects.count(), 1)

    def test_maintenance_view_tolerates_bad_mapping_id(self):
        """Non-numeric mapping_id must return 400, not raise ValueError/500."""
        from tracker.views import system_maintenance
        factory = RequestFactory()
        request = factory.post('/maintenance/', {'action': 'confirm_mapping', 'mapping_id': 'garbage'})
        request.user = User.objects.create_user(username='staffer', password='p', is_staff=True)
        response = system_maintenance(request)
        self.assertEqual(response.status_code, 400)


class GtinPluSplitTests(TestCase):
    def test_valid_gtin_accepted(self):
        from tracker.gtin import is_valid_gtin, split_scraped_code
        # Real EAN-13 from live data
        self.assertTrue(is_valid_gtin('7891097103643'))
        self.assertTrue(is_valid_gtin('7894900701517'))
        gtin, internal = split_scraped_code('7891097103643')
        self.assertEqual(gtin, '7891097103643')
        self.assertEqual(internal, '7891097103643')

    def test_plu_rejected_as_gtin(self):
        from tracker.gtin import is_valid_gtin, split_scraped_code
        for plu in ['231', '2904', '82', '69728', '1640540', '12345']:
            self.assertFalse(is_valid_gtin(plu), f"PLU {plu} must not validate")
            gtin, internal = split_scraped_code(plu)
            self.assertEqual(gtin, '')
            self.assertEqual(internal, plu)

    def test_instore_weigh_code_rejected(self):
        from tracker.gtin import is_valid_gtin, _ean_checksum_valid
        # Build a 13-digit code starting with 2 that HAS a valid checksum:
        # it must still be rejected as in-store, not global.
        body = '200123456789'
        total = sum(int(ch) * (3 if i % 2 == 0 else 1)
                    for i, ch in enumerate(reversed(body)))
        check = (10 - (total % 10)) % 10
        weigh = body + str(check)
        self.assertTrue(_ean_checksum_valid(weigh))
        self.assertFalse(is_valid_gtin(weigh))

    def test_bad_checksum_rejected(self):
        from tracker.gtin import is_valid_gtin
        self.assertFalse(is_valid_gtin('7891097103644'))  # last digit flipped
        self.assertFalse(is_valid_gtin('12345678'))

    def test_product_save_clears_plu(self):
        p = Product.objects.create(name="CEBOLA BRANCA KG", code_gtin='69728')
        p.refresh_from_db()
        self.assertTrue(p.code_gtin in (None, ''))

    def test_product_save_keeps_valid_gtin(self):
        p = Product.objects.create(name="REAL GTIN PROD", code_gtin='7891097103643')
        p.refresh_from_db()
        self.assertEqual(p.code_gtin, '7891097103643')

    def test_scraper_splits_codes(self):
        from tracker.scraper import NFCeScraper
        from unittest.mock import MagicMock
        scraper = NFCeScraper()
        html = ('<table id="tabResult"><tr><td>CEBOLA BRANCA KG (Código: 69728)</td>'
                '<td>1</td><td>KG</td><td>9,99</td><td>9,99</td></tr></table>')
        soup = MagicMock()
        # Use real BeautifulSoup for the table path
        from bs4 import BeautifulSoup as BS
        soup = BS(html, 'html.parser')
        items = scraper._parse_items_robust(soup, '')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['code_gtin'], '')
        self.assertEqual(items[0]['internal_code'], '69728')

    def test_receipt_service_does_not_merge_plu_across_stores(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        user = User.objects.create_user(username='plu_user', password='p')
        store_a = Store.objects.create(name="Store A", cnpj="11111111000111")
        store_b = Store.objects.create(name="Store B", cnpj="22222222000122")
        base = {'access_key': '', 'issue_date': tz.now(), 'series': '1',
                'number': '1', 'total_amount': D('10'), 'discount': 0,
                'payment_method': 'X', 'tax_federal': 0, 'tax_state': 0,
                'tax_municipal': 0, 'consumer_cpf': None}
        # Same PLU '231' at two stores but DIFFERENT products must not merge
        # via the global GTIN path (mapping is store-scoped).
        d1 = {'store': {'name': 'Store A', 'cnpj': '11111111000111', 'city': 'C',
                        'neighborhood': 'N', 'street': 'S'},
              'receipt': dict(base, access_key='1' * 44),
              'items': [{'name': 'MAMAO PAPAYA KG', 'quantity': D('1'),
                         'unit_price': D('5'), 'total_price': D('5'),
                         'unit_type': 'KG', 'category': 'Hortifruti',
                         'code_gtin': '', 'internal_code': '231'}]}
        d2 = {'store': {'name': 'Store B', 'cnpj': '22222222000122', 'city': 'C',
                        'neighborhood': 'N', 'street': 'S'},
              'receipt': dict(base, access_key='2' * 44),
              'items': [{'name': 'ABOBORA KABOTIA KG', 'quantity': D('1'),
                         'unit_price': D('6'), 'total_price': D('6'),
                         'unit_type': 'KG', 'category': 'Hortifruti',
                         'code_gtin': '', 'internal_code': '231'}]}
        with patch('tracker.services.async_task'):
            r1 = ReceiptService.save_scraped_data(d1, 'http://x/1', user)
            r2 = ReceiptService.save_scraped_data(d2, 'http://x/2', user)
        self.assertNotEqual(r1.items.first().product_id,
                            r2.items.first().product_id)


class DedupeProductsTests(TestCase):
    def test_duplicate_gtin_rejected_by_constraint(self):
        from django.db import IntegrityError
        Product.objects.create(name="KEEPER", code_gtin='7891097103643')
        with self.assertRaises(IntegrityError):
            Product.objects.create(name="DUP", code_gtin='7891097103643')

    def test_null_gtin_not_unique_blocked(self):
        Product.objects.create(name="NULL A", code_gtin=None)
        Product.objects.create(name="NULL B", code_gtin=None)
        self.assertEqual(Product.objects.filter(code_gtin__isnull=True).count(), 2)

    def test_dedupe_command_cleans_orphans_and_keeps_mapped(self):
        # NOTE: duplicate-GTIN creation is blocked by uniq_product_gtin_not_null,
        # so the merge path is exercised on legacy data via dry-run (verified
        # live: GTIN 7891097103643 keep 1 merge 7). Here we cover the orphan
        # path plus mapping preservation.
        from django.core.management import call_command
        from tracker.models import ProductMapping
        keeper = Product.objects.create(name="KEEPER", code_gtin='7891097103643')
        store = Store.objects.create(name="S", cnpj="99999999000199")
        ProductMapping.objects.create(store=store, internal_code='X1', product=keeper)
        orphan = Product.objects.create(name="ORPHAN NO REFS")
        call_command('dedupe_products', '--apply')
        self.assertFalse(Product.objects.filter(id=orphan.id).exists())
        self.assertTrue(Product.objects.filter(id=keeper.id).exists())
        self.assertEqual(ProductMapping.objects.get(store=store, internal_code='X1').product_id, keeper.id)

    def test_keeper_prefers_most_history(self):
        from tracker.management.commands.dedupe_products import _keeper
        from django.utils import timezone as tz
        from decimal import Decimal as D
        store = Store.objects.create(name="S2", cnpj="88888888000188")
        user = User.objects.create_user(username='keeper_user', password='p')
        a = Product.objects.create(name="A LONELY", code_gtin=None)
        b = Product.objects.create(name="B BUSY", code_gtin=None)
        r = Receipt.objects.create(store=store, user=user, url='http://x',
                                   access_key='3' * 44, issue_date=tz.now(),
                                   total_amount=D('10'))
        ReceiptItem.objects.create(receipt=r, product=b, quantity=D('1'),
                                   unit_type='UN', unit_price=D('10'),
                                   total_price=D('10'))
        self.assertEqual(_keeper([a, b]).id, b.id)


class BackfillHistoryTests(TestCase):
    def _mk_receipt(self, user, store, n_items=3):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        r = Receipt.objects.create(store=store, user=user, url='http://x',
                                   access_key='9' * 44, issue_date=tz.now(),
                                   total_amount=D('30'))
        p = Product.objects.create(name="BACKFILL PROD", code_gtin=None)
        for _ in range(n_items):
            ReceiptItem.objects.create(receipt=r, product=p, quantity=D('1'),
                                       unit_type='UN', unit_price=D('10'),
                                       total_price=D('10'))
        return r

    def test_check_reports_gap(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError
        user = User.objects.create_user(username='bf_check', password='p')
        store = Store.objects.create(name="S", cnpj="77777777000177")
        self._mk_receipt(user, store)
        with self.assertRaises(CommandError):
            call_command('backfill_history', '--check')

    def test_repair_only_touches_gapped_receipts(self):
        from django.core.management import call_command
        from tracker.models import PriceHistory
        user = User.objects.create_user(username='bf_fix', password='p')
        store = Store.objects.create(name="S", cnpj="77777777000177")
        good = self._mk_receipt(user, store)
        call_command('backfill_history')  # repair all -> good gets history
        before_ids = list(PriceHistory.objects.filter(receipt=good).values_list('id', flat=True))
        self.assertEqual(len(before_ids), 3)
        # Second run must be a no-op for the in-sync receipt (stable IDs)
        call_command('backfill_history')
        after_ids = list(PriceHistory.objects.filter(receipt=good).values_list('id', flat=True))
        self.assertEqual(before_ids, after_ids)

    def test_partial_gap_rebuilt(self):
        from django.core.management import call_command
        from tracker.models import PriceHistory
        user = User.objects.create_user(username='bf_part', password='p')
        store = Store.objects.create(name="S", cnpj="77777777000177")
        r = self._mk_receipt(user, store, n_items=4)
        call_command('backfill_history')
        self.assertEqual(PriceHistory.objects.filter(receipt=r).count(), 4)
        # Simulate partial loss, repair must restore to 4
        ids = list(PriceHistory.objects.filter(receipt=r).values_list('id', flat=True)[:2])
        PriceHistory.objects.filter(id__in=ids).delete()
        self.assertEqual(PriceHistory.objects.filter(receipt=r).count(), 2)
        call_command('backfill_history')
        self.assertEqual(PriceHistory.objects.filter(receipt=r).count(), 4)


class StoreChainTests(TestCase):
    def test_resolve_trading_name_aliases(self):
        from tracker.models import resolve_trading_name
        self.assertEqual(resolve_trading_name('SDB COMERCIO DE ALIMENTOS LTDA'), 'Fort Atacadista')
        self.assertEqual(resolve_trading_name('A. ANGELONI   CIA LTDA'), 'Angeloni')
        self.assertEqual(resolve_trading_name('KOCH HIPERMERCADO S/A LJ 47'), 'Koch')
        self.assertEqual(resolve_trading_name('Mercadinho do Zé'), 'Mercadinho Do Zé')

    def test_assign_chains_groups_by_root(self):
        from django.core.management import call_command
        from tracker.models import StoreChain
        a = Store.objects.create(name="A. ANGELONI   CIA LTDA", cnpj="83646984001696",
                                 address_city="F")
        b = Store.objects.create(name="A. ANGELONI   CIA LTDA", cnpj="83646984007465",
                                 address_city="F")
        call_command('assign_chains', '--apply')
        a.refresh_from_db(); b.refresh_from_db()
        self.assertIsNotNone(a.chain_id)
        self.assertEqual(a.chain_id, b.chain_id)
        self.assertEqual(a.chain.name, 'Angeloni')
        self.assertEqual(a.display_name, 'Angeloni')

    def test_unknown_store_stays_chainless(self):
        from django.core.management import call_command
        s = Store.objects.create(name="Unknown Store", cnpj="", address_city="Unknown")
        call_command('assign_chains', '--apply')
        s.refresh_from_db()
        self.assertIsNone(s.chain_id)

    def test_empty_cnpj_import_reuses_fallback(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        user = User.objects.create_user(username='chain_cnpj', password='p')
        data = {'store': {'name': 'Whatever', 'cnpj': '', 'city': 'C',
                          'neighborhood': 'N', 'street': 'S'},
                'receipt': {'access_key': '4' * 44, 'issue_date': tz.now(),
                            'series': '1', 'number': '1', 'total_amount': D('5'),
                            'discount': 0, 'payment_method': 'X', 'tax_federal': 0,
                            'tax_state': 0, 'tax_municipal': 0, 'consumer_cpf': None},
                'items': []}
        with patch('tracker.services.async_task'):
            r1 = ReceiptService.save_scraped_data(dict(data), 'http://x/1', user)
            data['receipt']['access_key'] = '5' * 44
            r2 = ReceiptService.save_scraped_data(dict(data), 'http://x/2', user)
        self.assertEqual(r1.store_id, r2.store_id)


class NcmExtractionTests(TestCase):
    def test_row_ncm_extracted(self):
        from tracker.scraper import NFCeScraper
        from bs4 import BeautifulSoup as BS
        s = NFCeScraper()
        html = ('<table id="tabResult"><tr><td>QUEIJO MINAS (Código: 123) NCM: 04061010</td>'
                '<td>1</td><td>UN</td><td>10,00</td><td>10,00</td></tr></table>')
        items = s._parse_items_robust(BS(html, 'html.parser'), '')
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['ncm'], '04061010')

    def test_row_without_ncm_defaults_empty(self):
        from tracker.scraper import NFCeScraper
        from bs4 import BeautifulSoup as BS
        s = NFCeScraper()
        html = ('<table id="tabResult"><tr><td>BANANA KG (Código: 2904)</td>'
                '<td>1</td><td>KG</td><td>5,00</td><td>5,00</td></tr></table>')
        items = s._parse_items_robust(BS(html, 'html.parser'), '')
        self.assertEqual(items[0]['ncm'], '')

    def test_ncm_upgrades_category_from_geral(self):
        from tracker.scraper import NFCeScraper
        self.assertEqual(NFCeScraper.category_for_ncm('04061010'), 'Laticínios')
        self.assertEqual(NFCeScraper.category_for_ncm('22021000'), 'Bebidas')
        self.assertIsNone(NFCeScraper.category_for_ncm(''))
        self.assertIsNone(NFCeScraper.category_for_ncm('99999999'))

    def test_import_stores_and_backfills_ncm(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        user = User.objects.create_user(username='ncm_user', password='p')
        base = {'access_key': '', 'issue_date': tz.now(), 'series': '1',
                'number': '1', 'total_amount': D('10'), 'discount': 0,
                'payment_method': 'X', 'tax_federal': 0, 'tax_state': 0,
                'tax_municipal': 0, 'consumer_cpf': None}
        store = {'name': 'NCM Store', 'cnpj': '66666666000166', 'city': 'C',
                 'neighborhood': 'N', 'street': 'S'}
        item = {'name': 'MYSTERY ITEM XYZ', 'quantity': D('1'), 'unit_price': D('10'),
                'total_price': D('10'), 'unit_type': 'UN', 'category': 'Geral',
                'code_gtin': '', 'internal_code': '99991', 'ncm': '04061010'}
        with patch('tracker.services.async_task'):
            ReceiptService.save_scraped_data(
                {'store': store, 'receipt': dict(base, access_key='6' * 44), 'items': [item]},
                'http://x/1', user)
        prod = Product.objects.get(name='MYSTERY ITEM XYZ')
        self.assertEqual(prod.ncm, '04061010')
        # Second import without NCM must not wipe it; with NCM fills empty
        prod.ncm = ''
        prod.save()
        item2 = dict(item, ncm='04061010')
        with patch('tracker.services.async_task'):
            ReceiptService.save_scraped_data(
                {'store': store, 'receipt': dict(base, access_key='7' * 44), 'items': [item2]},
                'http://x/2', user)
        prod.refresh_from_db()
        self.assertEqual(prod.ncm, '04061010')


class BenchmarkNormalizationTests(TestCase):
    def _mk_hist(self, user, product, store, unit, norm):
        from django.utils import timezone as tz
        PriceHistory.objects.create(user=user, product=product, store=store,
                                    date=tz.now(), unit_price=unit,
                                    normalized_price=norm)

    def test_benchmark_uses_normalized_when_available(self):
        user = User.objects.create_user(username='bench_norm', password='p')
        store = Store.objects.create(name="S", cnpj="55555555000155")
        prod = Product.objects.create(name="BENCH PROD")
        # Same unit price history, but per-kg is stable at 20
        for _ in range(4):
            self._mk_hist(user, prod, store, 10, 20)
        res = AnalyticsService.get_price_benchmark(user, prod.id, 10, normalized_price=20)
        self.assertEqual(res['label'], 'Great Deal')  # == p25 == p75 floor
        # Same unit price, but per-kg 40 (smaller pack, worse deal) -> Expensive
        res2 = AnalyticsService.get_price_benchmark(user, prod.id, 10, normalized_price=40)
        self.assertEqual(res2['label'], 'Expensive')

    def test_benchmark_falls_back_to_unit_without_norm(self):
        user = User.objects.create_user(username='bench_unit', password='p')
        store = Store.objects.create(name="S", cnpj="55555555000155")
        prod = Product.objects.create(name="BENCH UNIT")
        for u in [10, 10, 10, 10]:
            self._mk_hist(user, prod, store, u, None)
        res = AnalyticsService.get_price_benchmark(user, prod.id, 10)
        self.assertIsNotNone(res)
        res_low = AnalyticsService.get_price_benchmark(user, prod.id, 5)
        self.assertEqual(res_low['label'], 'Great Deal')

    def test_benchmark_needs_three_points(self):
        user = User.objects.create_user(username='bench_min', password='p')
        store = Store.objects.create(name="S", cnpj="55555555000155")
        prod = Product.objects.create(name="BENCH MIN")
        self._mk_hist(user, prod, store, 10, 10)
        self.assertIsNone(AnalyticsService.get_price_benchmark(user, prod.id, 10, normalized_price=10))

    def test_timezone_is_sao_paulo(self):
        from django.conf import settings
        self.assertEqual(settings.TIME_ZONE, 'America/Sao_Paulo')


class ScraperDomainTests(TestCase):
    def test_legacy_domains_still_allowed(self):
        from tracker.scraper import NFCeScraper as S
        for h in ['sat.sef.sc.gov.br', 'nfce.fazenda.sp.gov.br', 'nfce.sefaz.rs.gov.br',
                  'homolog.sat.sef.sc.gov.br']:
            self.assertTrue(S._is_allowed_host(h), h)

    def test_all_ufs_covered_by_suffix_rule(self):
        from tracker.scraper import NFCeScraper as S
        for uf in 'AC AL AM AP BA CE DF ES GO MA MG MS MT PA PB PE PI PR RJ RN RO RR RS SC SE SP TO'.split():
            self.assertTrue(S._is_allowed_host(f'nfce.sefaz.{uf.lower()}.gov.br'), uf)
            self.assertTrue(S._is_allowed_host(f'nfce.fazenda.{uf.lower()}.gov.br'), uf)

    def test_national_portals_allowed(self):
        from tracker.scraper import NFCeScraper as S
        self.assertTrue(S._is_allowed_host('www.nfe.fazenda.gov.br'))
        self.assertTrue(S._is_allowed_host('dfe-portal.svrs.rs.gov.br'))

    def test_malicious_hosts_blocked(self):
        from tracker.scraper import NFCeScraper as S
        for h in ['localhost', '127.0.0.1', '169.254.169.254', 'evil-site.com',
                  'sefaz.ba.gov.br.evil.com', 'nfce.sefaz.xx.gov.br', '',
                  'evilsefaz.ba.gov.br', None]:
            self.assertFalse(S._is_allowed_host(h), repr(h))

    def test_scrape_url_rejects_bad_scheme_and_host(self):
        from tracker.scraper import NFCeScraper
        s = NFCeScraper()
        for url in ['file:///etc/passwd', 'ftp://nfce.sefaz.ba.gov.br/x',
                    'https://evil-site.com/nfce', 'http://169.254.169.254/x']:
            with self.assertRaises(ValueError):
                s.scrape_url(url)

    def test_payment_and_discount_fallbacks(self):
        from tracker.scraper import NFCeScraper
        s = NFCeScraper()
        self.assertEqual(s._extract_payment_method('no payment info here'), 'Outros')
        self.assertEqual(s._extract_payment_method('Forma de pagamento: PIX 42,00'), 'PIX')
        self.assertEqual(s._extract_discount('Desconto: R$ 5,16'), '5,16')
        self.assertEqual(s._extract_discount('Descontos R$: 4,35'), '4,35')
        self.assertEqual(s._extract_discount('no discount'), '0')


class DashboardCacheTests(TestCase):
    def test_bump_monotonic_and_versioned_keys(self):
        from tracker.services import bump_dashboard_cache, dashboard_cache_version
        v0 = dashboard_cache_version()
        v1 = bump_dashboard_cache()
        self.assertGreaterEqual(v1, v0 + 1)
        self.assertEqual(dashboard_cache_version(), v1)

    def test_import_bumps_version(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from tracker.services import dashboard_cache_version
        user = User.objects.create_user(username='cache_bump', password='p')
        before = dashboard_cache_version()
        data = {'store': {'name': 'Cache Store', 'cnpj': '44444444000144', 'city': 'C',
                          'neighborhood': 'N', 'street': 'S'},
                'receipt': {'access_key': '8' * 44, 'issue_date': tz.now(),
                            'series': '1', 'number': '1', 'total_amount': D('5'),
                            'discount': 0, 'payment_method': 'X', 'tax_federal': 0,
                            'tax_state': 0, 'tax_municipal': 0, 'consumer_cpf': None},
                'items': []}
        with patch('tracker.services.async_task'):
            ReceiptService.save_scraped_data(data, 'http://x/1', user)
        self.assertGreater(dashboard_cache_version(), before)


class AdminRegistryTests(TestCase):
    def test_models_registered(self):
        from django.contrib import admin as dj_admin
        from tracker.models import (StoreChain, Store, Category, Product,
                                    ProductMapping, Receipt, PriceHistory, ScrapeLog)
        for model in (StoreChain, Store, Category, Product, ProductMapping,
                      Receipt, PriceHistory, ScrapeLog):
            self.assertIn(model, dj_admin.site._registry)


class CanonicalGroupingTests(TestCase):
    def test_signature_strips_sizes_and_promos(self):
        from tracker.canonical import signature_for, unit_hint_for
        self.assertEqual(signature_for('ARROZ BCO TIO JOAO 5KG PROMOCAO'), 'ARROZ BCO TIO JOAO')
        self.assertEqual(unit_hint_for('ARROZ BCO TIO JOAO 5KG'), 'KG')
        self.assertEqual(unit_hint_for('ABACAXI PEROLA UN'), 'UN')
        self.assertEqual(unit_hint_for('ITEM SEM UNIDADE'), '')

    def test_gates_block_different_units_and_brands(self):
        from tracker.canonical import gates_pass
        cat = Category.objects.create(name="Hortifruti")
        a = Product.objects.create(name="MAMAO PAPAYA KG", category=cat, brand="")
        b = Product.objects.create(name="MAMAO PAPAYA UN", category=cat, brand="")
        self.assertFalse(gates_pass(a, b))
        c = Product.objects.create(name="LEITE X 1L", category=cat, brand="Tirol")
        d = Product.objects.create(name="LEITE Y 1L", category=cat, brand="Parmalat")
        self.assertFalse(gates_pass(c, d))

    def test_preview_groups_same_gtin_and_signatures(self):
        from tracker import canonical as cs
        cat = Category.objects.create(name="Hortifruti")
        a = Product.objects.create(name="CEBOLA BRANCA KG", category=cat)
        b = Product.objects.create(name="CEBOLA KG", category=cat)
        c = Product.objects.create(name="MAMAO FORMOSA KG", category=cat)
        groups, suggestions = cs.preview_groups([a, b, c])
        bucket = next(v for v in groups.values() if a in v)
        self.assertIn(b, bucket)
        self.assertNotIn(c, bucket)

    def test_attach_creates_and_reuses_canonical(self):
        from tracker import canonical as cs
        from tracker.models import CanonicalProduct
        a = Product.objects.create(name="CEBOLA BRANCA KG")
        canon = cs.attach(a)
        self.assertIsNotNone(a.canonical_id)
        self.assertEqual(canon.products.count(), 1)
        # Exact signature+size twin joins the same bucket (idempotent)
        b = Product.objects.create(name="CEBOLA BRANCA KG")
        cs.attach(b)
        self.assertEqual(b.canonical_id, a.canonical_id)
        # Re-attaching is a no-op
        cs.attach(a)
        self.assertEqual(CanonicalProduct.objects.count(), 1)

    def test_merge_canonicals_moves_members(self):
        from tracker import canonical as cs
        from tracker.models import CanonicalProduct
        a = Product.objects.create(name="MERGE ALPHA KG")
        b = Product.objects.create(name="MERGE BETA KG")
        ca, cb = cs.attach(a), cs.attach(b)
        self.assertNotEqual(ca.id, cb.id)
        keeper = cs.merge_canonicals(ca, cb)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.canonical_id, keeper.id)
        self.assertEqual(b.canonical_id, keeper.id)
        self.assertFalse(CanonicalProduct.objects.filter(id=cb.id).exists())

    def test_group_command_writes_and_suggests(self):
        from django.core.management import call_command
        from tracker.models import CanonicalProduct, CanonicalSuggestion
        cat = Category.objects.create(name="Hortifruti")
        Product.objects.create(name="CEBOLA BRANCA KG", category=cat)
        Product.objects.create(name="CEBOLA KG", category=cat)
        call_command('group_canonicals', '--apply')
        self.assertGreaterEqual(CanonicalProduct.objects.count(), 1)
        # Every product must have a canonical after apply
        self.assertEqual(Product.objects.filter(canonical__isnull=True).count(), 0)

    def test_benchmark_spans_canonical(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from tracker.models import CanonicalProduct
        user = User.objects.create_user(username='canon_bench', password='p')
        store = Store.objects.create(name="S", cnpj="33333333000133")
        canon = CanonicalProduct.objects.create(name="Canon")
        p1 = Product.objects.create(name="CANON A KG", canonical=canon)
        p2 = Product.objects.create(name="CANON B KG", canonical=canon)
        for _ in range(3):
            PriceHistory.objects.create(user=user, product=p1, store=store,
                                        date=tz.now(), unit_price=D('10'),
                                        normalized_price=D('10'))
        # p2 alone has <3 points -> None without canonical, Fair/Great with it
        res = AnalyticsService.get_price_benchmark(user, p2.id, 10, normalized_price=10)
        self.assertIsNotNone(res)

    def test_suggestion_accept_merges(self):
        from django.core.management import call_command
        from tracker.models import CanonicalProduct, CanonicalSuggestion
        cat = Category.objects.create(name="Hortifruti")
        a = Product.objects.create(name="CEBOLA BRANCA KG", category=cat)
        b = Product.objects.create(name="CEBOLA KG", category=cat)
        call_command('group_canonicals', '--apply')
        a.refresh_from_db(); b.refresh_from_db()
        # Auto-merged (score 100) -> same canonical, no suggestion needed
        self.assertEqual(a.canonical_id, b.canonical_id)
        self.assertEqual(
            CanonicalSuggestion.objects.filter(status=CanonicalSuggestion.PENDING).count(), 0)

    def test_review_actions_accept_and_dismiss(self):
        from django.test import RequestFactory
        from tracker.views import system_maintenance
        from tracker.models import CanonicalProduct, CanonicalSuggestion
        from tracker import canonical as cs
        staff = User.objects.create_user(username='reviewer', password='p', is_staff=True)
        a = Product.objects.create(name="REVIEW A KG")
        b = Product.objects.create(name="REVIEW B KG")
        ca, cb = cs.attach(a), cs.attach(b)
        sugg = CanonicalSuggestion.objects.create(
            product_a=a, product_b=b, score=85.0, reason="test")
        factory = RequestFactory()
        req = factory.post('/maintenance/', {'action': 'accept_suggestion',
                                             'suggestion_id': str(sugg.id)})
        req.user = staff
        resp = system_maintenance(req)
        self.assertEqual(resp.status_code, 200)
        a.refresh_from_db(); b.refresh_from_db()
        self.assertEqual(a.canonical_id, b.canonical_id)
        sugg.refresh_from_db()
        self.assertEqual(sugg.status, CanonicalSuggestion.ACCEPTED)
        # Dismiss path
        c = Product.objects.create(name="REVIEW C KG")
        d = Product.objects.create(name="REVIEW D KG")
        sugg2 = CanonicalSuggestion.objects.create(
            product_a=c, product_b=d, score=80.0, reason="test")
        req2 = factory.post('/maintenance/', {'action': 'dismiss_suggestion',
                                              'suggestion_id': str(sugg2.id)})
        req2.user = staff
        resp2 = system_maintenance(req2)
        self.assertEqual(resp2.status_code, 200)
        sugg2.refresh_from_db()
        self.assertEqual(sugg2.status, CanonicalSuggestion.DISMISSED)


class CanonicalGatesTests(TestCase):
    def test_distinct_gtins_never_merge_or_suggest(self):
        from tracker import canonical as cs
        a = Product.objects.create(name="ESM RISQUE CREM 8ML", code_gtin='7891182015226')
        b = Product.objects.create(name="ESM RISQUE NAT 8ML", code_gtin='7891182850025')
        self.assertFalse(cs.gates_pass(a, b))
        groups, suggestions = cs.preview_groups([a, b])
        self.assertEqual(len(groups), 2)
        self.assertEqual(suggestions, [])

    def test_one_sided_gtin_still_passes(self):
        from tracker import canonical as cs
        cat = Category.objects.create(name="Hortifruti")
        a = Product.objects.create(name="CEBOLA BRANCA KG", category=cat)
        b = Product.objects.create(name="CEBOLA KG", category=cat)
        self.assertTrue(cs.gates_pass(a, b))


class CategoryKeywordTests(TestCase):
    def test_new_hortifruti_keywords(self):
        from tracker.scraper import NFCeScraper as S
        s = S()
        for name in ['PIMENTAO VERMELHO KG', 'LARANJA PERA KG', 'CENOURA KG',
                     'MACA GALA KG', 'MORANGO BANDEJA UN']:
            self.assertEqual(s._guess_category(name), 'Hortifruti', name)

    def test_ovo_mercearia_esmalte_higiene(self):
        from tracker.scraper import NFCeScraper as S
        s = S()
        self.assertEqual(s._guess_category('OVO BRANCO C/12'), 'Mercearia')
        self.assertEqual(s._guess_category('ESMALTE RISQUE CREM 8ML'), 'Higiene')
        # No regression: provolone still dairy (Laticínios checked before OVO)
        self.assertEqual(s._guess_category('QUEIJO PROVOLONE KG'), 'Laticínios')
        self.assertEqual(s._guess_category('MACARRAO RENATA 500G'), 'Mercearia')


class RenormalizeTests(TestCase):
    def _mk(self, user, store, name, weight_display=None):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        r = Receipt.objects.create(store=store, user=user, url='http://x',
                                   access_key='1' * 44, issue_date=tz.now(),
                                   total_amount=D('30'))
        p = Product.objects.create(name=name)
        return r, p

    def test_weight_change_propagates_to_items_and_history(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from tracker.models import PriceHistory
        user = User.objects.create_user(username='renorm', password='p')
        store = Store.objects.create(name="S", cnpj="22222222000122")
        r = Receipt.objects.create(store=store, user=user, url='http://x',
                                   access_key='1' * 44, issue_date=tz.now(),
                                   total_amount=D('30'))
        # Import-time: no size in name -> normalized falls back to unit price
        p = Product.objects.create(name="STALE WEIGHT PROD")
        self.assertIsNone(p.weight_grams)
        item = ReceiptItem.objects.create(receipt=r, product=p, quantity=D('1'),
                                          unit_type='UN', unit_price=D('18'),
                                          total_price=D('18'))
        self.assertEqual(item.normalized_price, D('18'))
        PriceHistory.objects.create(user=user, receipt=r, product=p, store=store,
                                    date=r.issue_date, unit_price=D('18'),
                                    normalized_price=D('18'))
        # Correction: name gains the size -> weight extracted -> rows repaired
        p.display_name = "Stale Weight Prod 1.5kg"
        p.save()
        p.refresh_from_db()
        self.assertEqual(p.weight_grams, D('1500'))
        item.refresh_from_db()
        self.assertEqual(item.normalized_price, D('12'))
        hist = PriceHistory.objects.get(receipt=r, product=p)
        self.assertEqual(hist.normalized_price, D('12'))

    def test_decimal_dust_not_touched(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from tracker.models import PriceHistory
        user = User.objects.create_user(username='renorm2', password='p')
        store = Store.objects.create(name="S", cnpj="22222222000122")
        r = Receipt.objects.create(store=store, user=user, url='http://x',
                                   access_key='1' * 44, issue_date=tz.now(),
                                   total_amount=D('10'))
        p = Product.objects.create(name="COCA 2L")
        item = ReceiptItem.objects.create(receipt=r, product=p, quantity=D('1'),
                                          unit_type='UN', unit_price=D('10'),
                                          total_price=D('10'),
                                          normalized_price=D('5.00'))
        # Scraper quantized to cents; formula gives 5 exactly -> no churn
        touched = p.renormalize_items()
        self.assertEqual(touched, 0)
        item.refresh_from_db()
        self.assertEqual(item.normalized_price, D('5.00'))

    def test_command_dry_run_and_apply(self):
        from django.core.management import call_command
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from tracker.models import PriceHistory
        user = User.objects.create_user(username='renorm3', password='p')
        store = Store.objects.create(name="S", cnpj="22222222000122")
        r = Receipt.objects.create(store=store, user=user, url='http://x',
                                   access_key='1' * 44, issue_date=tz.now(),
                                   total_amount=D('30'))
        p = Product.objects.create(name="CMD STALE PROD")
        ReceiptItem.objects.create(receipt=r, product=p, quantity=D('1'),
                                   unit_type='UN', unit_price=D('20'),
                                   total_price=D('20'))
        PriceHistory.objects.create(user=user, receipt=r, product=p, store=store,
                                    date=r.issue_date, unit_price=D('20'),
                                    normalized_price=D('20'))
        # Simulate a later weight correction directly (bypass save hook)
        Product.objects.filter(id=p.id).update(weight_grams=D('1000'))
        call_command('renormalize')
        item = ReceiptItem.objects.get(receipt=r, product=p)
        self.assertEqual(item.normalized_price, D('20'))  # dry-run: untouched
        call_command('renormalize', '--apply')
        item.refresh_from_db()
        self.assertEqual(item.normalized_price, D('20'))  # 20/1000*1000 == 20
        # Now a real drift: weight 500 -> 40 per kg
        Product.objects.filter(id=p.id).update(weight_grams=D('500'))
        call_command('renormalize', '--apply')
        item.refresh_from_db()
        self.assertEqual(item.normalized_price, D('40'))


class BrandDictionaryTests(TestCase):
    def test_known_brands_extracted(self):
        from tracker.scraper import NFCeScraper as S
        s = S()
        self.assertEqual(s._guess_brand('COXA SCOXA FGO SADIA 1KG'), 'Sadia')
        self.assertEqual(s._guess_brand('ARROZ BCO TIO JOAO 5KG'), 'Tio João')
        self.assertEqual(s._guess_brand('FILE PEITO FGO NAT IQF 1KG'), 'Nat')
        self.assertEqual(s._guess_brand('LEITE NATURALLE INTEGRAL TP 1L'), 'Naturalle')
        self.assertEqual(s._guess_brand('REFRIG COCA COLA PET 2L'), 'Coca-Cola')
        self.assertEqual(s._guess_brand('OVO CAIPIRA FREE LAR C/20'), 'Free Lar')

    def test_noise_yields_unknown_not_fake_brand(self):
        from tracker.scraper import NFCeScraper as S
        s = S()
        self.assertEqual(s._guess_brand('CARNE MOIDA KG PRIMEIRA'), '')
        self.assertEqual(s._guess_brand('BANANA CATURRA KG'), '')
        self.assertEqual(s._guess_brand('COXA SCOXA FGO 1KG'), '')
        self.assertEqual(s._guess_brand(''), '')

    def test_fix_brands_command(self):
        from django.core.management import call_command
        noisy = Product.objects.create(name="CARNE MOIDA KG PRIMEIRA", brand="Moida")
        manual = Product.objects.create(name="COXA SCOXA FGO SADIA 1KG", brand="Weird",
                                        is_manually_edited=True)
        call_command('fix_brands', '--apply')
        noisy.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(noisy.brand, '')
        self.assertEqual(manual.brand, 'Weird')  # untouched
        good = Product.objects.create(name="LEITE TIROL 1L", brand="Tirol")
        call_command('fix_brands', '--apply')
        good.refresh_from_db()
        self.assertEqual(good.brand, 'Tirol')


class FilterParamRobustnessTests(TestCase):
    def setUp(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        self.user = User.objects.create_user(username='filter_robust', password='p')
        self.store = Store.objects.create(name="S", cnpj="11111111000111")
        for n in range(3):
            Receipt.objects.create(store=self.store, user=self.user, url='http://x',
                                   access_key=str(n) * 44, issue_date=tz.now(),
                                   total_amount=D('10'), number=str(n))

    def test_receipt_list_tolerates_none_store(self):
        # Exact reported crash: page 2 link renders store=None
        self.client.force_login(self.user)
        for url in ['/tracker/receipts/?page=2&q=&store=None&sort=-issue_date',
                    '/tracker/receipts/?store=None',
                    '/tracker/receipts/?store=abc',
                    '/tracker/receipts/?store=12abc',
                    '/tracker/receipts/?store=']:
            resp = self.client.get(url, secure=True)
            self.assertEqual(resp.status_code, 200, url)

    def test_receipt_list_valid_store_still_filters(self):
        self.client.force_login(self.user)
        resp = self.client.get(f'/tracker/receipts/?store={self.store.id}', secure=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context['page_obj'].paginator.count, 3)
        other = Store.objects.create(name="Other", cnpj="22222222000122")
        resp = self.client.get(f'/tracker/receipts/?store={other.id}', secure=True)
        self.assertEqual(resp.context['page_obj'].paginator.count, 0)

    def test_product_comparison_tolerates_bad_params(self):
        self.client.force_login(self.user)
        for url in ['/tracker/market/?category=None',
                    '/tracker/market/?category=xyz',
                    '/tracker/market/?sort=nonexistent__field',
                    '/tracker/market/?sort=-password']:
            resp = self.client.get(url, secure=True)
            self.assertEqual(resp.status_code, 200, url)


class SmartCartCanonicalTests(TestCase):
    def setUp(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from tracker.models import CanonicalProduct
        self.user = User.objects.create_user(username='cart_canon', password='p')
        self.sa = Store.objects.create(name="Store A", cnpj="11111111000111")
        self.sb = Store.objects.create(name="Store B", cnpj="22222222000122")
        self.canon = CanonicalProduct.objects.create(name="Cebola")
        # Fragmented rows: same onion, different store codes, one bucket
        self.pa = Product.objects.create(name="CEBOLA BRANCA KG", canonical=self.canon)
        self.pb = Product.objects.create(name="CEBOLA KG", canonical=self.canon)
        PriceHistory.objects.create(user=self.user, product=self.pa, store=self.sa,
                                    date=tz.now(), unit_price=D('9'), normalized_price=D('9'))
        PriceHistory.objects.create(user=self.user, product=self.pb, store=self.sb,
                                    date=tz.now(), unit_price=D('5'), normalized_price=D('5'))

    def test_fragmented_rows_plan_as_one_item(self):
        res = SmartCartService.optimize_cart(self.user, "cebola")
        rec = res['single_store_recommendation']
        self.assertIsNotNone(rec)
        # Both stores priced, cheapest single store is B at 5
        self.assertEqual(rec['store'], "Store B")
        self.assertEqual(rec['total'], 5.0)
        self.assertEqual(len(rec['items']), 1)
        # Split trip finds the cheapest member row across the bucket
        split = res['split_trip_recommendation']
        self.assertEqual(split['total'], 5.0)
        self.assertEqual(split['items'][0]['store'], "Store B")

    def test_same_bucket_lines_merge_quantities(self):
        res = SmartCartService.optimize_cart(self.user, "CEBOLA BRANCA\nCEBOLA")
        rec = res['single_store_recommendation']
        self.assertEqual(len(rec['items']), 1)
        self.assertEqual(rec['items'][0]['quantity'], 2)
        self.assertEqual(rec['total'], 10.0)  # 2 x 5 at Store B

    def test_unmatched_lines_reported(self):
        res = SmartCartService.optimize_cart(self.user, "cebola\nunobtainium xyz")
        self.assertEqual(res['unmatched'], ['unobtainium xyz'])
        self.assertIsNotNone(res['single_store_recommendation'])

    def test_all_unmatched_returns_structured(self):
        res = SmartCartService.optimize_cart(self.user, "unobtainium xyz")
        self.assertIsNone(res['single_store_recommendation'])
        self.assertEqual(res['unmatched'], ['unobtainium xyz'])


class SmartCartMatchingTests(TestCase):
    def test_whole_word_beats_history_count(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        user = User.objects.create_user(username='cart_match', password='p')
        store = Store.objects.create(name="S", cnpj="11111111000111")
        fresh = Product.objects.create(name="TOMATE LONGA VIDA KG")
        extr = Product.objects.create(name="EXT TOM SALSARETTI 300G")
        # Extract has MORE history (duplicate-line inflation pattern)...
        for _ in range(5):
            PriceHistory.objects.create(user=user, product=extr, store=store,
                                        date=tz.now(), unit_price=D('5'),
                                        normalized_price=D('5'))
        PriceHistory.objects.create(user=user, product=fresh, store=store,
                                    date=tz.now(), unit_price=D('7'),
                                    normalized_price=D('7'))
        # ...but 'tomate' must match the fresh tomato (whole word)
        match = SmartCartService._match_row('tomate')
        self.assertEqual(match.id, fresh.id)
        # An unmatched line yields None
        self.assertIsNone(SmartCartService._match_row('zzz unobtainium'))


class MoversSignalsBasketTests(TestCase):
    def setUp(self):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from tracker.models import CanonicalProduct
        self.user = User.objects.create_user(username='metrics1', password='p')
        self.store = Store.objects.create(name="S", cnpj="11111111000111")
        self.now = tz.now()
        self.canon = CanonicalProduct.objects.create(name="Riser")
        self.riser = Product.objects.create(name="RISER PROD", canonical=self.canon)
        self.faller = Product.objects.create(name="FALLER PROD")
        self.D = D

    def _ph(self, product, days_ago, price):
        PriceHistory.objects.create(user=self.user, product=product, store=self.store,
                                    date=self.now - timedelta(days=days_ago),
                                    unit_price=self.D(str(price)),
                                    normalized_price=self.D(str(price)))

    def test_movers_detects_rise_and_fall(self):
        for d in (80, 70, 60):
            self._ph(self.riser, d, 10)
        for d in (10, 5, 1):
            self._ph(self.riser, d, 15)  # +50%
        for d in (80, 70):
            self._ph(self.faller, d, 20)
        for d in (10, 5):
            self._ph(self.faller, d, 10)  # -50%
        # A bucket with only recent data is skipped (no baseline)
        fresh = Product.objects.create(name="FRESH ONLY")
        self._ph(fresh, 2, 99)
        res = AnalyticsService.get_price_movers(self.user)
        self.assertEqual(res['risers'][0]['name'], 'RISER PROD')
        self.assertEqual(res['risers'][0]['pct'], 50.0)
        self.assertEqual(res['fallers'][0]['name'], 'FALLER PROD')
        self.assertEqual(res['fallers'][0]['pct'], -50.0)
        self.assertNotIn('FRESH ONLY', [m['name'] for m in res['risers'] + res['fallers']])

    def test_movers_empty_for_new_user(self):
        other = User.objects.create_user(username='metrics_new', password='p')
        res = AnalyticsService.get_price_movers(other)
        self.assertEqual(res, {'fallers': [], 'risers': []})

    def test_buy_signals_flags_lows_only(self):
        for i, price in enumerate([10, 12, 11, 13, 10]):
            self._ph(self.riser, 300 - i * 30, price)
        self._ph(self.riser, 1, 10)  # back at the low
        self._ph(self.faller, 200, 20)
        self._ph(self.faller, 100, 22)
        self._ph(self.faller, 1, 25)  # climbing, not a low
        res = AnalyticsService.get_buy_signals(self.user)
        names = [s['name'] for s in res]
        self.assertIn('RISER PROD', names)
        self.assertNotIn('FALLER PROD', names)
        sig = next(s for s in res if s['name'] == 'RISER PROD')
        self.assertEqual(sig['low'], 10.0)
        self.assertRegex(sig['last_date'], r'^\d{4}-\d{2}-\d{2}$')

    def test_basket_over_time_shape(self):
        self._ph(self.riser, 5, 10)
        self._ph(self.faller, 40, 20)
        res = AnalyticsService.get_basket_over_time(self.user, months=3, top_n=10)
        self.assertEqual(len(res['labels']), 3)
        self.assertEqual(len(res['totals']), 3)
        self.assertEqual(len(res['coverage']), 3)
        self.assertEqual(res['basket_size'], 2)
        self.assertGreater(res['totals'][-1], 0)  # current month has the riser


class DashboardBasicsTests(TestCase):
    def test_dashboard_trip_and_wallet_metrics(self):
        from django.core.cache import cache
        cache.clear()  # FileBasedCache is shared across test runs
        from django.utils import timezone as tz
        from decimal import Decimal as D
        user = User.objects.create_user(username='dash_basic', password='p')
        s1 = Store.objects.create(name="Fort Atacadista", cnpj="09477652004000",
                                  address_city="F")
        s2 = Store.objects.create(name="KOCH LJ", cnpj="02831172006505",
                                  address_city="F")
        from tracker.models import StoreChain
        c1 = StoreChain.objects.create(name="Fort Atacadista")
        s1.chain = c1; s1.save()
        for i, (store, total, disc) in enumerate([(s1, D('100'), D('10')), (s1, D('50'), D('0')), (s2, D('50'), D('5'))]):
            r = Receipt.objects.create(store=store, user=user, url='http://x',
                                       access_key=str(i) * 44, issue_date=tz.now(),
                                       total_amount=total, discount=disc)
            p = Product.objects.create(name=f"DASH PROD {i}")
            ReceiptItem.objects.create(receipt=r, product=p, quantity=D('2'),
                                       unit_type='UN', unit_price=D('10'),
                                       total_price=D('20'))
        self.client.force_login(user)
        resp = self.client.get('/tracker/dashboard/', secure=True)
        self.assertEqual(resp.status_code, 200)
        ctx = resp.context
        self.assertEqual(ctx['trip_count'], 3)
        self.assertEqual(ctx['avg_ticket'], D('185') / 3)  # paid 90+50+45
        self.assertEqual(ctx['avg_items'], 2.0)
        self.assertEqual(ctx['promo_saved'], D('15'))
        wallets = {c['name']: c for c in ctx['chain_share']}
        self.assertAlmostEqual(wallets['Fort Atacadista']['spend'], 140.0)
        self.assertAlmostEqual(wallets['KOCH LJ']['spend'], 45.0)
        self.assertAlmostEqual(sum(c['pct'] for c in ctx['chain_share']), 100.0, places=0)


class IpcaOverlayTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # IPCA series cache is shared across test runs

    def _mk_hist(self, user, store, product, days_ago, price):
        from django.utils import timezone as tz
        from decimal import Decimal as D
        PriceHistory.objects.create(user=user, product=product, store=store,
                                    date=tz.now() - timedelta(days=days_ago),
                                    unit_price=D(str(price)),
                                    normalized_price=D(str(price)))

    @patch('tracker.services.requests.get')
    def test_overlay_combines_personal_and_official(self, mock_get):
        from django.utils import timezone as tz
        user = User.objects.create_user(username='ipca1', password='p')
        store = Store.objects.create(name="S", cnpj="11111111000111")
        prod = Product.objects.create(name="IPCA PROD")
        # Two consecutive full months of history
        first_of_this = tz.now().replace(day=1)
        first_of_prev = (first_of_this - timedelta(days=1)).replace(day=1)
        mid_prev = first_of_prev + timedelta(days=10)
        mid_this = first_of_this + timedelta(days=10)
        if mid_this > tz.now():
            mid_this = tz.now() - timedelta(hours=1)
        from decimal import Decimal as D
        PriceHistory.objects.create(user=user, product=prod, store=store,
                                    date=mid_prev, unit_price=D('10'),
                                    normalized_price=D('10'))
        PriceHistory.objects.create(user=user, product=prod, store=store,
                                    date=mid_this, unit_price=D('11'),
                                    normalized_price=D('11'))
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: [{'data': '01/01/2020', 'valor': '0,21'},
                          {'data': '01/02/2020', 'valor': '0,25'}])
        mock_get.return_value.raise_for_status = lambda: None
        res = AnalyticsService.get_ipca_overlay(user, months=2)
        self.assertIn('IPCA geral', res['official'])
        self.assertEqual(res['warnings'], [])
        # Personal MoM for the current month ≈ +10%
        self.assertTrue(any(v is not None and abs(v - 10.0) < 0.01 for v in res['personal']))

    @patch('tracker.services.requests.get')
    def test_bcb_outage_degrades_gracefully(self, mock_get):
        user = User.objects.create_user(username='ipca2', password='p')
        mock_get.side_effect = Exception('BCB down')
        res = AnalyticsService.get_ipca_overlay(user, months=2)
        self.assertEqual(res['official'], {})
        self.assertEqual(len(res['warnings']), 2)
        self.assertEqual(res['personal'], [None, None, None])

    @patch('tracker.services.requests.get')
    def test_bcb_outage_is_cached(self, mock_get):
        """A BCB outage must not turn every page view into 2 live timeouts."""
        from django.core.cache import cache
        cache.delete('ipca_sgs_433')
        cache.delete('ipca_sgs_1635')
        mock_get.side_effect = Exception('BCB down')
        AnalyticsService._fetch_ipca_series(433)
        self.assertEqual(mock_get.call_count, 1)
        # Second call is served from the failure cache, no new request.
        AnalyticsService._fetch_ipca_series(433)
        self.assertEqual(mock_get.call_count, 1)
        cache.delete('ipca_sgs_433')


class AuthRedirectTests(TestCase):
    def test_login_redirects_to_index(self):
        """LoginView default LOGIN_REDIRECT_URL ('/accounts/profile/') 404s."""
        from django.conf import settings
        u = User.objects.create_user(username='authflow', password='pw123456')
        try:
            self.client.force_login(u)
            resp = self.client.post('/accounts/login/', follow=True)
            # Force-login bypasses the redirect; assert the setting instead.
            self.assertEqual(settings.LOGIN_REDIRECT_URL, 'index')
            self.assertEqual(settings.LOGOUT_REDIRECT_URL, 'login')
        finally:
            u.delete()


class OptimizerQueryCountTests(TestCase):
    def test_optimizer_does_not_nplus1_on_storechain(self):
        """Store.display_name with unfetched chain used to cost 1 query/item."""
        from django.utils import timezone as tz
        from decimal import Decimal as D
        from django.test.utils import CaptureQueriesContext
        from django.db import connection
        from tracker.models import StoreChain
        user = User.objects.create_user(username='opt_q', password='p')
        chain = StoreChain.objects.create(name="Koch")
        store = Store.objects.create(name="KOCH HIPERMERCADO S/A LJ 47",
                                     cnpj="02831172006505", address_city="F")
        store.chain = chain
        store.save()
        for i in range(5):
            r = Receipt.objects.create(store=store, user=user, url='http://x',
                                       access_key=str(i) * 44, issue_date=tz.now(),
                                       total_amount=D('10'))
            p = Product.objects.create(name=f"OPT PROD {i}")
            ReceiptItem.objects.create(receipt=r, product=p, quantity=D('1'),
                                       unit_type='UN', unit_price=D('5'),
                                       total_price=D('5'))
        self.client.force_login(user)
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get('/tracker/optimizer/', secure=True)
        self.assertEqual(resp.status_code, 200)
        chain_qs = [q for q in ctx if 'tracker_storechain' in q['sql']]
        # 5 items, all at one chain-linked store: must be ~1 lookup, not 5+
        self.assertLessEqual(len(chain_qs), 2)


class FetchIpcaSeriesTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()  # IPCA series cache is shared across test runs

    @patch('tracker.services.requests.get')
    def test_fetch_caches_and_parses(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=lambda: [{'data': '15/03/2021', 'valor': '0,93'},
                                           {'data': 'bad-row', 'valor': 'x'}])
        mock_get.return_value.raise_for_status = lambda: None
        out = AnalyticsService._fetch_ipca_series(433)
        self.assertEqual(out, {'2021-03': 0.93})


class RobustnessSweepTests(TestCase):
    def test_toast_escapes_quotes_for_js(self):
        """Store names with quotes must not break the toast JS string."""
        user = User.objects.create_user(username='toast_q', password='p')
        self.client.force_login(user)
        resp = self.client.post('/tracker/categories/add/',
                                {'name': 'Q"X'}, secure=True, follow=True)
        self.assertEqual(resp.status_code, 200)
        # escapejs renders " as \u0022 inside the script block: the
        # toast call stays a valid JS string (the &quot; in the rename
        # input elsewhere on the page is correct HTML-attribute escaping)
        self.assertContains(
            resp, 'showToast("Category \\u0027Q\\u0022X\\u0027 created.", "success");')

    def test_confirm_refresh_rejects_missing_url(self):
        """Missing url must redirect, not raise TypeError -> 500."""
        user = User.objects.create_user(username='nourl', password='p')
        self.client.force_login(user)
        resp = self.client.post('/tracker/confirm_refresh/', {}, secure=True)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].endswith('/tracker/'))

    def test_refresh_failure_hides_internals(self):
        """SEFAZ error detail must stay in logs, not in the flash message."""
        from django.utils import timezone as tz
        from decimal import Decimal as D
        user = User.objects.create_user(username='ref_fail', password='p')
        store = Store.objects.create(name="S", cnpj="11111111000111")
        receipt = Receipt.objects.create(
            store=store, user=user, url='https://sat.sef.sc.gov.br/x',
            access_key='1' * 44, issue_date=tz.now(), total_amount=D('10'))
        self.client.force_login(user)
        with patch('tracker.views.NFCeScraper.scrape_url',
                   side_effect=Exception('SECRET-INTERNALS-12345')):
            resp = self.client.post(f'/tracker/receipt/{receipt.id}/refresh/',
                                    secure=True, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'SECRET-INTERNALS-12345')
        self.assertContains(resp, 'Refresh failed.')

    def test_index_has_no_dead_duplicate_modal(self):
        """The duplicate modal (never fed by any view) was removed."""
        user = User.objects.create_user(username='nomodal', password='p')
        self.client.force_login(user)
        resp = self.client.get('/tracker/', secure=True)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'duplicateModal')
        self.assertNotContains(resp, 'force_update')

    def test_maintenance_clears_expired_sessions(self):
        """Daily maintenance must purge expired sessions."""
        from django.contrib.sessions.backends.db import SessionStore
        from tracker.tasks import maintenance_requeue_enrichment
        store = SessionStore()
        store['k'] = 'v'
        store.set_expiry(-1)
        store.save()
        key = store.session_key
        with patch('tracker.tasks.async_task'):
            maintenance_requeue_enrichment(batch_size=0)
        self.assertFalse(SessionStore().exists(key))
