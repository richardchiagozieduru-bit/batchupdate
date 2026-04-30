from django.db import models
from django.contrib.auth.models import User
import uuid


class BatchSubscriber(models.Model):
    """Read-only ORM view of the existing Sheet1 table in BatchUpdate. Not managed by Django."""
    subscriber_id = models.BigIntegerField(primary_key=True, db_column='SubscriberID')
    subscriber_name = models.CharField(max_length=500, db_column='SubscriberName')

    class Meta:
        managed = False
        db_table = 'Sheet1'
        ordering = ['subscriber_name']

    def __str__(self):
        return f"{self.subscriber_id} — {self.subscriber_name}"


class Subscriber(models.Model):
    """Lightweight FK anchor for tokens, profiles, and upload sessions.
    Populated on-demand via get_or_create when a subscriber is first selected."""
    subscriber_id = models.IntegerField(unique=True)
    subscriber_name = models.CharField(max_length=255)

    class Meta:
        ordering = ['subscriber_name']
        db_table = 'update_subscriber'  # preserve existing DB table

    def __str__(self):
        return f"{self.subscriber_id} — {self.subscriber_name}"


class UserSubscriberProfile(models.Model):
    """Binds an external user to a single subscriber via token redemption."""
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='subscriber_profile')
    subscriber = models.ForeignKey(Subscriber, on_delete=models.PROTECT, related_name='profiles')
    bound_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'update_usersubscriberprofile'  # preserve existing DB table

    def __str__(self):
        return f"{self.user.username} → {self.subscriber}"


class SubscriberToken(models.Model):
    """One-time token issued by admin to bind an external user to a subscriber."""
    token = models.UUIDField(unique=True, default=uuid.uuid4, editable=False)
    subscriber = models.ForeignKey(Subscriber, on_delete=models.CASCADE, related_name='tokens')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='issued_tokens')
    is_used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'update_subscribertoken'  # preserve existing DB table

    def __str__(self):
        status = 'used' if self.is_used else 'active'
        return f"Token for {self.subscriber} [{status}]"
