from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = ('email', 'first_name', 'last_name', 'role', 'sso_provider', 'is_active')
    list_filter = ('role', 'is_active', 'sso_provider')
    fieldsets = UserAdmin.fieldsets + (
        ('CLEFRESH Profile', {'fields': ('role', 'contact_number', 'address', 'profile_picture', 'sso_provider', 'email_notifications_enabled')}),
    )
    search_fields = ('email', 'first_name', 'last_name')
    ordering = ('email',)
