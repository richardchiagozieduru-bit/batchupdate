"""Shared helper utilities for acctmgt — safe to import from any app."""
from functools import wraps
from django.core.cache import cache
from django.http import HttpResponse


class HttpResponseTooManyRequests(HttpResponse):
    status_code = 429


def is_external(user):
    """Return True if user belongs to the 'external' group."""
    return user.groups.filter(name='external').exists()


def require_bound(user):
    """Return True if an external user still needs to redeem a token."""
    if not is_external(user):
        return False
    return not hasattr(user, 'subscriber_profile')


def rate_limit(max_attempts=10, window=300):
    """
    View decorator: limit POST requests by IP address.

    Args:
        max_attempts: maximum POST requests allowed within the window
        window:       time window in seconds (default 5 minutes)

    Only counts POST requests so normal page loads are not throttled.
    Uses the Django cache backend — no extra packages required.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if request.method == 'POST':
                ip = request.META.get('REMOTE_ADDR', 'unknown')
                key = f'ratelimit:{view_func.__name__}:{ip}'
                count = cache.get(key, 0)
                if count >= max_attempts:
                    return HttpResponseTooManyRequests(
                        'Too many requests. Please wait a few minutes before trying again.'
                    )
                cache.set(key, count + 1, window)
            return view_func(request, *args, **kwargs)
        return wrapped
    return decorator
