"""Operaciones de caja y venta.

Aca viven las invariantes que el producto promete:

- una sola sesion de caja abierta por terminal (reforzada por indice unico);
- una venta pagada tiene suma de pagos aprobados igual al total;
- el cobro no se traba esperando al adquirente (pago asincronico);
- un pago aprobado sobre una caja ya cerrada se marca como cobro tardio y NO
  reescribe el arqueo;
- anular una venta asienta el contrario en el stock, nunca borra.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Optional

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max, Sum
from django.utils import timezone

from apps.catalog.models import Producto
from apps.core.context import TenantNotSetError, get_current_tenant
from apps.sales.models import (
    Arqueo,
    ArqueoLinea,
    EgresoCaja,
    ItemVenta,
    Pago,
    SesionCaja,
    Turno,
    Venta,
)
from apps.stock.models import StockMovimiento
from apps.stock.services import descontar_venta, insumos_de_producto


class CajaYaCerrada(ValidationError):
    pass


class CajaYaAbierta(ValidationError):
    pass


def _tenant_actual():
    tenant = get_current_tenant()
    if tenant is None:
        raise TenantNotSetError("Sin tenant en contexto: no se puede operar caja.")
    return tenant


def _a_decimal(valor) -> Decimal:
    return Decimal(str(valor if valor is not None else "0"))


# --- turnos y caja --------------------------------------------------------


@transaction.atomic
def abrir_turno(*, local, nombre: str, fecha=None, momento=None) -> Turno:
    tenant = _tenant_actual()
    return Turno.objects.create(
        tenant=tenant,
        local=local,
        nombre=nombre,
        fecha=fecha or timezone.localdate(),
        abierto_en=momento or timezone.now(),
    )


@transaction.atomic
def abrir_caja(
    *, turno: Turno, terminal, usuario, fondo_inicial=Decimal("0"), momento=None
) -> SesionCaja:
    """Abre una caja. Falla si esa terminal ya tiene una sesion abierta."""
    tenant = _tenant_actual()

    if SesionCaja.objects.select_for_update().filter(
        terminal=terminal, estado=SesionCaja.Estado.ABIERTA
    ).exists():
        raise CajaYaAbierta(
            f"La terminal {terminal.nombre} ya tiene una caja abierta."
        )

    return SesionCaja.objects.create(
        tenant=tenant,
        turno=turno,
        terminal=terminal,
        usuario=usuario,
        fondo_inicial=_a_decimal(fondo_inicial),
        abierta_en=momento or timezone.now(),
    )


# --- venta ----------------------------------------------------------------


@transaction.atomic
def registrar_venta(
    *,
    sesion: SesionCaja,
    items: List[dict],
    usuario=None,
    idempotency_key: str = "",
    creada_en_cliente=None,
    descuento: Decimal = Decimal("0"),
    concepto: str = "",
    total_fijo=None,
    catalogo_version=None,
) -> Venta:
    """Registra una venta y descarga el stock por receta.

    items: [{"producto": Producto | id, "cantidad": n, "descuento": monto}]
    concepto: para ventas sin productos (entradas). Exige total_fijo.
    """
    tenant = _tenant_actual()

    sesion = SesionCaja.objects.select_for_update().get(pk=sesion.pk)
    if sesion.estado != SesionCaja.Estado.ABIERTA:
        raise CajaYaCerrada("No se puede vender con la caja cerrada.")

    if idempotency_key:
        existente = Venta.objects.filter(idempotency_key=idempotency_key).first()
        if existente is not None:
            return existente

    if not items and not concepto:
        raise ValidationError("Una venta sin items exige un concepto.")
    if not items and total_fijo is None:
        raise ValidationError("Una venta por concepto exige el total.")

    usuario = usuario or sesion.usuario
    terminal = sesion.terminal
    deposito = terminal.deposito

    ultimo = (
        Venta.objects.filter(sesion_caja=sesion).aggregate(n=Max("numero"))["n"] or 0
    )

    venta = Venta(
        tenant=tenant,
        sesion_caja=sesion,
        usuario=usuario,
        terminal=terminal,
        numero=ultimo + 1,
        estado=Venta.Estado.PENDIENTE,
        descuento=_a_decimal(descuento),
        creada_en_cliente=creada_en_cliente,
        idempotency_key=idempotency_key,
        concepto=concepto,
    )

    subtotal = Decimal("0")
    lineas = []
    for crudo in items:
        producto = crudo["producto"]
        if not isinstance(producto, Producto):
            producto = Producto.objects.get(pk=producto)
        cantidad = int(crudo.get("cantidad", 1))
        precio = _a_decimal(crudo.get("precio_unitario", producto.precio))
        desc_linea = _a_decimal(crudo.get("descuento", 0))
        total_linea = precio * cantidad - desc_linea
        subtotal += total_linea
        lineas.append(
            ItemVenta(
                tenant=tenant,
                venta=venta,
                producto=producto,
                cantidad=cantidad,
                precio_unitario=precio,
                descuento=desc_linea,
                total=total_linea,
            )
        )

    if not items:
        subtotal = _a_decimal(total_fijo)
    if catalogo_version is not None:
        from apps.catalog.version import version_actual

        # La venta entra igual: el cliente ya se fue con su trago. Lo que se
        # registra es que se cobro con una lista vieja, para que el reporte lo
        # muestre en vez de esconderlo.
        try:
            venta.catalogo_version = int(catalogo_version)
        except (TypeError, ValueError):
            venta.catalogo_version = None
        venta.precio_desactualizado = (
            venta.catalogo_version is not None
            and venta.catalogo_version != version_actual(tenant)
        )

    venta.subtotal = subtotal
    venta.total = subtotal - venta.descuento
    venta.full_clean(exclude=["tenant"])
    venta.save()

    for linea in lineas:
        linea.venta = venta
        linea.full_clean(exclude=["tenant", "venta"])
        linea.save()

    # El stock se descarga al vender: en una barra el trago se sirve en el acto.
    for linea in lineas:
        consumos = insumos_de_producto(linea.producto, linea.cantidad)
        if not consumos:
            continue
        if deposito is None:
            raise ValidationError(
                f"La terminal {terminal.nombre} no tiene punto de stock asignado y "
                f"{linea.producto.nombre} descuenta insumos. Asignalo antes de vender."
            )
        descontar_venta(
            deposito=deposito,
            producto=linea.producto,
            cantidad=linea.cantidad,
            usuario=usuario,
            terminal=terminal,
            idempotency_key=f"venta:{venta.id}",
            referencia_tipo="venta",
            referencia_id=venta.id,
        )

    return venta


@transaction.atomic
def registrar_pago(
    *,
    venta: Venta,
    medio: str,
    monto,
    estado: str = Pago.Estado.PENDIENTE,
    referencia_externa: str = "",
    payload=None,
    momento=None,
) -> Pago:
    tenant = _tenant_actual()
    venta = Venta.objects.select_for_update().get(pk=venta.pk)
    if venta.estado == Venta.Estado.ANULADA:
        raise ValidationError("No se puede cobrar una venta anulada.")

    pago = Pago(
        tenant=tenant,
        venta=venta,
        medio=medio,
        monto=_a_decimal(monto),
        estado=estado,
        referencia_externa=referencia_externa,
        payload=payload,
    )
    pago.full_clean(exclude=["tenant", "venta"])
    pago.save()

    if estado == Pago.Estado.APROBADO:
        pago.confirmado_en = momento or timezone.now()
        pago.save(update_fields=["confirmado_en"])
        _recalcular_estado_venta(venta)

    return pago


@transaction.atomic
def confirmar_pago(*, pago: Pago, referencia_externa: str = "", payload=None, momento=None):
    """Confirma un pago pendiente (tipicamente, el webhook del adquirente).

    Es idempotente: notificar dos veces el mismo pago no duplica nada.
    """
    pago = Pago.objects.select_for_update().get(pk=pago.pk)
    if pago.estado == Pago.Estado.APROBADO:
        return pago

    pago.estado = Pago.Estado.APROBADO
    pago.confirmado_en = momento or timezone.now()
    if referencia_externa:
        pago.referencia_externa = referencia_externa
    if payload is not None:
        pago.payload = payload

    # Si la caja ya cerro, no se reescribe el arqueo: se marca el cobro.
    sesion = SesionCaja.objects.get(pk=pago.venta.sesion_caja_id)
    pago.cobro_tardio = sesion.estado == SesionCaja.Estado.CERRADA
    pago.save()

    venta = Venta.objects.select_for_update().get(pk=pago.venta_id)
    _recalcular_estado_venta(venta)
    return pago


def _recalcular_estado_venta(venta: Venta) -> Venta:
    if venta.estado == Venta.Estado.ANULADA:
        return venta
    if venta.saldo <= 0:
        venta.estado = Venta.Estado.PAGADA
        venta.save(update_fields=["estado", "actualizado_en"])
    return venta


@transaction.atomic
def anular_venta(*, venta: Venta, usuario=None, motivo: str = "", momento=None) -> Venta:
    """Anula una venta asentando el contrario en el stock. Nada se borra."""
    tenant = _tenant_actual()
    venta = Venta.objects.select_for_update().get(pk=venta.pk)
    if venta.estado == Venta.Estado.ANULADA:
        return venta
    if not motivo:
        raise ValidationError("Anular una venta exige un motivo.")

    deposito = venta.terminal.deposito
    if deposito is not None:
        for linea in venta.items.select_related("producto"):
            consumos = insumos_de_producto(linea.producto, linea.cantidad)
            if not consumos:
                continue
            descontar_venta(
                deposito=deposito,
                producto=linea.producto,
                cantidad=linea.cantidad,
                usuario=usuario,
                terminal=venta.terminal,
                idempotency_key=f"anulacion:{venta.id}",
                referencia_tipo="venta_anulada",
                referencia_id=venta.id,
                revertir=True,
            )

    venta.estado = Venta.Estado.ANULADA
    venta.anulada_en = momento or timezone.now()
    venta.anulada_por = usuario
    venta.motivo_anulacion = motivo
    venta.save(
        update_fields=[
            "estado", "anulada_en", "anulada_por", "motivo_anulacion", "actualizado_en",
        ]
    )
    return venta


# --- egresos y arqueo -----------------------------------------------------


@transaction.atomic
def registrar_egreso(
    *, sesion: SesionCaja, tipo: str, monto, motivo: str, usuario, autorizado_por=None
) -> EgresoCaja:
    tenant = _tenant_actual()
    sesion = SesionCaja.objects.select_for_update().get(pk=sesion.pk)
    if sesion.estado != SesionCaja.Estado.ABIERTA:
        raise CajaYaCerrada("No se puede registrar un egreso con la caja cerrada.")
    if not motivo:
        raise ValidationError("Un egreso exige motivo.")

    egreso = EgresoCaja(
        tenant=tenant,
        sesion_caja=sesion,
        tipo=tipo,
        monto=_a_decimal(monto),
        motivo=motivo,
        usuario=usuario,
        autorizado_por=autorizado_por,
    )
    egreso.full_clean(exclude=["tenant", "sesion_caja", "usuario", "autorizado_por"])
    egreso.save()
    return egreso


def calcular_esperado(sesion: SesionCaja) -> Dict[str, Decimal]:
    """Esperado por medio de pago: fondo inicial + cobros - egresos de efectivo.

    Los cobros tardios se excluyen a proposito: pertenecen a esta caja pero se
    confirmaron despues del cierre, asi que no pueden reescribir el arqueo.
    """
    esperado: Dict[str, Decimal] = {
        medio: Decimal("0") for medio, _ in Pago.Medio.choices
    }

    pagos = (
        Pago.objects.filter(
            venta__sesion_caja=sesion,
            estado=Pago.Estado.APROBADO,
            cobro_tardio=False,
        )
        .values("medio")
        .annotate(total=Sum("monto"))
    )
    for fila in pagos:
        esperado[fila["medio"]] = _a_decimal(fila["total"])

    esperado[Pago.Medio.EFECTIVO] += sesion.fondo_inicial

    egresos_efectivo = EgresoCaja.objects.filter(sesion_caja=sesion).aggregate(
        total=Sum("monto")
    )["total"]
    esperado[Pago.Medio.EFECTIVO] -= _a_decimal(egresos_efectivo)

    return esperado


@transaction.atomic
def cerrar_caja(
    *,
    sesion: SesionCaja,
    declarado_por_medio: Dict[str, object],
    usuario=None,
    autorizado_por=None,
    observaciones: str = "",
    momento=None,
) -> Arqueo:
    """Cierra la caja con arqueo por medio de pago.

    Separacion de funciones: el cantinero declara, y una diferencia la autoriza
    otra persona. Nadie autoriza su propia diferencia.
    """
    tenant = _tenant_actual()
    sesion = SesionCaja.objects.select_for_update().get(pk=sesion.pk)
    if sesion.estado == SesionCaja.Estado.CERRADA:
        raise CajaYaCerrada("La caja ya estaba cerrada.")

    esperado = calcular_esperado(sesion)

    declarado = {}
    for medio, _ in Pago.Medio.choices:
        declarado[medio] = _a_decimal(declarado_por_medio.get(medio, 0))

    diferencia_total = sum(declarado.values()) - sum(esperado.values())
    if diferencia_total != 0:
        if autorizado_por is None:
            raise ValidationError(
                "Hay una diferencia de "
                f"{diferencia_total}. Exige un autorizante que no sea el cajero."
            )
        if usuario is not None and autorizado_por.pk == usuario.pk:
            raise ValidationError("Nadie autoriza su propia diferencia.")

    arqueo = Arqueo.objects.create(
        tenant=tenant,
        sesion_caja=sesion,
        cerrado_en=momento or timezone.now(),
        autorizado_por=autorizado_por,
        observaciones=observaciones,
    )

    for medio, _ in Pago.Medio.choices:
        ArqueoLinea.objects.create(
            tenant=tenant,
            arqueo=arqueo,
            medio=medio,
            esperado=esperado[medio],
            declarado=declarado[medio],
            diferencia=declarado[medio] - esperado[medio],
        )

    sesion.estado = SesionCaja.Estado.CERRADA
    sesion.cerrada_en = arqueo.cerrado_en
    sesion.save(update_fields=["estado", "cerrada_en", "actualizado_en"])

    # Si no quedan cajas abiertas en el turno, el turno se cierra solo.
    if not SesionCaja.objects.filter(
        turno=sesion.turno, estado=SesionCaja.Estado.ABIERTA
    ).exists():
        turno = Turno.objects.select_for_update().get(pk=sesion.turno_id)
        turno.estado = Turno.Estado.CERRADO
        turno.cerrado_en = arqueo.cerrado_en
        turno.save(update_fields=["estado", "cerrado_en", "actualizado_en"])

    return arqueo


def resumen_de_ventas(sesion: SesionCaja) -> dict:
    """Numeros que el dueño quiere ver al cerrar."""
    ventas = Venta.objects.filter(sesion_caja=sesion)
    pagadas = ventas.filter(estado=Venta.Estado.PAGADA)
    anuladas = ventas.filter(estado=Venta.Estado.ANULADA)

    por_medio = (
        Pago.objects.filter(venta__sesion_caja=sesion, estado=Pago.Estado.APROBADO)
        .values("medio")
        .annotate(total=Sum("monto"))
    )

    return {
        "cantidad_ventas": pagadas.count(),
        "total_vendido": pagadas.aggregate(t=Sum("total"))["t"] or Decimal("0"),
        "cantidad_anuladas": anuladas.count(),
        "total_anulado": anuladas.aggregate(t=Sum("total"))["t"] or Decimal("0"),
        "por_medio": {
            fila["medio"]: _a_decimal(fila["total"]) for fila in por_medio
        },
    }
