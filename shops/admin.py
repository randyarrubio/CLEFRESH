from django.contrib import admin
from .models import DeliverySlot, Promotion, Service, Shop


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ('name', 'owner', 'status', 'created_at')
    list_filter = ('status',)
    search_fields = ('name', 'owner__email')


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = ('shop', 'name', 'price_per_kg', 'price_per_piece')


@admin.register(DeliverySlot)
class DeliverySlotAdmin(admin.ModelAdmin):
    list_display = ('shop', 'date', 'time_start', 'time_end', 'current_bookings', 'max_capacity')



@admin.register(Promotion)
class PromotionAdmin(admin.ModelAdmin):
    list_display = ('title', 'shop', 'discount_type', 'discount_value', 'start_date', 'end_date', 'is_active')
    list_filter = ('discount_type', 'is_active')
    search_fields = ('title', 'shop__name')
