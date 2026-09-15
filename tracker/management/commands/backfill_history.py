from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from tracker.models import Receipt, ReceiptItem, PriceHistory


def _receipt_gaps():
    """Return [(receipt, n_items, n_history)] for receipts out of sync."""
    gaps = []
    for r in Receipt.objects.all().only('id'):
        n_items = ReceiptItem.objects.filter(receipt=r).count()
        n_hist = PriceHistory.objects.filter(receipt=r).count()
        if n_items != n_hist:
            gaps.append((r, n_items, n_hist))
    return gaps


def _rebuild_receipt(receipt):
    items = list(ReceiptItem.objects.filter(receipt=receipt).select_related(
        'receipt', 'receipt__store', 'receipt__user', 'product'))
    with transaction.atomic():
        PriceHistory.objects.filter(receipt=receipt).delete()
        PriceHistory.objects.bulk_create([
            PriceHistory(
                user=item.receipt.user,
                receipt=item.receipt,
                product=item.product,
                store=item.receipt.store,
                date=item.receipt.issue_date,
                unit_price=item.unit_price,
                normalized_price=item.normalized_price,
            )
            for item in items
        ])
    return len(items)


class Command(BaseCommand):
    help = ('Repairs PriceHistory from ReceiptItems. Default: rebuild history '
            'only for receipts whose item/history counts disagree (safe, '
            'leaves correct receipts untouched). --full wipes and rebuilds '
            'everything (also purges ghost rows). --check only reports.')

    def add_arguments(self, parser):
        parser.add_argument('--full', action='store_true',
                            help='Wipe whole table and rebuild from all items.')
        parser.add_argument('--check', action='store_true',
                            help='Report gaps without writing; exits 1 if any.')

    def handle(self, *args, **options):
        if options['full'] and options['check']:
            raise CommandError('--full and --check are mutually exclusive.')

        if options['full']:
            deleted, _ = PriceHistory.objects.all().delete()
            count = 0
            items = ReceiptItem.objects.select_related(
                'receipt', 'receipt__store', 'receipt__user', 'product')
            batch = []
            for item in items.iterator(chunk_size=500):
                batch.append(PriceHistory(
                    user=item.receipt.user, receipt=item.receipt,
                    product=item.product, store=item.receipt.store,
                    date=item.receipt.issue_date, unit_price=item.unit_price,
                    normalized_price=item.normalized_price))
                if len(batch) >= 500:
                    PriceHistory.objects.bulk_create(batch)
                    count += len(batch)
                    batch = []
            if batch:
                PriceHistory.objects.bulk_create(batch)
                count += len(batch)
            self.stdout.write(self.style.SUCCESS(
                f'Deleted {deleted} history rows; rebuilt {count} from ReceiptItems.'))
            return

        gaps = _receipt_gaps()
        if options['check']:
            for r, ni, nh in gaps:
                self.stdout.write(f"Gap: receipt {r.id}: {ni} items vs {nh} history")
            self.stdout.write(f"{len(gaps)} receipts out of sync.")
            if gaps:
                raise CommandError(f"{len(gaps)} receipts out of sync.")
            self.stdout.write(self.style.SUCCESS('PriceHistory in sync.'))
            return

        fixed = 0
        for r, ni, nh in gaps:
            n = _rebuild_receipt(r)
            fixed += 1
            self.stdout.write(f"Rebuilt receipt {r.id}: {nh} -> {n} history rows ({ni} items).")
        self.stdout.write(self.style.SUCCESS(
            f"Repaired {fixed} receipts; "
            f"{Receipt.objects.count() - fixed} already in sync."))
