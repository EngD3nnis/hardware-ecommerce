from django.contrib import admin

from .models import PriceList, ProductPrice


@admin.register(PriceList)
class PriceListAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "currency", "is_default", "prices_include_vat", "is_active")
    prepopulated_fields = {"code": ("name",)}


@admin.register(ProductPrice)
class ProductPriceAdmin(admin.ModelAdmin):
    """Price history, read-only. Prices are changed from the product page (audited service)."""

    list_display = ("product", "price_list", "amount", "valid_from", "valid_to", "source", "created_by")
    list_filter = ("price_list", "source")
    search_fields = ("product__sku", "product__name")
    date_hierarchy = "valid_from"
    readonly_fields = [f.name for f in ProductPrice._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
