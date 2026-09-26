"""Public quote requests from the website basket (replaces the plain-text WhatsApp list).

The customer still ends up in WhatsApp: the response contains a wa.me link
whose message quotes the new quotation number, so staff can open it in admin.
"""

import re
from decimal import Decimal
from urllib.parse import quote as urlquote

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from apps.catalog.models import Product, ProductStatus
from apps.catalog.selectors import product_by_code
from apps.core.actors import Actor
from apps.core.exceptions import ValidationError
from apps.core.models import BusinessProfile
from apps.customers import services as customers

from . import services
from .models import Channel

MAX_ITEMS = 50


class QuoteItemSerializer(serializers.Serializer):
    sku = serializers.CharField(required=False, max_length=64)
    legacy_id = serializers.IntegerField(required=False, min_value=0)
    quantity = serializers.DecimalField(max_digits=12, decimal_places=3, min_value=Decimal("0.001"))

    def validate(self, attrs):
        if not attrs.get("sku") and attrs.get("legacy_id") is None:
            raise serializers.ValidationError("Give a sku or legacy_id for each item.")
        return attrs


class QuoteRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=30)
    notes = serializers.CharField(max_length=1000, required=False, allow_blank=True)
    items = QuoteItemSerializer(many=True)

    def validate_items(self, items):
        if not items:
            raise serializers.ValidationError("Add at least one product.")
        if len(items) > MAX_ITEMS:
            raise serializers.ValidationError(f"At most {MAX_ITEMS} products per request.")
        return items


class QuoteRequestView(APIView):
    permission_classes = [AllowAny]
    authentication_classes: list = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "quote_request"

    @extend_schema(
        request=QuoteRequestSerializer,
        parameters=[OpenApiParameter("Idempotency-Key", str, OpenApiParameter.HEADER, required=True)],
        responses={201: dict},
    )
    def post(self, request):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9._-]{8,100}", key):
            raise ValidationError("Send an Idempotency-Key header (8-100 letters, digits, . _ -).")
        serializer = QuoteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        lines, unknown = [], []
        for item in data["items"]:
            product = (
                product_by_code(item["sku"])
                if item.get("sku")
                else Product.objects.filter(legacy_id=item["legacy_id"]).first()
            )
            if product is None or product.status != ProductStatus.ACTIVE:
                unknown.append(item.get("sku") or item.get("legacy_id"))
                continue
            if any(line["product"].pk == product.pk for line in lines):
                continue
            lines.append({"product": product, "quantity": item["quantity"]})
        if unknown:
            raise ValidationError("Some products are not available.", details={"unavailable": unknown})

        actor = Actor.integration("website")
        customer, _ = customers.get_or_create_by_phone(data["phone"], actor, name=data["name"])
        quote = services.create_quotation(
            actor,
            customer=customer,
            lines=lines,
            channel=Channel.WEBSITE,
            notes=data.get("notes", ""),
            idempotency_key=f"web:{key}",
        )
        profile = BusinessProfile.get()
        message = (
            f"Hello {profile.business_name}! I have sent quote request {quote.number} "
            f"({quote.lines.count()} products) from your website. Please send me prices and availability. Thank you!"
        )
        return Response(
            {
                "quotation": quote.number,
                "status": quote.status,
                "items": quote.lines.count(),
                "whatsapp_url": f"https://wa.me/{profile.whatsapp_sales_number}?text={urlquote(message)}",
            },
            status=status.HTTP_201_CREATED,
        )
