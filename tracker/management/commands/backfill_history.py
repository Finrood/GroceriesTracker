from django.core.management.base import BaseCommand
from tracker.models import ReceiptItem, PriceHistory

class Command(BaseCommand):
    help = ('Rebuilds PriceHistory from ReceiptItems. Idempotent: wipes the '
            'table and recreates one row per item, linked to its receipt. '
            'Fixes ghost rows left by deleted receipts and duplicated rows '
            'created by receipt refreshes before the receipt FK existed.')

    def handle(self, *args, **options):
        deleted, _ = PriceHistory.objects.all().delete()
        count = 0
        items = ReceiptItem.objects.select_related('receipt', 'receipt__store', 'receipt__user', 'product')
        for item in items:
            PriceHistory.objects.create(
                user=item.receipt.user,
                receipt=item.receipt,
                product=item.product,
                store=item.receipt.store,
                date=item.receipt.issue_date,
                unit_price=item.unit_price,
                normalized_price=item.normalized_price
            )
            count += 1
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted} history rows; rebuilt {count} from ReceiptItems.'))