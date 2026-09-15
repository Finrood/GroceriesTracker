from django.core.management.base import BaseCommand
from tracker.models import Product
from tracker.scraper import NFCeScraper


class Command(BaseCommand):
    help = ('Replaces noise-word brands (Moida, Peito, Caturra…) with '
            'dictionary matches or empty (unknown). Skips manually edited '
            'products. Dry-run by default.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes (default is dry-run).')

    def handle(self, *args, **options):
        apply = options['apply']
        scraper = NFCeScraper()
        known = {v for v in NFCeScraper.KNOWN_BRANDS.values() if v}
        changed = skipped_manual = 0
        for p in Product.objects.all().only('id', 'name', 'brand', 'is_manually_edited'):
            if p.brand in known:
                continue
            if p.is_manually_edited:
                skipped_manual += 1
                continue
            guess = scraper._guess_brand(p.name)
            if guess != (p.brand or ''):
                self.stdout.write(f"Product {p.id} {p.name[:40]!r}: {p.brand!r} -> {guess!r}")
                changed += 1
                if apply:
                    p.brand = guess
                    p.save(update_fields=['brand'])
        self.stdout.write(
            f"{changed} brands to clean{'' if apply else ' (dry-run, use --apply)'}; "
            f"{skipped_manual} manually-edited skipped.")
