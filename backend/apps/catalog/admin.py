"""Catalogue admin.

Product edits are routed through apps.catalog.services / apps.pricing.services,
so they are validated and audited exactly like API or import changes.
"""

from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html, format_html_join

from apps.core.actors import Actor
from apps.core.exceptions import DomainError
from apps.pricing import services as pricing
from apps.pricing.models import ProductPrice

from . import imports, services, tasks
from . import media as media_utils
from .models import (
    AttributeDefinition,
    Brand,
    CatalogChangeProposal,
    CatalogReviewItem,
    Category,
    ImportBatch,
    ImportRow,
    ImportStatus,
    MediaAsset,
    Product,
    ProductAlias,
    ProductAttributeValue,
    ProductDocument,
    ProductFamily,
    ProductIdentifier,
    ProductMedia,
    ProposalStatus,
    ReviewStatus,
)


def thumbnail(asset: MediaAsset | None, size: int = 60):
    if asset is None:
        return "—"
    return format_html(
        '<img src="{}" alt="" style="max-width:{}px;max-height:{}px;object-fit:contain">', asset.file.url, size, size
    )


def actor(request) -> Actor:
    return Actor.for_user(request.user)


# --- Taxonomy -------------------------------------------------------------------------


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("__str__", "slug", "product_count", "sort_order", "is_active")
    list_filter = ("is_active", ("parent", admin.RelatedOnlyFieldListFilter))
    search_fields = ("name", "slug")
    list_editable = ("sort_order", "is_active")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("parent",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("parent").annotate(n_products=Count("products"))

    @admin.display(ordering="n_products", description="Products")
    def product_count(self, obj):
        return obj.n_products


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "is_active")
    search_fields = ("name",)
    prepopulated_fields = {"slug": ("name",)}


@admin.register(ProductFamily)
class ProductFamilyAdmin(admin.ModelAdmin):
    list_display = ("name", "brand", "category")
    search_fields = ("name",)
    autocomplete_fields = ("brand", "category")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(AttributeDefinition)
class AttributeDefinitionAdmin(admin.ModelAdmin):
    list_display = ("label", "code", "data_type", "unit", "is_filterable", "sort_order")
    list_filter = ("data_type", "is_filterable")
    search_fields = ("label", "code")
    filter_horizontal = ("categories",)
    prepopulated_fields = {"code": ("label",)}


# --- Product -------------------------------------------------------------------------------


class ProductMediaInline(admin.TabularInline):
    model = ProductMedia
    extra = 0
    fields = ("preview", "alt_text", "sort_order", "is_primary")
    readonly_fields = ("preview",)

    def has_add_permission(self, request, obj=None):
        return False  # add images with the "Add image" field on the product form

    @admin.display(description="Image")
    def preview(self, obj):
        return thumbnail(obj.asset, 80)


class AttributeValueForm(forms.ModelForm):
    value = forms.CharField(max_length=255, help_text="Number, text, yes/no, or one of the attribute's choices.")

    class Meta:
        model = ProductAttributeValue
        fields = ("attribute",)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["value"].initial = self.instance.value
            self.fields["attribute"].disabled = True

    def clean(self):
        cleaned = super().clean()
        attribute, value = cleaned.get("attribute"), cleaned.get("value")
        if attribute and value is not None:
            try:
                services.coerce_attribute_value(attribute, value)
            except DomainError as exc:
                self.add_error("value", exc.message)
        return cleaned


class AttributeValueInline(admin.TabularInline):
    model = ProductAttributeValue
    form = AttributeValueForm
    extra = 1
    autocomplete_fields = ("attribute",)


class AliasInline(admin.TabularInline):
    model = ProductAlias
    extra = 1


class IdentifierInline(admin.TabularInline):
    model = ProductIdentifier
    extra = 0


class PriceHistoryInline(admin.TabularInline):
    model = ProductPrice
    extra = 0
    can_delete = False
    fields = ("price_list", "amount", "valid_from", "valid_to", "source", "created_by", "note")
    readonly_fields = fields
    ordering = ("price_list", "-valid_from")
    verbose_name_plural = "price history (change with “New price” above)"

    def has_add_permission(self, request, obj=None):
        return False


class DocumentInline(admin.TabularInline):
    model = ProductDocument
    extra = 0


class ProductAdminForm(forms.ModelForm):
    add_image = forms.ImageField(required=False, help_text="JPEG, PNG or WebP, up to 10 MB.")
    add_image_primary = forms.BooleanField(required=False, label="Make it the main image")
    new_price = forms.DecimalField(
        required=False, max_digits=12, decimal_places=2, min_value=0.01, help_text="Default price list, from now."
    )
    end_current_price = forms.BooleanField(required=False, help_text="Remove the current price (price on request).")

    class Meta:
        model = Product
        fields = "__all__"  # noqa: DJ007 - the admin fieldsets decide what is shown

    def clean_sku(self):
        try:
            return services.validate_sku(self.cleaned_data["sku"])
        except DomainError as exc:
            raise forms.ValidationError(exc.message) from exc

    def clean_add_image(self):
        upload = self.cleaned_data.get("add_image")
        if upload:
            try:
                media_utils.inspect_image(upload.read())
            except DomainError as exc:
                raise forms.ValidationError(exc.message) from exc
            upload.seek(0)
        return upload


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = ("image", "sku", "name", "category", "brand", "status", "price_on_request", "legacy_id")
    list_display_links = ("sku", "name")
    list_filter = ("status", "price_on_request", "category__parent", "category", "brand", "unit_of_measure")
    search_fields = ("sku", "name", "legacy_id", "barcode", "identifiers__value", "aliases__text")
    autocomplete_fields = ("category", "brand", "family")
    list_per_page = 50
    actions = ("make_active", "make_draft", "archive")
    inlines = (
        ProductMediaInline,
        AttributeValueInline,
        AliasInline,
        IdentifierInline,
        PriceHistoryInline,
        DocumentInline,
    )
    fieldsets = (
        (None, {"fields": ("sku", "legacy_id", "name", "slug", "status", "category", "brand", "family")}),
        ("Description", {"fields": ("description", "unit_of_measure", "material", "finish", "colour", "size_label")}),
        ("Dimensions", {"classes": ("collapse",), "fields": ("length_mm", "width_mm", "height_mm", "weight_g")}),
        ("Identifiers", {"fields": ("barcode",)}),
        ("Price", {"fields": ("price_on_request", "current_price", "new_price", "end_current_price")}),
        ("Images", {"fields": ("add_image", "add_image_primary")}),
    )

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("category", "category__parent", "brand")
            .prefetch_related("media__asset")
        )

    def get_readonly_fields(self, request, obj=None):
        readonly = ["slug", "current_price"]
        if obj is not None:
            readonly += ["sku", "legacy_id"]  # identifiers never change after creation
        return readonly

    @admin.display(description="")
    def image(self, obj):
        primary = next((m for m in obj.media.all() if m.is_primary), None)
        return thumbnail(primary.asset if primary else None, 48)

    @admin.display(description="Current price")
    def current_price(self, obj):
        if obj is None or obj.pk is None:
            return "—"
        try:
            return pricing.quote_price(obj).display
        except DomainError as exc:
            return exc.message

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        who = actor(request)
        data = form.cleaned_data
        if change:
            original = Product.objects.get(pk=obj.pk)
            changes = {f: data[f] for f in form.changed_data if f in services.EDITABLE_PRODUCT_FIELDS}
            services.update_product(original, changes, who, reason="Edited in admin")
            obj.refresh_from_db()
        else:
            fields = {f: data[f] for f in services.EDITABLE_PRODUCT_FIELDS if f in data and f != "category"}
            created = services.create_product(
                who,
                sku=data["sku"],
                name=data["name"],
                category=data["category"],
                legacy_id=data.get("legacy_id"),
                **{k: v for k, v in fields.items() if k != "name"},
            )
            obj.pk, obj.slug, obj.sku = created.pk, created.slug, created.sku
            obj._state.adding, obj._state.db = False, created._state.db

        if data.get("add_image"):
            upload = data["add_image"]
            asset, _ = services.store_image(upload.read(), upload.name, trusted=False)
            services.attach_image(obj, asset, who, make_primary=data.get("add_image_primary", False))
        if data.get("end_current_price"):
            pricing.end_price(obj, who, note="Ended in admin")
        if data.get("new_price"):
            pricing.set_price(obj, data["new_price"], who, source="admin")

    def save_formset(self, request, form, formset, change):
        who = actor(request)
        product = form.instance
        if formset.model in (ProductMedia, ProductAttributeValue, ProductIdentifier, ProductAlias):
            # Saved through services below; the admin's change-history message reads these.
            formset.new_objects, formset.changed_objects, formset.deleted_objects = [], [], []
        if formset.model is ProductMedia:
            for item_form in formset.deleted_forms:
                if item_form.instance.pk:
                    services.detach_image(item_form.instance, who)
            for item_form in formset.forms:
                item = item_form.instance
                if item_form in formset.deleted_forms or not item.pk or not item_form.has_changed():
                    continue
                ProductMedia.objects.filter(pk=item.pk).update(
                    alt_text=item_form.cleaned_data["alt_text"], sort_order=item_form.cleaned_data["sort_order"]
                )
                if "is_primary" in item_form.changed_data and item_form.cleaned_data["is_primary"]:
                    services.set_primary_image(item, who)
            return
        if formset.model is ProductAttributeValue:
            for item_form in formset.forms:
                if not item_form.cleaned_data or not item_form.has_changed():
                    continue
                code = item_form.cleaned_data["attribute"].code
                if item_form in formset.deleted_forms:
                    services.remove_attribute(product, code, who)
                else:
                    services.set_attribute(product, code, item_form.cleaned_data["value"], who)
            return
        if formset.model is ProductIdentifier:
            for item_form in formset.deleted_forms:
                if item_form.instance.pk:
                    item_form.instance.delete()
            for item_form in formset.forms:
                if item_form in formset.deleted_forms or not item_form.has_changed() or not item_form.cleaned_data:
                    continue
                if item_form.instance.pk:
                    item_form.instance.delete()
                services.add_identifier(product, item_form.cleaned_data["kind"], item_form.cleaned_data["value"], who)
            return
        if formset.model is ProductAlias:
            for item_form in formset.deleted_forms:
                if item_form.instance.pk:
                    item_form.instance.delete()
            for item_form in formset.forms:
                if item_form in formset.deleted_forms or not item_form.has_changed() or not item_form.cleaned_data:
                    continue
                if item_form.instance.pk:
                    item_form.instance.delete()
                services.add_alias(product, item_form.cleaned_data["text"], who)
            return
        super().save_formset(request, form, formset, change)

    def _bulk_status(self, request, queryset, status):
        who = actor(request)
        for product in queryset:
            services.set_status(product, status, who, reason="Bulk action in admin")
        self.message_user(request, f"{queryset.count()} product(s) updated.")

    @admin.action(description="Publish (make active)", permissions=["change"])
    def make_active(self, request, queryset):
        self._bulk_status(request, queryset, "ACTIVE")

    @admin.action(description="Unpublish (make draft)", permissions=["change"])
    def make_draft(self, request, queryset):
        self._bulk_status(request, queryset, "DRAFT")

    @admin.action(description="Archive (no longer sold)", permissions=["change"])
    def archive(self, request, queryset):
        self._bulk_status(request, queryset, "ARCHIVED")

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)  # archive instead; deleting loses order history later
        return actions


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("preview", "original_filename", "width", "height", "byte_size", "usage_count", "created_at")
    search_fields = ("sha256", "original_filename", "usages__product__sku")
    readonly_fields = (
        "preview",
        "sha256",
        "file",
        "content_type",
        "width",
        "height",
        "byte_size",
        "original_filename",
        "products",
    )

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(n_usages=Count("usages"))

    def has_add_permission(self, request):
        return False

    @admin.display(description="")
    def preview(self, obj):
        return thumbnail(obj, 120)

    @admin.display(ordering="n_usages", description="Used by")
    def usage_count(self, obj):
        return obj.n_usages

    @admin.display(description="Products")
    def products(self, obj):
        return _product_links(u.product for u in obj.usages.select_related("product"))


def _product_links(products):
    return format_html_join(
        ", ",
        '<a href="{}">{}</a>',
        ((reverse("admin:catalog_product_change", args=[p.pk]), p.sku) for p in products),
    )


# --- Review queue & proposals ------------------------------------------------------------------


@admin.register(CatalogReviewItem)
class CatalogReviewItemAdmin(admin.ModelAdmin):
    list_display = ("summary", "kind", "status", "source", "created_at")
    list_filter = ("status", "kind", "source")
    search_fields = ("summary", "products__sku", "products__name")
    readonly_fields = (
        "kind",
        "fingerprint",
        "summary",
        "product_links",
        "details",
        "source",
        "resolved_by",
        "resolved_at",
    )
    fields = (*readonly_fields, "status", "resolution_note")
    actions = ("mark_resolved", "mark_dismissed")

    def has_add_permission(self, request):
        return False

    @admin.display(description="Products")
    def product_links(self, obj):
        return _product_links(obj.products.all())

    def save_model(self, request, obj, form, change):
        if "status" in form.changed_data and obj.status != ReviewStatus.OPEN:
            services.resolve_review_item(obj, actor(request), status=obj.status, note=obj.resolution_note)
        else:
            super().save_model(request, obj, form, change)

    def _close(self, request, queryset, status):
        for item in queryset.filter(status=ReviewStatus.OPEN):
            services.resolve_review_item(item, actor(request), status=status)
        self.message_user(request, "Updated.")

    @admin.action(description="Mark resolved", permissions=["change"])
    def mark_resolved(self, request, queryset):
        self._close(request, queryset, ReviewStatus.RESOLVED)

    @admin.action(description="Dismiss (not a problem)", permissions=["change"])
    def mark_dismissed(self, request, queryset):
        self._close(request, queryset, ReviewStatus.DISMISSED)


@admin.register(CatalogChangeProposal)
class CatalogChangeProposalAdmin(admin.ModelAdmin):
    list_display = ("product", "field", "current_value", "proposed_value", "source", "status", "created_at")
    list_filter = ("status", "field", "source")
    search_fields = ("product__sku", "product__name", "reason")
    readonly_fields = [f.name for f in CatalogChangeProposal._meta.fields if f.name != "id"]
    actions = ("approve", "reject")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_decide_permission(self, request):
        return request.user.has_perm("catalog.decide_catalogchangeproposal")

    @admin.action(description="Approve and apply", permissions=["decide"])
    def approve(self, request, queryset):
        results = {"applied": 0, "stale": 0}
        for proposal in queryset.filter(status=ProposalStatus.PENDING):
            try:
                done = services.apply_proposal(proposal, actor(request), note="Approved in admin")
            except DomainError as exc:
                self.message_user(request, f"{proposal}: {exc.message}", messages.ERROR)
                continue
            results["applied" if done.status == ProposalStatus.APPLIED else "stale"] += 1
        self.message_user(request, f"Applied {results['applied']}; {results['stale']} were stale (value changed).")

    @admin.action(description="Reject", permissions=["decide"])
    def reject(self, request, queryset):
        for proposal in queryset.filter(status=ProposalStatus.PENDING):
            services.reject_proposal(proposal, actor(request), note="Rejected in admin")
        self.message_user(request, "Rejected.")


# --- Imports -----------------------------------------------------------------------------------------


class ImportBatchForm(forms.ModelForm):
    class Meta:
        model = ImportBatch
        fields = ("file", "update_existing", "activate_new")

    def clean_file(self):
        upload = self.cleaned_data["file"]
        name = upload.name.lower()
        if not name.endswith((".xlsx", ".csv")):
            raise forms.ValidationError("Upload an .xlsx or .csv file.")
        if upload.size > imports.MAX_FILE_BYTES:
            raise forms.ValidationError("File is larger than 10 MB.")
        return upload


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    form = ImportBatchForm
    list_display = ("__str__", "status", "rows_summary", "created_by", "created_at", "approved_by")
    list_filter = ("status",)
    actions = ("revalidate", "approve_and_import", "roll_back")

    def get_fields(self, request, obj=None):
        if obj is None:
            return ("file", "update_existing", "activate_new")
        return (
            "original_filename",
            "status",
            "update_existing",
            "activate_new",
            "report_html",
            "error",
            "created_by",
            "approved_by",
            "committed_at",
            "rolled_back_by",
            "rolled_back_at",
            "rows_link",
        )

    def get_readonly_fields(self, request, obj=None):
        return () if obj is None else self.get_fields(request, obj)

    def has_change_permission(self, request, obj=None):
        return obj is None and super().has_change_permission(request, obj)

    def has_view_permission(self, request, obj=None):
        return request.user.has_perm("catalog.view_importbatch") or request.user.has_perm("catalog.add_importbatch")

    def has_commit_permission(self, request):
        return request.user.has_perm("catalog.commit_importbatch")

    def has_rollback_permission(self, request):
        return request.user.has_perm("catalog.rollback_importbatch")

    def save_model(self, request, obj, form, change):
        upload = form.cleaned_data["file"]
        data = upload.read()
        upload.seek(0)
        obj.original_filename = upload.name[:255]
        obj.file_sha256 = imports.sha256_hex(data)
        obj.created_by = actor(request).label
        super().save_model(request, obj, form, change)
        batch_id = str(obj.pk)
        transaction.on_commit(lambda: tasks.validate_import_batch.delay(batch_id))
        self.message_user(request, "Uploaded. Validation is running; refresh this page to see the report.")

    @admin.display(description="Rows")
    def rows_summary(self, obj):
        report = obj.report or {}
        if not report:
            return "—"
        accepted, rejected = report.get("rows_accepted", 0), report.get("rows_rejected", 0)
        return f"{accepted} ok / {rejected} rejected / {report.get('duplicates', 0)} dup"

    @admin.display(description="Report")
    def report_html(self, obj):
        report = dict(obj.report or {})
        examples = report.pop("examples", [])
        summary = format_html_join(
            "", "<tr><th>{}</th><td>{}</td></tr>", ((k.replace("_", " "), v) for k, v in report.items())
        )
        problems = format_html_join(
            "",
            "<tr><td>{}</td><td>{}</td><td>{}</td></tr>",
            ((e["row"], e["outcome"], "; ".join(e["errors"] + e["warnings"])) for e in examples),
        )
        return format_html(
            "<table>{}</table><h4>First rows with problems</h4>"
            "<table><tr><th>Row</th><th>Outcome</th><th>Issues</th></tr>{}</table>",
            summary,
            problems,
        )

    @admin.display(description="All rows")
    def rows_link(self, obj):
        url = reverse("admin:catalog_importrow_changelist") + f"?batch__id__exact={obj.pk}"
        return format_html('<a href="{}">View all {} rows</a>', url, obj.rows.count())

    @admin.action(description="Validate again (dry run)", permissions=["view"])
    def revalidate(self, request, queryset):
        for batch in queryset.filter(status__in=[ImportStatus.UPLOADED, ImportStatus.VALIDATED, ImportStatus.FAILED]):
            tasks.validate_import_batch.delay(str(batch.pk))
        self.message_user(request, "Validation queued.")

    @admin.action(description="Approve and import validated batch", permissions=["commit"])
    def approve_and_import(self, request, queryset):
        payload = tasks.actor_payload(actor(request))
        queued = 0
        for batch in queryset.filter(status=ImportStatus.VALIDATED):
            tasks.commit_import_batch.delay(str(batch.pk), payload)
            queued += 1
        self.message_user(request, f"{queued} batch(es) queued for import.")

    @admin.action(description="Roll back imported batch", permissions=["rollback"])
    def roll_back(self, request, queryset):
        payload = tasks.actor_payload(actor(request))
        for batch in queryset.filter(status=ImportStatus.COMMITTED):
            tasks.rollback_import_batch.delay(str(batch.pk), payload)
        self.message_user(request, "Rollback queued.")


@admin.register(ImportRow)
class ImportRowAdmin(admin.ModelAdmin):
    list_display = ("row_number", "batch", "outcome", "sku", "issues", "product")
    list_filter = ("outcome", "batch")
    search_fields = ("normalized__sku", "normalized__name")
    readonly_fields = [f.name for f in ImportRow._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="SKU")
    def sku(self, obj):
        return obj.normalized.get("sku", "")

    @admin.display(description="Issues")
    def issues(self, obj):
        return "; ".join(obj.errors + obj.warnings)
