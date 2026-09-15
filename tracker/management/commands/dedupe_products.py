from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count
from tracker.models import Product, ProductMapping, ReceiptItem, PriceHistory


def _keeper(cands):
    """Pick survivor: most purchase history, tie-break lowest id (oldest)."""
    scored = []
    for p in cands:
        items = ReceiptItem.objects.filter(product=p).count()
        hist = PriceHistory.objects.filter(product=p).count()
        maps = ProductMapping.objects.filter(product=p).count()
        scored.append((items + hist + maps, -p.id, p))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return scored[0][2]


class Command(BaseCommand):
    help = ('Merge Products sharing the same valid code_gtin and delete pure '
            'orphans (no items, history or mappings). Dry-run by default.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes (default is dry-run).')

    def handle(self, *args, **options):
        apply = options['apply']
        merged = deleted = 0
        merged_ids = set()
        groups = (Product.objects.exclude(code_gtin__isnull=True)
                  .exclude(code_gtin='').values('code_gtin')
                  .annotate(c=Count('id')).filter(c__gt=1))
        for g in groups:
            cands = list(Product.objects.filter(code_gtin=g['code_gtin']).order_by('id'))
            keep = _keeper(cands)
            losers = [p for p in cands if p.id != keep.id]
            self.stdout.write(
                f"GTIN {g['code_gtin']}: keep id={keep.id}, merge {[p.id for p in losers]}")
            merged += len(losers)
            if apply:
                with transaction.atomic():
                    for loser in losers:
                        ProductMapping.objects.filter(product=loser).update(product=keep)
                        ReceiptItem.objects.filter(product=loser).update(product=keep)
                        PriceHistory.objects.filter(product=loser).update(product=keep)
                        # Reparent variants pointing at the loser
                        Product.objects.filter(parent=loser).update(parent=keep)
                        loser.delete()
                        merged_ids.add(loser.id)

        orphans = list(Product.objects.filter(
            receiptitems__isnull=True, price_history__isnull=True,
            store_mappings__isnull=True, parent__isnull=True))
        # Exclude products that are parents themselves (have variants)
        orphans = [p for p in orphans
                   if p.id not in merged_ids and not p.variants.exists()]
        for p in orphans:
            self.stdout.write(f"Orphan: id={p.id} name={p.name[:40]!r}")
            deleted += 1
            if apply:
                p.delete()
        self.stdout.write(self.style.SUCCESS(
            f"{'Merged/removed' if apply else 'Would merge/remove'}: "
            f"{merged} duplicates, {deleted} orphans."))
