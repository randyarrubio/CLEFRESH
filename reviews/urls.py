from django.urls import path
from . import views

urlpatterns = [
    path('orders/<int:order_id>/review/', views.submit_review_view, name='submit_review'),
]
