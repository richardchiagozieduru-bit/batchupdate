from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from .models import DroppedFile
from acctmgt.utils import is_internal_dropper


def drop_folder_context(request):
    """
    Context processor injecting drop folder stats for authenticated users:
    - pending_drop_count: number of files waiting in the drop folder (for staff)
    - is_internal_dropper: boolean flag for template conditional rendering
    """
    if not request.user.is_authenticated:
        return {'pending_drop_count': 0, 'is_internal_dropper': False}

    context = {
        'is_internal_dropper': is_internal_dropper(request.user),
    }

    if request.user.is_staff:
        try:
            retention_days = getattr(settings, 'DROPPED_FILE_RETENTION_DAYS', 2)
            cutoff = timezone.now() - timedelta(days=retention_days)
            context['pending_drop_count'] = DroppedFile.objects.filter(
                status='pending',
                dropped_at__gte=cutoff
            ).count()
        except Exception:
            context['pending_drop_count'] = 0
    else:
        context['pending_drop_count'] = 0

    return context
