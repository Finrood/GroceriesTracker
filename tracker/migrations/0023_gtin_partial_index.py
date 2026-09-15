from django.db import migrations


# Django's SQLite schema editor does not materialize conditional
# UniqueConstraints added via AddConstraint (migration 0020 recorded as
# applied, but the partial index is missing and duplicates slip through --
# caught by test_duplicate_gtin_rejected_by_constraint failing in isolation).
# Create the partial unique index explicitly. Same index name as the
# constraint so IF NOT EXISTS skips where the backing index already exists
# (fresh Postgres installs via 0020, and this migration is a no-op there).
PARTIAL_GTIN_INDEX_SQL = (
    'CREATE UNIQUE INDEX IF NOT EXISTS "uniq_product_gtin_not_null" '
    'ON "tracker_product" ("code_gtin") WHERE "code_gtin" IS NOT NULL'
)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0022_canonicalproduct_canonicalsuggestion_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            PARTIAL_GTIN_INDEX_SQL,
            reverse_sql='DROP INDEX IF EXISTS "uniq_product_gtin_not_null"',
        ),
    ]
