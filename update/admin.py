from django.contrib import admin
from .models import UploadSession, ColumnMapping, MappingTemplate


@admin.register(UploadSession)
class UploadSessionAdmin(admin.ModelAdmin):
    list_display = ['original_filename', 'user', 'subscriber', 'status', 'rows_processed', 'rows_uploaded', 'uploaded_at']
    list_filter = ['status', 'uploaded_at', 'user']
    search_fields = ['original_filename', 'user__username']
    readonly_fields = ['uploaded_at']
    ordering = ['-uploaded_at']


@admin.register(ColumnMapping)
class ColumnMappingAdmin(admin.ModelAdmin):
    list_display = ['session', 'original_header', 'target_column']
    list_filter = ['target_column']
    search_fields = ['original_header']


@admin.register(MappingTemplate)
class MappingTemplateAdmin(admin.ModelAdmin):
    list_display = ['name', 'user', 'use_count', 'created_at']
    list_filter = ['user', 'created_at']
    search_fields = ['name']
