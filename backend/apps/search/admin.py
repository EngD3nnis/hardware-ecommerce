from django.contrib import admin

from .models import Synonym


@admin.register(Synonym)
class SynonymAdmin(admin.ModelAdmin):
    list_display = ("terms", "is_active")
    list_editable = ("is_active",)
    search_fields = ("terms",)
