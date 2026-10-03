from django.contrib import admin
from .models import Order, OrderService, OrderStatusLog, PhotoProof, EmailLog


class OrderServiceInline(admin.TabularInline):
    model = OrderService
    extra = 0


class OrderStatusLogInline(admin.TabularInline):
    model = OrderStatusLog
    extra = 0
    readonly_fields = ('timestamp',)


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'customer', 'shop', 'status', 'payment_method', 'payment_status', 'total_amount', 'created_at')
    list_filter = ('status', 'payment_method', 'payment_status')
    inlines = [OrderServiceInline, OrderStatusLogInline]
    search_fields = ('customer__email', 'shop__name')


@admin.register(EmailLog)
class EmailLogAdmin(admin.ModelAdmin):
    list_display = ('recipient_email', 'subject', 'triggered_by', 'formspree_response_status', 'sent_at')
    list_filter = ('triggered_by',)
