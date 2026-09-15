from django.core.management.base import BaseCommand
from tracker.models import Product, normalized_for, needs_renorm


class Command(BaseCommand):
    help = ('Recomputes frozen ReceiptItem/PriceHistory normalized prices from '
            'each product\u2019s CURRENT weight. Fixes rows imported under a '
            'misparsed size that was corrected later (e.g. \u201cBD 18L\u201d -> 1.8L). '
            'Dry-run by default.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write corrections (default is dry-run).')

    def handle(self, *args, **options):
        apply = options['apply']
        stale_products = 0
        stale_rows = 0
        for p in Product.objects.all().only('id', 'name', 'weight_grams'):
            items = list(p.receiptitems.all().only('id', 'unit_price', 'normalized_price'))
            if not items or not p.weight_grams:
                continue
            bad = 0
            first = None
            for item in items:
                want = normalized_for(item.unit_price, p.weight_grams)
                if needs_renorm(item.normalized_price, want):
                    bad += 1
                    if first is None:
                        first = (item.normalized_price, want)
            if bad:
                stale_products += 1
                stale_rows += bad
                self.stdout.write(
                    f"Product {p.id} {p.name[:40]!r} w={p.weight_grams}: "
                    f"{bad}/{len(items)} rows stale e.g. {first[0]} -> {first[1]}")
        if apply:
            fixed = 0
            for p in Product.objects.all().only('id'):
                fixed += p.renormalize_items()
            self.stdout.write(self.style.SUCCESS(f"Recomputed {fixed} rows."))
        else:
            self.stdout.write(f"{stale_products} products, {stale_rows} rows stale "
                              f"(dry-run, use --apply).")
