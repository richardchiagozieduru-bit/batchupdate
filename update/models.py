from django.db import models
from django.contrib.auth.models import User
import uuid

# Models moved to acctmgt — re-exported here for backward compatibility
from acctmgt.models import (  # noqa: F401
    BatchSubscriber,
    Subscriber,
    SubscriberToken,
    UserSubscriberProfile,
)
from .columns import TARGET_COLUMN_CHOICES


class UploadSession(models.Model):
    """Represents a single file upload session"""
    STATUS_CHOICES = [
        ('pending_mapping', 'Pending Mapping'),
        ('processing', 'Processing'),
        ('processed', 'Processed'),
        ('uploaded', 'Uploaded to SQL'),
        ('error', 'Error'),
    ]
    
    user = models.ForeignKey(User, on_delete=models.CASCADE)
    original_file = models.FileField(upload_to='uploads/')
    original_filename = models.CharField(max_length=255)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending_mapping')
    processed_file = models.FileField(upload_to='processed/', blank=True, null=True)
    rejected_file = models.FileField(upload_to='processed/', blank=True, null=True)
    rows_processed = models.IntegerField(default=0)
    rows_uploaded = models.IntegerField(default=0)
    rows_rejected = models.IntegerField(default=0)
    error_message = models.TextField(blank=True)
    sheet_name = models.CharField(max_length=255, blank=True)
    generated_script = models.FileField(upload_to='generated_scripts/', blank=True, null=True)
    batchupdate_uploaded = models.BooleanField(default=False)
    batch_id = models.UUIDField(null=True, blank=True, db_index=True)
    source_filename = models.CharField(max_length=255, blank=True)  # original Excel filename for batch
    header_row = models.IntegerField(default=0)  # 0-based row index of the header in the uploaded file
    subscriber = models.ForeignKey(
        'acctmgt.Subscriber', on_delete=models.SET_NULL, null=True, blank=True, related_name='sessions'
    )
    
    class Meta:
        ordering = ['-uploaded_at']
    
    def __str__(self):
        return f"{self.original_filename} - {self.status}"


class ColumnMapping(models.Model):
    """Maps Excel headers to target columns"""

    session = models.ForeignKey(UploadSession, on_delete=models.CASCADE, related_name='mappings')
    original_header = models.CharField(max_length=255)
    target_column = models.CharField(max_length=50, choices=TARGET_COLUMN_CHOICES, blank=True)
    
    class Meta:
        unique_together = ['session', 'original_header']
    
    def __str__(self):
        return f"{self.original_header} -> {self.target_column or 'unmapped'}"


class MappingTemplate(models.Model):
    """Saved mapping templates for automatic column detection"""
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='mapping_templates')
    name = models.CharField(max_length=100)
    header_signature = models.TextField()  # JSON list of original column names
    mappings = models.JSONField()  # {original_header: target_column}
    created_at = models.DateTimeField(auto_now_add=True)
    use_count = models.IntegerField(default=0)
    
    class Meta:
        ordering = ['-use_count', '-created_at']
    
    def __str__(self):
        return f"{self.name} ({self.use_count} uses)"
