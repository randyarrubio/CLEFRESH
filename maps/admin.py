from django.contrib import admin
from .models import RiderLocation


@admin.register(RiderLocation)
class RiderLocationAdmin(admin.ModelAdmin):
    list_display = ('rider', 'latitude', 'longitude', 'updated_at')
