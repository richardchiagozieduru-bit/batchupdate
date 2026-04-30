from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('update', '0006_uploadsession_batch_id_uploadsession_source_filename_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='uploadsession',
            name='header_row',
            field=models.IntegerField(default=0),
        ),
    ]
