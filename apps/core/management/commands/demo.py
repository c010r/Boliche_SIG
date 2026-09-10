"""Simula una noche completa en un boliche de demostracion.

En un solo comando deja el sistema con datos creibles y muestra la varianza, que
es lo que el producto vende. Sirve para dos cosas: probar sin cargar nada a mano,
y mostrarle el sistema a un boliche en cinco minutos.

Uso:  python manage.py demo
"""

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.accounts.models import Usuario
from apps.catalog.models import Insumo, Producto
from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.sales.models import Pago
from apps.sales.services import (
    abrir_caja,
    abrir_turno,
    calcular_esperado,
    cerrar_caja,
    registrar_pago,
    registrar_venta,
)
from apps.stock.conteo import cerrar_conteo, iniciar_conteo, registrar_item
from apps.stock.models import Deposito, StockMovimiento
from apps.stock.reportes import reporte_de_varianza
from apps.stock.services import registrar_movimiento
from apps.tenancy.models import Local, Terminal

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


class Command(BaseCommand):
    help = "Crea un boliche de demostracion y simula una noche completa."

    def add_arguments(self, parser):
        parser.add_argument("--slug", default="demo")
        parser.add_argument("--nombre", default="Boliche Demo")

    def handle(self, *args, **opciones):
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
            terminal = Terminal.objects.get(local=local, nombre="Barra 1")
            dueno = datos["admin"]
            encargado = self._encargado(tenant, local)

            self.stdout.write("Cargando stock inicial en la barra...")
            self._cargar_stock(barra, deposito)

            self.stdout.write("Abriendo turno y caja...")
            turno = abrir_turno(local=local, nombre="Viernes de demo")
            sesion = abrir_caja(
                turno=turno,
                terminal=terminal,
                usuario=encargado,
                fondo_inicial=Decimal("3000"),
            )

            self.stdout.write("Vendiendo...")
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
                    venta=venta,
                    medio=medio,
                    monto=venta.total,
                    estado=(
                        Pago.Estado.APROBADO
                        if medio == Pago.Medio.EFECTIVO
                        else Pago.Estado.PENDIENTE
                    ),
                )
                total += venta.total

            self.stdout.write(f"  Vendido: {total} en {len(VENTAS_DEMO)} ventas")

            esperado = calcular_esperado(sesion)
            cerrar_caja(
                sesion=sesion,
                declarado_por_medio={
                    medio: monto for medio, monto in esperado.items()
                },
                usuario=encargado,
            )
            self.stdout.write("  Caja cerrada sin diferencia.")

            self.stdout.write("Contando stock (conteo ciego)...")
            conteo = iniciar_conteo(deposito=barra, usuario=encargado, turno=turno)
            for insumo in Insumo.objects.filter(activo=True):
                from apps.stock.services import saldo_calculado

                teorico = saldo_calculado(barra, insumo)
                if teorico <= 0:
                    continue
                declarado = teorico
                if insumo.nombre in FALTANTE_DEMO:
                    declarado = teorico - Decimal(FALTANTE_DEMO[insumo.nombre])
                registrar_item(
                    conteo=conteo,
                    insumo=insumo,
                    unidades_cerradas=max(Decimal("0"), declarado),
                )
            cerrar_conteo(
                conteo=conteo,
                autorizado_por=dueno,
                motivos={"Ron": "Faltante detectado en el conteo"},
            )

            # El reporte se arma adentro del contexto: los modelos de negocio
            # son fail-closed y consultarlos fuera revienta a proposito.
            reporte = reporte_de_varianza()

        self._imprimir_varianza(reporte, datos)

    def _encargado(self, tenant, local):
        usuario = Usuario.objects.filter(tenant=tenant, numero="0002").first()
        if usuario is None:
            from apps.accounts.models import MembresiaUsuario, Rol

            usuario = Usuario.objects.create_user(
                email=None, nombre="Enzo", apellido="Encargado",
                tenant=tenant, numero="0002",
            )
            usuario.set_pin("2222")
            usuario.save()
            rol = Rol.unscoped.get(tenant=tenant, codigo="encargado")
            MembresiaUsuario.objects.create(usuario=usuario, local=local, rol=rol)
        return usuario

    def _cargar_stock(self, barra, deposito):
        compras = {
            "Ron": "6000", "Vodka": "4500", "Gin": "4500", "Whisky": "3000",
            "Fernet": "3000", "Tequila": "2250", "Cerveza barril": "60000",
            "Cerveza lata": "120", "Cola": "18000", "Tonica": "12000",
            "Agua": "60", "Hielo": "30000", "Vaso descartable": "500",
        }
        for nombre, cantidad in compras.items():
            insumo = Insumo.objects.get(nombre=nombre)
            for destino in (barra, deposito):
                registrar_movimiento(
                    deposito=destino,
                    insumo=insumo,
                    tipo=StockMovimiento.Tipo.COMPRA,
                    cantidad=Decimal(cantidad) / 2,
                    costo_unitario=insumo.costo_promedio,
                    motivo="Carga inicial de demo",
                    idempotency_key=f"demo-compra-{insumo.id}-{destino.id}",
                )

    def _imprimir_varianza(self, reporte, datos):
        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("=" * 62))
        self.stdout.write(self.style.SUCCESS("  VARIANZA DEL EVENTO"))
        self.stdout.write(self.style.SUCCESS("=" * 62))
        encabezado = f"  {'Insumo':<20}{'Teorico':>10}{'Contado':>10}{'Dif.':>10}{'Valor':>11}"
        self.stdout.write(encabezado)
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
        self.stdout.write(
            f"  Varianza valorizada: {reporte['varianza_valorizada']}"
        )
        duracion = reporte["duracion_promedio_seg"]
        if duracion:
            self.stdout.write(f"  Duracion del conteo: {duracion} segundos")
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "  Ingreso: numero 0001 PIN 1234 (dueno) | numero 0002 PIN 2222 (encargado)"
            )
        )
