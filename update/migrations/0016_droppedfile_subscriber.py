# Generated for DroppedFile subscriber foreign key

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('acctmgt', '0001_initial'),
        ('update', '0015_alter_uploadsession_upload_to_db_droppedfile'),
    ]

    operations = [
        migrations.AddField(
            model_name='droppedfile',
            name='subscriber',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='dropped_files',
                to='acctmgt.subscriber',
            ),
        ),
    ]
