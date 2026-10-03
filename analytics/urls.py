from django.urls import path
from . import views

urlpatterns = [
    path('', views.admin_dashboard_view, name='admin_dashboard'),
    path('shops/', views.admin_shops_view, name='admin_shops'),
    path('shops/<int:shop_id>/action/', views.admin_approve_shop_view, name='admin_shop_action'),
    path('users/', views.admin_users_view, name='admin_users'),
    path('users/<int:user_id>/toggle/', views.admin_toggle_user_view, name='admin_toggle_user'),
    path('users/<int:user_id>/delete/', views.admin_delete_user_view, name='admin_delete_user'),
    path('riders/', views.admin_riders_view, name='admin_riders'),
    path('riders/<int:profile_id>/action/', views.admin_rider_action_view, name='admin_rider_action'),
    path('orders/', views.admin_orders_view, name='admin_orders'),
    path('transactions/', views.admin_transactions_view, name='admin_transactions'),
    path('analytics/', views.admin_analytics_view, name='admin_analytics'),
    path('broadcast/', views.admin_broadcast_view, name='admin_broadcast'),
]
