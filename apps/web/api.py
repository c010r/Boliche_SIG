"""API para la barra offline.

La barra tiene que poder seguir vendiendo cuando se cae el wifi. La terminal
guarda las ventas localmente y las manda cuando vuelve la conexion.

Tres reglas que hacen que esto sea seguro y no un agujero:

1. El SERVIDOR resuelve los precios. La terminal manda producto y cantidad, nunca
   el precio: un cliente manipulado no puede inventar cuanto sale un trago.
2. Cada venta lleva su CLAVE DE IDEMPOTENCIA. Reintentar la cola entera las veces
   que haga falta no duplica nada.
3. El reloj del dispositivo se guarda aparte del del servidor, para poder ordenar
   las ventas que llegaron tarde.

Una venta que falla no tumba al resto del lote: se devuelve el resultado de cada
una y la terminal decide que reintentar.
"""

from __future__ import annotations

import json
from datetime import datetime

from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.http import require_POST

from apps.catalog.models import Producto
from apps.catalog.version import version_actual
from apps.sales.models import Pago, SesionCaja
from apps.sales.services import registrar_pago, registrar_venta

MEDIOS_QUE_SE_PUEDEN_COBRAR_SIN_CONEXION = {Pago.Medio.EFECTIVO}


def _fecha_del_cliente(texto):
    if not texto:
        return None
    try:
        return datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
    except ValueError:
        return None


@csrf_protect
@require_POST
def sincronizar_ventas(request):
    """Recibe un lote de ventas hechas sin conexion."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "mensaje": "No autenticado."}, status=403)
    if not request.user.tiene_permiso("ventas.vender"):
        return JsonResponse(
            {"ok": False, "mensaje": "No tenes permiso para vender."}, status=403
        )
    if request.tenant is None:
        return JsonResponse({"ok": False, "mensaje": "Sin boliche."}, status=400)

    try:
        cuerpo = json.loads(request.body.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({"ok": False, "mensaje": "Cuerpo invalido."}, status=400)

    ventas = cuerpo.get("ventas") or []
    if not isinstance(ventas, list):
        return JsonResponse({"ok": False, "mensaje": "Formato invalido."}, status=400)

    if not ventas:
        return JsonResponse(
            {
                "ok": True,
                "resultados": [],
                "catalogo_version": version_actual(request.tenant),
                "servidor": timezone.now().isoformat(),
            }
        )

    sesion = (
        SesionCaja.objects.filter(
            usuario=request.user, estado=SesionCaja.Estado.ABIERTA
        )
        .select_related("terminal")
        .first()
    )
    if sesion is None:
        return JsonResponse(
            {
                "ok": False,
                "mensaje": "No hay una caja abierta para esta terminal.",
                "resultados": [],
            },
            status=409,
        )

    version_del_cliente = cuerpo.get("catalogo_version")
    resultados = []

    for crudo in ventas:
        resultados.append(
            _sincronizar_una(
                crudo, sesion=sesion, usuario=request.user,
                version_del_cliente=version_del_cliente,
            )
        )

    return JsonResponse(
        {
            "ok": True,
            "resultados": resultados,
            "catalogo_version": version_actual(request.tenant),
            "servidor": timezone.now().isoformat(),
        }
    )


def _sincronizar_una(crudo, *, sesion, usuario, version_del_cliente) -> dict:
    id_local = str(crudo.get("id_local") or "")
    clave = str(crudo.get("idempotency_key") or id_local or "")
    base = {"id_local": id_local, "idempotency_key": clave}

    if not clave:
        return {**base, "estado": "error", "mensaje": "Sin clave de idempotencia."}

    producto_id = str(crudo.get("producto") or "")
    if not producto_id:
        return {**base, "estado": "error", "mensaje": "Sin producto."}

    # Ojo con "cantidad or 1": una cantidad 0 es falsy y pasaria como si faltara.
    crudo_cantidad = crudo.get("cantidad")
    if crudo_cantidad is None or crudo_cantidad == "":
        cantidad = 1
    else:
        try:
            cantidad = int(crudo_cantidad)
        except (TypeError, ValueError):
            cantidad = 1
    if cantidad <= 0:
        return {**base, "estado": "error", "mensaje": "Cantidad invalida."}

    producto = Producto.objects.filter(pk=producto_id, activo=True).first()
    if producto is None:
        return {**base, "estado": "error", "mensaje": "Ese producto ya no existe."}

    medio = str(crudo.get("medio") or Pago.Medio.EFECTIVO)
    if medio not in dict(Pago.Medio.choices):
        medio = Pago.Medio.EFECTIVO

    version = crudo.get("catalogo_version", version_del_cliente)

    try:
        venta = registrar_venta(
            sesion=sesion,
            items=[{"producto": producto, "cantidad": cantidad}],
            usuario=usuario,
            idempotency_key=clave,
            creada_en_cliente=_fecha_del_cliente(crudo.get("creada_en_cliente")),
            catalogo_version=version,
        )
    except ValidationError as error:
        return {**base, "estado": "error", "mensaje": "; ".join(error.messages)}
    except Exception as error:  # noqa: BLE001 - se le informa a la terminal
        return {**base, "estado": "error", "mensaje": str(error)}

    ya_existia = venta.idempotency_key == clave and venta.items.count() == 0

    # El medio se registra igual; lo que NO se puede es confirmar sin conexion un
    # cobro con tarjeta o QR, asi que queda pendiente hasta que vuelva la red.
    if not venta.pagos.exists():
        registrar_pago(
            venta=venta,
            medio=medio,
            monto=venta.total,
            estado=(
                Pago.Estado.APROBADO
                if medio in MEDIOS_QUE_SE_PUEDEN_COBRAR_SIN_CONEXION
                else Pago.Estado.PENDIENTE
            ),
        )

    return {
        **base,
        "estado": "registrada",
        "venta": str(venta.id),
        "numero": venta.numero,
        "total": str(venta.total),
        "precio_desactualizado": venta.precio_desactualizado,
        "medio_pendiente": medio not in MEDIOS_QUE_SE_PUEDEN_COBRAR_SIN_CONEXION,
        "mensaje": "Registrada.",
    }
