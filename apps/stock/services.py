"""Operaciones de stock.

Todo lo que toca el ledger pasa por aca. La regla es que el movimiento y el
saldo se actualicen en la misma transaccion, y que el saldo se bloquee para que
dos ventas simultaneas no se pisen.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from django.db import IntegrityError, transaction

from apps.catalog.models import Insumo, Producto
from apps.core.context import TenantNotSetError, get_current_tenant
from apps.stock.models import Deposito, StockItem, StockMovimiento


def _tenant_actual():
    tenant = get_current_tenant()
    if tenant is None:
        raise TenantNotSetError(
            "Sin tenant en contexto: no se puede operar stock."
        )
    return tenant


@transaction.atomic
def registrar_movimiento(
    *,
    deposito: Deposito,
    insumo: Insumo,
    tipo: str,
    cantidad,
    usuario=None,
    terminal=None,
    motivo: str = "",
    costo_unitario=None,
    idempotency_key: str = "",
    referencia_tipo: str = "",
    referencia_id: str = "",
) -> StockMovimiento:
    """Asienta un movimiento y actualiza el saldo del deposito.

    Si llega una idempotency_key ya vista, devuelve el movimiento existente sin
    volver a aplicar el descuento.
    """
    tenant = _tenant_actual()
    cantidad = Decimal(cantidad)

    if idempotency_key:
        existente = StockMovimiento.objects.filter(
            idempotency_key=idempotency_key
        ).first()
        if existente is not None:
            return existente

    item = (
        StockItem.objects.select_for_update()
        .filter(deposito=deposito, insumo=insumo)
        .first()
    )
    if item is None:
        try:
            item = StockItem.objects.create(
                deposito=deposito, insumo=insumo, cantidad=Decimal("0")
            )
        except IntegrityError:
            item = (
                StockItem.objects.select_for_update()
                .filter(deposito=deposito, insumo=insumo)
                .first()
            )

    movimiento = StockMovimiento(
        tenant=tenant,
        deposito=deposito,
        insumo=insumo,
        tipo=tipo,
        cantidad=cantidad,
        costo_unitario=costo_unitario,
        motivo=motivo,
        usuario=usuario,
        terminal=terminal,
        idempotency_key=idempotency_key,
        referencia_tipo=referencia_tipo,
        referencia_id=str(referencia_id) if referencia_id else "",
    )
    movimiento.full_clean(exclude=["tenant"])
    movimiento.save()

    item.cantidad = item.cantidad + movimiento.cantidad
    item.save(update_fields=["cantidad", "actualizado_en"])

    return movimiento


def insumos_de_producto(
    producto: Producto, cantidad: int = 1, _vistos: Optional[set] = None
) -> Dict[str, Decimal]:
    """Expande un producto (y sus combos) a insumos, en unidad base.

    Devuelve {insumo_id: cantidad}. Los combos se expanden recursivamente, con
    proteccion contra ciclos: un combo que se contiene a si mismo cuelga el
    sistema en la barra, que es el peor lugar posible para descubrirlo.
    """
    if _vistos is None:
        _vistos = set()

    if producto.id in _vistos:
        raise ValueError(f"Combo ciclico detectado en {producto.nombre}.")

    consumos: Dict[str, Decimal] = {}
    cantidad = Decimal(cantidad)

    if producto.es_combo:
        vistos = _vistos | {producto.id}
        for item in producto.combo_items.select_related("producto_hijo"):
            hijos = insumos_de_producto(item.producto_hijo, item.cantidad, vistos)
            for insumo_id, cant in hijos.items():
                consumos[insumo_id] = consumos.get(insumo_id, Decimal("0")) + cant
    else:
        receta = getattr(producto, "receta", None)
        if receta is not None and receta.activa:
            for item in receta.items.select_related("insumo", "unidad"):
                base = item.cantidad_en_unidad_base()
                # Las claves van como texto: el resultado se serializa para la
                # cola offline y para los reportes.
                clave = str(item.insumo_id)
                consumos[clave] = consumos.get(clave, Decimal("0")) + base

    return {k: v * cantidad for k, v in consumos.items()}


@transaction.atomic
def descontar_venta(
    *,
    deposito: Deposito,
    producto: Producto,
    cantidad: int = 1,
    usuario=None,
    terminal=None,
    idempotency_key: str = "",
    referencia_tipo: str = "venta",
    referencia_id: str = "",
) -> List[StockMovimiento]:
    """Descuenta del stock lo que consume vender un producto."""
    movimientos = []
    for insumo_id, cant in insumos_de_producto(producto, cantidad).items():
        insumo = Insumo.objects.get(pk=insumo_id)
        movimientos.append(
            registrar_movimiento(
                deposito=deposito,
                insumo=insumo,
                tipo=StockMovimiento.Tipo.VENTA,
                cantidad=-abs(cant),
                usuario=usuario,
                terminal=terminal,
                idempotency_key=(
                    f"{idempotency_key}:{insumo_id}" if idempotency_key else ""
                ),
                referencia_tipo=referencia_tipo,
                referencia_id=referencia_id,
            )
        )
    return movimientos


def saldo_calculado(deposito: Deposito, insumo: Insumo) -> Decimal:
    """Suma del ledger. Es la verdad contra la que se verifica StockItem."""
    from django.db.models import Sum

    total = StockMovimiento.objects.filter(
        deposito=deposito, insumo=insumo
    ).aggregate(total=Sum("cantidad"))["total"]
    return total or Decimal("0")
