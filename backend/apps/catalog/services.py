"""Catalogue write operations.

All changes to products, attributes, media and review/proposal records go
through here, so validation and auditing happen in one place, whether the
caller is the admin, an importer, the API or (later) an AI tool.
"""

from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.text import slugify

from apps.audit import services as audit
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, ValidationError

from . import media as media_utils
from .models import (
    SKU_PATTERN,
    AttributeDefinition,
    AttributeType,
    Brand,
    CatalogChangeProposal,
    CatalogReviewItem,
    Category,
    IdentifierKind,
    MediaAsset,
    Product,
    ProductAlias,
    ProductAttributeValue,
    ProductIdentifier,
    ProductMedia,
    ProductStatus,
    ProposalStatus,
    ReviewStatus,
    normalise_sku,
)

# Fields that services may change with update_product(). sku/legacy_id are
# identifiers and have their own, deliberately harder, paths.
EDITABLE_PRODUCT_FIELDS = (
    "name",
    "description",
    "status",
    "category",
    "brand",
    "family",
    "unit_of_measure",
    "material",
    "finish",
    "colour",
    "size_label",
    "length_mm",
    "width_mm",
    "height_mm",
    "weight_g",
    "barcode",
    "price_on_request",
)
AUDITED_PRODUCT_FIELDS = ("sku", "legacy_id", *EDITABLE_PRODUCT_FIELDS)


def clean_name(value: str) -> str:
    """Trim and collapse internal whitespace. Does not otherwise alter names."""
    return " ".join((value or "").split())


def _full_clean(instance, exclude=None):
    try:
        instance.full_clean(exclude=exclude, validate_unique=False, validate_constraints=False)
    except DjangoValidationError as exc:
        raise ValidationError("Invalid product data.", details=exc.message_dict) from exc


# --- Categories & brands ------------------------------------------------------


def get_or_create_category(name: str, actor: Actor, parent: Category | None = None) -> tuple[Category, bool]:
    name = clean_name(name)
    if not name:
        raise ValidationError("Category name is required.")
    existing = Category.objects.filter(name__iexact=name, parent=parent).first()
    if existing:
        return existing, False
    base = f"{parent.slug}-{slugify(name)}" if parent else slugify(name)
    category = Category.objects.create(name=name, parent=parent, slug=_unique_slug(Category, base))
    audit.record(actor, "catalog.category.created", category, after={"name": name, "parent": str(parent or "")})
    return category, True


def get_or_create_brand(name: str, actor: Actor) -> tuple[Brand, bool]:
    name = clean_name(name)
    if not name:
        raise ValidationError("Brand name is required.")
    existing = Brand.objects.filter(name__iexact=name).first()
    if existing:
        return existing, False
    brand = Brand.objects.create(name=name, slug=_unique_slug(Brand, slugify(name)))
    audit.record(actor, "catalog.brand.created", brand, after={"name": name})
    return brand, True


def _unique_slug(model, base: str, max_length: int = 180) -> str:
    base = (base or "item")[:max_length]
    slug, n = base, 2
    while model.objects.filter(slug=slug).exists():
        suffix = f"-{n}"
        slug, n = base[: max_length - len(suffix)] + suffix, n + 1
    return slug


# --- Products -----------------------------------------------------------------


def validate_sku(sku: str) -> str:
    sku = normalise_sku(sku)
    if not SKU_PATTERN.match(sku):
        raise ValidationError(
            "SKU must start with a letter or digit and contain only A-Z, 0-9 and . _ / -",
            details={"sku": sku},
        )
    return sku


def _check_code_is_free(code: str, *, ignore_product: Product | None = None) -> None:
    """A code must identify at most one product, across SKUs and identifiers."""
    sku_owner = Product.objects.filter(sku=code).exclude(pk=getattr(ignore_product, "pk", None)).first()
    ident_owner = (
        ProductIdentifier.objects.filter(value=code).exclude(product=ignore_product).select_related("product").first()
    )
    owner = sku_owner or (ident_owner.product if ident_owner else None)
    if owner:
        raise Conflict(f"Code {code} is already used by {owner.sku}.", details={"code": code, "product": owner.sku})


@transaction.atomic
def create_product(actor: Actor, *, sku: str, name: str, category: Category, **fields) -> Product:
    unknown = set(fields) - set(EDITABLE_PRODUCT_FIELDS) - {"legacy_id"}
    if unknown:
        raise ValidationError(f"Unknown product fields: {', '.join(sorted(unknown))}")
    sku = validate_sku(sku)
    name = clean_name(name)
    if not name:
        raise ValidationError("Product name is required.")
    _check_code_is_free(sku)
    if fields.get("barcode") == "":
        fields["barcode"] = None

    product = Product(sku=sku, name=name, category=category, **fields)
    product.slug = _unique_slug(Product, slugify(f"{name}-{sku}"), max_length=300)
    _full_clean(product)
    try:
        with transaction.atomic():
            product.save()
    except IntegrityError as exc:
        # Lost a race with a concurrent create, or duplicate legacy_id/barcode.
        raise Conflict("A product with this SKU, legacy id or barcode already exists.", details={"sku": sku}) from exc
    audit.record(actor, "catalog.product.created", product, after=audit.snapshot(product, AUDITED_PRODUCT_FIELDS))
    return product


@transaction.atomic
def update_product(product: Product, changes: dict, actor: Actor, reason: str = "") -> Product:
    """Apply field changes to a product; records only what actually changed."""
    unknown = set(changes) - set(EDITABLE_PRODUCT_FIELDS)
    if unknown:
        raise ValidationError(f"These fields cannot be changed here: {', '.join(sorted(unknown))}")

    locked = Product.objects.select_for_update().get(pk=product.pk)
    before = audit.snapshot(locked, EDITABLE_PRODUCT_FIELDS)
    for field, value in changes.items():
        if field == "name":
            value = clean_name(value)
        if field == "barcode" and value == "":
            value = None
        setattr(locked, field, value)
    _full_clean(locked)
    after = audit.snapshot(locked, EDITABLE_PRODUCT_FIELDS)
    changed_before, changed_after = audit.diff(before, after)
    if not changed_after:
        return locked
    try:
        with transaction.atomic():
            locked.save(update_fields=[*changed_after.keys(), "updated_at"])
    except IntegrityError as exc:
        raise Conflict("Another product already uses this barcode.") from exc
    audit.record(actor, "catalog.product.updated", locked, before=changed_before, after=changed_after, reason=reason)
    # Keep the caller's instance in sync.
    for field in changed_after:
        setattr(product, field, getattr(locked, field))
    return locked


def set_status(product: Product, status: str, actor: Actor, reason: str = "") -> Product:
    if status not in ProductStatus.values:
        raise ValidationError(f"Unknown status {status!r}.")
    return update_product(product, {"status": status}, actor, reason=reason)


# --- Identifiers & aliases ----------------------------------------------------


@transaction.atomic
def add_identifier(product: Product, kind: str, value: str, actor: Actor) -> ProductIdentifier:
    if kind not in IdentifierKind.values:
        raise ValidationError(f"Unknown identifier kind {kind!r}.")
    value = normalise_sku(value)
    if not value:
        raise ValidationError("Identifier value is required.")
    existing = ProductIdentifier.objects.filter(kind=kind, value=value).first()
    if existing and existing.product_id == product.pk:
        return existing
    _check_code_is_free(value, ignore_product=product)
    identifier = ProductIdentifier.objects.create(product=product, kind=kind, value=value)
    audit.record(actor, "catalog.identifier.added", product, after={"kind": kind, "value": value})
    return identifier


@transaction.atomic
def add_alias(product: Product, text: str, actor: Actor) -> ProductAlias:
    text = clean_name(text)
    if not text:
        raise ValidationError("Alias text is required.")
    existing = ProductAlias.objects.filter(product=product, text__iexact=text).first()
    if existing:
        return existing
    alias = ProductAlias.objects.create(product=product, text=text)
    audit.record(actor, "catalog.alias.added", product, after={"alias": text})
    return alias


# --- Attributes -----------------------------------------------------------------


def coerce_attribute_value(definition: AttributeDefinition, raw) -> dict:
    """Return the value-column kwargs for `raw`, validated against the definition."""
    empty = {"value_text": None, "value_number": None, "value_bool": None}
    if definition.data_type == AttributeType.NUMBER:
        try:
            number = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(f"{definition.label} must be a number.", details={"value": raw}) from exc
        if not number.is_finite():
            raise ValidationError(f"{definition.label} must be a finite number.")
        return {**empty, "value_number": number}
    if definition.data_type == AttributeType.BOOLEAN:
        if isinstance(raw, bool):
            return {**empty, "value_bool": raw}
        text = str(raw).strip().lower()
        if text in ("yes", "true", "1", "y"):
            return {**empty, "value_bool": True}
        if text in ("no", "false", "0", "n"):
            return {**empty, "value_bool": False}
        raise ValidationError(f"{definition.label} must be yes or no.", details={"value": raw})
    text = clean_name(str(raw))
    if not text:
        raise ValidationError(f"{definition.label} cannot be empty.")
    if definition.data_type == AttributeType.CHOICE:
        matches = [c for c in definition.choices if str(c).lower() == text.lower()]
        if not matches:
            raise ValidationError(
                f"{definition.label} must be one of: {', '.join(map(str, definition.choices))}.",
                details={"value": raw},
            )
        text = str(matches[0])
    return {**empty, "value_text": text[:255]}


def check_attribute_scope(definition: AttributeDefinition, category: Category | None) -> None:
    """Scoped attributes apply to their categories and those categories' children."""
    allowed = {c.pk for c in definition.categories.all()}
    if not allowed:
        return
    if category is None or (category.pk not in allowed and category.parent_id not in allowed):
        raise ValidationError(f"{definition.label} does not apply to category {category or '(new category)'}.")


@transaction.atomic
def set_attribute(product: Product, code: str, raw_value, actor: Actor) -> ProductAttributeValue:
    definition = AttributeDefinition.objects.filter(code=code).first()
    if definition is None:
        raise ValidationError(f"Unknown attribute {code!r}.")
    check_attribute_scope(definition, product.category)
    values = coerce_attribute_value(definition, raw_value)

    existing = ProductAttributeValue.objects.select_for_update().filter(product=product, attribute=definition).first()
    before = existing.value if existing else None
    if existing:
        for key, value in values.items():
            setattr(existing, key, value)
        existing.save()
        attribute_value = existing
    else:
        attribute_value = ProductAttributeValue.objects.create(product=product, attribute=definition, **values)
    after = attribute_value.value
    if before != after:
        audit.record(actor, "catalog.attribute.set", product, before={code: before}, after={code: after})
    return attribute_value


@transaction.atomic
def remove_attribute(product: Product, code: str, actor: Actor) -> None:
    existing = ProductAttributeValue.objects.filter(product=product, attribute__code=code).first()
    if existing:
        before = existing.value
        existing.delete()
        audit.record(actor, "catalog.attribute.removed", product, before={code: before})


# --- Media ----------------------------------------------------------------------


def store_image(data: bytes, original_filename: str, *, trusted: bool = False) -> tuple[MediaAsset, bool]:
    """Validate and store image bytes once; returns (asset, created).

    Untrusted images are re-encoded before hashing and storing. Trusted
    sources (the legacy website files) are stored byte-for-byte so re-running
    an import finds the same hash.
    """
    info = media_utils.inspect_image(data)
    if not trusted:
        data = media_utils.reencode(data, info)
        info = media_utils.inspect_image(data)
    digest = media_utils.sha256_hex(data)
    existing = MediaAsset.objects.filter(sha256=digest).first()
    if existing:
        return existing, False
    asset = MediaAsset(
        sha256=digest,
        content_type=info.content_type,
        width=info.width,
        height=info.height,
        byte_size=len(data),
        original_filename=(original_filename or "")[:255],
    )
    asset.file.save(f"{digest}.{asset.extension}", ContentFile(data), save=False)
    try:
        with transaction.atomic():
            asset.save()
    except IntegrityError:
        # Stored concurrently by someone else; theirs is identical by definition.
        # Our copy (saved under a different name) is left as a harmless orphan rather
        # than risk deleting a file another row points at.
        return MediaAsset.objects.get(sha256=digest), False
    return asset, True


@transaction.atomic
def attach_image(
    product: Product, asset: MediaAsset, actor: Actor, *, alt_text: str = "", make_primary: bool = False
) -> tuple[ProductMedia, bool]:
    # Lock the product so two concurrent "make primary" calls cannot both win.
    Product.objects.select_for_update().filter(pk=product.pk).first()
    existing = ProductMedia.objects.filter(product=product, asset=asset).first()
    if existing:
        if make_primary and not existing.is_primary:
            set_primary_image(existing, actor)
        return existing, False
    has_primary = ProductMedia.objects.filter(product=product, is_primary=True).exists()
    primary = make_primary or not has_primary
    if primary and has_primary:
        ProductMedia.objects.filter(product=product, is_primary=True).update(is_primary=False)
    last = ProductMedia.objects.filter(product=product).order_by("-sort_order").first()
    item = ProductMedia.objects.create(
        product=product,
        asset=asset,
        alt_text=(alt_text or product.name)[:255],
        is_primary=primary,
        sort_order=last.sort_order + 1 if last else 0,
    )
    audit.record(actor, "catalog.media.attached", product, after={"asset": asset.sha256, "primary": primary})
    return item, True


@transaction.atomic
def set_primary_image(item: ProductMedia, actor: Actor) -> None:
    ProductMedia.objects.filter(product_id=item.product_id, is_primary=True).exclude(pk=item.pk).update(
        is_primary=False
    )
    ProductMedia.objects.filter(pk=item.pk).update(is_primary=True)
    item.is_primary = True
    audit.record(actor, "catalog.media.primary_set", item.product, after={"asset": item.asset.sha256})


@transaction.atomic
def detach_image(item: ProductMedia, actor: Actor) -> None:
    product, was_primary, sha = item.product, item.is_primary, item.asset.sha256
    item.delete()
    if was_primary:
        replacement = ProductMedia.objects.filter(product=product).order_by("sort_order").first()
        if replacement:
            ProductMedia.objects.filter(pk=replacement.pk).update(is_primary=True)
    audit.record(actor, "catalog.media.detached", product, before={"asset": sha})


# --- Review queue -----------------------------------------------------------------


def flag_for_review(
    *, kind: str, fingerprint: str, summary: str, products=(), details=None, source: str = ""
) -> tuple[CatalogReviewItem, bool]:
    """Create a review item unless one with this fingerprint already exists (any status)."""
    item, created = CatalogReviewItem.objects.get_or_create(
        fingerprint=fingerprint[:200],
        defaults={"kind": kind, "summary": summary[:300], "details": details or {}, "source": source},
    )
    if created and products:
        item.products.set(products)
    return item, created


@transaction.atomic
def resolve_review_item(item: CatalogReviewItem, actor: Actor, *, status: str, note: str = "") -> CatalogReviewItem:
    if status not in (ReviewStatus.RESOLVED, ReviewStatus.DISMISSED):
        raise ValidationError("A review item can only be resolved or dismissed.")
    item.status = status
    item.resolved_by = actor.label
    item.resolved_at = timezone.now()
    item.resolution_note = note
    item.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_note", "updated_at"])
    audit.record(actor, "catalog.review.closed", item, after={"status": status}, reason=note)
    return item


# --- Change proposals ---------------------------------------------------------------


def _field_value_for_proposal(product: Product, field: str):
    """Comparable plain value of a product field (FKs by slug/name)."""
    value = getattr(product, field)
    if field == "category":
        return value.slug
    if field == "brand":
        return value.name if value else None
    return value


def _resolve_proposed_value(field: str, value, actor: Actor):
    if field == "category":
        category = Category.objects.filter(slug=value).first()
        if category is None:
            raise ValidationError(f"Category {value!r} does not exist.")
        return category
    if field == "brand":
        return get_or_create_brand(value, actor)[0] if value else None
    return value


@transaction.atomic
def propose_change(
    product: Product, field: str, proposed_value, actor: Actor, *, source: str, reason: str = ""
) -> tuple[CatalogChangeProposal, bool]:
    """Suggest a change for human review. Never changes the product itself."""
    if field not in CatalogChangeProposal.PROPOSABLE_FIELDS:
        raise ValidationError(f"Changes to {field!r} cannot be proposed.")
    current = _field_value_for_proposal(product, field)
    if current == proposed_value:
        raise ValidationError("The proposed value is the same as the current value.")
    existing = CatalogChangeProposal.objects.filter(
        product=product, field=field, source=source, status=ProposalStatus.PENDING
    ).first()
    if existing:
        return existing, False
    proposal = CatalogChangeProposal.objects.create(
        product=product,
        field=field,
        current_value=current,
        proposed_value=proposed_value,
        reason=reason,
        source=source,
        proposed_by=actor.label,
    )
    audit.record(
        actor, "catalog.proposal.created", product, after={"field": field, "value": proposed_value}, reason=reason
    )
    return proposal, True


@transaction.atomic
def apply_proposal(proposal: CatalogChangeProposal, actor: Actor, note: str = "") -> CatalogChangeProposal:
    if actor.is_agent:
        raise Conflict("Proposals must be approved by a person.")
    proposal = CatalogChangeProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status != ProposalStatus.PENDING:
        raise Conflict(f"Proposal is already {proposal.get_status_display().lower()}.")
    product = proposal.product
    if _field_value_for_proposal(product, proposal.field) != proposal.current_value:
        # Someone changed the field after the proposal was made; don't overwrite their edit.
        proposal.status = ProposalStatus.STALE
    else:
        value = _resolve_proposed_value(proposal.field, proposal.proposed_value, actor)
        update_product(product, {proposal.field: value}, actor, reason=f"Approved proposal #{proposal.pk}. {note}")
        proposal.status = ProposalStatus.APPLIED
    proposal.decided_by, proposal.decided_at, proposal.decision_note = actor.label, timezone.now(), note
    proposal.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    return proposal


@transaction.atomic
def reject_proposal(proposal: CatalogChangeProposal, actor: Actor, note: str = "") -> CatalogChangeProposal:
    proposal = CatalogChangeProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status != ProposalStatus.PENDING:
        raise Conflict(f"Proposal is already {proposal.get_status_display().lower()}.")
    proposal.status = ProposalStatus.REJECTED
    proposal.decided_by, proposal.decided_at, proposal.decision_note = actor.label, timezone.now(), note
    proposal.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    audit.record(actor, "catalog.proposal.rejected", proposal.product, after={"field": proposal.field}, reason=note)
    return proposal
