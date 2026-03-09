from django.test import TestCase, Client, TransactionTestCase
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
        product = Product.objects.create(name="Test Prod", code_gtin="7891234567890")
        
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
