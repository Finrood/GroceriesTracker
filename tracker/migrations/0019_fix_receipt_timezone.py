"""
Correct receipts stored with the wrong timezone.

The scraper used to hand Django a NAIVE datetime in Brazil wall-clock time
(America/Sao_Paulo, UTC-3). With USE_TZ=True Django interpreted it as UTC, so
every Receipt.issue_date / PriceHistory.date was stored +3h ahead of reality.

Verified empirically 2026-09-15: for the 29 receipts whose ScrapeLog entry was
created the same day as the purchase, scrape_time - issue_time never dropped
below 182 minutes (the UTC-3 offset), with 11 of them in the 3-4h window.

This migration shifts every affected datetime back exactly 3 hours, running
on raw SQL because Django would otherwise re-serialize the (already wrong)
stored values as aware datetimes.
"""
from django.db import migrations


def shift_back_three_hours(apps, schema_editor):
    if schema_editor.connection.vendor != 'sqlite':
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "UPDATE tracker_receipt SET issue_date = "
            "strftime('%Y-%m-%d %H:%M:%f', issue_date, '-3 hours') "
            "WHERE strftime('%Y-%m-%d %H:%M:%f', issue_date, '-3 hours') IS NOT NULL"
        )
        cursor.execute(
            "UPDATE tracker_pricehistory SET date = "
            "strftime('%Y-%m-%d %H:%M:%f', date, '-3 hours') "
            "WHERE strftime('%Y-%m-%d %H:%M:%f', date, '-3 hours') IS NOT NULL"
        )


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0018_pricehistory_receipt_alter_receipt_access_key_and_more'),
    ]

    operations = [
        migrations.RunPython(shift_back_three_hours, migrations.RunPython.noop),
    ]