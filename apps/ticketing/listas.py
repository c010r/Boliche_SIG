"""Listas de invitados y promotores.

La lista es el mecanismo con el que un boliche llena la sala, y el promotor es el
canal por el que llega la mayoria de las reservas. No es fidelizacion: es ACCESO,
AFORO y PLATA (ANALISIS.md seccion 9 ter).

Las tres reglas que evitan que se convierta en un agujero:

1. El corte lo aplica el SISTEMA.
2. La atribucion se registra en la puerta al ingresar, y no se edita despues.
3. La lista consume AFORO.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Dict, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from apps.core.context import TenantNotSetError, get_current_tenant
from apps.ticketing.models import (
    ComisionPromotor,
    Evento,
    Lista,
    ListaInvitado,
    MovimientoAforo,
    Promotor,
    UsoLista,
)
from apps.ticketing.services import aforo_en_vivo, token_desde_texto


class ListaCerrada(ValidationError):
    pass


class CupoDeListaAgotado(ValidationError):
    pass


class InvitadoInvalido(ValidationError):
    pass


def _tenant_actual():
    tenant = get_current_tenant()
    if tenant is None:
        raise TenantNotSetError("Sin tenant en contexto: no se puede operar listas.")
    return tenant


# --- corte de lista -------------------------------------------------------


def corte_efectivo(lista: Lista) -> Optional[datetime]:
    """Momento exacto en que la lista deja de valer.

    El detalle que hay que resolver bien: un boliche opera pasada la medianoche.
    Si el corte es a las 01:00 y el evento empieza a las 23:00, el corte cae al
    dia SIGUIENTE. Comparar solo la hora del reloj daria por vigente una lista
    cortada a las 23:00 cuando ya son las 00:30.
    """
    if lista.hora_de_corte is None:
        return None

    fecha = lista.evento.fecha
    zona = timezone.get_current_timezone()
    corte = timezone.make_aware(datetime.combine(fecha, lista.hora_de_corte), zona)

    apertura = lista.evento.hora_apertura
    if apertura is not None and lista.hora_de_corte <= apertura:
        corte = corte + timedelta(days=1)

    return corte


def lista_vigente(lista: Lista, momento=None) -> bool:
    if lista.estado != Lista.Estado.ABIERTA:
        return False
    corte = corte_efectivo(lista)
    if corte is None:
        return True
    return (momento or timezone.now()) < corte


# --- alta -----------------------------------------------------------------


@transaction.atomic
def crear_promotor(
    *,
    nombre: str,
    contacto: str = "",
    valor_comision=Decimal("0"),
    tipo_comision: str = Promotor.TipoComision.POR_PERSONA,
) -> Promotor:
    tenant = _tenant_actual()
    promotor = Promotor(
        tenant=tenant,
        nombre=nombre,
        contacto=contacto,
        valor_comision=Decimal(str(valor_comision)),
        tipo_comision=tipo_comision,
    )
    promotor.full_clean(exclude=["tenant"])
    promotor.save()
    return promotor


@transaction.atomic
def crear_lista(
    *,
    evento: Evento,
    nombre: str,
    cupo: int,
    tipo: str = Lista.Tipo.CASA,
    promotor: Optional[Promotor] = None,
    creada_por=None,
    hora_de_corte=None,
) -> Lista:
    tenant = _tenant_actual()
    if cupo <= 0:
        raise ValidationError("Una lista sin cupo no tiene sentido.")
    lista = Lista(
        tenant=tenant,
        evento=evento,
        nombre=nombre,
        tipo=tipo,
        cupo=cupo,
        promotor=promotor,
        creada_por=creada_por,
        hora_de_corte=hora_de_corte,
    )
    lista.full_clean(exclude=["tenant", "evento", "promotor", "creada_por"])
    lista.save()
    return lista


@transaction.atomic
def agregar_invitado(
    *, lista: Lista, nombre: str, personas: int = 1, contacto: str = ""
) -> ListaInvitado:
    from apps.ticketing.models import generar_token

    tenant = _tenant_actual()
    if personas <= 0:
        raise ValidationError("Una invitacion cubre al menos una persona.")
    invitado = ListaInvitado(
        tenant=tenant,
        lista=lista,
        nombre=nombre,
        personas_que_cubre=personas,
        contacto=contacto,
        qr_token=generar_token(),
    )
    invitado.full_clean(exclude=["tenant", "lista"])
    invitado.save()
    return invitado


# --- puerta ---------------------------------------------------------------


@transaction.atomic
def validar_ingreso_de_lista(
    *,
    evento: Evento,
    texto: str = "",
    nombre: str = "",
    personas: int = 1,
    terminal=None,
    usuario=None,
    forzar: bool = False,
    momento=None,
) -> Dict:
    """Registra el ingreso de un invitado y deja la atribucion.

    El parametro forzar existe para el caso real de "el aforo esta completo pero
    el encargado decide que pasa": exige el permiso acceso.forzar y el ingreso
    queda marcado en las observaciones.
    """
    tenant = _tenant_actual()
    momento = momento or timezone.now()

    invitado = None
    if texto:
        invitado = (
            ListaInvitado.objects.filter(qr_token=token_desde_texto(texto))
            .select_related("lista")
            .first()
        )
    if invitado is None and nombre:
        # Busqueda por nombre: a las 2 AM, con cola. Tiene que funcionar aunque
        # el invitado no traiga QR.
        candidatos = list(
            ListaInvitado.objects.filter(
                lista__evento=evento, nombre__icontains=nombre.strip()
            )
            .select_related("lista")
            .order_by("nombre")[:2]
        )
        if len(candidatos) > 1:
            return {
                "ok": False,
                "motivo": "ambiguo",
                "mensaje": "Hay mas de un invitado con ese nombre. Busca por el QR.",
            }
        invitado = candidatos[0] if candidatos else None

    if invitado is None:
        return {"ok": False, "motivo": "no_existe", "mensaje": "No esta en la lista."}

    if invitado.lista.evento_id != evento.id:
        return {
            "ok": False,
            "motivo": "otro_evento",
            "mensaje": f"Ese invitado es para {invitado.lista.evento.nombre}.",
            "invitado": invitado,
        }

    if invitado.anulado:
        return {
            "ok": False,
            "motivo": "anulado",
            "mensaje": "Esa invitacion esta anulada.",
            "invitado": invitado,
        }

    lista = Lista.objects.select_for_update().get(pk=invitado.lista_id)

    # REGLA 1: el corte lo aplica el sistema, no el de la puerta.
    if not lista_vigente(lista, momento):
        return {
            "ok": False,
            "motivo": "lista_cortada",
            "mensaje": "La lista no esta vigente a esta hora.",
            "invitado": invitado,
        }

    invitado = ListaInvitado.objects.select_for_update().get(pk=invitado.pk)

    if personas <= 0:
        personas = 1
    if personas > invitado.personas_restantes:
        return {
            "ok": False,
            "motivo": "sin_personas",
            "mensaje": (
                f"{invitado.nombre} tiene {invitado.personas_restantes} lugar(es) "
                f"de {invitado.personas_que_cubre}."
            ),
            "invitado": invitado,
        }

    if personas > lista.disponibles:
        return {
            "ok": False,
            "motivo": "cupo_de_lista",
            "mensaje": f"A la lista {lista.nombre} le quedan {lista.disponibles} lugares.",
            "invitado": invitado,
        }

    # REGLA 3: la lista consume aforo.
    dentro = aforo_en_vivo(evento)
    if dentro + personas > evento.aforo and not forzar:
        return {
            "ok": False,
            "motivo": "aforo_completo",
            "mensaje": (
                f"El aforo esta completo ({dentro}/{evento.aforo}). "
                "Hace falta autorizacion para pasar."
            ),
            "invitado": invitado,
        }

    # REGLA 2: la atribucion se registra aca y no se edita despues.
    uso = UsoLista.objects.create(
        tenant=tenant,
        invitado=invitado,
        lista=lista,
        personas=personas,
        terminal=terminal,
        registrado_por=usuario,
        observaciones="Ingreso forzado con el aforo completo" if forzar else "",
    )

    MovimientoAforo.objects.create(
        tenant=tenant,
        evento=evento,
        tipo=MovimientoAforo.Tipo.INGRESO,
        delta=personas,
        motivo=f"Lista {lista.nombre}: {invitado.nombre}",
        usuario=usuario,
        terminal=terminal,
    )

    return {
        "ok": True,
        "motivo": "ok",
        "mensaje": f"Adelante. {invitado.nombre} ({personas}).",
        "invitado": invitado,
        "uso": uso,
        "aforo_en_vivo": aforo_en_vivo(evento),
        "aforo": evento.aforo,
    }


@transaction.atomic
def anular_invitado(*, invitado: ListaInvitado, motivo: str, usuario=None) -> ListaInvitado:
    invitado = ListaInvitado.objects.select_for_update().get(pk=invitado.pk)
    if not motivo:
        raise ValidationError("Anular una invitacion exige un motivo.")
    if invitado.personas_usadas:
        raise InvitadoInvalido(
            "Ese invitado ya ingreso. Si hay que sacarlo, se registra un egreso."
        )
    invitado.anulado = True
    invitado.motivo_anulacion = motivo
    invitado.save(update_fields=["anulado", "motivo_anulacion", "actualizado_en"])
    return invitado


# --- comisiones -----------------------------------------------------------


@transaction.atomic
def liquidar_comisiones(*, evento: Evento, usuario=None) -> List[ComisionPromotor]:
    """Calcula la comision de cada promotor por este evento.

    La base NUNCA es lo que declara el promotor: es la suma de los ingresos que
    quedaron registrados en la puerta por sus listas.
    """
    tenant = _tenant_actual()

    por_promotor = (
        UsoLista.objects.filter(lista__evento=evento, lista__promotor__isnull=False)
        .values("lista__promotor_id")
        .annotate(personas=Sum("personas"))
    )

    liquidaciones: List[ComisionPromotor] = []
    for fila in por_promotor:
        promotor = Promotor.objects.get(pk=fila["lista__promotor_id"])
        personas = int(fila["personas"] or 0)
        monto = (promotor.valor_comision * personas).quantize(Decimal("0.01"))

        comision, _ = ComisionPromotor.objects.update_or_create(
            evento=evento,
            promotor=promotor,
            defaults={
                "tenant": tenant,
                "personas": personas,
                "base": Decimal(personas),
                "monto": monto,
            },
        )
        liquidaciones.append(comision)

    return liquidaciones


@transaction.atomic
def aprobar_comision(*, comision: ComisionPromotor, usuario=None) -> ComisionPromotor:
    comision = ComisionPromotor.objects.select_for_update().get(pk=comision.pk)
    comision.estado = ComisionPromotor.Estado.APROBADA
    comision.aprobada_por = usuario
    comision.save(update_fields=["estado", "aprobada_por", "actualizado_en"])
    return comision


@transaction.atomic
def pagar_comision(*, comision: ComisionPromotor, usuario=None) -> ComisionPromotor:
    comision = ComisionPromotor.objects.select_for_update().get(pk=comision.pk)
    if comision.estado != ComisionPromotor.Estado.APROBADA:
        raise ValidationError("Hay que aprobar la comision antes de pagarla.")
    comision.estado = ComisionPromotor.Estado.PAGADA
    comision.pagada_en = timezone.now()
    comision.save(update_fields=["estado", "pagada_en", "actualizado_en"])
    return comision


def resumen_de_listas(evento: Evento, momento=None) -> dict:
    """Lo que el dueno quiere saber de las listas."""
    momento = momento or timezone.now()
    filas = []
    for lista in evento.listas.select_related("promotor"):
        filas.append(
            {
                "lista": lista,
                "cupo": lista.cupo,
                "usados": lista.usados,
                "disponibles": lista.disponibles,
                "vigente": lista_vigente(lista, momento),
                "corte": corte_efectivo(lista),
                "invitados": lista.invitados.count(),
                "promotor": lista.promotor,
            }
        )
    return {
        "filas": filas,
        "personas_por_lista": int(
            UsoLista.objects.filter(lista__evento=evento).aggregate(t=Sum("personas"))["t"]
            or 0
        ),
    }
