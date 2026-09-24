from django.db import models
from django.contrib.auth.models import User
import uuid


class ExtractionBatch(models.Model):
    """
    Parent container for a bulk extraction request.
    One batch can contain multiple subscriber extraction sessions.
    """
    STATUS_CHOICES = [
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('error', 'Error'),
    ]

    batch_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='extraction_batches')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='processing')
    include_consumer = models.BooleanField(default=True)
    include_commercial = models.BooleanField(default=True)
    master_zip = models.FileField(upload_to='extractions/zips/', null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"ExtractionBatch {self.batch_id} — {self.status}"


class ExtractionSession(models.Model):
    """
    Tracks the extraction of a single subscriber within a batch.
    Each selected subscriber gets its own session with independent
    status tracking and error isolation.
    """
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('error', 'Error'),
    ]

    batch = models.ForeignKey(
        ExtractionBatch, on_delete=models.CASCADE, related_name='sessions'
    )
    subscriber_id = models.IntegerField()
    subscriber_name = models.CharField(max_length=255)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    consumer_rows = models.IntegerField(default=0)
    commercial_rows = models.IntegerField(default=0)
    agric_count = models.IntegerField(default=0)
    subscriber_zip = models.FileField(upload_to='extractions/zips/', null=True, blank=True)
    error_message = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['subscriber_name']

    def __str__(self):
        return f"{self.subscriber_name} (ID:{self.subscriber_id}) — {self.status}"
