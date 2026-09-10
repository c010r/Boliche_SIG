"""Reservas con vencimiento: el patron que evita la sobreventa.

El orden importa y no es negociable:

1. RESERVAR de forma atomica. Si no hay cupo, falla ACA, no despues de cobrar.
2. COBRAR, con la reserva vencible.
3. CONFIRMAR: recien ahi se emite la entrada.
4. EXPIRAR: un proceso libera las reservas vencidas sin pago.

La invariante que sostiene todo: el aforo nunca se sobrevende, y una reserva
vencida nunca emite entrada. Si el pago se aprueba tarde, se registra, se avisa y
se devuelve: no se sobrevende "para que funcione".
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import List, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.core.context import TenantNotSetError, get_current_tenant
from apps.ticketing.models import Entrada, Evento, Reserva, TipoEntrada, generar_token


class ReservaVencida(ValidationError):
    pass


class SinCupo(ValidationError):
    pass


def _tenant_actual():
    tenant = get_current_tenant()
    if tenant is None:
        raise TenantNotSetError("Sin tenant en contexto: no se puede reservar.")
    return tenant


def _minutos_de_validez() -> int:
    return int(getattr(settings, "RESERVA_MINUTOS_DE_VALIDEZ", 10))


def reservas_activas(tipo: TipoEntrada, momento=None) -> int:
    """Cupo tomado por reservas vigentes.

    Las vencidas y todavia no barridas NO cuentan: si contaran, un proceso de
    limpieza caido dejaria el evento bloqueado sin que nadie sepa por que.
    """
    momento = momento or timezone.now()
    total = Reserva.objects.filter(
        tipo=tipo, estado=Reserva.Estado.ACTIVA, expira_en__gt=momento
    ).aggregate(t=Sum("cantidad"))["t"]
    return int(total or 0)


def disponibles_de_tipo(tipo: TipoEntrada, momento=None) -> int:
    """Lo que se puede vender o reservar ahora mismo."""
    return max(0, tipo.cupo - tipo.vendidas - reservas_activas(tipo, momento))


@transaction.atomic
def crear_reserva(
    *,
    tipo: TipoEntrada,
    cantidad: int = 1,
    comprador_nombre: str = "",
    comprador_contacto: str = "",
    minutos: Optional[int] = None,
    momento=None,
) -> Reserva:
    """Toma cupo. Falla en el momento si no hay, no despues de cobrar."""
    tenant = _tenant_actual()
    momento = momento or timezone.now()

    # La fila del tipo va bloqueada: sin esto, dos compradores simultaneos toman
    # la ultima entrada.
    tipo = TipoEntrada.objects.select_for_update().get(pk=tipo.pk)
    evento = Evento.objects.select_for_update().get(pk=tipo.evento_id)

    if cantidad <= 0:
        raise ValidationError("La cantidad tiene que ser mayor que cero.")
    if evento.estado == Evento.Estado.CANCELADO:
        raise ValidationError("El evento esta cancelado.")

    libres = disponibles_de_tipo(tipo, momento)
    if cantidad > libres:
        raise SinCupo(
            f"Quedan {libres} entradas {tipo.nombre}. No se pueden reservar {cantidad}."
        )

    if evento.aforo and tipo.vendidas + reservas_activas(tipo, momento) + cantidad > evento.aforo:
        raise SinCupo("El evento no tiene mas aforo disponible.")

    return Reserva.objects.create(
        tenant=tenant,
        evento=evento,
        tipo=tipo,
        cantidad=cantidad,
        comprador_nombre=comprador_nombre,
        comprador_contacto=comprador_contacto,
        estado=Reserva.Estado.ACTIVA,
        token=generar_token(),
        expira_en=momento + timedelta(minutes=minutos or _minutos_de_validez()),
    )


@transaction.atomic
def confirmar_reserva(
    *,
    reserva: Reserva,
    referencia_pago: str = "",
    medio: str = "",
    momento=None,
) -> dict:
    """Confirma el pago y emite las entradas.

    Si la reserva vencio, NO emite: marca el pago como tardio y lo informa. Es la
    unica salida aceptable: la alternativa es sobrevender el aforo, que es lo que
    la Intendencia fiscaliza.
    """
    tenant = _tenant_actual()
    momento = momento or timezone.now()

    reserva = Reserva.objects.select_for_update().get(pk=reserva.pk)

    if reserva.estado == Reserva.Estado.CONFIRMADA:
        entradas = list(
            Entrada.objects.filter(
                evento=reserva.evento,
                canal=Entrada.Canal.ONLINE,
                comprador_contacto=reserva.comprador_contacto,
                comprador_nombre=reserva.comprador_nombre,
            )[: reserva.cantidad]
        )
        return {
            "estado": "ya_confirmada",
            "entradas": entradas,
            "mensaje": "La reserva ya estaba confirmada.",
        }

    if reserva.estado == Reserva.Estado.CANCELADA:
        return {
            "estado": "cancelada",
            "entradas": [],
            "mensaje": "La reserva estaba cancelada.",
        }

    if reserva.estado == Reserva.Estado.ACTIVA and reserva.expira_en <= momento:
        reserva.estado = Reserva.Estado.EXPIRADA
        reserva.save(update_fields=["estado", "actualizado_en"])

    if reserva.estado == Reserva.Estado.EXPIRADA:
        reserva.pago_tardio = True
        reserva.referencia_pago = referencia_pago or reserva.referencia_pago
        reserva.medio_pago = medio or reserva.medio_pago
        reserva.pagado_en = momento
        reserva.save(
            update_fields=[
                "pago_tardio", "referencia_pago", "medio_pago", "pagado_en",
                "actualizado_en",
            ]
        )
        return {
            "estado": "pago_tardio",
            "entradas": [],
            "mensaje": (
                "La reserva vencio antes de acreditarse el pago. Hay que "
                "reembolsar: NO se sobrevende el aforo."
            ),
        }

    tipo = TipoEntrada.objects.select_for_update().get(pk=reserva.tipo_id)

    ultimo = (
        Entrada.objects.filter(evento=reserva.evento)
        .order_by("-folio")
        .values_list("folio", flat=True)
        .first()
    )
    correlativo = int(ultimo) if ultimo and ultimo.isdigit() else 0

    entradas: List[Entrada] = []
    for _ in range(reserva.cantidad):
        correlativo += 1
        entrada = Entrada(
            tenant=tenant,
            evento=reserva.evento,
            tipo=tipo,
            folio=f"{correlativo:06d}",
            precio=tipo.precio,
            canal=Entrada.Canal.ONLINE,
            comprador_nombre=reserva.comprador_nombre,
            comprador_contacto=reserva.comprador_contacto,
            reserva=reserva,
            qr_token=generar_token(),
        )
        entrada.full_clean(exclude=["tenant", "evento", "tipo", "venta", "reserva"])
        entrada.save()
        entradas.append(entrada)

    reserva.estado = Reserva.Estado.CONFIRMADA
    reserva.confirmada_en = momento
    reserva.referencia_pago = referencia_pago or reserva.referencia_pago
    reserva.medio_pago = medio or reserva.medio_pago
    reserva.pagado_en = momento
    reserva.save(
        update_fields=[
            "estado", "confirmada_en", "referencia_pago", "medio_pago", "pagado_en",
            "actualizado_en",
        ]
    )

    return {"estado": "confirmada", "entradas": entradas, "mensaje": "Listo."}


@transaction.atomic
def expirar_reservas(*, momento=None) -> int:
    """Libera las reservas vencidas sin pago. Devuelve cuantas expiro."""
    momento = momento or timezone.now()
    return Reserva.objects.filter(
        estado=Reserva.Estado.ACTIVA, expira_en__lte=momento
    ).update(estado=Reserva.Estado.EXPIRADA, actualizado_en=momento)


@transaction.atomic
def cancelar_reserva(*, reserva: Reserva) -> Reserva:
    reserva = Reserva.objects.select_for_update().get(pk=reserva.pk)
    if reserva.estado == Reserva.Estado.CONFIRMADA:
        raise ValidationError(
            "Una reserva confirmada ya emitio entradas: hay que anularlas."
        )
    if reserva.estado == Reserva.Estado.ACTIVA:
        reserva.estado = Reserva.Estado.CANCELADA
        reserva.save(update_fields=["estado", "actualizado_en"])
    return reserva


def resumen_de_reservas(evento: Evento, momento=None) -> dict:
    momento = momento or timezone.now()
    activas = Reserva.objects.filter(
        evento=evento, estado=Reserva.Estado.ACTIVA, expira_en__gt=momento
    )
    return {
        "activas": activas.count(),
        "lugares_tomados": int(activas.aggregate(t=Sum("cantidad"))["t"] or 0),
        "confirmadas": Reserva.objects.filter(
            evento=evento, estado=Reserva.Estado.CONFIRMADA
        ).count(),
        "expiradas": Reserva.objects.filter(
            evento=evento, estado=Reserva.Estado.EXPIRADA
        ).count(),
        "pagos_tardios": Reserva.objects.filter(
            evento=evento, pago_tardio=True
        ).count(),
    }
