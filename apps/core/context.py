"""Contexto del tenant actual, seguro frente a async (contextvars).

El multi-tenant es row-based con tenant_id en cada tabla de negocio, pero el
riesgo real no es migrar: es filtrar (seccion 6 de ANALISIS.md). Por eso el
manager por defecto es fail-closed: si no hay tenant en contexto, revienta en
vez de devolver datos de todos los boliches.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager

_tenant_actual = contextvars.ContextVar("tenant_actual", default=None)


class TenantNotSetError(RuntimeError):
    """Se consultaron datos de negocio sin tenant en contexto."""


def get_current_tenant():
    """Devuelve el tenant en contexto, o None."""
    return _tenant_actual.get()


def set_current_tenant(tenant):
    """Publica el tenant y devuelve un token para restaurar el valor previo."""
    return _tenant_actual.set(tenant)


def clear_current_tenant(token) -> None:
    """Restaura el tenant previo usando el token de set_current_tenant."""
    _tenant_actual.reset(token)


@contextmanager
def tenant_context(tenant):
    """Ejecuta un bloque con un tenant fijo en contexto.

    Uso: tests, comandos de gestion y tareas de plataforma que operan un boliche
    a la vez.
    """
    token = set_current_tenant(tenant)
    try:
        yield tenant
    finally:
        clear_current_tenant(token)
