import csv

from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse
from django.shortcuts import render
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import services


def _days(request, default=30) -> int:
    try:
        return max(1, min(int(request.GET.get("days", default)), 365))
    except ValueError:
        return default


@staff_member_required
def reports(request):
    days = _days(request)
    context = {
        "days": days,
        "summary": services.summary(days),
        "inventory": services.inventory_value(),
        "dead_stock": services.dead_stock(),
        "reorder": services.reorder_suggestions(),
    }
    return render(request, "analytics/reports.html", context)


@staff_member_required
def daily_csv(request):
    summary = services.summary(_days(request, 90))
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="dewmix-daily-sales.csv"'
    writer = csv.writer(response)
    writer.writerow(["date", "orders", "revenue_paid_kes"])
    for row in summary["daily"]:
        writer.writerow([row["date"], row["orders"], row["revenue_paid"]])
    return response


class SummaryAPI(APIView):
    permission_classes = [IsAdminUser]

    @extend_schema(parameters=[OpenApiParameter("days", int)], responses=dict)
    def get(self, request):
        data = services.summary(_days(request))
        data["inventory"] = services.inventory_value()
        return Response(data)
