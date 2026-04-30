from django.shortcuts import render, redirect
from django.contrib.auth import login, authenticate, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User, Group
from django.contrib import messages
from django.db import transaction
from django.views.decorators.cache import never_cache

from .models import Subscriber, SubscriberToken, UserSubscriberProfile
from .utils import is_external, require_bound, rate_limit
from update.services import get_subscribers_from_batchupdate

# Private aliases kept for any legacy imports
_is_external = is_external
_require_bound = require_bound


@never_cache
@rate_limit(max_attempts=5, window=3600)  # 5 registration attempts per hour per IP
def register_view(request):
    """External user self-registration."""
    if request.user.is_authenticated:
        return redirect('upload')

    if request.method == 'POST':
        username = request.POST.get('username', '').strip()
        email = request.POST.get('email', '').strip()
        password = request.POST.get('password', '')
        password2 = request.POST.get('password2', '')

        if not username or not password:
            messages.error(request, 'Username and password are required.')
        elif not email:
            messages.error(request, 'Email address is required for password recovery.')
        elif password != password2:
            messages.error(request, 'Passwords do not match.')
        elif User.objects.filter(username=username).exists():
            messages.error(request, 'Username already taken.')
        else:
            user = User.objects.create_user(username=username, email=email, password=password)
            external_group, _ = Group.objects.get_or_create(name='external')
            user.groups.add(external_group)
            login(request, user)
            return redirect('redeem_token')

    return render(request, 'acctmgt/register.html')


@login_required
@never_cache
def redeem_token_view(request):
    """Bind an external user to a subscriber via a one-time token."""
    if hasattr(request.user, 'subscriber_profile'):
        return redirect('upload')

    subscribers = get_subscribers_from_batchupdate()

    if request.method == 'POST':
        raw_sub_id = request.POST.get('subscriber', '').strip()
        raw_token = request.POST.get('token', '').strip()

        try:
            sub_id_int = int(float(raw_sub_id))
        except (ValueError, TypeError):
            messages.error(request, 'Invalid subscriber selected.')
            return render(request, 'acctmgt/redeem_token.html', {'subscribers': subscribers})

        sub_match = next((s for s in subscribers if s['subscriber_id'] == sub_id_int), None)
        if not sub_match:
            messages.error(request, 'Invalid subscriber selected.')
            return render(request, 'acctmgt/redeem_token.html', {'subscribers': subscribers})

        selected_sub, _ = Subscriber.objects.get_or_create(
            subscriber_id=sub_id_int,
            defaults={'subscriber_name': sub_match['subscriber_name']},
        )

        try:
            token_obj = SubscriberToken.objects.select_related('subscriber').get(
                token=raw_token, is_used=False
            )
        except (SubscriberToken.DoesNotExist, ValueError):
            messages.error(request, 'Token is invalid or has already been used.')
            return render(request, 'acctmgt/redeem_token.html', {'subscribers': subscribers})

        if token_obj.subscriber.subscriber_id != sub_id_int:
            messages.error(request, 'Token does not match the selected subscriber.')
            return render(request, 'acctmgt/redeem_token.html', {'subscribers': subscribers})

        with transaction.atomic():
            UserSubscriberProfile.objects.create(user=request.user, subscriber=selected_sub)
            token_obj.is_used = True
            token_obj.save(update_fields=['is_used'])

        messages.success(request, f'Bound to {selected_sub.subscriber_name}. You can now upload files.')
        return redirect('upload')

    return render(request, 'acctmgt/redeem_token.html', {'subscribers': subscribers})


@never_cache
@rate_limit(max_attempts=10, window=300)  # 10 login attempts per 5 minutes per IP
def login_view(request):
    """User login page."""
    if request.user.is_authenticated:
        if _require_bound(request.user):
            return redirect('redeem_token')
        return redirect('upload')

    if request.method == 'POST':
        username = request.POST.get('username')
        password = request.POST.get('password')
        user = authenticate(request, username=username, password=password)

        if user is not None:
            login(request, user)
            return redirect('upload')
        else:
            messages.error(request, 'Invalid username or password')

    return render(request, 'acctmgt/login.html')


def logout_view(request):
    """Logout user."""
    logout(request)
    return redirect('login')
