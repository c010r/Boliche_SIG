from __future__ import annotations

import uuid

from django.db import models

from apps.core.context import (
    TenantNotSetError,
    get_current_tenant,
)
from apps.core.managers import TenantManager, TenantMismatchError, UnscopedManager


class UUIDModel(models.Model):
    """Clave primaria UUID generada en el origen.

    Es una de las decisiones que no se pueden posponer: los IDs generados en el
    cliente son lo que hace posible la cola offline sin colisiones (seccion 4).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimeStampedModel(models.Model):
    creado_en = models.DateTimeField(auto_now_add=True, db_index=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class TenantModel(UUIDModel, TimeStampedModel):
    """Base de todo modelo de negocio.

    objects es fail-closed (filtra por tenant o revienta).
    unscoped es el escape explicito para tareas de plataforma.

    base_manager_name queda apuntando a unscoped para que las rutas internas de
    Django (acceso por FK, recoleccion en cascada) no revienten.
    """

    tenant = models.ForeignKey(
        "tenancy.Tenant",
        on_delete=models.PROTECT,
        related_name="+",
        verbose_name="Boliche",
    )

    objects = TenantManager()
    unscoped = UnscopedManager()

    class Meta:
        abstract = True
        base_manager_name = "unscoped"
        default_manager_name = "objects"

    def save(self, *args, **kwargs):
        """Fuerza el tenant del contexto en TODOS los caminos de escritura.

        El manager cubre objects.create(), pero get_or_create(), update_or_create()
        y cualquier ruta que llame al create del queryset lo saltean. Ponerlo aca
        es lo que hace que la regla no tenga agujeros.
        """
        tenant = get_current_tenant()

        if self.tenant_id is None:
            if tenant is None:
                raise TenantNotSetError(
                    "Sin tenant en contexto: no se puede guardar "
                    + self._meta.label
                    + ". Usa " + self._meta.model_name + ".unscoped."
                )
            self.tenant = tenant
        elif tenant is not None and self.tenant_id != tenant.id:
            raise TenantMismatchError(
                "El objeto pertenece a otro boliche que el del contexto en "
                + self._meta.label
                + "."
            )

        return super().save(*args, **kwargs)
