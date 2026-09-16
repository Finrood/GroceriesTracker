from datetime import timedelta
from unittest.mock import patch
from django.shortcuts import render
from django.test import RequestFactory, TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from decimal import Decimal
from .models import CanonicalProduct, PriceHistory, Store, Product, Category, Receipt, ReceiptItem
from .services import AnalyticsService
from .views import receipt_detail

class OptimizationRegressionTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username='opt_user', password='password123')
        self.store_a = Store.objects.create(name="Opt Store A", cnpj="11111111111111")
        self.store_b = Store.objects.create(name="Opt Store B", cnpj="22222222222222")
        self.receipt = Receipt.objects.create(
            user=self.user,
            store=self.store_a,
            access_key="44444444444444444444444444444444444444444444",
            issue_date=timezone.now(),
            total_amount=Decimal('50.00'),
            payment_method='Credit Card'
        )
        self.brand_product_a = Product.objects.create(
            name="NESCAU 400G", display_name="Chocolate Nescau 400g", brand="Nestle", weight_grams=Decimal('400'))
        self.brand_product_b = Product.objects.create(
            name="NESCAU 800G", display_name="Chocolate Nescau 800g", brand="Nestle", weight_grams=Decimal('800'))
        self.brand_product_c = Product.objects.create(
            name="NESCAU 1KG", display_name="Chocolate Nescau 1kg", brand="Nestle", weight_grams=Decimal('1000'))
        self.brand_product_d = Product.objects.create(
            name="NESQUIK 400G", display_name="Nesquik 400g", brand="Nestle", weight_grams=Decimal('400'))
        self.plain_product = Product.objects.create(name="PAO FRANCES", brand="")
        for product in (self.brand_product_a, self.brand_product_b, self.brand_product_c,
                        self.brand_product_d, self.plain_product):
            ReceiptItem.objects.create(
                receipt=self.receipt,
                product=product,
                quantity=Decimal('1.000'),
                unit_type='UN',
                unit_price=Decimal('10.00'),
                total_price=Decimal('10.00')
            )

    def _add_history(self, product, store, price, days_ago, receipt=None):
        return PriceHistory.objects.create(
            user=self.user,
            receipt=receipt,
            product=product,
            store=store,
            date=timezone.now() - timedelta(days=days_ago),
            unit_price=Decimal(price),
            normalized_price=None
        )

    def test_variant_suggestions_matches_same_brand_pairs(self):
        suggestions = AnalyticsService.get_variant_suggestions(self.user)
        self.assertEqual(len(suggestions), 3)
        pairs = {frozenset((s['p1'].id, s['p2'].id)) for s in suggestions}
        self.assertIn(frozenset((self.brand_product_a.id, self.brand_product_b.id)), pairs)
        self.assertIn(frozenset((self.brand_product_a.id, self.brand_product_c.id)), pairs)
        self.assertIn(frozenset((self.brand_product_b.id, self.brand_product_c.id)), pairs)

    def test_variant_suggestions_skips_brands_without_valid_pairs(self):
        Product.objects.create(name="OUTRO 500G", brand="Outro", weight_grams=Decimal('500'))
        suggestions = AnalyticsService.get_variant_suggestions(self.user)
        self.assertEqual(len(suggestions), 3)

    def test_variant_suggestions_single_query(self):
        for brand_count in (1, 6):
            for index in range(brand_count):
                product = Product.objects.create(name=f'Chocolate Other {index} 500G', brand=f'Brand {index}')
                ReceiptItem.objects.create(
                    receipt=self.receipt, product=product, quantity=1, unit_type='UN',
                    unit_price=10, total_price=10)
            with self.assertNumQueries(1):
                self.assertEqual(len(AnalyticsService.get_variant_suggestions(self.user)), 3)

    def test_budget_drift_computes_from_latest_two_observations(self):
        product = self.brand_product_a
        ReceiptItem.objects.create(
            receipt=self.receipt,
            product=product,
            quantity=Decimal('1.000'),
            unit_type='UN',
            unit_price=Decimal('9.00'),
            total_price=Decimal('9.00')
        )
        self._add_history(product, self.store_a, '100.00', 20)
        self._add_history(product, self.store_a, '8.00', 10)
        self._add_history(product, self.store_a, '10.00', 2)
        report = AnalyticsService.get_budget_drift(self.user)
        self.assertEqual(len(report), 1)
        self.assertEqual(report[0]['store'], "Opt Store A")
        self.assertEqual(report[0]['diff'], 2.0)
        self.assertEqual(report[0]['pct'], 25.0)
        self.assertEqual(report[0]['status'], 'up')

    def test_budget_drift_ignores_other_users_observations(self):
        other_user = User.objects.create_user(username='other_user', password='password123')
        product = self.brand_product_a
        PriceHistory.objects.create(
            user=other_user,
            product=product,
            store=self.store_a,
            date=timezone.now() - timedelta(days=1),
            unit_price=Decimal('99.00')
        )
        PriceHistory.objects.create(
            user=other_user,
            product=product,
            store=self.store_a,
            date=timezone.now() - timedelta(days=2),
            unit_price=Decimal('98.00')
        )
        report = AnalyticsService.get_budget_drift(self.user)
        self.assertEqual(report, [])

    def test_budget_drift_fixed_query_count(self):
        self._add_history(self.brand_product_a, self.store_a, '8.00', 10)
        self._add_history(self.brand_product_a, self.store_a, '10.00', 2)
        extra_store = Store.objects.create(name="Opt Store C", cnpj="33333333333333")
        Receipt.objects.create(
            user=self.user,
            store=extra_store,
            access_key="55555555555555555555555555555555555555555555",
            issue_date=timezone.now(),
            total_amount=Decimal('5.00'),
            payment_method='Credit Card'
        )
        self._add_history(self.brand_product_b, extra_store, '7.00', 3)
        self._add_history(self.brand_product_b, extra_store, '6.00', 5)
        with self.assertNumQueries(3):
            AnalyticsService.get_budget_drift(self.user)

    def test_receipt_detail_benchmarks_bulk_queries(self):
        product = self.brand_product_a
        for price, days_ago in (('8.00', 10), ('9.00', 5), ('11.00', 1)):
            self._add_history(product, self.store_a, price, days_ago)
        request = self.factory.get('/receipt/')
        request.user = self.user
        with patch('tracker.views.render', wraps=render) as mock_render:
            with self.assertNumQueries(5):
                response = receipt_detail(request, self.receipt.id)
        self.assertEqual(response.status_code, 200)
        items = list(mock_render.call_args.args[2]['items'])
        self.assertEqual(len(items), 5)
        self.assertEqual(items[0].benchmark['label'], 'Fair Price')
        self.assertEqual(items[1].benchmark, None)
        self.assertEqual(items[2].benchmark, None)
        self.assertEqual(items[3].benchmark, None)
        self.assertEqual(items[4].benchmark, None)

    def test_receipt_detail_bulk_matches_single_helper(self):
        product = self.brand_product_a
        for price, days_ago in (('8.00', 10), ('9.00', 5), ('12.00', 1)):
            self._add_history(product, self.store_a, price, days_ago)
        items = list(self.receipt.items.select_related('product', 'product__category'))
        bulk_map = AnalyticsService.get_price_benchmarks(self.user, items)
        for item in items:
            single = AnalyticsService.get_price_benchmark(
                self.user, item.product_id, item.unit_price,
                normalized_price=item.normalized_price)
            self.assertEqual(bulk_map[item.pk], single)

    def test_receipt_detail_benchmark_spans_canonical_bucket(self):
        product = self.brand_product_a
        for price, days_ago in (('8.00', 10), ('9.00', 5), ('12.00', 1)):
            self._add_history(product, self.store_a, price, days_ago)
        canonical = CanonicalProduct.objects.create(name="Chocolate Nescau")
        sibling = Product.objects.create(
            name="NESCAU 400G PLU", brand="Nestle", canonical=canonical, weight_grams=Decimal('400'))
        product.canonical = canonical
        product.save(update_fields=['canonical'])
        PriceHistory.objects.create(
            user=self.user,
            product=sibling,
            store=self.store_b,
            date=timezone.now() - timedelta(days=3),
            unit_price=Decimal('20.00')
        )
        items = list(self.receipt.items.select_related('product', 'product__category'))
        bulk_map = AnalyticsService.get_price_benchmarks(self.user, items)
        target = next(item for item in items if item.product_id == product.id)
        single = AnalyticsService.get_price_benchmark(
            self.user, product.id, target.unit_price,
            normalized_price=target.normalized_price)
        self.assertEqual(bulk_map[target.pk], single)
        self.assertEqual(single['label'], 'Fair Price')


class WeightExtractionRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='test_user', password='password123')
        self.store = Store.objects.create(name="Test Store", cnpj="12345678901234")
        self.category = Category.objects.create(name="Test Category")
        self.receipt = Receipt.objects.create(
            user=self.user,
            store=self.store,
            access_key="12345678901234567890123456789012345678901234",
            issue_date=timezone.now(),
            total_amount=Decimal('10.00'),
            payment_method='Credit Card'
        )

    def test_receipt_item_save_with_auto_extracted_weight(self):
        """
        Verify that creating a ReceiptItem for a product with auto-extracted weight
        doesn't crash with TypeError: unsupported operand type(s) for /: 'decimal.Decimal' and 'float'
        """
        # Product name triggers automatic weight extraction in Product.save()
        product = Product.objects.create(
            name="SHAMPOO ANTI-CASPA 400ML",
            category=self.category
        )

        # Check if weight was extracted as Decimal (our fix)
        self.assertIsInstance(product.weight_grams, Decimal)
        self.assertEqual(product.weight_grams, Decimal('400'))

        # This should NOT raise TypeError
        try:
            item = ReceiptItem.objects.create(
                receipt=self.receipt,
                product=product,
                quantity=Decimal('1.000'),
                unit_type='UN',
                unit_price=Decimal('25.50'),
                total_price=Decimal('25.50')
            )
            # Verify normalization happened correctly
            # (25.50 / 400) * 1000 = 63.75
            self.assertEqual(item.normalized_price, Decimal('63.75'))
        except TypeError as e:
            self.fail(f"ReceiptItem.create raised TypeError unexpectedly: {e}")

    def test_weight_extraction_units(self):
        """Test various units to ensure they are extracted as Decimals."""
        test_cases = [
            ("Arroz 5KG", Decimal('5000')),
            ("Feijao 1kg", Decimal('1000')),
            ("Creme Dental 90g", Decimal('90')),
            ("Refrigerante 2L", Decimal('2000')),
            ("Suco 500ml", Decimal('500')),
        ]
        for name, expected_weight in test_cases:
            with self.subTest(name=name):
                p = Product.objects.create(name=name)
                self.assertIsInstance(p.weight_grams, Decimal)
                self.assertEqual(p.weight_grams, expected_weight)
