"""Simula una noche completa en un boliche de demostracion.

En un solo comando deja el sistema con datos creibles y muestra lo que el producto
vende: la varianza de stock y el control de acceso. Sirve para dos cosas: probar
sin cargar nada a mano, y mostrarle el sistema a un boliche en cinco minutos.

Uso:  python manage.py demo
"""

from datetime import date, time
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import MembresiaUsuario, Rol, Usuario
from apps.catalog.models import Insumo, Producto
from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.sales.models import Pago
from apps.sales.services import (
    abrir_caja,
    abrir_turno,
    anular_venta,
    calcular_esperado,
    cerrar_caja,
    registrar_pago,
    registrar_venta,
)
from apps.stock.conteo import cerrar_conteo, iniciar_conteo, registrar_item
from apps.stock.models import Deposito, StockMovimiento
from apps.stock.reportes import reporte_de_varianza
from apps.stock.services import registrar_movimiento, saldo_calculado
from apps.tenancy.models import Local, Terminal
from apps.ticketing.listas import (
    agregar_invitado,
    crear_lista,
    crear_promotor,
    liquidar_comisiones,
    validar_ingreso_de_lista,
)
from apps.ticketing.models import Entrada, Lista
from apps.ticketing.services import (
    aforo_en_vivo,
    agregar_tipo,
    crear_evento,
    publicar_evento,
    registrar_ingreso_manual,
    resumen_de_evento,
    validar_entrada,
    vender_entrada,
)

VENTAS_DEMO = [
    ("Cuba libre", 18, Pago.Medio.EFECTIVO),
    ("Gin tonic", 12, Pago.Medio.QR),
    ("Vodka tonic", 9, Pago.Medio.EFECTIVO),
    ("Fernet con cola", 14, Pago.Medio.QR),
    ("Chopp", 40, Pago.Medio.EFECTIVO),
    ("Cerveza lata", 26, Pago.Medio.EFECTIVO),
    ("Whisky", 6, Pago.Medio.QR),
    ("Tequila", 5, Pago.Medio.EFECTIVO),
    ("Agua", 11, Pago.Medio.EFECTIVO),
]

# Faltante deliberado, para que la varianza tenga algo que mostrar.
FALTANTE_DEMO = {"Ron": "200"}

COMPRAS_INICIALES = {
    "Ron": "6000", "Vodka": "4500", "Gin": "4500", "Whisky": "3000",
    "Fernet": "3000", "Tequila": "2250", "Cerveza barril": "60000",
    "Cerveza lata": "120", "Cola": "18000", "Tonica": "12000",
    "Agua": "60", "Hielo": "30000", "Vaso descartable": "500",
}


class Command(BaseCommand):
    help = "Crea un boliche de demostracion y simula una noche completa."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="demo")
        parser.add_argument("--nombre", default="Boliche Demo")
        parser.add_argument(
            "--limpiar",
            action="store_true",
            help="Borra los datos previos de ese boliche antes de simular de nuevo.",
        )

    def handle(self, *args, **opciones):
        if opciones["limpiar"]:
            self.stdout.write("Borrando los datos previos del boliche de demo...")
            self._limpiar(opciones["slug"])

        datos = provisionar_boliche(
            nombre=opciones["nombre"],
            slug=opciones["slug"],
            rut="210000000000",
            aforo=300,
            nombre_local="Principal",
            terminales=["Barra 1", "Barra 2"],
            usuario_admin="Dueno",
            numero_admin="0001",
            pin_admin="1234",
        )
        tenant = datos["tenant"]

        with tenant_context(tenant):
            local = Local.objects.get(nombre="Principal")
            barra = Deposito.objects.get(local=local, nombre="Barra 1")
            deposito = Deposito.objects.get(local=local, nombre="Deposito")
            terminal_barra = Terminal.objects.get(local=local, nombre="Barra 1")
            terminal_puerta = Terminal.objects.get(local=local, nombre="Puerta")
            dueno = datos["admin"]
            encargado = self._encargado(tenant, local)

            self.stdout.write("Cargando stock inicial en la barra...")
            self._cargar_stock(barra, deposito)

            self.stdout.write("Abriendo turno y caja de barra...")
            turno = abrir_turno(local=local, nombre="Viernes de demo")
            sesion_barra = abrir_caja(
                turno=turno, terminal=terminal_barra, usuario=encargado,
                fondo_inicial=Decimal("3000"),
            )

            self.stdout.write("Vendiendo en la barra...")
            total_bebida = self._vender_bebidas(sesion_barra, encargado)
            self.stdout.write(
                f"  Vendido: {total_bebida} en {len(VENTAS_DEMO)} ventas"
            )

            self.stdout.write("Abriendo la puerta y vendiendo entradas...")
            resumen_evento, entradas_validadas = self._operar_puerta(
                turno, terminal_puerta, encargado, local
            )

            esperado_barra = calcular_esperado(sesion_barra)
            cerrar_caja(
                sesion=sesion_barra,
                declarado_por_medio={m: v for m, v in esperado_barra.items()},
                usuario=encargado,
            )
            self.stdout.write("  Caja de barra cerrada sin diferencia.")

            self.stdout.write("Contando stock (conteo ciego)...")
            self._contar(barra, encargado, turno, dueno)

            reporte = reporte_de_varianza()

        self._imprimir_varianza(reporte)
        self._imprimir_evento(resumen_evento, entradas_validadas)
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "  Ingreso: numero 0001 PIN 1234 (dueno) | numero 0002 PIN 2222 (encargado)"
            )
        )

    # --- limpieza ---------------------------------------------------------

    def _limpiar(self, slug):
        """Borra los datos del boliche de demo, en orden de dependencias.

        Es explicito y solo lo usa la demo: en produccion un boliche no se borra,
        se suspende. Aca hace falta para poder correr la simulacion muchas veces.
        """
        from django.db import transaction

        from apps.accounts.models import MembresiaUsuario, Rol
        from apps.catalog.models import (
            CategoriaProducto, ComboItem, Insumo, InsumoPresentacion,
            Producto, Receta, RecetaItem,
        )
        from apps.sales.models import (
            Arqueo, ArqueoLinea, EgresoCaja, ItemVenta, Pago, SesionCaja, Turno, Venta,
        )
        from apps.stock.models import (
            AjusteInventario, Conteo, ConteoItem, Deposito, StockItem, StockMovimiento,
        )
        from apps.tenancy.models import Local, Tenant, Terminal
        from apps.ticketing.models import (
            ComisionPromotor, Entrada, Evento, Lista, ListaInvitado,
            MovimientoAforo, Promotor, Reserva, TipoEntrada, UsoLista,
        )

        tenant = Tenant.objects.filter(slug=slug).first()
        if tenant is None:
            return

        # El orden importa: casi todas las FK son PROTECT.
        #
        # Encadenamientos a respetar:
        #   UsoLista -> ListaInvitado -> Lista -> Promotor
        #   MovimientoAforo -> Entrada -> Reserva / Venta
        #   ComisionPromotor -> Evento y Promotor
        en_orden = [
            ArqueoLinea, Arqueo, AjusteInventario, ConteoItem, Conteo,
            Pago, ItemVenta,
            UsoLista, MovimientoAforo, Entrada, Reserva,
            ComisionPromotor, ListaInvitado, Lista, Promotor,
            Venta, EgresoCaja, SesionCaja, Turno, TipoEntrada, Evento,
            StockMovimiento, StockItem,
            Terminal, Deposito,
            ComboItem, RecetaItem, Receta, Producto, CategoriaProducto,
            InsumoPresentacion, Insumo,
            MembresiaUsuario, Rol, Local,
        ]
        with transaction.atomic():
            for modelo in en_orden:
                modelo.unscoped.filter(tenant=tenant).delete()

    # --- pasos ------------------------------------------------------------

    def _encargado(self, tenant, local):
        usuario = Usuario.objects.filter(tenant=tenant, numero="0002").first()
        if usuario is None:
            usuario = Usuario.objects.create_user(
                email=None, nombre="Enzo", apellido="Encargado",
                tenant=tenant, numero="0002",
            )
            usuario.set_pin("2222")
            usuario.save()
            MembresiaUsuario.objects.create(
                usuario=usuario, local=local,
                rol=Rol.unscoped.get(tenant=tenant, codigo="encargado"),
            )
        return usuario

    def _cargar_stock(self, barra, deposito):
        for nombre, cantidad in COMPRAS_INICIALES.items():
            insumo = Insumo.objects.get(nombre=nombre)
            for destino in (barra, deposito):
                registrar_movimiento(
                    deposito=destino, insumo=insumo,
                    tipo=StockMovimiento.Tipo.COMPRA,
                    cantidad=Decimal(cantidad) / 2,
                    costo_unitario=insumo.costo_promedio,
                    motivo="Carga inicial de demo",
                    idempotency_key=f"demo-compra-{insumo.id}-{destino.id}",
                )

    def _vender_bebidas(self, sesion, encargado):
        total = Decimal("0")
        for nombre, cantidad, medio in VENTAS_DEMO:
            producto = Producto.objects.get(nombre=nombre)
            venta = registrar_venta(
                sesion=sesion,
                items=[{"producto": producto, "cantidad": cantidad}],
                usuario=encargado,
                idempotency_key=f"demo-{nombre}",
            )
            registrar_pago(
                venta=venta, medio=medio, monto=venta.total,
                estado=(
                    Pago.Estado.APROBADO
                    if medio == Pago.Medio.EFECTIVO
                    else Pago.Estado.PENDIENTE
                ),
            )
            total += venta.total
        return total

    def _operar_puerta(self, turno, terminal_puerta, encargado, local):
        # La hora de apertura no es decorativa: es lo que permite calcular bien el
        # corte de lista. Un corte a la 01:00 con apertura a las 23:00 cae al dia
        # SIGUIENTE; sin apertura, el sistema lo tomaria como la 01:00 de hoy, o
        # sea en el pasado, y la lista nace cortada.
        evento = crear_evento(
            local=local, nombre="Fiesta de demo",
            fecha=timezone.localdate(), aforo=120,
            hora_apertura=time(23, 0), hora_cierre=time(6, 0),
        )
        general = agregar_tipo(
            evento=evento, nombre="General", precio="500", cupo=80, orden=1
        )
        vip = agregar_tipo(evento=evento, nombre="VIP", precio="1200", cupo=15, orden=2)
        publicar_evento(evento)

        sesion_puerta = abrir_caja(
            turno=turno, terminal=terminal_puerta, usuario=encargado,
            fondo_inicial=Decimal("2000"),
        )

        vendidas = []
        for tipo, cantidad, medio in (
            (general, 12, Pago.Medio.EFECTIVO),
            (vip, 3, Pago.Medio.QR),
        ):
            entradas = vender_entrada(
                tipo=tipo, cantidad=cantidad, usuario=encargado,
                sesion_caja=sesion_puerta, terminal=terminal_puerta,
            )
            for entrada in entradas:
                registrar_pago(
                    venta=entrada.venta, medio=medio, monto=entrada.precio,
                    estado=(
                        Pago.Estado.APROBADO
                        if medio == Pago.Medio.EFECTIVO
                        else Pago.Estado.PENDIENTE
                    ),
                )
            vendidas.extend(entradas)

        # Un ingreso con QR real y uno manual por lista.
        validadas = 0
        for entrada in vendidas[:8]:
            resultado = validar_entrada(
                evento=evento, texto_qr=entrada.qr_token,
                terminal=terminal_puerta, usuario=encargado,
            )
            if resultado["ok"]:
                validadas += 1
        registrar_ingreso_manual(
            evento=evento, cantidad=4, motivo="Lista del DJ",
            terminal=terminal_puerta, usuario=encargado,
        )

        # Listas y promotores: el corte lo aplica el sistema, la atribucion se
        # registra en la puerta, y las comisiones salen de ese registro.
        promotor = crear_promotor(nombre="Rocio", valor_comision="150")
        lista_promotor = crear_lista(
            evento=evento, nombre="Lista de Rocio", cupo=25,
            tipo=Lista.Tipo.PROMOTOR, promotor=promotor,
            creada_por=encargado, hora_de_corte=time(1, 0),
        )
        crear_lista(
            evento=evento, nombre="Lista de la casa", cupo=15,
            tipo=Lista.Tipo.CASA, creada_por=encargado,
        )

        personas_por_lista = 0
        rechazos = []
        for nombre, personas in (("Ana", 3), ("Beto", 2), ("Carla", 4), ("Dani", 1)):
            invitado = agregar_invitado(
                lista=lista_promotor, nombre=nombre, personas=personas
            )
            resultado = validar_ingreso_de_lista(
                evento=evento, texto=invitado.qr_token, personas=personas,
                terminal=terminal_puerta, usuario=encargado,
            )
            if resultado["ok"]:
                personas_por_lista += personas
            else:
                # Que un rechazo no pase inadvertido: la primera version de esta
                # demo rechazaba las diez y no lo decia.
                rechazos.append(f"{nombre}: {resultado['mensaje']}")

        for linea in rechazos:
            self.stdout.write(self.style.ERROR(f"  RECHAZADO en lista -> {linea}"))

        comisiones = liquidar_comisiones(evento=evento, usuario=encargado)
        total_comision = sum((c.monto for c in comisiones), Decimal("0"))

        esperado = calcular_esperado(sesion_puerta)
        cerrar_caja(
            sesion=sesion_puerta,
            declarado_por_medio={m: v for m, v in esperado.items()},
            usuario=encargado,
        )
        self.stdout.write(
            f"  Entradas vendidas: {len(vendidas)} | ingresaron con QR: {validadas}"
        )
        self.stdout.write(
            f"  Listas: {personas_por_lista} personas | comision liquidada: "
            f"{total_comision}"
        )
        return resumen_de_evento(evento), validadas

    def _contar(self, barra, encargado, turno, dueno):
        conteo = iniciar_conteo(deposito=barra, usuario=encargado, turno=turno)
        for insumo in Insumo.objects.filter(activo=True):
            teorico = saldo_calculado(barra, insumo)
            if teorico <= 0:
                continue
            declarado = teorico
            if insumo.nombre in FALTANTE_DEMO:
                declarado = teorico - Decimal(FALTANTE_DEMO[insumo.nombre])
            registrar_item(
                conteo=conteo, insumo=insumo,
                unidades_cerradas=max(Decimal("0"), declarado),
            )
        cerrar_conteo(
            conteo=conteo, autorizado_por=dueno,
            motivos={"Ron": "Faltante detectado en el conteo"},
        )

    # --- salida -----------------------------------------------------------

    def _imprimir_varianza(self, reporte):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 62))
        self.stdout.write(self.style.SUCCESS("  VARIANZA DE STOCK DEL EVENTO"))
        self.stdout.write(self.style.SUCCESS("=" * 62))
        self.stdout.write(
            f"  {'Insumo':<20}{'Teorico':>10}{'Contado':>10}{'Dif.':>10}{'Valor':>11}"
        )
        for fila in reporte["filas"]:
            self.stdout.write(
                f"  {fila['insumo']:<20}{fila['teorico']:>10.1f}{fila['contado']:>10.1f}"
                f"{fila['diferencia']:>10.1f}{fila['valorizado']:>11.1f}"
            )
        if not reporte["filas"]:
            self.stdout.write("  (sin diferencias)")
        if reporte["depositos_sin_conteo"]:
            sin = ", ".join(d["deposito"] for d in reporte["depositos_sin_conteo"])
            self.stdout.write(self.style.WARNING(f"  Sin conteo: {sin}"))
        self.stdout.write("")
        self.stdout.write(f"  Varianza valorizada: {reporte['varianza_valorizada']}")
        if reporte["duracion_promedio_seg"]:
            self.stdout.write(
                f"  Duracion del conteo: {reporte['duracion_promedio_seg']} segundos"
            )

    def _imprimir_evento(self, resumen, validadas):
        evento = resumen["evento"]
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 62))
        self.stdout.write(self.style.SUCCESS("  CONTROL DE ACCESO"))
        self.stdout.write(self.style.SUCCESS("=" * 62))
        self.stdout.write(f"  Evento: {evento.nombre} ({evento.fecha})")
        self.stdout.write(
            f"  Aforo: {resumen['aforo']} | Adentro: {resumen['dentro']} | "
            f"Disponible: {resumen['disponible']}"
        )
        self.stdout.write(
            f"  Vendidas: {resumen['vendidas']} | Ingresaron: {resumen['usadas']} | "
            f"Anuladas: {resumen['anuladas']}"
        )
        self.stdout.write(f"  Recaudado en entradas: {resumen['recaudado']}")
        for fila in resumen["tipos"]:
            self.stdout.write(
                f"    {fila['tipo'].nombre:<12} cupo {fila['cupo']:>3}  "
                f"vendidas {fila['vendidas']:>3}  quedan {fila['disponibles']:>3}"
            )
