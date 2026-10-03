from django.urls import path
from . import views

urlpatterns = [
    path('register/', views.register_view, name='register'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),
    path('check-email/', views.check_email_view, name='check_email'),
    path('verify-email/', views.verify_email_view, name='verify_email'),
    path('verify-email/resend/', views.resend_verify_code_view, name='verify_email_resend'),
    path('sso/verify-email/', views.sso_verify_email_view, name='sso_verify_email'),
    path('sso/verify-email/resend/', views.sso_resend_verify_code_view, name='sso_verify_email_resend'),
    path('profile/', views.profile_view, name='profile'),
    path('rider/onboarding/', views.rider_onboarding_view, name='rider_onboarding'),
    path('forgot-password/', views.forgot_password_view, name='forgot_password'),
    path('forgot-password/done/', views.password_reset_done_view, name='password_reset_done'),
    path('reset-password/<uidb64>/<token>/', views.reset_password_confirm_view, name='password_reset_confirm'),
    path('media/rider-docs/<str:filename>', views.serve_rider_doc_view, name='serve_rider_doc'),
]
