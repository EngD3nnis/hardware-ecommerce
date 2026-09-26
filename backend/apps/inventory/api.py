"""Staff-only inventory API."""

from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.catalog.selectors import product_by_code
from apps.core.exceptions import NotFound

from . import services
from .models import StockBalance

AvailabilitySerializer = inline_serializer(
    "Availability",
    {
        "sku": serializers.CharField(),
        "on_hand": serializers.DecimalField(12, 3),
        "reserved": serializers.DecimalField(12, 3),
        "available": serializers.DecimalField(12, 3),
        "damaged": serializers.DecimalField(12, 3),
        "incoming": serializers.DecimalField(12, 3),
        "locations": serializers.ListField(child=serializers.DictField()),
    },
)


class AvailabilityView(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(responses=AvailabilitySerializer)
    def get(self, request, code):
        product = product_by_code(code)
        if product is None:
            raise NotFound("No such product.")
        totals = services.availability(product)
        locations = [
            {"location": b.location.code, "on_hand": str(b.on_hand), "reserved": str(b.reserved),
             "available": str(b.available), "damaged": str(b.damaged)}
            for b in StockBalance.objects.filter(product=product).select_related("location")
        ]  # fmt: skip
        return Response(
            {
                "sku": product.sku,
                "on_hand": str(totals.on_hand),
                "reserved": str(totals.reserved),
                "available": str(totals.available),
                "damaged": str(totals.damaged),
                "incoming": str(totals.incoming),
                "locations": locations,
            }
        )
