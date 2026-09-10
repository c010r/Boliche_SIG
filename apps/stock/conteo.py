"""Conteo fisico de stock.

La promesa comercial del producto depende de esto: sin conteo no hay varianza, y
sin varianza el sistema es una caja registradora linda (ANALISIS.md seccion 11 ter).

Reglas que implementa:

- CONTE O CIEGO: mientras el conteo esta abierto, el teorico no se calcula ni se
  expone. Se revela al cerrar.
- NO MEDIDO NO ES CERO: un punto sin conteo se reporta como sin conteo.
- La diferencia se asienta como movimiento de tipo inventario. Nunca se edita el
  saldo.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.catalog.models import Insumo
from apps.core.context import TenantNotSetError, get_current_tenant
from apps.stock.models import (
    AjusteInventario,
    Conteo,
    ConteoItem,
    Deposito,
    StockMovimiento,
)
from apps.stock.services import registrar_movimiento, saldo_calculado


class ConteoYaCerrado(ValidationError):
    pass


def _tenant_actual():
    tenant = get_current_tenant()
    if tenant is None:
        raise TenantNotSetError("Sin tenant en contexto: no se puede contar stock.")
    return tenant


@transaction.atomic
def iniciar_conteo(
    *, deposito: Deposito, usuario, turno=None, ciego: bool = True, momento=None
) -> Conteo:
    tenant = _tenant_actual()
    return Conteo.objects.create(
        tenant=tenant,
        deposito=deposito,
        usuario=usuario,
        turno=turno,
        ciego=ciego,
        iniciado_en=momento or timezone.now(),
    )


@transaction.atomic
def registrar_item(
    *,
    conteo: Conteo,
    insumo: Insumo,
    unidades_cerradas=Decimal("0"),
    fraccion_abierta=Decimal("0"),
    presentacion=None,
) -> ConteoItem:
    """Anota lo contado para un insumo.

    No calcula ni devuelve el teorico: eso es lo que hace que el conteo sea ciego.
    """
    conteo = Conteo.objects.select_for_update().get(pk=conteo.pk)
    if conteo.estado != Conteo.Estado.ABIERTO:
        raise ConteoYaCerrado("No se puede contar sobre un conteo ya cerrado.")

    item, _ = ConteoItem.objects.get_or_create(
        conteo=conteo, insumo=insumo, defaults={"presentacion": presentacion}
    )
    if presentacion is not None:
        item.presentacion = presentacion
    item.unidades_cerradas = Decimal(str(unidades_cerradas))
    item.fraccion_abierta = Decimal(str(fraccion_abierta))
    item.full_clean(exclude=["tenant", "conteo", "insumo", "presentacion"])
    item.save()
    return item


@transaction.atomic
def cerrar_conteo(
    *,
    conteo: Conteo,
    motivos: Optional[Dict[str, str]] = None,
    autorizado_por=None,
    observaciones: str = "",
    momento=None,
) -> Conteo:
    """Cierra el conteo: revela el teorico, calcula la varianza y la asienta."""
    tenant = _tenant_actual()
    conteo = Conteo.objects.select_for_update().get(pk=conteo.pk)
    if conteo.estado == Conteo.Estado.CERRADO:
        raise ConteoYaCerrado("El conteo ya estaba cerrado.")

    motivos = motivos or {}
    momento = momento or timezone.now()

    for item in conteo.items.select_related("insumo", "presentacion"):
        # El teorico se lee del ledger ANTES de aplicar el ajuste.
        teorica = saldo_calculado(conteo.deposito, item.insumo)
        declarada = item.calcular_declarada()
        diferencia = declarada - teorica

        item.cantidad_declarada = declarada
        item.cantidad_teorica = teorica
        item.diferencia = diferencia
        item.save(
            update_fields=[
                "cantidad_declarada", "cantidad_teorica", "diferencia", "actualizado_en",
            ]
        )

        if diferencia == 0:
            continue

        valorizado = abs(diferencia) * item.insumo.costo_promedio
        umbral = Decimal(str(getattr(settings, "AJUSTE_UMBRAL_AUTORIZACION", "0")))
        if valorizado > umbral and autorizado_por is None:
            raise ValidationError(
                f"El ajuste de {item.insumo.nombre} vale {valorizado} y supera el "
                f"umbral de {umbral}. Exige un autorizante."
            )

        movimiento = registrar_movimiento(
            deposito=conteo.deposito,
            insumo=item.insumo,
            tipo=StockMovimiento.Tipo.INVENTARIO,
            cantidad=diferencia,
            usuario=conteo.usuario,
            motivo=motivos.get(str(item.insumo_id), "Diferencia de conteo"),
            referencia_tipo="conteo",
            referencia_id=conteo.id,
            idempotency_key=f"conteo:{conteo.id}:{item.insumo_id}",
        )

        AjusteInventario.objects.create(
            tenant=tenant,
            conteo_item=item,
            diferencia=diferencia,
            motivo=motivos.get(str(item.insumo_id), "Diferencia de conteo"),
            autorizado_por=autorizado_por,
            movimiento=movimiento,
        )

    conteo.estado = Conteo.Estado.CERRADO
    conteo.cerrado_en = momento
    conteo.duracion_seg = max(0, int((momento - conteo.iniciado_en).total_seconds()))
    conteo.observaciones = observaciones
    conteo.save(
        update_fields=["estado", "cerrado_en", "duracion_seg", "observaciones", "actualizado_en"]
    )
    return conteo
