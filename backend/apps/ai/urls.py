from django.urls import path

from . import views

urlpatterns = [
    path("", views.control_centre, name="ops-control-centre"),
    path("automations/", views.toggle_all, name="ops-toggle-all"),
    path("agents/<slug:name>/toggle/", views.toggle_agent, name="ops-toggle-agent"),
]
