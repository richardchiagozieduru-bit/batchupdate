from django.urls import path
from . import views

urlpatterns = [
    path('', views.upload_view, name='upload'),
    path('mapping/<int:session_id>/', views.mapping_view, name='mapping'),
    path('process/<int:session_id>/', views.process_view, name='process'),
    path('result/<int:session_id>/', views.result_view, name='result'),
    path('download/<int:session_id>/', views.download_view, name='download'),
    path('download-rejected/<int:session_id>/', views.download_rejected_view, name='download_rejected'),
    path('download-script/<int:session_id>/', views.download_script_view, name='download_script'),
    path('batch/<uuid:batch_id>/', views.batch_view, name='batch'),
    path('batch/<uuid:batch_id>/mapping/', views.batch_mapping_view, name='batch_mapping'),
    path('batch/<uuid:batch_id>/progress/', views.batch_progress_view, name='batch_progress'),
    path('batch/<uuid:batch_id>/download/', views.download_batch_combined, name='download_batch_combined'),
    path('batch/<uuid:batch_id>/scripts/', views.download_batch_scripts_zip, name='download_batch_scripts_zip'),
    path('batch/<uuid:batch_id>/delete/', views.delete_batch_view, name='delete_batch'),
    path('undo/<int:session_id>/', views.undo_upload_view, name='undo_upload'),
    path('progress/<int:session_id>/', views.task_progress_view, name='task_progress'),
    path('delete/<int:session_id>/', views.delete_session_view, name='delete_session'),
]
