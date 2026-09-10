"""Reporte de varianza: la razon por la que el producto se vende.

Responde la unica pregunta que el dueño no puede contestar hoy: cuanta bebida se
fue, cuanto cuesta, y a que turno corresponde.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Dict, List, Optional

from apps.stock.models import Conteo, Deposito


def reporte_de_varianza(
    *, turno=None, deposito: Optional[Deposito] = None
) -> Dict[str, object]:
    """Varianza por insumo, ordenada por impacto en plata.

    Los depositos que no se contaron aparecen como "sin conteo". Confundir "no
    medido" con "cero" produce varianzas falsas y destruye la confianza en el
    reporte mas rapido que cualquier bug (seccion 11 ter).
    """
    conteos = Conteo.objects.filter(estado=Conteo.Estado.CERRADO)
    if turno is not None:
        conteos = conteos.filter(turno=turno)
    if deposito is not None:
        conteos = conteos.filter(deposito=deposito)

    filas: List[dict] = []
    sin_conteo: List[dict] = []

    depositos_con_conteo = set()
    for conteo in conteos.select_related("deposito", "usuario"):
        depositos_con_conteo.add(conteo.deposito_id)
        for item in conteo.items.select_related("insumo"):
            if item.diferencia is None:
                continue
            valor = item.diferencia * item.insumo.costo_promedio
            filas.append(
                {
                    "deposito": conteo.deposito.nombre,
                    "insumo": item.insumo.nombre,
                    "teorico": item.cantidad_teorica,
                    "contado": item.cantidad_declarada,
                    "diferencia": item.diferencia,
                    "valorizado": valor,
                    "contado_por": str(conteo.usuario),
                    "duracion_seg": conteo.duracion_seg,
                }
            )

    filas.sort(key=lambda f: abs(f["valorizado"]), reverse=True)

    # Un punto que no se conto no vale cero: vale "no medido".
    consulta = Deposito.objects.filter(activo=True)
    if deposito is not None:
        consulta = consulta.filter(pk=deposito.pk)
    for dep in consulta:
        if dep.id not in depositos_con_conteo:
            sin_conteo.append({"deposito": dep.nombre})

    total = sum((f["valorizado"] for f in filas), Decimal("0"))
    segundos = [f["duracion_seg"] for f in filas if f["duracion_seg"] is not None]

    return {
        "filas": filas,
        "depositos_sin_conteo": sin_conteo,
        "varianza_valorizada": total,
        "duracion_promedio_seg": (sum(segundos) // len(segundos)) if segundos else None,
        "hay_varianza": bool(filas),
    }
