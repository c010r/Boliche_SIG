"""Proveedor de pago, detras de una interfaz.

IMPORTANTE: Mercado Pago NO esta integrado todavia. Lo que hay es el contrato que
MP tiene que implementar, mas un proveedor simulado para desarrollo y demo. La
integracion real necesita credenciales por boliche (cada local es el comerciante:
ver ANALISIS.md seccion 9 bis).

Cuando se integre, ademas, hay que usar la Orders API nueva y no la de QR legacy,
que esta en discontinuacion.
"""

from __future__ import annotations

from django.conf import settings


class ProveedorDePago:
    """Contrato que tiene que cumplir cualquier adquirente."""

    nombre = "base"

    def crear_intento(self, *, reserva, url_retorno: str) -> dict:
        """Devuelve {'url': ..., 'referencia': ...} para mandar al comprador."""
        raise NotImplementedError

    def interpretar_webhook(self, *, payload: dict, cabeceras: dict) -> dict:
        """Traduce la notificacion a {'referencia', 'estado', 'monto'}.

        El estado es uno de: aprobado, rechazado, pendiente.
        """
        raise NotImplementedError


class ProveedorSimulado(ProveedorDePago):
    """Solo para desarrollo y demostracion. NO cobra nada."""

    nombre = "simulado"

    def crear_intento(self, *, reserva, url_retorno: str) -> dict:
        return {
            "url": f"{url_retorno}?pago=simulado",
            "referencia": f"SIM-{reserva.token[:16]}",
        }

    def interpretar_webhook(self, *, payload: dict, cabeceras: dict) -> dict:
        return {
            "referencia": (payload.get("referencia") or "").strip(),
            "estado": (payload.get("estado") or "aprobado").strip(),
            "monto": payload.get("monto"),
        }


_PROVEEDORES = {
    ProveedorSimulado.nombre: ProveedorSimulado,
}


def proveedor_actual() -> ProveedorDePago:
    nombre = getattr(settings, "PROVEEDOR_DE_PAGO", "simulado")
    clase = _PROVEEDORES.get(nombre)
    if clase is None:
        raise ImproperlyConfiguredPago(
            f"Proveedor de pago desconocido: {nombre}. "
            f"Disponibles: {', '.join(_PROVEEDORES)}."
        )
    return clase()


class ImproperlyConfiguredPago(RuntimeError):
    pass
