from django.urls import path
from . import views

urlpatterns = [
    path('', views.order_list_view, name='order_list'),
    path('<int:order_id>/', views.order_detail_view, name='order_detail'),
    path('<int:order_id>/cancel/', views.cancel_order_view, name='cancel_order'),
    path('<int:order_id>/pay-on-delivery/', views.switch_to_cod_view, name='switch_to_cod'),
    path('<int:order_id>/cash-received/', views.confirm_cash_received_view, name='confirm_cash_received'),
    path('place/<int:shop_id>/', views.place_order_view, name='place_order'),
    path('api/orders/<int:order_id>/status/', views.api_order_status_view, name='api_order_status'),
]
