from django.urls import path
from . import views

urlpatterns = [
    path('checkout/<int:order_id>/', views.checkout_view, name='payment_checkout'),
    path('process/<int:order_id>/', views.process_payment_view, name='payment_process'),
    path('return/', views.payment_return_view, name='payment_return'),
    path('webhook/', views.paymongo_webhook_view, name='paymongo_webhook'),
    path('refund/<int:order_id>/', views.initiate_refund_view, name='initiate_refund'),
]
