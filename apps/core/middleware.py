from __future__ import annotations

from apps.core.context import clear_current_tenant, set_current_tenant


class TenantMiddleware:
    """Resuelve el tenant de la request y lo publica en el contexto.

    Orden de resolucion (seccion 6):
    1. subdominio (bolicox.ejemplo.com -> slug bolicox)
    2. sesion autenticada (el usuario ya trae su tenant)

    La tienda publica de entradas, que es anonima, se resolvera por dominio
    propio cuando exista (seccion 9 bis). Hoy no esta implementada.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tenant = self.resolve_tenant(request)
        token = set_current_tenant(tenant)
        request.tenant = tenant
        try:
            return self.get_response(request)
        finally:
            clear_current_tenant(token)

    def resolve_tenant(self, request):
        from apps.tenancy.models import Tenant

        slug = self.slug_from_host(request.get_host())

        if slug:
            # Tenant es la raiz del aislamiento: no esta scopeado por tenant,
            # asi que su manager por defecto ya es el unscoped.
            tenant = Tenant.objects.filter(
                slug=slug, estado=Tenant.Estado.ACTIVO
            ).first()
            if tenant is not None:
                return tenant

        usuario = getattr(request, "user", None)
        if usuario is not None and getattr(usuario, "is_authenticated", False):
            return getattr(usuario, "tenant", None)

        return None

    @staticmethod
    def slug_from_host(host: str) -> str:
        """bolicox.ejemplo.com:8000 -> bolicox. Sin subdominio -> cadena vacia."""
        nombre = (host or "").split(":")[0]
        partes = nombre.split(".")
        if len(partes) >= 3:
            return partes[0]
        return ""
