from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tracker', '0020_product_gtin_unique'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='receiptitem',
            index=models.Index(fields=['receipt', 'product'], name='ri_receipt_product_idx'),
        ),
        migrations.AddIndex(
            model_name='pricehistory',
            index=models.Index(fields=['product', 'store', 'date'], name='ph_prod_store_date_idx'),
        ),
        migrations.AddIndex(
            model_name='productmapping',
            index=models.Index(fields=['store', 'internal_code'], name='pm_store_code_idx'),
        ),
    ]
