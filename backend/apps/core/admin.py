from django.contrib import admin

from .models import BusinessProfile


@admin.register(BusinessProfile)
class BusinessProfileAdmin(admin.ModelAdmin):
    """The single business profile row: edit it, never add or delete."""

    fieldsets = (
        ("Business", {"fields": ("business_name", "legal_name", "kra_pin")}),
        ("Contact", {"fields": ("whatsapp_sales_number", "other_phone_numbers", "email", "website_url")}),
        ("Location & hours", {"fields": ("address", "map_url", "opening_hours")}),
        ("Sales settings", {"fields": ("show_prices_online", "quote_validity_days")}),
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
