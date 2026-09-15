from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from tracker.models import Product, CanonicalProduct, CanonicalSuggestion
from tracker import canonical as canon_svc


class Command(BaseCommand):
    help = ('Groups Products into CanonicalProducts (cross-store identity). '
            'Same GTIN and high-similarity signature pairs merge automatically; '
            'mid-similarity pairs become review suggestions. Dry-run by default.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write groups and suggestions (default is dry-run).')
        parser.add_argument('--check', action='store_true',
                            help='Exit 1 when products lack a canonical.')

    def handle(self, *args, **options):
        if options['check']:
            n = Product.objects.filter(canonical__isnull=True).count()
            self.stdout.write(f"{n} products without canonical.")
            if n:
                raise CommandError(f"{n} products without canonical.")
            return

        products = list(Product.objects.select_related('category').all())
        groups, suggestions = canon_svc.preview_groups(products)
        multi = {k: v for k, v in groups.items() if len(v) > 1}
        self.stdout.write(f"{len(products)} products -> {len(groups)} canonicals "
                          f"({len(multi)} multi-product), {len(suggestions)} suggestions.")
        for members in list(multi.values())[:10]:
            self.stdout.write("  GROUP: " + " | ".join(
                f"{m.id}:{(m.display_name or m.name)[:30]}" for m in members))

        if not options['apply']:
            self.stdout.write("Dry-run: use --apply to write.")
            return

        created_groups = 0
        with transaction.atomic():
            for members in groups.values():
                existing = {m.canonical_id for m in members if m.canonical_id}
                if len(existing) == 1:
                    canon_id = existing.pop()
                    canon = CanonicalProduct.objects.get(id=canon_id)
                    Product.objects.filter(id__in=[m.id for m in members],
                                           canonical__isnull=True).update(canonical=canon)
                elif len(existing) > 1:
                    canons = list(CanonicalProduct.objects.filter(id__in=existing).order_by('id'))
                    keeper = canons[0]
                    for extra in canons[1:]:
                        canon_svc.merge_canonicals(keeper, extra)
                    Product.objects.filter(id__in=[m.id for m in members],
                                           canonical__isnull=True).update(canonical=keeper)
                else:
                    rep = members[0]
                    canon = CanonicalProduct.objects.create(
                        name=canon_svc.representative_name(members)[:255],
                        category=rep.category)
                    Product.objects.filter(id__in=[m.id for m in members]).update(canonical=canon)
                    created_groups += 1

            created_sugg = 0
            seen_pairs = set()
            for a, b, score, reason in suggestions:
                key = tuple(sorted((a.id, b.id)))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                first, second = (a, b) if a.id == key[0] else (b, a)
                _, was_created = CanonicalSuggestion.objects.get_or_create(
                    product_a=first, product_b=second,
                    defaults={'score': score, 'reason': reason})
                if not was_created:
                    CanonicalSuggestion.objects.filter(
                        product_a=first, product_b=second,
                        status=CanonicalSuggestion.PENDING
                    ).update(score=score, reason=reason)
                created_sugg += int(was_created)
        self.stdout.write(self.style.SUCCESS(
            f"Wrote {created_groups} new canonicals; {created_sugg} new suggestions."))
