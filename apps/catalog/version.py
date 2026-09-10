"""Version del catalogo.

Existe por una razon concreta: una terminal puede estar vendiendo sin conexion con
una lista de precios vieja. Cuando sincroniza hay que poder distinguir "cobro bien"
de "cobro con la lista de anteayer".

La version NO guarda precios historicos: guarda un numero que sube cuando el
catalogo cambia. Si la terminal manda una version distinta a la actual, la venta
se registra igual -el cliente ya se fue con su trago- pero queda marcada.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import F


@transaction.atomic
def subir_version_del_catalogo(tenant=None) -> int:
    """Sube la version del catalogo y devuelve la nueva."""
    from apps.tenancy.models import Tenant

    if tenant is None:
        from apps.core.context import get_current_tenant

        tenant = get_current_tenant()
        if tenant is None:
            raise RuntimeError("Sin tenant en contexto para versionar el catalogo.")

    Tenant.objects.filter(pk=tenant.pk).update(catalogo_version=F("catalogo_version") + 1)
    tenant.refresh_from_db(fields=["catalogo_version"])
    return tenant.catalogo_version


def version_actual(tenant) -> int:
    return int(tenant.catalogo_version or 1)
