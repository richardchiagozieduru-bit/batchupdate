from django.urls import path
from . import views

app_name = 'unupdated'

urlpatterns = [
    path('', views.index, name='index'),
    path('batch/<uuid:batch_id>/', views.batch_progress, name='batch_progress'),
    path('batch/<uuid:batch_id>/status/', views.batch_status_api, name='batch_status_api'),
    path('batch/<uuid:batch_id>/cancel/', views.cancel_batch, name='cancel_batch'),
    path('batch/<uuid:batch_id>/delete/', views.delete_batch, name='delete_batch'),
    path('download/<uuid:batch_id>/', views.download_master_zip, name='download_master_zip'),
    path('download/session/<int:session_id>/', views.download_session_file, name='download_session'),
    path('api/subscribers/', views.subscriber_search_api, name='subscriber_search_api'),
    path('api/ungenerated-subscribers/', views.check_ungenerated_subscribers_api, name='check_ungenerated_subscribers_api'),
    path('api/generate-ungenerated/', views.dispatch_ungenerated_batch, name='dispatch_ungenerated_batch'),
]
