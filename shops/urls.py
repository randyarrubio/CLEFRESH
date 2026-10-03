from django.urls import path
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    path('', views.home_view, name='home'),
    path('about/', RedirectView.as_view(url='/#about-us', permanent=False), name='about'),
    path('info/<slug:slug>/', views.info_page_view, name='info_page'),
    path('shops/', views.shop_list_view, name='shop_list'),
    path('shops/<int:shop_id>/', views.shop_detail_view, name='shop_detail'),
    path('shops/<int:shop_id>/services/', views.shop_site_view, {'page': 'services'}, name='shop_site_services'),
    path('shops/<int:shop_id>/how-it-works/', views.shop_site_view, {'page': 'how_it_works'}, name='shop_site_how_it_works'),
    path('shops/<int:shop_id>/about/', views.shop_site_view, {'page': 'about'}, name='shop_site_about'),
    path('api/slots/<int:shop_id>/', views.api_slots_view, name='api_slots'),
    path('shops/<int:shop_id>/docs/<str:field>/', views.serve_shop_doc_view, name='serve_shop_doc'),
]
