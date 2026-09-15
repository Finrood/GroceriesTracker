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
