import pytest
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from apps.audit.models import AuditEvent
from apps.catalog import services
from apps.catalog.models import CatalogChangeProposal, ImportBatch, ImportStatus, Product, ProposalStatus
from apps.core.actors import Actor
from apps.pricing import services as pricing

from .factories import CategoryFactory, ProductFactory

pytestmark = pytest.mark.django_db

CHANGELISTS = [
    "catalog_category",
    "catalog_brand",
    "catalog_productfamily",
    "catalog_attributedefinition",
    "catalog_product",
    "catalog_mediaasset",
    "catalog_catalogreviewitem",
    "catalog_catalogchangeproposal",
    "catalog_importbatch",
    "catalog_importrow",
    "pricing_pricelist",
    "pricing_productprice",
    "core_businessprofile",
    "audit_auditevent",
    "authentication_user",
]


@pytest.fixture
def admin_client(client, superuser):
    client.force_login(superuser)
    return client


@pytest.mark.parametrize("name", CHANGELISTS)
def test_changelists_load(admin_client, name):
    ProductFactory()
    assert admin_client.get(reverse(f"admin:{name}_changelist")).status_code == 200


def test_product_change_page_loads(admin_client, image_bytes, system_actor):
    product = ProductFactory()
    asset, _ = services.store_image(image_bytes(), "x.jpg")
    services.attach_image(product, asset, system_actor)
    assert admin_client.get(reverse("admin:catalog_product_change", args=[product.pk])).status_code == 200


def _product_form(product, **overrides):
    data = {
        "name": product.name,
        "status": product.status,
        "category": str(product.category_id),
        "brand": "",
        "family": "",
        "description": "",
        "unit_of_measure": "PIECE",
        "material": "",
        "finish": "",
        "colour": "",
        "size_label": "",
        "length_mm": "",
        "width_mm": "",
        "height_mm": "",
        "weight_g": "",
        "barcode": "",
        "price_on_request": "on",
    }
    for prefix in ("media", "attribute_values", "aliases", "identifiers", "prices", "documents"):
        data.update(
            {
                f"{prefix}-TOTAL_FORMS": "0",
                f"{prefix}-INITIAL_FORMS": "0",
                f"{prefix}-MIN_NUM_FORMS": "0",
                f"{prefix}-MAX_NUM_FORMS": "1000",
            }
        )
    data.update(overrides)
    return data


def test_admin_edit_goes_through_service_and_is_audited(admin_client, image_bytes):
    product = ProductFactory(name="Old name")
    upload = SimpleUploadedFile("photo.png", image_bytes("PNG"), content_type="image/png")
    data = _product_form(product, name="New name", new_price="499.00", add_image=upload, add_image_primary="on")
    response = admin_client.post(reverse("admin:catalog_product_change", args=[product.pk]), data)
    assert response.status_code == 302, response.context["adminform"].form.errors if response.context else response
    product.refresh_from_db()
    assert product.name == "New name"
    assert product.media.get().is_primary
    assert pricing.current_price(product).amount == 499
    event = AuditEvent.objects.get(action="catalog.product.updated")
    assert event.actor_type == "ADMIN" and event.after == {"name": "New name"}


def test_admin_rejects_non_image_upload(admin_client):
    product = ProductFactory()
    upload = SimpleUploadedFile("photo.jpg", b"not an image", content_type="image/jpeg")
    response = admin_client.post(
        reverse("admin:catalog_product_change", args=[product.pk]), _product_form(product, add_image=upload)
    )
    assert response.status_code == 200  # form redisplayed with an error
    assert not product.media.exists()


def test_admin_add_product(admin_client):
    category = CategoryFactory()
    data = _product_form(ProductFactory.build(category=category), sku="new-77", name="Brand new thing")
    data["status"] = "DRAFT"
    response = admin_client.post(reverse("admin:catalog_product_add"), data)
    assert response.status_code == 302, response.context["adminform"].form.errors
    product = Product.objects.get(sku="NEW-77")
    assert AuditEvent.objects.filter(action="catalog.product.created", object_id=str(product.pk)).exists()


def test_delete_action_removed_for_products(admin_client):
    response = admin_client.get(reverse("admin:catalog_product_changelist"))
    assert b"delete_selected" not in response.content


def test_import_upload_validates_in_background(admin_client):
    csv = b"sku,name,category\nNEW-1,Thing,Tools\n"
    upload = SimpleUploadedFile("stock.csv", csv, content_type="text/csv")
    response = admin_client.post(reverse("admin:catalog_importbatch_add"), {"file": upload})
    assert response.status_code == 302
    batch = ImportBatch.objects.get()
    # Celery runs eagerly in tests and on_commit fires at the end of the request transaction.
    batch.refresh_from_db()
    assert batch.status in (ImportStatus.VALIDATED, ImportStatus.UPLOADED)


def test_commit_action_requires_permission(client, staff_user):
    ImportBatch.objects.create(file="imports/x.csv", original_filename="x.csv")  # actions show only for non-empty lists
    staff_user.user_permissions.add(*Permission.objects.filter(codename__in=["view_importbatch", "add_importbatch"]))
    client.force_login(staff_user)
    response = client.get(reverse("admin:catalog_importbatch_changelist"))
    assert response.status_code == 200
    assert b"approve_and_import" not in response.content
    staff_user.user_permissions.add(Permission.objects.get(codename="commit_importbatch"))
    response = client.get(reverse("admin:catalog_importbatch_changelist"))
    assert b"approve_and_import" in response.content


def test_proposal_approval_action(admin_client):
    product = ProductFactory()
    proposal, _ = services.propose_change(product, "colour", "Black", Actor.agent("catalogue"), source="agent")
    response = admin_client.post(
        reverse("admin:catalog_catalogchangeproposal_changelist"),
        {"action": "approve", "_selected_action": [str(proposal.pk)]},
    )
    assert response.status_code == 302
    proposal.refresh_from_db()
    product.refresh_from_db()
    assert proposal.status == ProposalStatus.APPLIED and product.colour == "Black"
    assert CatalogChangeProposal.objects.filter(status=ProposalStatus.PENDING).count() == 0
