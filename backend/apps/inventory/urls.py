from django.urls import path

from . import api

urlpatterns = [
    path("products/<str:code>/availability/", api.AvailabilityView.as_view(), name="inventory-availability"),
]
