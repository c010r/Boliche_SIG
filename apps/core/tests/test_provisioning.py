"""Alta de un boliche: tiene que ser un comando, no una tarde de carga a mano."""

from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import MembresiaUsuario, Permiso, Rol, Usuario
from apps.catalog.models import Insumo, Producto, Receta
from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche, sincronizar_unidades
from apps.sales.models import Pago, Venta
from apps.sales.services import abrir_caja, abrir_turno, registrar_pago, registrar_venta
from apps.stock.models import Deposito, StockMovimiento
from apps.stock.services import registrar_movimiento, saldo_calculado
from apps.tenancy.models import Local, Tenant, Terminal


class ProvisioningTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(
            nombre="Boliche Nuevo", slug="nuevo", rut="210000000000"
        )
        cls.tenant = cls.datos["tenant"]

    def test_crea_el_boliche_con_su_rut(self):
        self.assertEqual(self.tenant.nombre, "Boliche Nuevo")
        self.assertEqual(self.tenant.rut, "210000000000")

    def test_crea_local_con_aforo(self):
        with tenant_context(self.tenant):
            local = Local.objects.get()
        self.assertEqual(local.aforo, 300)

    def test_crea_los_puntos_de_stock(self):
        with tenant_context(self.tenant):
            nombres = set(Deposito.objects.values_list("nombre", flat=True))
        self.assertEqual(nombres, {"Deposito", "Barra 1", "Barra 2", "Puerta"})

    def test_cada_barra_tiene_su_terminal_con_punto_de_stock(self):
        with tenant_context(self.tenant):
            terminal = Terminal.objects.get(nombre="Barra 1")
            self.assertEqual(terminal.tipo, Terminal.Tipo.BARRA)
            self.assertIsNotNone(terminal.deposito)
            self.assertEqual(terminal.deposito.nombre, "Barra 1")

    def test_la_puerta_tiene_terminal_propia(self):
        with tenant_context(self.tenant):
            puerta = Terminal.objects.get(nombre="Puerta")
        self.assertEqual(puerta.tipo, Terminal.Tipo.PUERTA)

    def test_crea_los_roles_con_sus_permisos(self):
        roles = self.datos["roles"]
        self.assertIn("dueno", roles)
        self.assertIn("cantinero", roles)
        self.assertEqual(
            roles["cantinero"].permisos.count(), 5
        )
        self.assertLess(
            roles["cantinero"].permisos.count(), roles["dueno"].permisos.count()
        )

    def test_el_cantinero_no_puede_autorizar_ajustes(self):
        roles = self.datos["roles"]
        codigos = set(roles["cantinero"].permisos.values_list("codigo", flat=True))
        self.assertNotIn("stock.autorizar_ajuste", codigos)
        self.assertNotIn("ventas.anular", codigos)

    def test_el_encargado_si_puede_autorizar(self):
        roles = self.datos["roles"]
        codigos = set(roles["encargado"].permisos.values_list("codigo", flat=True))
        self.assertIn("stock.autorizar_ajuste", codigos)
        self.assertIn("caja.autorizar_diferencia", codigos)

    def test_el_dueno_queda_con_membresia_de_dueno(self):
        with tenant_context(self.tenant):
            membresia = MembresiaUsuario.objects.get()
        self.assertEqual(membresia.rol.codigo, "dueno")
        self.assertEqual(membresia.usuario.numero, "0001")

    def test_el_dueno_puede_ingresar_con_su_pin(self):
        admin = self.datos["admin"]
        self.assertTrue(admin.check_pin("1234"))

    def test_carga_la_plantilla_del_vertical(self):
        with tenant_context(self.tenant):
            self.assertGreaterEqual(Producto.objects.count(), 9)
            self.assertGreaterEqual(Insumo.objects.count(), 10)
            # Los tragos descuentan insumos: eso es lo que hace que el stock cuadre.
            cuba = Producto.objects.get(nombre="Cuba libre")
            self.assertTrue(Receta.objects.filter(producto=cuba, activa=True).exists())
            self.assertGreaterEqual(cuba.receta.items.count(), 3)

    def test_el_combo_agrupa_productos(self):
        with tenant_context(self.tenant):
            combo = Producto.objects.get(nombre="Chopp + Tequila")
            self.assertTrue(combo.es_combo)
            self.assertEqual(combo.combo_items.count(), 2)

    def test_es_idempotente(self):
        provisionar_boliche(nombre="Boliche Nuevo", slug="nuevo")
        self.assertEqual(Tenant.objects.filter(slug="nuevo").count(), 1)
        with tenant_context(self.tenant):
            self.assertEqual(Local.objects.count(), 1)
            self.assertEqual(Producto.objects.filter(nombre="Cuba libre").count(), 1)

    def test_sincroniza_las_unidades_globales(self):
        unidades = sincronizar_unidades()
        self.assertEqual(unidades["ml"].magnitud, "volumen")
        self.assertEqual(unidades["l"].factor_a_base, Decimal("1000"))

    def test_los_permisos_globales_se_crean_una_sola_vez(self):
        antes = Permiso.objects.count()
        provisionar_boliche(nombre="Otro", slug="otro")
        self.assertEqual(Permiso.objects.count(), antes)


class FlujoCompletoTests(TestCase):
    """Un boliche recien dado de alta ya puede vender y descontar stock."""

    def test_alta_y_primera_venta(self):
        datos = provisionar_boliche(nombre="Bar Nuevo", slug="bar-nuevo")
        tenant = datos["tenant"]

        with tenant_context(tenant):
            local = Local.objects.get()
            barra = Deposito.objects.get(nombre="Barra 1")
            terminal = Terminal.objects.get(nombre="Barra 1")
            admin = datos["admin"]

            ron = Insumo.objects.get(nombre="Ron")
            registrar_movimiento(
                deposito=barra, insumo=ron,
                tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("2000"),
            )

            turno = abrir_turno(local=local, nombre="Sabado")
            sesion = abrir_caja(
                turno=turno, terminal=terminal, usuario=admin,
                fondo_inicial=Decimal("1000"),
            )
            cuba = Producto.objects.get(nombre="Cuba libre")
            venta = registrar_venta(
                sesion=sesion, items=[{"producto": cuba, "cantidad": 2}], usuario=admin
            )
            registrar_pago(
                venta=venta, medio=Pago.Medio.EFECTIVO, monto=venta.total,
                estado=Pago.Estado.APROBADO,
            )

            venta.refresh_from_db()
            self.assertEqual(venta.estado, Venta.Estado.PAGADA)
            self.assertEqual(venta.total, Decimal("700.00"))
            # 2 cubas a 50 ml: 100 ml menos de ron.
            self.assertEqual(saldo_calculado(barra, ron), Decimal("1900.0000"))


class ComandoDemoTests(TestCase):
    def test_el_comando_demo_deja_datos_y_muestra_varianza(self):
        call_command("demo", slug="demo-test", verbosity=0)
        tenant = Tenant.objects.get(slug="demo-test")
        with tenant_context(tenant):
            self.assertGreater(Producto.objects.count(), 0)
            from apps.sales.models import Venta
            self.assertGreater(Venta.objects.count(), 0)
            self.assertGreater(StockMovimiento.objects.count(), 0)
