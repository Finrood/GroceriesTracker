from django.test import TestCase
from django.contrib.auth.models import User
from django.utils import timezone
from decimal import Decimal
from .models import Store, Product, Category, Receipt, ReceiptItem

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
