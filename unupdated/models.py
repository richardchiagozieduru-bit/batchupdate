import uuid
from django.db import models
from django.contrib.auth.models import User


class UnupdatedBatch(models.Model):
    """
    Parent container for a batch unupdated accounts audit request.
    One batch can encompass multiple subscribers and both Consumer/Commercial types.
    """
    STATUS_CHOICES = [
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('error', 'Error'),
    ]

    batch_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='unupdated_batches')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='processing')
    include_consumer = models.BooleanField(default=True)
    include_commercial = models.BooleanField(default=True)
    reporting_month = models.CharField(max_length=7, default='', help_text="e.g. 2026-07")
    last_period_num = models.CharField(max_length=8, default='', help_text="e.g. 20260731")
    cutoff_date = models.CharField(max_length=10, default='', help_text="e.g. 2026-08-01")
    master_zip = models.FileField(upload_to='unupdated/zips/', max_length=500, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"UnupdatedBatch {self.batch_id} ({self.reporting_month}) — {self.status}"


class UnupdatedSession(models.Model):
    """
    Tracks the unupdated records extraction for a single subscriber in a batch.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('pending_upload', 'Awaiting Upload'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('error', 'Error'),
    ]

    batch = models.ForeignKey(
        UnupdatedBatch, on_delete=models.CASCADE, related_name='sessions'
    )
    subscriber_id = models.IntegerField()
    subscriber_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    is_file_ready = models.BooleanField(default=False)
    consumer_max_period = models.CharField(max_length=8, blank=True, default='', help_text="e.g. 20260731")
    commercial_max_period = models.CharField(max_length=8, blank=True, default='', help_text="e.g. 20260731")
    consumer_rows = models.IntegerField(default=0)
    commercial_rows = models.IntegerField(default=0)
    subscriber_file = models.FileField(upload_to='unupdated/reports/', max_length=500, null=True, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['subscriber_name']

    def __str__(self):
        return f"{self.subscriber_name} (ID:{self.subscriber_id}) — {self.status}"
