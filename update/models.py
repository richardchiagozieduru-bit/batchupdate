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
        ('uploading_to_db', 'Uploading to DB'),
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
    upload_to_db = models.BooleanField(default=False)
    content_fingerprint = models.CharField(max_length=64, blank=True, db_index=True)
    
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


class DroppedFile(models.Model):
    """Represents a file dropped into the flat drop folder by an internal dropper."""
    STATUS_CHOICES = [
        ('pending', 'Pending Import'),
        ('imported', 'Imported'),
        ('deleted', 'Deleted'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dropped_files')
    subscriber = models.ForeignKey(
        'acctmgt.Subscriber', on_delete=models.SET_NULL, null=True, blank=True, related_name='dropped_files'
    )
    file = models.FileField(upload_to='drop_folder/')
    original_filename = models.CharField(max_length=255)
    file_size_bytes = models.BigIntegerField(default=0)
    notes = models.TextField(blank=True)
    dropped_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    imported_at = models.DateTimeField(null=True, blank=True)
    imported_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='imported_drops')

    class Meta:
        ordering = ['-dropped_at']

    def __str__(self):
        return f"{self.original_filename} ({self.user.username}) - {self.status}"

    @property
    def formatted_size(self):
        """Human-readable file size."""
        bytes_val = self.file_size_bytes or 0
        if bytes_val >= 1024 * 1024:
            return f"{bytes_val / (1024 * 1024):.1f} MB"
        elif bytes_val >= 1024:
            return f"{bytes_val / 1024:.1f} KB"
        return f"{bytes_val} B"


class ImplementedArtifact(models.Model):
    """
    Durable registry of datasets that have been stream-uploaded to BatchUpdate SQL Server.
    Survives the 30-day session retention cleanup to enforce global duplicate detection.
    """
    content_fingerprint = models.CharField(max_length=64, db_index=True, unique=True)
    table_name = models.CharField(max_length=64)
    subscriber = models.ForeignKey(
        'acctmgt.Subscriber', on_delete=models.SET_NULL, null=True, blank=True, related_name='implemented_artifacts'
    )
    rows_uploaded = models.IntegerField(default=0)
    implemented_at = models.DateTimeField(auto_now_add=True)
    implemented_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name='implemented_artifacts'
    )
    source_session = models.ForeignKey(
        UploadSession, on_delete=models.SET_NULL, null=True, blank=True, related_name='implemented_artifacts'
    )
    is_historical_backfill = models.BooleanField(default=False)

    class Meta:
        ordering = ['-implemented_at']

    def __str__(self):
        return f"[{self.table_name}] ({self.rows_uploaded:,} rows) - {self.implemented_at.strftime('%Y-%m-%d')}"

