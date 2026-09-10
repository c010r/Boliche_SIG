from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from apps.accounts.models import (
    AuditoriaLog,
    MembresiaUsuario,
    Permiso,
    Rol,
    RolPermiso,
    Usuario,
)


@admin.register(Permiso)
class PermisoAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre")
    search_fields = ("codigo", "nombre")


@admin.register(Rol)
class RolAdmin(admin.ModelAdmin):
    list_display = ("nombre", "codigo", "tenant")
    list_filter = ("tenant",)


@admin.register(Usuario)
class UsuarioAdmin(BaseUserAdmin):
    list_display = ("nombre", "apellido", "numero", "tenant", "is_active", "is_staff")
    list_filter = ("tenant", "is_active", "is_staff")
    search_fields = ("nombre", "apellido", "numero", "email")
    ordering = ("nombre",)
    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Boliche", {"fields": ("tenant", "numero", "nombre", "apellido")}),
        ("Permisos", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")}),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("email", "nombre", "password1", "password2")}),
    )


@admin.register(MembresiaUsuario)
class MembresiaUsuarioAdmin(admin.ModelAdmin):
    list_display = ("usuario", "local", "rol", "activo")
    list_filter = ("tenant", "rol", "activo")


admin.site.register(RolPermiso)


@admin.register(AuditoriaLog)
class AuditoriaLogAdmin(admin.ModelAdmin):
    list_display = ("creado_en", "accion", "entidad", "entidad_id", "usuario", "tenant")
    list_filter = ("accion", "tenant")
    search_fields = ("entidad", "entidad_id")
    readonly_fields = [f.name for f in AuditoriaLog._meta.fields]

    def has_add_permission(self, request):  # la auditoria no se carga a mano
        return False
