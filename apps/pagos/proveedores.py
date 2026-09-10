"""Proveedores de pago.

El contrato es chico a proposito: crear un intento de cobro y traducir la
notificacion del adquirente a algo que el sistema entienda.

Sobre Mercado Pago, tres decisiones que no son obvias:

1. Se usa la **Orders API** (POST /v1/orders), no las APIs anteriores. La de QR
   legacy esta en discontinuacion y su propia documentacion dice que las
   integraciones nuevas usen Orders.
2. Las ordenes de QR vencen a los 15 minutos por defecto. Nuestra reserva vence
   antes (10 minutos), que es la direccion segura: si el pago se acredita tarde,
   la reserva ya vencio y el sistema lo detecta en vez de sobrevender.
3. La notificacion SOLO avisa que algo cambio: el estado se va a buscar con un
   GET. Confiar en el cuerpo seria confiar en datos no verificados.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.request
import uuid
from decimal import Decimal

from django.conf import settings


class ErrorDelProveedor(RuntimeError):
    """El adquirente rechazo, o no se pudo contactar."""


class ProveedorDePago:
    """Contrato que cumple cualquier adquirente."""

    nombre = "base"

    def crear_intento(self, *, reserva, url_retorno: str) -> dict:
        """Devuelve un dict con url, referencia y, si aplica, el QR."""
        raise NotImplementedError

    def interpretar_webhook(self, *, payload: dict, cabeceras: dict) -> dict:
        """Traduce la notificacion a referencia, estado y detalle.

        estado es uno de: aprobado, rechazado, pendiente.
        """
        raise NotImplementedError


class ProveedorSimulado(ProveedorDePago):
    """Solo para desarrollo y demostracion. NO cobra nada."""

    nombre = "simulado"

    def crear_intento(self, *, reserva, url_retorno: str) -> dict:
        return {
            "url": url_retorno + "?pago=simulado",
            "referencia": "SIM-" + reserva.token[:16],
        }

    def interpretar_webhook(self, *, payload: dict, cabeceras: dict) -> dict:
        return {
            "referencia": (payload.get("referencia") or "").strip(),
            "estado": (payload.get("estado") or "aprobado").strip(),
            "detalle": "simulado",
        }


# --- Mercado Pago ---------------------------------------------------------

# Estados de la ORDEN segun la documentacion de Mercado Pago.
ESTADOS_DE_ORDEN = {
    "created": "pendiente",
    "processed": "aprobado",
    "canceled": "rechazado",
    "expired": "rechazado",
    "refunded": "rechazado",
}

# Estados de la TRANSACCION, que es el que dice si la plata entro de verdad.
ESTADOS_DE_TRANSACCION = {
    "created": "pendiente",
    "processed": "aprobado",
    "expired": "rechazado",
    "canceled": "rechazado",
    "refunded": "rechazado",
}


class ProveedorMercadoPago(ProveedorDePago):
    """Orders API de Mercado Pago.

    El transporte HTTP es inyectable para poder probar sin red: los tests pasan
    un transporte falso y verifican el cuerpo y las cabeceras que se mandan.
    """

    nombre = "mercadopago"
    base = "https://api.mercadopago.com"

    def __init__(self, *, credencial, transporte=None):
        self.credencial = credencial
        self._transporte = transporte or _transporte_urllib

    # --- cobro ---------------------------------------------------------

    def crear_intento(self, *, reserva, url_retorno: str) -> dict:
        if not getattr(self.credencial, "configurada", False):
            raise ErrorDelProveedor(
                "El boliche no tiene configurada su cuenta de Mercado Pago."
            )

        monto = _a_decimal(reserva.tipo.precio) * reserva.cantidad
        cuerpo = {
            "type": "qr",
            "total_amount": float(monto),
            "description": f"{reserva.cantidad} x {reserva.tipo.nombre}"[:150],
            # Tiene que ser unico por orden: el token de la reserva lo es.
            "external_reference": reserva.token,
            "config": {
                "qr": {
                    "external_pos_id": self.credencial.external_pos_id,
                    "mode": "dynamic",
                }
            },
            "transactions": {"payments": [{"amount": float(monto)}]},
            "items": [
                {
                    "title": reserva.tipo.nombre[:150],
                    "unit_price": float(_a_decimal(reserva.tipo.precio)),
                    "quantity": reserva.cantidad,
                }
            ],
        }

        respuesta = self._transporte(
            "POST",
            self.base + "/v1/orders",
            cuerpo,
            {
                "Authorization": f"Bearer {self.credencial.access_token}",
                "Content-Type": "application/json",
                # Idempotencia: un reintento no crea dos ordenes.
                "X-Idempotency-Key": str(uuid.uuid4()),
            },
        )

        orden_id = (respuesta or {}).get("id")
        if not orden_id:
            raise ErrorDelProveedor(
                "Mercado Pago no devolvio el identificador de la orden."
            )

        return {
            "url": url_retorno,
            "referencia": str(orden_id),
            "orden": respuesta,
            "qr": _qr_de(respuesta),
        }

    # --- notificacion --------------------------------------------------

    def interpretar_webhook(self, *, payload: dict, cabeceras: dict) -> dict:
        if not self.clave_de_firma_configurada():
            return {
                "referencia": _referencia_del_payload(payload),
                "estado": "pendiente",
                "detalle": "sin_clave_de_firma",
                "mensaje": (
                    "El boliche no tiene clave de firma: no se puede validar que la "
                    "notificacion venga de Mercado Pago. Configurala antes de cobrar."
                ),
            }

        if not self._firma_valida(payload, cabeceras):
            return {
                "referencia": "",
                "estado": "rechazado",
                "detalle": "firma_invalida",
                "mensaje": "La firma de la notificacion no es valida.",
            }

        orden_id = _orden_id_del_payload(payload)
        if not orden_id:
            return {
                "referencia": "",
                "estado": "pendiente",
                "detalle": "sin_orden",
                "mensaje": "La notificacion no trae el identificador de la orden.",
            }

        orden = self._transporte(
            "GET",
            self.base + f"/v1/orders/{orden_id}",
            None,
            {"Authorization": f"Bearer {self.credencial.access_token}"},
        )

        estado, detalle = _estado_de_la_orden(orden)
        return {
            "referencia": str(orden.get("external_reference") or orden_id),
            "orden_id": str(orden_id),
            "estado": estado,
            "detalle": detalle,
            "orden": orden,
        }

    # --- firma ---------------------------------------------------------

    def clave_de_firma_configurada(self) -> bool:
        return bool(getattr(self.credencial, "clave_de_firma", ""))

    def _firma_valida(self, payload: dict, cabeceras: dict) -> bool:
        """Esquema de firma de Mercado Pago: ts y v1 en el encabezado x-signature."""
        encabezado = _cabecera(cabeceras, "x-signature")
        if not encabezado:
            return False

        partes = {}
        for trozo in encabezado.split(","):
            if "=" in trozo:
                clave, valor = trozo.split("=", 1)
                partes[clave.strip()] = valor.strip()

        ts = partes.get("ts")
        v1 = partes.get("v1")
        if not ts or not v1:
            return False

        data_id = str((payload.get("data") or {}).get("id") or payload.get("id") or "")
        request_id = _cabecera(cabeceras, "x-request-id")

        plantilla = f"id:{data_id};request-id:{request_id};ts:{ts};"
        calculada = hmac.new(
            self.credencial.clave_de_firma.encode("utf-8"),
            plantilla.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(calculada, v1)


# --- utilidades -----------------------------------------------------------


def _a_decimal(valor) -> Decimal:
    return Decimal(str(valor if valor is not None else "0"))


def _cabecera(cabeceras: dict, nombre: str) -> str:
    for clave, valor in (cabeceras or {}).items():
        if clave.lower() == nombre:
            return str(valor)
    return ""


def _referencia_del_payload(payload: dict) -> str:
    return str(
        payload.get("external_reference")
        or (payload.get("data") or {}).get("external_reference")
        or ""
    )


def _orden_id_del_payload(payload: dict) -> str:
    datos = payload.get("data") or {}
    return str(datos.get("id") or payload.get("id") or "")


def _qr_de(orden: dict) -> str:
    return (
        (orden.get("config") or {}).get("qr", {}).get("template_document")
        or (orden.get("point_of_interaction") or {})
        .get("transaction_data", {})
        .get("qr_code", "")
        or ""
    )


def _estado_de_la_orden(orden: dict) -> tuple:
    """Traduce la orden a aprobado, rechazado o pendiente.

    Se mira primero la TRANSACCION: una orden puede estar procesada con la
    transaccion todavia creada, y en ese caso la plata no entro.
    """
    transacciones = (orden or {}).get("transactions") or {}
    pagos = transacciones.get("payments") or []
    if pagos:
        estado_pago = str((pagos[0] or {}).get("status") or "")
        if estado_pago:
            return (ESTADOS_DE_TRANSACCION.get(estado_pago, "pendiente"), estado_pago)

    estado = str((orden or {}).get("status") or "")
    return (ESTADOS_DE_ORDEN.get(estado, "pendiente"), estado)


def _transporte_urllib(metodo: str, url: str, cuerpo, cabeceras: dict) -> dict:
    datos = json.dumps(cuerpo).encode("utf-8") if cuerpo is not None else None
    pedido = urllib.request.Request(url, data=datos, method=metodo)
    for clave, valor in (cabeceras or {}).items():
        pedido.add_header(clave, valor)

    try:
        with urllib.request.urlopen(pedido, timeout=20) as respuesta:
            return json.loads(respuesta.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as error:
        detalle = error.read().decode("utf-8", "replace")[:400]
        raise ErrorDelProveedor(
            f"Mercado Pago respondio {error.code}: {detalle}"
        ) from error
    except urllib.error.URLError as error:
        raise ErrorDelProveedor(
            f"No se pudo contactar al adquirente: {error}"
        ) from error


# --- seleccion ------------------------------------------------------------

_PROVEEDORES = {
    ProveedorSimulado.nombre: ProveedorSimulado,
    ProveedorMercadoPago.nombre: ProveedorMercadoPago,
}


def proveedor_para(tenant):
    """Proveedor configurado para el boliche, listo para usar.

    Si el boliche no tiene credenciales propias, cae al simulado: es preferible
    que una demostracion funcione antes que reventar por falta de configuracion.
    """
    from apps.pagos.models import CredencialDePago

    predeterminado = getattr(settings, "PROVEEDOR_DE_PAGO", "simulado")

    credencial = (
        CredencialDePago.unscoped.filter(tenant=tenant, activo=True).first()
        if tenant is not None
        else None
    )

    if credencial is None:
        return _PROVEEDORES[predeterminado]()

    clase = _PROVEEDORES.get(credencial.proveedor)
    if clase is None:
        raise ErrorDelProveedor(
            f"Proveedor desconocido: {credencial.proveedor}. "
            f"Disponibles: {', '.join(_PROVEEDORES)}."
        )

    if clase is ProveedorSimulado:
        return ProveedorSimulado()
    return clase(credencial=credencial)


def proveedor_actual() -> ProveedorDePago:
    """Proveedor segun la configuracion, sin boliche. No sirve para cobrar."""
    nombre = getattr(settings, "PROVEEDOR_DE_PAGO", "simulado")
    clase = _PROVEEDORES.get(nombre)
    if clase is None:
        raise ErrorDelProveedor(f"Proveedor desconocido: {nombre}.")
    if clase is ProveedorSimulado:
        return ProveedorSimulado()
    raise ErrorDelProveedor(
        "Mercado Pago necesita las credenciales de un boliche: usa proveedor_para()."
    )
