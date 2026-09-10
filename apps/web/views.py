"""Interfaz de operacion.

Decisiones de UX tomadas en ANALISIS.md seccion 7 y aplicadas aca:

- Tema oscuro y sin destellos blancos: una pantalla blanca al 100% en un boliche
  a oscuras encandila, y termina con la tablet dada vuelta.
- Blancos tactiles grandes: se opera con las manos mojadas y con una sola mano.
- Vender es producto -> cobrar -> medio. Tres toques.
- Nada de dialogos de confirmacion en el camino critico de la venta.
- El ingreso es numero + PIN, no email y contrasena.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.db.models import Count, Sum
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.utils import timezone

from apps.accounts.models import Usuario
from apps.core.context import tenant_context
from apps.catalog.models import CategoriaProducto, Producto
from apps.sales.models import Pago, SesionCaja, Turno, Venta
from apps.sales.services import (
    abrir_caja,
    abrir_turno,
    anular_venta,
    calcular_esperado,
    cerrar_caja,
    registrar_pago,
    registrar_venta,
    resumen_de_ventas,
)
from apps.stock.reportes import reporte_de_varianza
from apps.ticketing.listas import (
    agregar_invitado,
    crear_lista,
    liquidar_comisiones,
)
from apps.ticketing.reservas import (
    confirmar_reserva,
    crear_reserva,
    disponibles_de_tipo,
)
from apps.ticketing.services import qr_svg
from apps.tenancy.models import Terminal


def _decimal(valor, por_defecto="0"):
    try:
        return Decimal(str(valor).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(por_defecto)


def _sesion_abierta(request):
    if request.tenant is None:
        return None
    return (
        SesionCaja.objects.filter(usuario=request.user, estado=SesionCaja.Estado.ABIERTA)
        .select_related("terminal", "turno", "usuario")
        .first()
    )


# --- ingreso --------------------------------------------------------------


def ingresar(request):
    """Ingreso por numero de operador y PIN.

    Si el boliche se resuelve por subdominio, el numero alcanza. Si no, y hay mas
    de un operador con ese numero en distintos boliches, se pide el subdominio:
    mejor un mensaje claro que entrar al boliche equivocado.
    """
    error = None
    if request.method == "POST":
        numero = (request.POST.get("numero") or "").strip()
        pin = (request.POST.get("pin") or "").strip()

        candidatos = Usuario.objects.filter(numero=numero, is_active=True)
        if request.tenant is not None:
            candidatos = candidatos.filter(tenant=request.tenant)

        candidatos = list(candidatos.select_related("tenant")[:2])

        if len(candidatos) > 1:
            error = (
                "Hay mas de un operador con ese numero. Ingresa por el "
                "subdominio de tu boliche."
            )
        elif not candidatos:
            error = "Numero o PIN incorrecto."
        else:
            usuario = candidatos[0]
            if usuario.esta_bloqueado():
                error = "El operador esta bloqueado por intentos fallidos."
            elif not usuario.check_pin(pin):
                usuario.registrar_pin_fallido()
                error = "Numero o PIN incorrecto."
            elif usuario.tenant is None:
                error = "Ese usuario no pertenece a ningun boliche."
            else:
                usuario.registrar_pin_correcto()
                auth_login(request, usuario)
                return redirect("web:inicio")

    return render(request, "web/ingresar.html", {"error": error})


def salir(request):
    auth_logout(request)
    return redirect("web:ingresar")


def inicio(request):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return render(request, "web/sin_boliche.html")

    sesion = _sesion_abierta(request)
    if sesion is not None:
        return redirect("web:barra")

    abiertas = SesionCaja.objects.filter(estado=SesionCaja.Estado.ABIERTA).count()
    return render(
        request,
        "web/inicio.html",
        {
            "sesion": sesion,
            "cajas_abiertas": abiertas,
            "terminales": Terminal.objects.filter(activo=True),
        },
    )


# --- barra ----------------------------------------------------------------


def barra(request):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")

    sesion = _sesion_abierta(request)
    if sesion is None:
        messages.warning(request, "Primero hay que abrir la caja.")
        return redirect("web:inicio")

    if request.method == "POST":
        return _cobrar(request, sesion)

    categoria_id = request.GET.get("categoria")
    categorias = CategoriaProducto.objects.filter(activo=True)
    productos = Producto.objects.filter(activo=True).select_related("categoria")
    if categoria_id:
        productos = productos.filter(categoria_id=categoria_id)

    # Los mas vendidos de esta sesion primero: a las 3 AM nadie busca por orden
    # alfabetico.
    vendidos = {
        fila["items__producto_id"]: fila["total"]
        for fila in Venta.objects.filter(sesion_caja=sesion, estado=Venta.Estado.PAGADA)
        .values("items__producto_id")
        .annotate(total=Count("items"))
    }

    lista = list(productos)
    lista.sort(key=lambda p: (-vendidos.get(p.id, 0), p.nombre))

    return render(
        request,
        "web/barra.html",
        {
            "sesion": sesion,
            "categorias": categorias,
            "productos": lista,
            "categoria_actual": categoria_id,
        },
    )


def _cobrar(request, sesion):
    producto_id = request.POST.get("producto")
    producto = Producto.objects.filter(pk=producto_id, activo=True).first()
    if producto is None:
        messages.error(request, "Ese producto no existe.")
        return redirect("web:barra")

    medio = request.POST.get("medio") or Pago.Medio.EFECTIVO
    cantidad = int(request.POST.get("cantidad") or 1)

    venta = registrar_venta(
        sesion=sesion,
        items=[{"producto": producto, "cantidad": cantidad}],
        usuario=request.user,
        idempotency_key=(request.POST.get("idempotency_key") or "").strip(),
    )
    registrar_pago(
        venta=venta,
        medio=medio,
        monto=venta.total,
        estado=(
            Pago.Estado.APROBADO
            if medio == Pago.Medio.EFECTIVO
            else Pago.Estado.PENDIENTE
        ),
    )
    messages.success(request, f"Vendido: {producto.nombre} por {venta.total}")
    return redirect("web:barra")


def anular(request, venta_id):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if not request.user.tiene_permiso("ventas.anular"):
        messages.error(request, "No tenes permiso para anular ventas.")
        return redirect("web:barra")

    venta = Venta.objects.filter(pk=venta_id).first()
    if venta is None:
        messages.error(request, "La venta no existe.")
        return redirect("web:barra")

    anular_venta(
        venta=venta,
        usuario=request.user,
        motivo=(request.POST.get("motivo") or "Anulada desde la barra").strip(),
    )
    messages.warning(
        request, f"Venta numero {venta.numero} anulada. Queda registrada."
    )
    return redirect("web:barra")


# --- caja -----------------------------------------------------------------


def caja(request):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return redirect("web:inicio")

    sesion = _sesion_abierta(request)

    if request.method == "POST":
        accion = request.POST.get("accion")

        if accion == "abrir" and sesion is None:
            terminal = Terminal.objects.filter(
                pk=request.POST.get("terminal"), activo=True
            ).first()
            if terminal is None:
                messages.error(request, "Elegi una terminal.")
                return redirect("web:caja")

            turno = Turno.objects.filter(
                local=terminal.local, estado=Turno.Estado.ABIERTO
            ).first()
            if turno is None:
                turno = abrir_turno(
                    local=terminal.local,
                    nombre=request.POST.get("turno") or "Turno",
                )

            try:
                abrir_caja(
                    turno=turno,
                    terminal=terminal,
                    usuario=request.user,
                    fondo_inicial=_decimal(request.POST.get("fondo_inicial")),
                )
            except Exception as error:  # noqa: BLE001 - se le muestra al operador
                messages.error(request, str(error))
                return redirect("web:caja")

            messages.success(request, f"Caja abierta en {terminal.nombre}.")
            return redirect("web:barra")

        if accion == "cerrar" and sesion is not None:
            declarado = {
                medio: _decimal(request.POST.get(f"declarado_{medio}"))
                for medio, _ in Pago.Medio.choices
            }
            autorizante = None
            numero_autoriza = (request.POST.get("autorizado_por") or "").strip()
            if numero_autoriza:
                autorizante = Usuario.objects.filter(
                    tenant=request.tenant, numero=numero_autoriza
                ).first()

            try:
                arqueo = cerrar_caja(
                    sesion=sesion,
                    declarado_por_medio=declarado,
                    usuario=request.user,
                    autorizado_por=autorizante,
                    observaciones=(request.POST.get("observaciones") or "").strip(),
                )
            except Exception as error:  # noqa: BLE001
                messages.error(request, str(error))
                return redirect("web:caja")

            messages.success(
                request, f"Caja cerrada. Diferencia: {arqueo.diferencia_total}"
            )
            return redirect("web:panel")

    contexto = {"sesion": sesion, "ahora": timezone.now()}
    if sesion is None:
        contexto["terminales"] = Terminal.objects.filter(activo=True)
        contexto["turnos_abiertos"] = Turno.objects.filter(estado=Turno.Estado.ABIERTO)
    else:
        contexto["esperado"] = calcular_esperado(sesion)
        contexto["medios"] = Pago.Medio.choices
        contexto["resumen"] = resumen_de_ventas(sesion)
        contexto["ultimas"] = (
            sesion.ventas.select_related("usuario").order_by("-creado_en")[:15]
        )
    return render(request, "web/caja.html", contexto)


# --- panel del dueno ------------------------------------------------------


def panel(request):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return redirect("web:inicio")
    if not request.user.tiene_permiso("reportes.ver"):
        messages.error(request, "No tenes permiso para ver reportes.")
        return redirect("web:inicio")

    reporte = reporte_de_varianza()

    ultimos_turnos = Turno.objects.filter(estado=Turno.Estado.CERRADO).order_by(
        "-cerrado_en"
    )[:10]

    por_medio = (
        Pago.objects.filter(estado=Pago.Estado.APROBADO)
        .values("medio")
        .annotate(total=Sum("monto"))
    )

    anuladas = Venta.objects.filter(estado=Venta.Estado.ANULADA).select_related(
        "usuario"
    )[:10]

    return render(
        request,
        "web/panel.html",
        {
            "reporte": reporte,
            "ultimos_turnos": ultimos_turnos,
            "por_medio": por_medio,
            "anuladas": anuladas,
            "ahora": timezone.now(),
        },
    )


# --- entradas y control de acceso -----------------------------------------


def eventos(request):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return redirect("web:inicio")

    from apps.ticketing.models import Evento

    lista = Evento.objects.select_related("local").order_by("fecha", "nombre")
    return render(request, "web/eventos.html", {"eventos": lista})


def evento(request, evento_id):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return redirect("web:inicio")

    from apps.ticketing.models import Evento
    from apps.ticketing.services import resumen_de_evento

    objeto = Evento.objects.filter(pk=evento_id).first()
    if objeto is None:
        messages.error(request, "Ese evento no existe.")
        return redirect("web:eventos")

    return render(
        request,
        "web/evento.html",
        {"resumen": resumen_de_evento(objeto), "evento": objeto},
    )


def vender_entradas(request, evento_id):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if not request.user.tiene_permiso("entradas.vender"):
        messages.error(request, "No tenes permiso para vender entradas.")
        return redirect("web:inicio")

    from apps.ticketing.models import Evento, TipoEntrada
    from apps.ticketing.services import vender_entrada

    objeto = Evento.objects.filter(pk=evento_id).first()
    tipo_id = (request.POST.get("tipo") or "").strip()
    tipo = (
        TipoEntrada.objects.filter(pk=tipo_id, evento=objeto).first() if tipo_id else None
    )
    if objeto is None or tipo is None:
        messages.error(request, "Elegi un tipo de entrada.")
        return redirect("web:eventos")

    try:
        cantidad = int(request.POST.get("cantidad") or 1)
    except ValueError:
        cantidad = 1

    sesion = _sesion_abierta(request)

    try:
        entradas = vender_entrada(
            tipo=tipo,
            cantidad=cantidad,
            usuario=request.user,
            sesion_caja=sesion,
            terminal=sesion.terminal if sesion else None,
            comprador_nombre=(request.POST.get("comprador") or "").strip(),
        )
    except Exception as error:  # noqa: BLE001 - se le muestra al operador
        messages.error(request, str(error))
        return redirect("web:evento", evento_id=objeto.id)

    if sesion is None:
        messages.warning(
            request,
            f"{len(entradas)} entrada(s) emitidas SIN caja abierta: no quedaron "
            f"en el arqueo.",
        )
    else:
        messages.success(request, f"{len(entradas)} entrada(s) vendidas y cobradas.")

    if len(entradas) == 1:
        return redirect("web:entrada_qr", entrada_id=entradas[0].id)
    return redirect("web:evento", evento_id=objeto.id)


def entrada_qr(request, entrada_id):
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return redirect("web:inicio")

    from apps.ticketing.models import Entrada
    from apps.ticketing.services import contenido_qr, qr_svg

    entrada = Entrada.objects.filter(pk=entrada_id).select_related("evento", "tipo").first()
    if entrada is None:
        messages.error(request, "Esa entrada no existe.")
        return redirect("web:eventos")

    return render(
        request,
        "web/entrada_qr.html",
        {
            "entrada": entrada,
            "qr": qr_svg(entrada),
            "contenido": contenido_qr(entrada),
        },
    )


def puerta(request, evento_id=None):
    """Pantalla de la puerta.

    El sonido si sirve aca: el operador mira de reojo mientras hay cola, y el
    ruido en el acceso es menor que en la barra (ANALISIS.md seccion 7).
    """
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return redirect("web:inicio")

    from apps.ticketing.models import Evento

    lista = Evento.objects.exclude(estado=Evento.Estado.CANCELADO).order_by("fecha")
    elegido = None
    if evento_id:
        elegido = lista.filter(pk=evento_id).first()
    if elegido is None:
        elegido = lista.filter(estado=Evento.Estado.PUBLICADO).first() or lista.first()

    return render(
        request,
        "web/puerta.html",
        {"eventos": lista, "evento": elegido},
    )


def validar(request):
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "mensaje": "No autenticado."}, status=403)
    if not request.user.tiene_permiso("acceso.validar"):
        return JsonResponse(
            {"ok": False, "mensaje": "No tenes permiso para validar accesos."}, status=403
        )

    from apps.ticketing.models import Evento
    from apps.ticketing.services import registrar_ingreso_manual, validar_entrada

    objeto = Evento.objects.filter(pk=request.POST.get("evento")).first()
    if objeto is None:
        return JsonResponse({"ok": False, "mensaje": "Elegi un evento."}, status=400)

    terminal = Terminal.objects.filter(nombre="Puerta").first()
    manual = (request.POST.get("manual") or "").strip()

    if manual:
        motivo = (request.POST.get("motivo") or "").strip()
        if not motivo:
            return JsonResponse(
                {"ok": False, "mensaje": "Un ingreso manual exige motivo."}, status=400
            )
        try:
            cantidad = int(request.POST.get("cantidad") or 1)
        except ValueError:
            cantidad = 1
        dentro = registrar_ingreso_manual(
            evento=objeto, cantidad=cantidad, motivo=motivo,
            terminal=terminal, usuario=request.user,
        )
        return JsonResponse(
            {
                "ok": True,
                "mensaje": f"Ingreso manual registrado. Adentro: {dentro}.",
                "aforo_en_vivo": dentro,
                "aforo": objeto.aforo,
            }
        )

    resultado = validar_entrada(
        evento=objeto,
        texto_qr=request.POST.get("texto") or "",
        terminal=terminal,
        usuario=request.user,
    )
    entrada = resultado.get("entrada")
    return JsonResponse(
        {
            "ok": resultado["ok"],
            "motivo": resultado["motivo"],
            "mensaje": resultado["mensaje"],
            "folio": getattr(entrada, "folio", None),
            "tipo": getattr(getattr(entrada, "tipo", None), "nombre", None),
            "aforo_en_vivo": resultado.get("aforo_en_vivo"),
            "aforo": resultado.get("aforo"),
            "aforo_completo": resultado.get("aforo_completo", False),
        }
    )


# --- tienda publica -------------------------------------------------------
#
# Estas vistas son ANONIMAS: el tenant no sale de la sesion sino del slug de la
# URL. Es el unico lugar del sistema donde se entra sin autenticacion, asi que
# cada una abre el contexto del boliche a mano y lo cierra al salir.


def _boliche_o_404(slug):
    from apps.tenancy.models import Tenant

    return Tenant.objects.filter(slug=slug, estado=Tenant.Estado.ACTIVO).first()


def tienda(request, slug):
    """Cartelera publica del boliche."""
    from apps.ticketing.models import Evento

    boliche = _boliche_o_404(slug)
    if boliche is None:
        return render(request, "web/tienda_404.html", status=404)

    with tenant_context(boliche):
        eventos = list(
            Evento.objects.filter(estado=Evento.Estado.PUBLICADO)
            .prefetch_related("tipos")
            .order_by("fecha")
        )
        cartel = []
        for evento in eventos:
            cartel.append(
                {
                    "evento": evento,
                    "tipos": [
                        {"tipo": t, "disponibles": disponibles_de_tipo(t)}
                        for t in evento.tipos.all()
                    ],
                }
            )

    return render(request, "web/tienda.html", {"boliche": boliche, "cartel": cartel})


def tienda_evento(request, slug, evento_id):
    from apps.ticketing.models import Evento

    boliche = _boliche_o_404(slug)
    if boliche is None:
        return render(request, "web/tienda_404.html", status=404)

    with tenant_context(boliche):
        evento = Evento.objects.filter(
            pk=evento_id, estado=Evento.Estado.PUBLICADO
        ).first()
        if evento is None:
            return render(request, "web/tienda_404.html", status=404)

        tipos = [
            {"tipo": t, "disponibles": disponibles_de_tipo(t)}
            for t in evento.tipos.all()
        ]
        return render(
            request,
            "web/tienda_evento.html",
            {"boliche": boliche, "evento": evento, "tipos": tipos},
        )


def reservar(request, slug, evento_id):
    """Toma el cupo y manda al comprador a pagar."""
    from apps.ticketing.models import Evento, TipoEntrada
    from apps.ticketing.pagos import proveedor_actual

    boliche = _boliche_o_404(slug)
    if boliche is None:
        return render(request, "web/tienda_404.html", status=404)
    if request.method != "POST":
        return redirect("web:tienda_evento", slug=slug, evento_id=evento_id)

    with tenant_context(boliche):
        evento = Evento.objects.filter(pk=evento_id).first()
        tipo = TipoEntrada.objects.filter(
            pk=request.POST.get("tipo"), evento=evento
        ).first()
        if tipo is None:
            messages.error(request, "Elegi un tipo de entrada.")
            return redirect("web:tienda_evento", slug=slug, evento_id=evento_id)

        try:
            cantidad = int(request.POST.get("cantidad") or 1)
        except ValueError:
            cantidad = 1

        try:
            reserva = crear_reserva(
                tipo=tipo,
                cantidad=cantidad,
                comprador_nombre=(request.POST.get("nombre") or "").strip(),
                comprador_contacto=(request.POST.get("contacto") or "").strip(),
            )
        except Exception as error:  # noqa: BLE001 - se le muestra al comprador
            messages.error(request, str(error))
            return redirect("web:tienda_evento", slug=slug, evento_id=evento_id)

        intento = proveedor_actual().crear_intento(
            reserva=reserva,
            url_retorno=request.build_absolute_uri(
                reverse("web:reserva_pago", args=[reserva.token])
            ),
        )
        reserva.referencia_pago = intento["referencia"]
        reserva.save(update_fields=["referencia_pago", "actualizado_en"])
        token = reserva.token

    return redirect("web:reserva_pago", token=token)


def reserva_pago(request, token):
    """Pantalla de pago de una reserva.

    El cupo queda tomado mientras el comprador paga. El reloj corre: el sistema
    dice cuanto falta.
    """
    from apps.ticketing.models import Reserva

    # Vista anonima: el token es lo unico que identifica al boliche. Por eso se
    # busca con unscoped y recien despues se abre el contexto.
    reserva = Reserva.unscoped.filter(token=token).select_related(
        "evento", "tipo", "tenant"
    ).first()
    if reserva is None:
        return render(request, "web/tienda_404.html", status=404)

    with tenant_context(reserva.tenant):
        reserva = Reserva.objects.select_related("evento", "tipo").get(pk=reserva.pk)
        return render(
            request,
            "web/reserva_pago.html",
            {
                "reserva": reserva,
                "boliche": reserva.tenant,
                "segundos_restantes": max(
                    0, int((reserva.expira_en - timezone.now()).total_seconds())
                ),
            },
        )


def reserva_confirmar(request, token):
    """Vuelta del adquirente. En produccion esto lo dispara el webhook."""
    from apps.ticketing.models import Reserva
    from apps.ticketing.pagos import proveedor_actual

    reserva = Reserva.unscoped.filter(token=token).select_related("tenant").first()
    if reserva is None:
        return render(request, "web/tienda_404.html", status=404)

    if request.method != "POST":
        return redirect("web:reserva_pago", token=token)

    with tenant_context(reserva.tenant):
        reserva = Reserva.objects.select_related("evento", "tipo").get(pk=reserva.pk)
        # .dict() y no dict(): un QueryDict convertido a secas devuelve listas.
        datos = proveedor_actual().interpretar_webhook(
            payload=request.POST.dict(), cabeceras=dict(request.headers)
        )
        resultado = confirmar_reserva(
            reserva=reserva,
            referencia_pago=datos.get("referencia") or reserva.referencia_pago,
            medio=datos.get("medio") or "simulado",
        )
        if resultado["estado"] == "pago_tardio":
            return render(
                request,
                "web/reserva_vencida.html",
                {"reserva": reserva, "boliche": reserva.tenant},
            )
        entradas = resultado["entradas"]
        primer_token = entradas[0].qr_token if entradas else None

    if primer_token:
        return redirect("web:entrada_publica", token=primer_token)
    return redirect("web:tienda", slug=reserva.tenant.slug)


def entrada_publica(request, token):
    """La entrada, para que el comprador la muestre en la puerta.

    Es un titulo al portador: quien tiene el token, entra. En la puerta se
    consume una sola vez, asi que reenviarlo no sirve dos veces.
    """
    from apps.ticketing.models import Entrada

    # Igual que la reserva: anonima, se resuelve por token y despues se abre el
    # contexto del boliche.
    entrada = Entrada.unscoped.filter(qr_token=token).select_related(
        "evento", "tipo", "tenant"
    ).first()
    if entrada is None:
        return render(request, "web/tienda_404.html", status=404)

    with tenant_context(entrada.tenant):
        entrada = Entrada.objects.select_related("evento", "tipo").get(pk=entrada.pk)
        # Si la compra fue de varias entradas, se muestran todas: quien compro
        # cuatro tiene que poder mostrar las cuatro.
        hermanas = []
        if entrada.reserva_id:
            hermanas = list(
                Entrada.objects.filter(reserva_id=entrada.reserva_id)
                .exclude(pk=entrada.pk)
                .select_related("tipo")
            )
        return render(
            request,
            "web/entrada_publica.html",
            {
                "entrada": entrada,
                "hermanas": hermanas,
                "boliche": entrada.tenant,
                "qr": qr_svg(entrada),
            },
        )


@csrf_exempt
@require_POST
def webhook_de_pago(request):
    """Notificacion del adquirente.

    Es idempotente por diseño: confirmar_reserva devuelve 'ya_confirmada' si el
    pago se notifica dos veces, y el cupo no se toca dos veces.
    """
    from apps.ticketing.models import Reserva
    from apps.ticketing.pagos import proveedor_actual

    datos = proveedor_actual().interpretar_webhook(
        payload=request.POST.dict(), cabeceras=dict(request.headers)
    )
    referencia = datos.get("referencia") or ""
    if not referencia:
        return JsonResponse({"ok": False, "mensaje": "Sin referencia."}, status=400)

    # El proveedor simulado usa SIM-<token>; un adquirente real mapearia su propio
    # identificador de pago a la reserva.
    buscado = referencia[4:] if referencia.startswith("SIM-") else referencia
    reserva = Reserva.unscoped.filter(token__startswith=buscado).first()
    if reserva is None:
        reserva = Reserva.unscoped.filter(referencia_pago=referencia).first()
    if reserva is None:
        return JsonResponse({"ok": False, "mensaje": "Reserva desconocida."}, status=404)

    if datos.get("estado") != "aprobado":
        return JsonResponse({"ok": True, "estado": "ignorado"})

    with tenant_context(reserva.tenant):
        reserva = Reserva.objects.get(pk=reserva.pk)
        resultado = confirmar_reserva(
            reserva=reserva, referencia_pago=referencia, medio=datos.get("medio") or ""
        )

    return JsonResponse(
        {
            "ok": True,
            "estado": resultado["estado"],
            "mensaje": resultado["mensaje"],
            "entradas": [e.qr_token for e in resultado["entradas"]],
        }
    )


# --- listas y promotores --------------------------------------------------


def listas(request, evento_id):
    """Listas del evento, su uso y la liquidacion de comisiones."""
    if not request.user.is_authenticated:
        return redirect("web:ingresar")
    if request.tenant is None:
        return redirect("web:inicio")
    if not request.user.tiene_permiso("listas.gestionar"):
        messages.error(request, "No tenes permiso para gestionar listas.")
        return redirect("web:inicio")

    from apps.ticketing.models import ComisionPromotor, Evento, Lista, Promotor
    from apps.ticketing.listas import resumen_de_listas

    objeto = Evento.objects.filter(pk=evento_id).first()
    if objeto is None:
        messages.error(request, "Ese evento no existe.")
        return redirect("web:eventos")

    if request.method == "POST":
        accion = request.POST.get("accion")

        if accion == "crear_lista":
            try:
                cupo = int(request.POST.get("cupo") or 0)
            except ValueError:
                cupo = 0
            corte = (request.POST.get("hora_de_corte") or "").strip()
            # filter(pk="") con un UUID vacio revienta: hay que preguntar antes.
            promotor_id = (request.POST.get("promotor") or "").strip()
            promotor = (
                Promotor.objects.filter(pk=promotor_id).first() if promotor_id else None
            )
            try:
                crear_lista(
                    evento=objeto,
                    nombre=(request.POST.get("nombre") or "").strip(),
                    cupo=cupo,
                    tipo=request.POST.get("tipo") or Lista.Tipo.CASA,
                    promotor=promotor,
                    creada_por=request.user,
                    hora_de_corte=corte or None,
                )
            except Exception as error:  # noqa: BLE001
                messages.error(request, str(error))
            else:
                messages.success(request, "Lista creada.")
            return redirect("web:listas", evento_id=objeto.id)

        if accion == "agregar_invitado":
            lista_id = (request.POST.get("lista") or "").strip()
            lista = Lista.objects.filter(pk=lista_id).first() if lista_id else None
            if lista is None:
                messages.error(request, "Elegi una lista.")
                return redirect("web:listas", evento_id=objeto.id)
            try:
                personas = int(request.POST.get("personas") or 1)
            except ValueError:
                personas = 1
            try:
                agregar_invitado(
                    lista=lista,
                    nombre=(request.POST.get("nombre") or "").strip(),
                    personas=personas,
                    contacto=(request.POST.get("contacto") or "").strip(),
                )
            except Exception as error:  # noqa: BLE001
                messages.error(request, str(error))
            else:
                messages.success(request, "Invitado agregado.")
            return redirect("web:listas", evento_id=objeto.id)

        if accion == "liquidar":
            liquidaciones = liquidar_comisiones(evento=objeto, usuario=request.user)
            messages.success(
                request, f"Comisiones liquidadas: {len(liquidaciones)}."
            )
            return redirect("web:listas", evento_id=objeto.id)

        if accion == "aprobar":
            from apps.ticketing.listas import aprobar_comision

            comision_id = (request.POST.get("comision") or "").strip()
            comision = (
                ComisionPromotor.objects.filter(pk=comision_id).first()
                if comision_id
                else None
            )
            if comision is not None:
                aprobar_comision(comision=comision, usuario=request.user)
                messages.success(request, "Comision aprobada.")
            return redirect("web:listas", evento_id=objeto.id)

        if accion == "pagar":
            from apps.ticketing.listas import pagar_comision

            comision_id = (request.POST.get("comision") or "").strip()
            comision = (
                ComisionPromotor.objects.filter(pk=comision_id).first()
                if comision_id
                else None
            )
            if comision is not None:
                try:
                    pagar_comision(comision=comision, usuario=request.user)
                except Exception as error:  # noqa: BLE001
                    messages.error(request, str(error))
                else:
                    messages.success(request, "Comision pagada.")
            return redirect("web:listas", evento_id=objeto.id)

    return render(
        request,
        "web/listas.html",
        {
            "evento": objeto,
            "resumen": resumen_de_listas(objeto),
            "promotores": Promotor.objects.filter(activo=True),
            "tipos_de_lista": Lista.Tipo.choices,
            "comisiones": ComisionPromotor.objects.filter(evento=objeto).select_related(
                "promotor"
            ),
        },
    )


def validar_lista(request):
    """Ingreso por lista desde la puerta."""
    if not request.user.is_authenticated:
        return JsonResponse({"ok": False, "mensaje": "No autenticado."}, status=403)
    if not request.user.tiene_permiso("acceso.validar"):
        return JsonResponse(
            {"ok": False, "mensaje": "No tenes permiso para validar accesos."},
            status=403,
        )

    from apps.ticketing.models import Evento
    from apps.ticketing.listas import validar_ingreso_de_lista

    objeto = Evento.objects.filter(pk=request.POST.get("evento")).first()
    if objeto is None:
        return JsonResponse({"ok": False, "mensaje": "Elegi un evento."}, status=400)

    forzar = (request.POST.get("forzar") or "") == "1"
    if forzar and not request.user.tiene_permiso("acceso.forzar"):
        return JsonResponse(
            {"ok": False, "mensaje": "No tenes permiso para forzar un ingreso."},
            status=403,
        )

    try:
        personas = int(request.POST.get("personas") or 1)
    except ValueError:
        personas = 1

    terminal = Terminal.objects.filter(nombre="Puerta").first()
    resultado = validar_ingreso_de_lista(
        evento=objeto,
        texto=(request.POST.get("texto") or "").strip(),
        nombre=(request.POST.get("nombre") or "").strip(),
        personas=personas,
        terminal=terminal,
        usuario=request.user,
        forzar=forzar,
    )
    invitado = resultado.get("invitado")
    return JsonResponse(
        {
            "ok": resultado["ok"],
            "motivo": resultado["motivo"],
            "mensaje": resultado["mensaje"],
            "invitado": getattr(invitado, "nombre", None),
            "aforo_en_vivo": resultado.get("aforo_en_vivo"),
            "aforo": resultado.get("aforo"),
        }
    )
