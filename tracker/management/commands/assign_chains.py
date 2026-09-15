from collections import Counter
from django.core.management.base import BaseCommand
from django.db import transaction
from tracker.models import Store, StoreChain, resolve_trading_name


def assign_store_chain(store, save=True):
    """Link a store to its chain (grouped by CNPJ root). Returns StoreChain or None.

    Stores without CNPJ (unknown/manual) stay chain-less. New chains are named
    after the consumer-facing brand resolved from the group's names.
    """
    if not store.cnpj_root:
        return None
    if store.chain_id:
        return store.chain
    siblings = Store.objects.filter(cnpj_root=store.cnpj_root)
    votes = Counter(resolve_trading_name(s.name) for s in siblings)
    # Most common brand wins; tie-break: shortest (usually the clean brand).
    chain_name = sorted(votes.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
    with transaction.atomic():
        chain, _ = StoreChain.objects.get_or_create(name=chain_name)
        Store.objects.filter(cnpj_root=store.cnpj_root, chain__isnull=True).update(chain=chain)
    store.refresh_from_db()
    return store.chain


class Command(BaseCommand):
    help = ('Groups stores into chains by CNPJ root (first 8 digits) and '
            'names each chain with its consumer-facing brand. Dry-run by default.')

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true',
                            help='Write changes (default is dry-run).')

    def handle(self, *args, **options):
        apply = options['apply']
        roots = (Store.objects.exclude(cnpj_root='').values_list('cnpj_root', flat=True).distinct())
        created = linked = 0
        for root in roots:
            stores = list(Store.objects.filter(cnpj_root=root).order_by('id'))
            votes = Counter(resolve_trading_name(s.name) for s in stores)
            chain_name = sorted(votes.items(), key=lambda kv: (-kv[1], len(kv[0])))[0][0]
            unlinked = [s for s in stores if s.chain_id is None]
            self.stdout.write(
                f"Root {root}: chain={chain_name!r} stores={[s.name[:30] for s in stores]} "
                f"unlinked={[s.id for s in unlinked]}")
            linked += len(unlinked)
            if apply and unlinked:
                chain, was_created = StoreChain.objects.get_or_create(name=chain_name)
                created += int(was_created)
                Store.objects.filter(cnpj_root=root, chain__isnull=True).update(chain=chain)
        self.stdout.write(self.style.SUCCESS(
            f"{'Linked' if apply else 'Would link'} {linked} stores into chains "
            f"({created} new chains)."))
