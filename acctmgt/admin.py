from django.contrib import admin
from django.contrib import messages
from django.db import transaction
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html

from .models import Subscriber, SubscriberToken, UserSubscriberProfile, BatchSubscriber
from update.services import get_subscribers_from_batchupdate


@admin.register(Subscriber)
class SubscriberAdmin(admin.ModelAdmin):
    list_display = ['subscriber_id', 'subscriber_name', 'active_tokens']
    search_fields = ['subscriber_name', 'subscriber_id']

    @admin.display(description='Active Tokens')
    def active_tokens(self, obj):
        return obj.tokens.filter(is_used=False).count()


@admin.register(SubscriberToken)
class SubscriberTokenAdmin(admin.ModelAdmin):
    list_display = ['token', 'subscriber', 'is_used', 'created_by', 'created_at']
    list_filter = ['is_used', 'subscriber']
    search_fields = ['subscriber__subscriber_name']
    readonly_fields = ['token', 'created_at', 'created_by', 'is_used']

    def has_add_permission(self, request):
        return False

    change_list_template = 'admin/update/subscribertoken/change_list.html'

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                'generate-tokens/',
                self.admin_site.admin_view(self.generate_tokens_view),
                name='update_subscribertoken_generate',
            ),
            path(
                'subscriber-search/',
                self.admin_site.admin_view(self.subscriber_search_api),
                name='update_subscribertoken_subscriber_search',
            ),
        ]
        return custom + urls

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    def subscriber_search_api(self, request):
        """Return subscribers from Sheet1 matching the search query."""
        q = request.GET.get('q', '').strip().lower()
        subscribers = get_subscribers_from_batchupdate()
        if q:
            subscribers = [
                s for s in subscribers
                if q in s['subscriber_name'].lower() or q in str(s['subscriber_id'])
            ]
        return JsonResponse({'results': subscribers[:50]})

    def generate_tokens_view(self, request):
        """Admin page to search/select subscribers and generate tokens."""
        subscribers = get_subscribers_from_batchupdate()

        if request.method == 'POST':
            selected_ids = request.POST.getlist('subscriber_ids')
            if not selected_ids:
                messages.error(request, 'Please select at least one subscriber.')
                return render(request, 'admin/update/subscribertoken/generate_tokens.html', {
                    'subscribers': subscribers,
                    'opts': self.model._meta,
                    'title': 'Generate Subscriber Tokens',
                })

            created_count = 0
            sub_lookup = {s['subscriber_id']: s['subscriber_name'] for s in subscribers}

            for raw_id in selected_ids:
                try:
                    sub_id = int(float(raw_id))
                except (ValueError, TypeError):
                    continue

                sub_name = sub_lookup.get(sub_id)
                if not sub_name:
                    continue

                subscriber, _ = Subscriber.objects.get_or_create(
                    subscriber_id=sub_id,
                    defaults={'subscriber_name': sub_name},
                )
                SubscriberToken.objects.create(
                    subscriber=subscriber,
                    created_by=request.user,
                )
                created_count += 1

            if created_count:
                messages.success(
                    request,
                    f'Generated {created_count} token(s). Copy them from the list below.',
                )
            return HttpResponseRedirect(reverse('admin:acctmgt_subscribertoken_changelist'))

        return render(request, 'admin/update/subscribertoken/generate_tokens.html', {
            'subscribers': subscribers,
            'opts': self.model._meta,
            'title': 'Generate Subscriber Tokens',
        })


@admin.register(UserSubscriberProfile)
class UserSubscriberProfileAdmin(admin.ModelAdmin):
    list_display = ['user', 'subscriber', 'bound_at']
    list_filter = ['subscriber']
    search_fields = ['user__username', 'subscriber__subscriber_name']
    readonly_fields = ['bound_at']


@admin.register(BatchSubscriber)
class BatchSubscriberAdmin(admin.ModelAdmin):
    list_display = ['subscriber_id', 'subscriber_name']
    search_fields = ['subscriber_name', 'subscriber_id']
    ordering = ['subscriber_name']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
