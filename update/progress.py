"""
Cache-based progress tracking for async tasks.

Two separate cache keys per session — one for the cleaning phase, one for the
DB upload phase — so the frontend can poll each independently.
"""
from django.core.cache import caches

progress_cache = caches['progress'] if 'progress' in caches else caches['default']


def set_progress(session_id, phase, *, step='', percent=0, detail=''):
    """
    Write progress for a given phase.

    Args:
        session_id: UploadSession PK.
        phase:      'clean' or 'dbupload'.
        step:       Human-readable description of the current step.
        percent:    0–100 integer.
        detail:     Optional extra detail (e.g. "50,000 of 200,000 rows").
    """
    progress_cache.set(f'progress:{phase}:{session_id}', {
        'step': step,
        'percent': percent,
        'detail': detail,
    }, timeout=3600)


def get_progress(session_id, phase):
    """Read progress for a given phase; returns sensible defaults if not set."""
    return progress_cache.get(f'progress:{phase}:{session_id}', {
        'step': 'Waiting...',
        'percent': 0,
        'detail': '',
    })


def clear_progress(session_id, phase):
    """Remove progress entry once the phase is done."""
    progress_cache.delete(f'progress:{phase}:{session_id}')
