import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from django_q.models import OrmQ, Task

from tracker.enrichment import ProductEnrichmentService
from tracker.models import Product
from tracker.tasks import async_enrich_product, maintenance_requeue_enrichment, prune_completed_tasks


class PruneCompletedTasksTests(TestCase):
    def test_deletes_only_old_completed_tasks(self):
        old_done = Task.objects.create(
            id=uuid4().hex,
            func='tracker.tasks.async_enrich_product',
            started=timezone.now() - timedelta(days=91),
            stopped=timezone.now() - timedelta(days=91),
            success=True,
        )
        new_done = Task.objects.create(
            id=uuid4().hex,
            func='tracker.tasks.async_enrich_product',
            started=timezone.now() - timedelta(days=1),
            stopped=timezone.now() - timedelta(days=1),
            success=True,
        )
        OrmQ.objects.create(key='isolated-maintenance-test', payload='x')
        deleted = prune_completed_tasks(retention_days=90)
        self.assertEqual(deleted, 1)
        self.assertFalse(Task.objects.filter(pk=old_done.pk).exists())
        self.assertTrue(Task.objects.filter(pk=new_done.pk).exists())
        self.assertEqual(OrmQ.objects.count(), 1)

    def test_default_retention_uses_stop_time_and_strict_cutoff(self):
        now = timezone.now()
        cutoff = now - timedelta(days=90)
        retained = []
        for stopped in (cutoff, cutoff + timedelta(microseconds=1), now):
            retained.append(Task.objects.create(
                id=uuid4().hex,
                func='tracker.tasks.async_enrich_product',
                started=now - timedelta(days=100),
                stopped=stopped,
                success=False,
            ).pk)
        for success in (True, False):
            Task.objects.create(
                id=uuid4().hex,
                func='tracker.tasks.async_enrich_product',
                started=now - timedelta(days=100),
                stopped=cutoff - timedelta(microseconds=1),
                success=success,
            )
        pending = OrmQ.objects.create(
            key='isolated-maintenance-test', payload='pending', lock=cutoff,
        )
        with patch.dict('os.environ', {}, clear=True), patch('tracker.tasks.timezone.now', return_value=now):
            self.assertEqual(prune_completed_tasks(), 2)
        self.assertCountEqual(Task.objects.values_list('pk', flat=True), retained)
        pending.refresh_from_db()
        self.assertEqual(pending.payload, 'pending')
        self.assertEqual(pending.lock, cutoff)

    def test_retention_days_env_configurable(self):
        old_done = Task.objects.create(
            id=uuid4().hex,
            func='tracker.tasks.async_enrich_product',
            started=timezone.now() - timedelta(days=30),
            stopped=timezone.now() - timedelta(days=30),
            success=True,
        )
        with patch.dict('os.environ', {'TASK_RETENTION_DAYS': '7'}):
            deleted = prune_completed_tasks()
        self.assertEqual(deleted, 1)
        self.assertFalse(Task.objects.filter(pk=old_done.pk).exists())

    def test_rejects_invalid_retention_days(self):
        for bad in ('0', '-5', 'abc'):
            with self.assertRaises(ValueError):
                prune_completed_tasks(retention_days=bad)
        with self.assertRaises(ValueError):
            prune_completed_tasks(retention_days=True)

    def test_maintenance_requeue_invokes_prune(self):
        with patch('tracker.tasks.prune_completed_tasks', return_value=0) as mock_prune:
            maintenance_requeue_enrichment(batch_size=100)
        mock_prune.assert_called_once_with()
        self.assertEqual(Product.objects.count(), 0)


class GroceriesBackupTests(SimpleTestCase):
    def test_backup_failure_and_same_day_retry(self):
        script = Path(__file__).resolve().parent.parent / 'backup_apps.sh'
        if not script.exists():
            self.skipTest('Local backup script is not installed')
        section = script.read_text().split('# ---------- Groceries ----------', 1)[1]
        section = section.split('# ---------- Joplin ----------', 1)[0]
        mocks = '''
set -u
fail=0
STAMP=2026-09-16
LOG="$BACKUP_ROOT/backup.log"
docker() {
  if [ "$1" = cp ]; then
    [ "$SCENARIO" != copy ] || return 1
    /bin/cp "$FIXTURE" "$3"
  elif [ "$3" = rm ]; then
    return 0
  elif [ "$4" = manage.py ]; then
    printf 'GTCOUNTS 0 0 0\\n'
  else
    [ "$SCENARIO" != api ]
  fi
}
tar() {
  [ "$SCENARIO" != archive ] || return 1
  printf 'archive' > "$2"
}
cp() { return 0; }
sha256sum() {
  [ "$SCENARIO" != checksum ] || return 1
  command sha256sum "$@"
}
'''
        for scenario in ('api', 'copy', 'archive', 'checksum', 'corrupt', 'missing_tables', 'success'):
            with self.subTest(scenario=scenario), TemporaryDirectory(prefix='groceries-backup-test-') as directory:
                root = Path(directory)
                previous = root / 'groceries' / '2026-09-16'
                previous.mkdir(parents=True)
                saved = previous / 'db.sqlite3'
                saved.write_bytes(b'previous-good-backup')
                latest = previous.parent / 'latest'
                latest.symlink_to(previous, target_is_directory=True)
                fixture = root / 'fixture.sqlite3'
                if scenario == 'corrupt':
                    fixture.write_bytes(b'not sqlite')
                else:
                    with sqlite3.connect(fixture) as connection:
                        if scenario != 'missing_tables':
                            for table in ('tracker_receipt', 'tracker_product', 'tracker_receiptitem'):
                                connection.execute(f'CREATE TABLE {table} (id INTEGER)')
                result = subprocess.run(
                    ['bash', '-c', mocks + section],
                    env={**os.environ, 'BACKUP_ROOT': str(root), 'FIXTURE': str(fixture), 'SCENARIO': scenario},
                    capture_output=True, text=True, timeout=20,
                )
                self.assertEqual(saved.read_bytes(), b'previous-good-backup')
                if scenario == 'success':
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotEqual(latest.resolve(), previous)
                    checked = subprocess.run(
                        ['sha256sum', '-c', 'SHA256SUMS'], cwd=latest.resolve(),
                        capture_output=True, text=True, timeout=10,
                    )
                    self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(latest.resolve(), previous)


class AsyncEnrichProductIsolationTests(TestCase):
    def test_enrichment_failure_does_not_enqueue(self):
        product = Product.objects.create(name='LONELY PRODUCT')
        with patch.object(ProductEnrichmentService, 'enrich_product', return_value=False):
            result = async_enrich_product(product.id)
        self.assertIn('No external data found', result)
        self.assertEqual(OrmQ.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)
        product.refresh_from_db()
        self.assertIsNotNone(product.last_enrichment_attempt)
