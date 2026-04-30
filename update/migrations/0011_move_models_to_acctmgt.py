"""
State-only migration: re-points UploadSession.subscriber FK to acctmgt.Subscriber
and removes the model definitions from the update app's migration state.
No database operations — the tables are unchanged (they keep their update_* names
via db_table on the acctmgt models).
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('update', '0010_delete_cleaneddata'),
        ('acctmgt', '0001_initial'),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                # Re-point the FK to acctmgt.Subscriber (same DB table, no SQL change)
                migrations.AlterField(
                    model_name='uploadsession',
                    name='subscriber',
                    field=models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='sessions',
                        to='acctmgt.subscriber',
                    ),
                ),
                # Remove models from update state (they now live in acctmgt)
                migrations.DeleteModel(name='SubscriberToken'),
                migrations.DeleteModel(name='UserSubscriberProfile'),
                migrations.DeleteModel(name='Subscriber'),
                migrations.DeleteModel(name='BatchSubscriber'),
            ],
            database_operations=[],
        ),
    ]
