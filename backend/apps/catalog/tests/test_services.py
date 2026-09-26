from decimal import Decimal

import pytest
from django.db import IntegrityError, transaction

from apps.audit.models import AuditEvent
from apps.catalog import services
from apps.catalog.models import (
    AttributeDefinition,
    AttributeType,
    CatalogChangeProposal,
    IdentifierKind,
    MediaAsset,
    Product,
    ProductMedia,
    ProposalStatus,
    ReviewKind,
    ReviewStatus,
)
from apps.core.actors import Actor
from apps.core.exceptions import Conflict, ValidationError

from .factories import BrandFactory, CategoryFactory, ProductFactory

pytestmark = pytest.mark.django_db


class TestCreateProduct:
    def test_normalises_and_audits(self, system_actor):
        category = CategoryFactory()
        product = services.create_product(system_actor, sku=" dwx-9001 ", name="  PPR  Pipe 20mm ", category=category)
        assert product.sku == "DWX-9001"
        assert product.name == "PPR Pipe 20mm"
        assert product.slug == "ppr-pipe-20mm-dwx-9001"
        event = AuditEvent.objects.get(action="catalog.product.created")
        assert event.object_id == str(product.pk)
        assert event.after["sku"] == "DWX-9001"
        assert event.actor_type == "SYSTEM"

    @pytest.mark.parametrize("sku", ["", "  ", "BAD SKU", "-LEADING", "Ä-1", "X" * 65])
    def test_rejects_invalid_sku(self, system_actor, sku):
        with pytest.raises(ValidationError):
            services.create_product(system_actor, sku=sku, name="Thing", category=CategoryFactory())

    def test_rejects_duplicate_sku_case_insensitively(self, system_actor):
        ProductFactory(sku="DWX-1")
        with pytest.raises(Conflict):
            services.create_product(system_actor, sku="dwx-1", name="Other", category=CategoryFactory())

    def test_rejects_sku_already_used_as_identifier(self, system_actor):
        product = ProductFactory()
        services.add_identifier(product, IdentifierKind.LEGACY_SKU, "OLD-77", system_actor)
        with pytest.raises(Conflict, match="already used by"):
            services.create_product(system_actor, sku="old-77", name="New", category=CategoryFactory())

    def test_database_enforces_upper_case_sku(self):
        with pytest.raises(IntegrityError), transaction.atomic():
            ProductFactory(sku="lower-1")

    def test_rejects_unknown_fields(self, system_actor):
        with pytest.raises(ValidationError, match="Unknown product fields"):
            services.create_product(system_actor, sku="A-1", name="Thing", category=CategoryFactory(), price=10)


class TestUpdateProduct:
    def test_records_only_changed_fields(self, system_actor):
        product = ProductFactory(name="Old name", colour="Red")
        services.update_product(product, {"name": "New  name", "colour": "Red"}, system_actor, reason="typo")
        product.refresh_from_db()
        assert product.name == "New name"
        event = AuditEvent.objects.get(action="catalog.product.updated")
        assert event.before == {"name": "Old name"}
        assert event.after == {"name": "New name"}
        assert event.reason == "typo"

    def test_no_change_writes_no_audit(self, system_actor):
        product = ProductFactory(name="Same")
        services.update_product(product, {"name": "Same"}, system_actor)
        assert not AuditEvent.objects.filter(action="catalog.product.updated").exists()

    def test_identifiers_cannot_be_changed(self, system_actor):
        with pytest.raises(ValidationError):
            services.update_product(ProductFactory(), {"sku": "NEW-1"}, system_actor)

    def test_invalid_choice_rejected(self, system_actor):
        with pytest.raises(ValidationError):
            services.update_product(ProductFactory(), {"status": "SOLD_OUT"}, system_actor)

    def test_duplicate_barcode_is_conflict(self, system_actor):
        ProductFactory(barcode="6161100000017")
        with pytest.raises((Conflict, ValidationError)):
            services.update_product(ProductFactory(), {"barcode": "6161100000017"}, system_actor)


class TestAttributes:
    @pytest.fixture
    def diameter(self):
        return AttributeDefinition.objects.create(
            code="diameter_mm", label="Diameter", data_type=AttributeType.NUMBER, unit="mm"
        )

    @pytest.fixture
    def pressure(self):
        return AttributeDefinition.objects.create(
            code="pressure_class", label="Pressure class", data_type=AttributeType.CHOICE, choices=["PN16", "PN20"]
        )

    def test_number_attribute(self, system_actor, diameter):
        product = ProductFactory()
        value = services.set_attribute(product, "diameter_mm", "20", system_actor)
        assert value.value_number == Decimal("20")
        assert str(value) == "Diameter: 20 mm"

    def test_rejects_wrong_type(self, system_actor, diameter):
        with pytest.raises(ValidationError, match="must be a number"):
            services.set_attribute(ProductFactory(), "diameter_mm", "twenty", system_actor)

    def test_choice_is_case_insensitive_but_stored_canonically(self, system_actor, pressure):
        value = services.set_attribute(ProductFactory(), "pressure_class", "pn20", system_actor)
        assert value.value_text == "PN20"
        with pytest.raises(ValidationError, match="must be one of"):
            services.set_attribute(ProductFactory(), "pressure_class", "PN99", system_actor)

    def test_scope_limits_categories_including_children(self, system_actor, pressure):
        pipes = CategoryFactory(name="Pipes")
        ppr = CategoryFactory(name="PPR", parent=pipes)
        pressure.categories.add(pipes)
        services.set_attribute(ProductFactory(category=ppr), "pressure_class", "PN16", system_actor)
        with pytest.raises(ValidationError, match="does not apply"):
            services.set_attribute(ProductFactory(), "pressure_class", "PN16", system_actor)

    def test_update_and_remove_are_audited(self, system_actor, diameter):
        product = ProductFactory()
        services.set_attribute(product, "diameter_mm", "20", system_actor)
        services.set_attribute(product, "diameter_mm", "25", system_actor)
        services.remove_attribute(product, "diameter_mm", system_actor)
        actions = list(AuditEvent.objects.order_by("id").values_list("action", flat=True))
        assert actions == ["catalog.attribute.set", "catalog.attribute.set", "catalog.attribute.removed"]
        assert not product.attribute_values.exists()


class TestMedia:
    def test_same_image_stored_once(self, image_bytes):
        data = image_bytes()
        first, created1 = services.store_image(data, "a.jpg", trusted=True)
        second, created2 = services.store_image(data, "b.jpg", trusted=True)
        assert (created1, created2) == (True, False)
        assert first.pk == second.pk
        assert MediaAsset.objects.count() == 1
        assert first.file.name.startswith(f"products/{first.sha256[:2]}/{first.sha256}")

    def test_first_image_becomes_primary_and_primary_can_move(self, system_actor, image_bytes):
        product = ProductFactory()
        a, _ = services.store_image(image_bytes(color=(1, 2, 3)), "a.jpg")
        b, _ = services.store_image(image_bytes(color=(9, 9, 9)), "b.jpg")
        first, _ = services.attach_image(product, a, system_actor)
        second, _ = services.attach_image(product, b, system_actor, make_primary=True)
        first.refresh_from_db()
        assert (first.is_primary, second.is_primary) == (False, True)
        assert ProductMedia.objects.filter(product=product, is_primary=True).count() == 1

    def test_detaching_primary_promotes_another(self, system_actor, image_bytes):
        product = ProductFactory()
        a, _ = services.store_image(image_bytes(color=(1, 2, 3)), "a.jpg")
        b, _ = services.store_image(image_bytes(color=(9, 9, 9)), "b.jpg")
        primary, _ = services.attach_image(product, a, system_actor)
        other, _ = services.attach_image(product, b, system_actor)
        services.detach_image(primary, system_actor)
        other.refresh_from_db()
        assert other.is_primary

    def test_database_allows_only_one_primary(self, image_bytes):
        product = ProductFactory()
        a, _ = services.store_image(image_bytes(color=(1, 2, 3)), "a.jpg")
        b, _ = services.store_image(image_bytes(color=(9, 9, 9)), "b.jpg")
        ProductMedia.objects.create(product=product, asset=a, is_primary=True)
        with pytest.raises(IntegrityError), transaction.atomic():
            ProductMedia.objects.create(product=product, asset=b, is_primary=True)


class TestReviewAndProposals:
    def test_flagging_is_idempotent(self):
        product = ProductFactory()
        kwargs = dict(kind=ReviewKind.DUPLICATE_NAME, fingerprint="x:1", summary="dup", products=[product])
        item, created = services.flag_for_review(**kwargs)
        services.resolve_review_item(item, Actor.system(), status=ReviewStatus.DISMISSED)
        again, created_again = services.flag_for_review(**kwargs)
        assert (created, created_again) == (True, False)
        assert again.status == ReviewStatus.DISMISSED  # a dismissed item is not reopened

    def test_proposal_applies_only_after_approval(self, system_actor, staff_user):
        product = ProductFactory()
        proposal, _ = services.propose_change(product, "brand", "Yale", Actor.agent("catalogue"), source="agent")
        product.refresh_from_db()
        assert product.brand is None
        services.apply_proposal(proposal, Actor.for_user(staff_user))
        product.refresh_from_db()
        proposal.refresh_from_db()
        assert product.brand.name == "Yale"
        assert proposal.status == ProposalStatus.APPLIED
        assert proposal.decided_by == staff_user.email

    def test_agent_cannot_approve(self):
        product = ProductFactory()
        proposal, _ = services.propose_change(product, "colour", "Black", Actor.agent("catalogue"), source="agent")
        with pytest.raises(Conflict):
            services.apply_proposal(proposal, Actor.agent("catalogue"))

    def test_stale_proposal_does_not_overwrite_newer_edit(self, system_actor):
        product = ProductFactory(colour="Red")
        proposal, _ = services.propose_change(product, "colour", "Black", system_actor, source="agent")
        services.update_product(product, {"colour": "Blue"}, system_actor)
        result = services.apply_proposal(proposal, system_actor)
        product.refresh_from_db()
        assert result.status == ProposalStatus.STALE
        assert product.colour == "Blue"

    def test_one_pending_proposal_per_field_and_source(self, system_actor):
        product = ProductFactory()
        _, created = services.propose_change(product, "colour", "Black", system_actor, source="agent")
        _, created_again = services.propose_change(product, "colour", "White", system_actor, source="agent")
        assert (created, created_again) == (True, False)
        assert CatalogChangeProposal.objects.count() == 1

    def test_cannot_propose_identifier_changes(self, system_actor):
        with pytest.raises(ValidationError):
            services.propose_change(ProductFactory(), "sku", "NEW", system_actor, source="agent")


def test_brand_and_category_get_or_create_are_case_insensitive(system_actor):
    brand, created = services.get_or_create_brand("Yale", system_actor)
    same, created_again = services.get_or_create_brand("  yale ", system_actor)
    assert brand.pk == same.pk and (created, created_again) == (True, False)
    BrandFactory(name="Crown")
    top, _ = services.get_or_create_category("Locks", system_actor)
    sub, _ = services.get_or_create_category("Padlocks", system_actor, parent=top)
    assert sub.slug == "locks-padlocks"
    assert Product.objects.count() == 0
