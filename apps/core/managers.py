from __future__ import annotations

from django.db import models

from apps.core.context import TenantNotSetError, get_current_tenant


class TenantMismatchError(RuntimeError):
    """Se intento crear un objeto para un tenant distinto al del contexto."""


class UnscopedManager(models.Manager):
    """Acceso sin filtro de tenant.

    Existe para tareas de plataforma (provisioning, metricas, export por tenant).
    Es el UNICO camino que puede cruzar boliches, y por eso se pide por nombre.
    """


class TenantManager(models.Manager):
    """Manager por defecto de todo modelo de negocio: fail-closed.

    - Sin tenant en contexto: TenantNotSetError. Nunca devuelve todo.
    - Con tenant en contexto: filtra por el, siempre.
    """

    def get_queryset(self):
        tenant = get_current_tenant()
        if tenant is None:
            raise TenantNotSetError(
                "Sin tenant en contexto: no se puede consultar "
                + self.model._meta.label
                + ". Si es una tarea de plataforma, usa "
                + self.model._meta.model_name
                + ".unscoped."
            )
        return super().get_queryset().filter(tenant=tenant)

    def create(self, **kwargs):
        """Fuerza el tenant del contexto y rechaza cualquier otro.

        Sin esto, codigo con contexto del boliche A podria escribir filas de B.
        El provisioning de plataforma usa .unscoped.create().
        """
        tenant = get_current_tenant()
        if tenant is None:
            raise TenantNotSetError(
                "Sin tenant en contexto: no se puede crear "
                + self.model._meta.label
                + ". Usa " + self.model._meta.model_name + ".unscoped."
            )
        declarado = kwargs.get("tenant")
        if declarado is not None and declarado != tenant:
            raise TenantMismatchError(
                "El tenant declarado no coincide con el del contexto en "
                + self.model._meta.label
                + "."
            )
        kwargs["tenant"] = tenant
        return super().create(**kwargs)
