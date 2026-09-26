"""Public, read-only catalogue API (anonymous access).

Prices are included only when BusinessProfile.show_prices_online is on;
otherwise every product reads "price on request", as on the website today.
"""

from django.db.models import Count, Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import generics, serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.core.exceptions import NotFound, ValidationError
from apps.core.models import BusinessProfile
from apps.pricing import services as pricing
from apps.pricing.models import PriceList

from . import selectors
from .models import Brand, Category, Product, ProductStatus

MAX_LEGACY_IDS = 100


class CategorySerializer(serializers.ModelSerializer):
    children = serializers.SerializerMethodField()

    class Meta:
        model = Category
        fields = ["slug", "name", "description", "children"]

    def get_children(self, obj) -> list[dict]:
        return [{"slug": c.slug, "name": c.name} for c in obj.children.all()]


class BrandSerializer(serializers.ModelSerializer):
    class Meta:
        model = Brand
        fields = ["slug", "name"]


class PriceSerializer(serializers.Serializer):
    on_request = serializers.BooleanField()
    amount = serializers.DecimalField(max_digits=12, decimal_places=2, allow_null=True)
    currency = serializers.CharField()
    includes_vat = serializers.BooleanField()


class ProductSerializer(serializers.ModelSerializer):
    category = serializers.SerializerMethodField()
    brand = serializers.SerializerMethodField()
    images = serializers.SerializerMethodField()
    attributes = serializers.SerializerMethodField()
    price = serializers.SerializerMethodField()

    class Meta:
        model = Product
        fields = [
            "sku",
            "legacy_id",
            "name",
            "slug",
            "description",
            "category",
            "brand",
            "unit_of_measure",
            "material",
            "finish",
            "colour",
            "size_label",
            "barcode",
            "images",
            "attributes",
            "price",
        ]

    def get_category(self, obj) -> dict:
        parent = obj.category.parent
        return {
            "slug": obj.category.slug,
            "name": obj.category.name,
            "parent": {"slug": parent.slug, "name": parent.name} if parent else None,
        }

    def get_brand(self, obj) -> dict | None:
        return {"slug": obj.brand.slug, "name": obj.brand.name} if obj.brand else None

    def get_images(self, obj) -> list[dict]:
        request = self.context.get("request")
        images = []
        for item in obj.media.all():
            url = item.asset.file.url
            images.append(
                {
                    "url": request.build_absolute_uri(url) if request and url.startswith("/") else url,
                    "alt": item.alt_text,
                    "width": item.asset.width,
                    "height": item.asset.height,
                    "primary": item.is_primary,
                }
            )
        return images

    def get_attributes(self, obj) -> list[dict]:
        return [
            {"code": v.attribute.code, "label": v.attribute.label, "value": v.value, "unit": v.attribute.unit}
            for v in obj.attribute_values.all()
        ]

    def get_price(self, obj) -> dict:
        price_list = self.context["price_list"]
        if not self.context["show_prices"] or price_list is None:
            return {
                "on_request": True,
                "amount": None,
                "currency": price_list.currency if price_list else "KES",
                "includes_vat": True,
            }
        quote = pricing.quote_price(obj, price_list)
        return {
            "on_request": quote.on_request,
            # Money as a string with 2 decimals, never a float.
            "amount": f"{quote.amount:.2f}" if quote.amount is not None else None,
            "currency": quote.currency,
            "includes_vat": quote.includes_vat,
        }


def catalog_context(request) -> dict:
    return {
        "request": request,
        "show_prices": BusinessProfile.get().show_prices_online,
        "price_list": PriceList.objects.filter(is_default=True).first(),
    }


class PublicCatalogMixin:
    """Anonymous read access, rate limited per client IP."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "public"

    def get_serializer_context(self):
        return {**super().get_serializer_context(), **catalog_context(self.request)}


class CategoryListView(PublicCatalogMixin, generics.ListAPIView):
    serializer_class = CategorySerializer
    pagination_class = None

    def get_queryset(self):
        return selectors.category_tree()


class BrandListView(PublicCatalogMixin, generics.ListAPIView):
    serializer_class = BrandSerializer
    pagination_class = None

    def get_queryset(self):
        return (
            Brand.objects.filter(is_active=True)
            .annotate(n=Count("products", filter=Q(products__status=ProductStatus.ACTIVE)))
            .filter(n__gt=0)
        )


@extend_schema(
    parameters=[
        OpenApiParameter("category", str, description="Category or subcategory slug"),
        OpenApiParameter("brand", str, description="Brand slug"),
        OpenApiParameter("q", str, description="SKU, barcode or words in the name"),
    ]
)
class ProductListView(PublicCatalogMixin, generics.ListAPIView):
    serializer_class = ProductSerializer
    filter_backends: list = []  # explicit filters below; ranked search comes with apps.search

    def get_queryset(self):
        params = self.request.query_params
        qs = selectors.public_products()
        return selectors.filter_products(
            qs,
            category=params.get("category", "").strip(),
            brand=params.get("brand", "").strip(),
            q=params.get("q", "").strip()[:100],
        )


class ProductDetailView(PublicCatalogMixin, generics.RetrieveAPIView):
    serializer_class = ProductSerializer

    def get_object(self):
        product = selectors.product_by_code(self.kwargs["code"], selectors.public_products())
        if product is None:
            raise NotFound("No such product.")
        return product


@extend_schema(
    parameters=[OpenApiParameter("ids", str, required=True, description="Comma-separated legacy website ids")],
    responses=ProductSerializer(many=True),
)
class LegacyProductsView(PublicCatalogMixin, APIView):
    """Resolve the product ids used in old website/WhatsApp quote links (e.g. ?ids=0,12,45)."""

    def get(self, request):
        raw = request.query_params.get("ids", "")
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if not tokens or not all(t.isdigit() for t in tokens):
            raise ValidationError("ids must be a comma-separated list of whole numbers.")
        ids = list(dict.fromkeys(int(t) for t in tokens))
        if len(ids) > MAX_LEGACY_IDS:
            raise ValidationError(f"At most {MAX_LEGACY_IDS} ids per request.")
        products = selectors.products_by_legacy_ids(ids, selectors.public_products())
        serializer = ProductSerializer(products, many=True, context=catalog_context(request))
        return Response(serializer.data)


class BusinessProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = BusinessProfile
        fields = [
            "business_name",
            "whatsapp_sales_number",
            "other_phone_numbers",
            "email",
            "website_url",
            "address",
            "map_url",
            "opening_hours",
            "show_prices_online",
        ]


class BusinessProfileView(PublicCatalogMixin, generics.RetrieveAPIView):
    """Public contact details for websites and quote links."""

    serializer_class = BusinessProfileSerializer

    def get_object(self):
        return BusinessProfile.get()
