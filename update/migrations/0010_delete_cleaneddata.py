from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('update', '0009_batchsubscriber'),
    ]

    operations = [
        migrations.DeleteModel(
            name='CleanedData',
        ),
    ]
