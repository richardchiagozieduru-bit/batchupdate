"""
State-only migration: registers acctmgt models in Django's migration state
without touching the database. The underlying tables already exist under
their update_* names (created by update migrations 0008 and 0009).
"""
import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('update', '0010_delete_cleaneddata'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name='BatchSubscriber',
                    fields=[
                        ('subscriber_id', models.BigIntegerField(db_column='SubscriberID', primary_key=True, serialize=False)),
                        ('subscriber_name', models.CharField(db_column='SubscriberName', max_length=500)),
                    ],
                    options={
                        'ordering': ['subscriber_name'],
                        'db_table': 'Sheet1',
                        'managed': False,
                    },
                ),
                migrations.CreateModel(
                    name='Subscriber',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('subscriber_id', models.IntegerField(unique=True)),
                        ('subscriber_name', models.CharField(max_length=255)),
                    ],
                    options={
                        'ordering': ['subscriber_name'],
                        'db_table': 'update_subscriber',
                    },
                ),
                migrations.CreateModel(
                    name='UserSubscriberProfile',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('bound_at', models.DateTimeField(auto_now_add=True)),
                        ('user', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='subscriber_profile', to=settings.AUTH_USER_MODEL)),
                        ('subscriber', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='profiles', to='acctmgt.subscriber')),
                    ],
                    options={
                        'db_table': 'update_usersubscriberprofile',
                    },
                ),
                migrations.CreateModel(
                    name='SubscriberToken',
                    fields=[
                        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                        ('token', models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
                        ('is_used', models.BooleanField(default=False)),
                        ('created_at', models.DateTimeField(auto_now_add=True)),
                        ('subscriber', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='tokens', to='acctmgt.subscriber')),
                        ('created_by', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='issued_tokens', to=settings.AUTH_USER_MODEL)),
                    ],
                    options={
                        'db_table': 'update_subscribertoken',
                    },
                ),
            ],
            database_operations=[],
        ),
    ]
