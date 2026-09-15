from django.db import migrations, models
from django.db.models import Q


def blank_gtin_to_null(apps, schema_editor):
    Product = apps.get_model('tracker', 'Product')
    Product.objects.filter(code_gtin='').update(code_gtin=None)


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0019_fix_receipt_timezone'),
    ]

    operations = [
        migrations.RunPython(blank_gtin_to_null, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name='product',
            constraint=models.UniqueConstraint(
                fields=['code_gtin'],
                condition=Q(code_gtin__isnull=False),
                name='uniq_product_gtin_not_null',
            ),
        ),
    ]
