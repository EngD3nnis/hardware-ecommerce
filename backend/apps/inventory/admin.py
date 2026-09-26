"""Inventory admin: balances and ledger are read-only; changes go through adjustments and counts."""

import csv
import io

from django import forms
from django.contrib import admin, messages
from django.db.models import F

from apps.core.actors import Actor
from apps.core.exceptions import DomainError

from . import services
from .models import (
    AdjustmentKind,
    CountStatus,
    Reservation,
    ReservationStatus,
    StockAdjustment,
    StockBalance,
    StockCount,
    StockCountLine,
    StockLocation,
    StockMovement,
)


def actor(request) -> Actor:
    return Actor.for_user(request.user)


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockLocation)
class StockLocationAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_default", "is_active")
    prepopulated_fields = {"code": ("name",)}


class LowStockFilter(admin.SimpleListFilter):
    title = "stock level"
    parameter_name = "level"

    def lookups(self, request, model_admin):
        return [("low", "At or below reorder point"), ("out", "Nothing available"), ("damaged", "Has damaged stock")]

    def queryset(self, request, queryset):
        if self.value() == "low":
            return queryset.filter(reorder_point__isnull=False, on_hand__lte=F("reorder_point") + F("reserved"))
        if self.value() == "out":
            return queryset.filter(on_hand__lte=F("reserved"))
        if self.value() == "damaged":
            return queryset.filter(damaged__gt=0)
        return queryset


@admin.register(StockBalance)
class StockBalanceAdmin(admin.ModelAdmin):
    """Quantities are read-only here (use Stock adjustments or Stock counts). Reorder levels are editable."""

    list_display = ("product", "location", "on_hand", "reserved", "available", "damaged", "reorder_point")
    list_filter = (LowStockFilter, "location")
    search_fields = ("product__sku", "product__name")
    list_select_related = ("product", "location")
    readonly_fields = ("product", "location", "on_hand", "reserved", "damaged", "available")
    fields = (*readonly_fields, "reorder_point", "reorder_quantity")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Available")
    def available(self, obj):
        return obj.available


@admin.register(StockMovement)
class StockMovementAdmin(ReadOnlyAdmin):
    list_display = (
        "created_at",
        "product",
        "location",
        "bucket",
        "quantity",
        "balance_after",
        "reason",
        "actor_label",
    )
    list_filter = ("reason", "bucket", "location", "actor_type")
    search_fields = ("product__sku", "product__name", "note", "source_id", "correlation_id")
    date_hierarchy = "created_at"
    list_select_related = ("product", "location")


@admin.register(Reservation)
class ReservationAdmin(ReadOnlyAdmin):
    list_display = ("product", "quantity", "location", "status", "source_type", "source_id", "expires_at", "created_at")
    list_filter = ("status", "location", "source_type")
    search_fields = ("product__sku", "source_id")
    actions = ("release_selected",)

    def has_release_permission(self, request):
        return request.user.has_perm("inventory.add_stockadjustment")

    @admin.action(description="Release selected reservations", permissions=["release"])
    def release_selected(self, request, queryset):
        who = actor(request)
        for reservation in queryset.filter(status=ReservationStatus.ACTIVE):
            services.release(reservation, who, reason="Released in admin")
        self.message_user(request, "Released.")


class StockAdjustmentForm(forms.ModelForm):
    class Meta:
        model = StockAdjustment
        fields = ("product", "location", "kind", "quantity", "note")

    def clean(self):
        cleaned = super().clean()
        product, quantity = cleaned.get("product"), cleaned.get("quantity")
        location, kind = cleaned.get("location"), cleaned.get("kind")
        if product and quantity is not None:
            try:
                services.validate_quantity(product, quantity)
            except DomainError as exc:
                self.add_error("quantity", exc.message)
                return cleaned
        if product and location and kind and quantity:
            # Friendly early check; the service re-checks under a row lock.
            balance = StockBalance.objects.filter(product=product, location=location).first()
            available = balance.available if balance else 0
            damaged = balance.damaged if balance else 0
            if kind in (AdjustmentKind.LOST, AdjustmentKind.DAMAGE) and quantity > available:
                self.add_error("quantity", f"Only {available} available (not reserved) at {location}.")
            if kind in (AdjustmentKind.DAMAGE_WRITE_OFF, AdjustmentKind.DAMAGE_RECOVERED) and quantity > damaged:
                self.add_error("quantity", f"Only {damaged} damaged at {location}.")
        return cleaned


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    """The manual override for stock. Each saved adjustment writes ledger movements."""

    form = StockAdjustmentForm
    list_display = ("created_at", "kind", "product", "quantity", "location", "note", "created_by")
    list_filter = ("kind", "location")
    search_fields = ("product__sku", "product__name", "note")
    autocomplete_fields = ("product",)

    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in StockAdjustment._meta.fields] if obj else []

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        location = StockLocation.objects.filter(is_default=True).first()
        if location:
            initial.setdefault("location", location.pk)
        return initial

    def save_model(self, request, obj, form, change):
        try:
            saved = services.record_adjustment(
                actor(request),
                product=obj.product,
                location=obj.location,
                kind=obj.kind,
                quantity=obj.quantity,
                note=obj.note,
            )
        except DomainError as exc:
            # The admin has no clean way to fail inside save_model; show the error and keep nothing.
            self.message_user(request, exc.message, messages.ERROR)
            raise _AdjustmentRejected from exc
        obj.pk, obj._state.adding = saved.pk, False

    def add_view(self, request, form_url="", extra_context=None):
        try:
            return super().add_view(request, form_url, extra_context)
        except _AdjustmentRejected:
            from django.http import HttpResponseRedirect

            return HttpResponseRedirect(request.get_full_path())


class _AdjustmentRejected(Exception):
    pass


class StockCountLineInline(admin.TabularInline):
    model = StockCountLine
    extra = 0
    fields = ("product", "counted_quantity", "system_quantity", "variance")
    readonly_fields = ("system_quantity", "variance")
    autocomplete_fields = ("product",)

    def has_change_permission(self, request, obj=None):
        return obj is None or obj.status == CountStatus.OPEN

    def has_add_permission(self, request, obj=None):
        return obj is None or obj.status == CountStatus.OPEN

    def has_delete_permission(self, request, obj=None):
        return obj is None or obj.status == CountStatus.OPEN


class StockCountForm(forms.ModelForm):
    include_all_products = forms.BooleanField(
        required=False, help_text="Add a line for every active product (use for the first count)."
    )
    counts_file = forms.FileField(
        required=False, help_text="Optional CSV with columns sku,counted to fill in counted quantities."
    )

    class Meta:
        model = StockCount
        fields = ("location", "note")

    def clean_counts_file(self):
        upload = self.cleaned_data.get("counts_file")
        if not upload:
            return []
        if upload.size > 5 * 1024 * 1024 or not upload.name.lower().endswith(".csv"):
            raise forms.ValidationError("Upload a .csv file up to 5 MB.")
        text = upload.read().decode("utf-8-sig", errors="replace")
        rows = [r for r in csv.reader(io.StringIO(text)) if len(r) >= 2 and r[0].strip()]
        if rows and rows[0][0].strip().lower() in ("sku", "code", "item code"):
            rows = rows[1:]
        return [(r[0].strip(), r[1].strip()) for r in rows]


@admin.register(StockCount)
class StockCountAdmin(admin.ModelAdmin):
    form = StockCountForm
    list_display = ("__str__", "location", "status", "started_by", "completed_by", "completed_at")
    list_filter = ("status", "location")
    inlines = (StockCountLineInline,)
    actions = ("complete_counts",)
    readonly_fields = ("status", "started_by", "completed_by", "completed_at")

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("location", "note", "include_all_products", "counts_file")
        return ("location", "note", "status", "started_by", "completed_by", "completed_at", "counts_file")

    def save_model(self, request, obj, form, change):
        who = actor(request)
        if not change:
            products = "all" if form.cleaned_data.get("include_all_products") else None
            count = services.start_count(obj.location, who, products=products, note=obj.note)
            obj.pk, obj._state.adding, obj.started_by = count.pk, False, count.started_by
        else:
            super().save_model(request, obj, form, change)
        rows = form.cleaned_data.get("counts_file") or []
        if rows:
            result = services.load_counts(obj, rows, who)
            level = messages.WARNING if result["unknown_skus"] or result["invalid"] else messages.SUCCESS
            self.message_user(
                request,
                f"Loaded {result['loaded']} counts. Unknown SKUs: {result['unknown_skus'][:20]}. "
                f"Invalid: {result['invalid'][:20]}.",
                level,
            )

    def has_complete_permission(self, request):
        return request.user.has_perm("inventory.complete_stockcount")

    @admin.action(description="Complete count and apply corrections", permissions=["complete"])
    def complete_counts(self, request, queryset):
        who = actor(request)
        for count in queryset.filter(status=CountStatus.OPEN):
            try:
                services.complete_count(count, who)
                self.message_user(request, f"{count}: completed.")
            except DomainError as exc:
                self.message_user(request, f"{count}: {exc.message}", messages.ERROR)
