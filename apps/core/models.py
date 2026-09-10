from __future__ import annotations

import uuid

from django.db import models

from apps.core.managers import TenantManager, UnscopedManager


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
