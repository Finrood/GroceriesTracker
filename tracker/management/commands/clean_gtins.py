from django.core.management.base import BaseCommand
from tracker.models import Product
from tracker.gtin import is_valid_gtin


class Command(BaseCommand):
    help = ('Clears store-local PLU / weigh codes mistakenly stored in '
            'Product.code_gtin. Only real global GTINs are kept; per-store '
            'identity lives in ProductMapping and is untouched.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes (default is dry-run).')

    def handle(self, *args, **options):
        apply = options['apply']
        fakes = 0
        checked = 0
        for p in Product.objects.exclude(code_gtin__isnull=True).exclude(code_gtin=''):
            checked += 1
            if not is_valid_gtin(p.code_gtin or ''):
                fakes += 1
                self.stdout.write(f"PLU-as-GTIN: id={p.id} name={p.name[:40]!r} code={p.code_gtin!r}")
                if apply:
                    Product.objects.filter(id=p.id).update(code_gtin=None)
        self.stdout.write(self.style.SUCCESS(
            f"Checked {checked} products with codes; {fakes} invalid "
            f"{'cleared' if apply else '(dry-run, use --apply to clear)'}."))
