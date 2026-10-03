from django.urls import path
from . import views

urlpatterns = [
    path('', views.rider_dashboard_view, name='rider_dashboard'),
    path('tasks/<int:order_id>/accept/', views.rider_accept_job_view, name='rider_accept_job'),
    path('tasks/<int:order_id>/', views.rider_task_detail_view, name='rider_task_detail'),
    path('tasks/<int:order_id>/status/', views.rider_update_status_view, name='rider_update_status'),
    path('tasks/<int:order_id>/proof/', views.upload_proof_view, name='upload_proof'),
    path('api/rider/<int:rider_id>/location/', views.update_rider_location_view, name='update_rider_location'),
    path('api/geocode/', views.geocode_proxy_view, name='geocode_proxy'),
]
