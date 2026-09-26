from django import forms
from django.contrib import admin

from apps.core.actors import Actor
from apps.core.exceptions import DomainError

from . import services
from .models import Customer


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ("name", "kind", "phone", "email", "kra_pin", "address", "whatsapp_opt_in", "notes")

    def clean_phone(self):
        phone = self.cleaned_data.get("phone")
        if not phone:
            return None
        try:
            return services.normalise_phone(phone)
        except DomainError as exc:
            raise forms.ValidationError(exc.message) from exc


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    form = CustomerForm
    list_display = ("name", "phone", "kind", "email", "whatsapp_opt_in", "created_at")
    list_filter = ("kind", "whatsapp_opt_in")
    search_fields = ("name", "phone", "email", "kra_pin")

    def save_model(self, request, obj, form, change):
        who = Actor.for_user(request.user)
        data = {f: form.cleaned_data[f] for f in form.Meta.fields}
        if change:
            services.update_customer(Customer.objects.get(pk=obj.pk), {f: data[f] for f in form.changed_data}, who)
        else:
            created = services.create_customer(who, name=data.pop("name"), phone=data.pop("phone") or "", **data)
            obj.pk, obj._state.adding = created.pk, False
