from django.urls import path
from . import views

app_name = 'extraction'

urlpatterns = [
    path('general/', views.general_extraction_view, name='general'),
    path('general/search/', views.subscriber_search_api, name='subscriber_search'),
    path('general/start/', views.start_extraction_view, name='start'),
    path('general/batch/<uuid:batch_id>/', views.extraction_progress_view, name='batch_progress'),
    path('general/batch/<uuid:batch_id>/status/', views.extraction_batch_status_api, name='batch_status'),
    path('general/download/<uuid:batch_id>/', views.download_extraction_view, name='download_batch'),
    path('general/download-subscriber/<int:session_id>/', views.download_subscriber_zip_view, name='download_subscriber'),
    path('general/retry/<int:session_id>/', views.retry_extraction_view, name='retry'),
    path('general/cancel/<uuid:batch_id>/', views.cancel_batch_view, name='cancel_batch'),
    path('general/cancel-subscriber/<int:session_id>/', views.cancel_session_view, name='cancel_session'),
    path('general/delete/<uuid:batch_id>/', views.delete_batch_view, name='delete_batch'),
]
