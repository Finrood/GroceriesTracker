from django.core.management.base import BaseCommand
from tracker.models import ReceiptItem, PriceHistory

class Command(BaseCommand):
    help = 'Populates PriceHistory from existing ReceiptItems'

    def handle(self, *args, **options):
        items = ReceiptItem.objects.all()
        count = 0
        for item in items:
            PriceHistory.objects.get_or_create(
                user=item.receipt.user,
                product=item.product,
                store=item.receipt.store,
                date=item.receipt.issue_date,
                defaults={
                    'unit_price': item.unit_price,
                    'normalized_price': item.normalized_price
                }
            )
            count += 1
        self.stdout.write(self.style.SUCCESS(f'Successfully backfilled {count} price history records.'))
