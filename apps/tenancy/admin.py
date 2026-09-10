from django.contrib import admin

from apps.tenancy.models import Local, Tenant, Terminal


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("nombre", "slug", "plan", "estado", "creado_en")
    search_fields = ("nombre", "slug", "rut")
    prepopulated_fields = {"slug": ("nombre",)}


@admin.register(Local)
class LocalAdmin(admin.ModelAdmin):
    list_display = ("nombre", "tenant", "aforo", "activo")
    list_filter = ("tenant", "activo")


@admin.register(Terminal)
class TerminalAdmin(admin.ModelAdmin):
    list_display = ("nombre", "tipo", "local", "activo")
    list_filter = ("tenant", "tipo", "activo")
