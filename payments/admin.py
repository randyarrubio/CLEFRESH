from django.contrib import admin
from .models import PayMongoTransaction, Refund


@admin.register(PayMongoTransaction)
class PayMongoTransactionAdmin(admin.ModelAdmin):
    list_display = ('order', 'status', 'amount', 'currency', 'created_at')
    list_filter = ('status', 'currency')
    readonly_fields = ('raw_payload',)


@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = ('order', 'amount', 'reason', 'status', 'initiated_by', 'created_at')
    list_filter = ('status',)
