"""Operaciones de entradas y control de acceso.

Las invariantes que importan en una puerta real:

- El aforo NO se sobrevende: la venta se bloquea de forma atomica al llegar al cupo.
- Una entrada se usa UNA sola vez, con actualizacion condicional atomica.
- Una entrada de otro evento se rechaza.
- Anular no borra: deja rastro y quien lo hizo.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import List, Optional

import qrcode
import qrcode.image.svg
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.context import TenantNotSetError, get_current_tenant
from apps.ticketing.models import (
    Entrada,
    Evento,
    MovimientoAforo,
    TipoEntrada,
    generar_token,
)


class CupoAgotado(ValidationError):
    pass


class EntradaInvalida(ValidationError):
    pass


def _tenant_actual():
    tenant = get_current_tenant()
    if tenant is None:
        raise TenantNotSetError("Sin tenant en contexto: no se puede operar entradas.")
    return tenant


# --- eventos --------------------------------------------------------------


@transaction.atomic
def crear_evento(
    *, local, nombre: str, fecha, aforo: int, hora_apertura=None, hora_cierre=None
) -> Evento:
    return Evento.objects.create(
        tenant=_tenant_actual(),
        local=local,
        nombre=nombre,
        fecha=fecha,
        aforo=aforo,
        hora_apertura=hora_apertura,
        hora_cierre=hora_cierre,
    )


@transaction.atomic
def agregar_tipo(
    *, evento: Evento, nombre: str, precio, cupo: int, orden: int = 0
) -> TipoEntrada:
    tipo = TipoEntrada(
        tenant=_tenant_actual(),
        evento=evento,
        nombre=nombre,
        precio=Decimal(str(precio)),
        cupo=cupo,
        orden=orden,
    )
    tipo.full_clean(exclude=["tenant", "evento"])
    tipo.save()
    return tipo


@transaction.atomic
def publicar_evento(evento: Evento) -> Evento:
    evento = Evento.objects.select_for_update().get(pk=evento.pk)
    if not evento.tipos.exists():
        raise ValidationError("Un evento sin tipos de entrada no se puede publicar.")
    evento.estado = Evento.Estado.PUBLICADO
    evento.save(update_fields=["estado", "actualizado_en"])
    return evento


# --- aforo ----------------------------------------------------------------


def aforo_en_vivo(evento: Evento) -> int:
    """Gente adentro ahora: la suma del ledger, nunca un contador mutable."""
    total = MovimientoAforo.objects.filter(evento=evento).aggregate(
        t=Sum("delta")
    )["t"]
    return int(total or 0)


def aforo_disponible(evento: Evento) -> int:
    return max(0, evento.aforo - aforo_en_vivo(evento))


def entradas_vendidas(evento: Evento) -> int:
    return Entrada.objects.filter(
        evento=evento, estado__in=[Entrada.Estado.EMITIDA, Entrada.Estado.USADA]
    ).count()


# --- venta ----------------------------------------------------------------


@transaction.atomic
def vender_entrada(
    *,
    tipo: TipoEntrada,
    cantidad: int = 1,
    usuario,
    sesion_caja=None,
    terminal=None,
    canal: str = Entrada.Canal.PUERTA,
    comprador_nombre: str = "",
    comprador_contacto: str = "",
    idempotency_key: str = "",
) -> List[Entrada]:
    """Emite entradas y, si hay caja abierta, registra la venta en ella.

    Los cupos se verifican con la fila del tipo bloqueada: sin eso, dos cajeros
    vendiendo a la vez sobrevenden la ultima entrada.
    """
    tenant = _tenant_actual()
    tipo = TipoEntrada.objects.select_for_update().get(pk=tipo.pk)
    evento = Evento.objects.select_for_update().get(pk=tipo.evento_id)

    if evento.estado == Evento.Estado.CANCELADO:
        raise EntradaInvalida("El evento esta cancelado.")

    disponibles_tipo = tipo.cupo - tipo.vendidas
    if cantidad > disponibles_tipo:
        raise CupoAgotado(
            f"Solo quedan {max(0, disponibles_tipo)} entradas {tipo.nombre}."
        )

    # El aforo del local limita el total vendido: es la capacidad que fiscaliza
    # la Intendencia (ANALISIS.md seccion 8 ter).
    if entradas_vendidas(evento) + cantidad > evento.aforo:
        raise CupoAgotado(
            f"El evento tiene aforo {evento.aforo} y ya se vendieron "
            f"{entradas_vendidas(evento)} entradas."
        )

    venta = None
    if sesion_caja is not None:
        from apps.sales.services import registrar_venta

        venta = registrar_venta(
            sesion=sesion_caja,
            items=[],
            usuario=usuario,
            concepto=f"{cantidad} x {tipo.nombre} - {evento.nombre}",
            total_fijo=Decimal(str(tipo.precio)) * cantidad,
            idempotency_key=idempotency_key,
        )

    ultimo = (
        Entrada.objects.filter(evento=evento)
        .order_by("-folio")
        .values_list("folio", flat=True)
        .first()
    )
    correlativo = int(ultimo) if ultimo and ultimo.isdigit() else 0

    emitidas = []
    for i in range(cantidad):
        correlativo += 1
        entrada = Entrada(
            tenant=tenant,
            evento=evento,
            tipo=tipo,
            folio=f"{correlativo:06d}",
            precio=tipo.precio,
            canal=canal,
            comprador_nombre=comprador_nombre,
            comprador_contacto=comprador_contacto,
            venta=venta,
            qr_token=generar_token(),
        )
        entrada.full_clean(exclude=["tenant", "evento", "tipo", "venta"])
        entrada.save()
        emitidas.append(entrada)

    return emitidas


# --- control de acceso ----------------------------------------------------


def contenido_qr(entrada: Entrada) -> str:
    """Lo que se codifica en el QR. Es el token, no el identificador."""
    return entrada.qr_token


def token_desde_texto(texto: str) -> str:
    """Acepta tanto el token pelado como una URL que lo contenga.

    En la puerta se escanea lo que venga: un QR nuestro, uno reenviado por
    WhatsApp, o el texto copiado a mano.
    """
    texto = (texto or "").strip()
    if not texto:
        return ""
    if "/" in texto:
        return texto.rstrip("/").split("/")[-1]
    return texto


@transaction.atomic
def validar_entrada(
    *, evento: Evento, texto_qr: str, terminal=None, usuario=None
) -> dict:
    """Consume una entrada. Devuelve el resultado con el detalle para la puerta.

    El consumo es una actualizacion CONDICIONAL: si otra puerta la consumio un
    instante antes, esta no afecta ninguna fila y se rechaza. Leer, chequear y
    despues escribir aceptaria la misma entrada dos veces.
    """
    tenant = _tenant_actual()
    token = token_desde_texto(texto_qr)

    entrada = Entrada.objects.filter(qr_token=token).select_related("tipo").first()
    if entrada is None:
        return {"ok": False, "motivo": "no_existe", "mensaje": "Esa entrada no existe."}

    if entrada.evento_id != evento.id:
        return {
            "ok": False,
            "motivo": "otro_evento",
            "mensaje": f"La entrada es para {entrada.evento.nombre}, no para este evento.",
            "entrada": entrada,
        }

    if entrada.estado == Entrada.Estado.ANULADA:
        return {
            "ok": False,
            "motivo": "anulada",
            "mensaje": "La entrada esta anulada.",
            "entrada": entrada,
        }

    momento = timezone.now()
    filas = Entrada.objects.filter(
        pk=entrada.pk, estado=Entrada.Estado.EMITIDA
    ).update(
        estado=Entrada.Estado.USADA,
        usada_en=momento,
        usada_en_terminal=terminal,
        usada_por=usuario,
        actualizado_en=momento,
    )

    if filas == 0:
        entrada.refresh_from_db()
        return {
            "ok": False,
            "motivo": "ya_usada",
            "mensaje": f"La entrada ya se uso el {entrada.usada_en:%d/%m a las %H:%M}.",
            "entrada": entrada,
        }

    MovimientoAforo.objects.create(
        tenant=tenant,
        evento=evento,
        tipo=MovimientoAforo.Tipo.INGRESO,
        delta=1,
        motivo=f"Entrada {entrada.folio}",
        entrada=entrada,
        usuario=usuario,
        terminal=terminal,
    )

    entrada.refresh_from_db()
    dentro = aforo_en_vivo(evento)
    return {
        "ok": True,
        "motivo": "ok",
        "mensaje": f"Adelante. {entrada.tipo.nombre} {entrada.folio}.",
        "entrada": entrada,
        "aforo_en_vivo": dentro,
        "aforo_completo": dentro >= evento.aforo,
        "aforo": evento.aforo,
    }


@transaction.atomic
def registrar_ingreso_manual(
    *, evento: Evento, cantidad: int = 1, motivo: str, terminal=None, usuario=None
) -> int:
    """Ingreso sin entrada: invitado, lista, o alguien que paso sin escanear.

    Siempre con motivo y con autor. Es el equivalente en la puerta del cobro no
    registrado: si se puede hacer sin dejar rastro, se va a hacer.
    """
    tenant = _tenant_actual()
    if not motivo:
        raise ValidationError("Un ingreso manual exige motivo.")
    if cantidad <= 0:
        raise ValidationError("La cantidad tiene que ser mayor que cero.")

    MovimientoAforo.objects.create(
        tenant=tenant,
        evento=evento,
        tipo=MovimientoAforo.Tipo.INGRESO,
        delta=cantidad,
        motivo=motivo,
        usuario=usuario,
        terminal=terminal,
    )
    return aforo_en_vivo(evento)


@transaction.atomic
def registrar_egreso(*, evento: Evento, cantidad: int = 1, motivo: str, usuario=None):
    tenant = _tenant_actual()
    MovimientoAforo.objects.create(
        tenant=tenant,
        evento=evento,
        tipo=MovimientoAforo.Tipo.EGRESO,
        delta=-abs(cantidad),
        motivo=motivo,
        usuario=usuario,
    )
    return aforo_en_vivo(evento)


@transaction.atomic
def anular_entrada(*, entrada: Entrada, usuario=None, motivo: str = "") -> Entrada:
    entrada = Entrada.objects.select_for_update().get(pk=entrada.pk)
    if not motivo:
        raise ValidationError("Anular una entrada exige un motivo.")
    if entrada.estado == Entrada.Estado.USADA:
        raise EntradaInvalida(
            "Esa entrada ya ingreso. Si hay que sacarla, se registra el egreso."
        )
    if entrada.estado == Entrada.Estado.ANULADA:
        return entrada

    entrada.estado = Entrada.Estado.ANULADA
    entrada.anulada_en = timezone.now()
    entrada.anulada_por = usuario
    entrada.motivo_anulacion = motivo
    entrada.save(
        update_fields=[
            "estado", "anulada_en", "anulada_por", "motivo_anulacion", "actualizado_en",
        ]
    )
    return entrada


# --- QR -------------------------------------------------------------------


def qr_svg(entrada: Entrada) -> str:
    """El QR como SVG: se imprime nitido y no necesita Pillow."""
    imagen = qrcode.make(
        contenido_qr(entrada),
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=12,
        border=2,
    )
    buffer = io.BytesIO()
    imagen.save(buffer)
    return buffer.getvalue().decode("utf-8")


def resumen_de_evento(evento: Evento) -> dict:
    """Lo que el dueño quiere saber de la noche."""
    dentro = aforo_en_vivo(evento)
    return {
        "evento": evento,
        "aforo": evento.aforo,
        "dentro": dentro,
        "disponible": max(0, evento.aforo - dentro),
        "vendidas": entradas_vendidas(evento),
        "usadas": Entrada.objects.filter(
            evento=evento, estado=Entrada.Estado.USADA
        ).count(),
        "anuladas": Entrada.objects.filter(
            evento=evento, estado=Entrada.Estado.ANULADA
        ).count(),
        "recaudado": sum(
            (
                e.precio
                for e in Entrada.objects.filter(
                    evento=evento,
                    estado__in=[Entrada.Estado.EMITIDA, Entrada.Estado.USADA],
                )
            ),
            Decimal("0"),
        ),
        "tipos": [
            {
                "tipo": t,
                "cupo": t.cupo,
                "vendidas": t.vendidas,
                "disponibles": t.disponibles,
            }
            for t in evento.tipos.all()
        ],
    }
