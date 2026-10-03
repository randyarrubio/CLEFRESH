from django.urls import path
from . import views

urlpatterns = [
    path('', views.dashboard_view, name='shop_dashboard'),
    path('register/', views.register_shop_view, name='register_shop'),
    path('edit/', views.edit_shop_view, name='edit_shop'),
    path('orders/', views.dashboard_orders_view, name='shop_dashboard_orders'),
    path('orders/<int:order_id>/', views.dashboard_order_detail_view, name='shop_dashboard_order_detail'),
    path('orders/<int:order_id>/status/', views.update_order_status_view, name='update_order_status'),
    path('orders/<int:order_id>/assign-rider/', views.assign_rider_view, name='assign_rider'),
    path('orders/<int:order_id>/confirm-weight/', views.confirm_order_weight_view, name='confirm_order_weight'),
    path('slots/', views.dashboard_slots_view, name='shop_dashboard_slots'),
    path('slots/create/', views.create_slot_view, name='create_slot'),
    path('slots/<int:slot_id>/delete/', views.delete_slot_view, name='delete_slot'),
    path('services/', views.dashboard_services_view, name='shop_dashboard_services'),
    path('services/create/', views.create_service_view, name='create_service'),
    path('services/<int:service_id>/delete/', views.delete_service_view, name='delete_service'),
    path('promos/', views.dashboard_promos_view, name='shop_dashboard_promos'),
    path('promos/<int:promo_id>/toggle/', views.toggle_promo_view, name='toggle_promo'),
    path('promos/<int:promo_id>/delete/', views.delete_promo_view, name='delete_promo'),
    path('analytics/', views.dashboard_analytics_view, name='shop_dashboard_analytics'),
    path('payments/', views.dashboard_payments_view, name='shop_dashboard_payments'),
]
