# Generated for UploadSession content_fingerprint and ImplementedArtifact model

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('acctmgt', '0001_initial'),
        ('update', '0016_droppedfile_subscriber'),
    ]

    operations = [
        migrations.AddField(
            model_name='uploadsession',
            name='content_fingerprint',
            field=models.CharField(blank=True, db_index=True, max_length=64),
        ),
        migrations.CreateModel(
            name='ImplementedArtifact',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('content_fingerprint', models.CharField(db_index=True, max_length=64, unique=True)),
                ('table_name', models.CharField(max_length=64)),
                ('rows_uploaded', models.IntegerField(default=0)),
                ('implemented_at', models.DateTimeField(auto_now_add=True)),
                ('is_historical_backfill', models.BooleanField(default=False)),
                ('implemented_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='implemented_artifacts', to=settings.AUTH_USER_MODEL)),
                ('source_session', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='implemented_artifacts', to='update.uploadsession')),
                ('subscriber', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='implemented_artifacts', to='acctmgt.subscriber')),
            ],
            options={
                'ordering': ['-implemented_at'],
            },
        ),
    ]
