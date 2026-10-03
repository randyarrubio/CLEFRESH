from django.urls import path
from . import views
from orders.views import api_order_status_view
from maps.views import geocode_proxy_view, update_rider_location_view

urlpatterns = [
    path('notifications/', views.notifications_api_view, name='api_notifications'),
    path('notifications/stream/', views.notifications_stream_view, name='api_notifications_stream'),
    path('notifications/read/', views.mark_notifications_read_view, name='mark_notifications_read'),
    path('orders/<int:order_id>/status/', api_order_status_view, name='api_order_status_v2'),
    path('geocode/', geocode_proxy_view, name='api_geocode'),
    path('rider/<int:rider_id>/location/', update_rider_location_view, name='api_rider_location'),
]
