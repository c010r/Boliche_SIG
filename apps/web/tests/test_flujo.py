"""La interfaz: que un boliche pueda operar sin tocar la base de datos."""

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Usuario
from apps.catalog.models import Insumo, Producto
from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.sales.models import Pago, SesionCaja, Venta
from apps.stock.models import Deposito, StockMovimiento
from apps.stock.services import registrar_movimiento, saldo_calculado
from apps.tenancy.models import Local, Terminal


class BaseWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Web", slug="web")
        cls.tenant = cls.datos["tenant"]
        cls.admin = cls.datos["admin"]

    def ingresar(self, numero="0001", pin="1234"):
        return self.client.post(
            reverse("web:ingresar"), {"numero": numero, "pin": pin}
        )

    def con_stock(self, cantidad="5000"):
        with tenant_context(self.tenant):
            barra = Deposito.objects.get(nombre="Barra 1")
            ron = Insumo.objects.get(nombre="Ron")
            registrar_movimiento(
                deposito=barra, insumo=ron,
                tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal(cantidad),
            )


class IngresoTests(BaseWebTests):
    def test_la_pantalla_de_ingreso_responde(self):
        respuesta = self.client.get(reverse("web:ingresar"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Numero de operador")

    def test_ingresa_con_numero_y_pin(self):
        respuesta = self.ingresar()
        self.assertRedirects(respuesta, reverse("web:inicio"))

    def test_rechaza_el_pin_incorrecto(self):
        respuesta = self.ingresar(pin="9999")
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "incorrecto")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_rechaza_el_numero_inexistente(self):
        respuesta = self.ingresar(numero="4444")
        self.assertContains(respuesta, "incorrecto")

    def test_el_intento_fallido_se_cuenta(self):
        self.ingresar(pin="9999")
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.pin_intentos, 1)

    def test_un_operador_bloqueado_no_entra(self):
        usuario = self.admin
        usuario.pin_bloqueado_hasta = usuario.pin_bloqueado_hasta or None
        from django.utils import timezone
        from datetime import timedelta
        usuario.pin_bloqueado_hasta = timezone.now() + timedelta(minutes=10)
        usuario.save(update_fields=["pin_bloqueado_hasta"])
        respuesta = self.ingresar()
        self.assertContains(respuesta, "bloqueado")

    def test_sin_sesion_la_barra_manda_a_ingresar(self):
        respuesta = self.client.get(reverse("web:barra"))
        self.assertRedirects(respuesta, reverse("web:ingresar"))


class FlujoDeUnaNocheTests(BaseWebTests):
    def setUp(self):
        self.con_stock()
        self.ingresar()

    def abrir_caja(self):
        with tenant_context(self.tenant):
            terminal = Terminal.objects.get(nombre="Barra 1")
        return self.client.post(
            reverse("web:caja"),
            {"accion": "abrir", "terminal": str(terminal.id),
             "fondo_inicial": "3000", "turno": "Viernes"},
        )

    def test_abrir_caja_lleva_a_la_barra(self):
        respuesta = self.abrir_caja()
        self.assertRedirects(respuesta, reverse("web:barra"))
        with tenant_context(self.tenant):
            self.assertEqual(SesionCaja.objects.filter(estado="abierta").count(), 1)

    def test_la_barra_muestra_los_productos(self):
        self.abrir_caja()
        respuesta = self.client.get(reverse("web:barra"))
        self.assertContains(respuesta, "Cuba libre")
        self.assertContains(respuesta, "Chopp")

    def test_vender_descuenta_stock_y_cobra(self):
        self.abrir_caja()
        with tenant_context(self.tenant):
            cuba = Producto.objects.get(nombre="Cuba libre")
            barra = Deposito.objects.get(nombre="Barra 1")
            ron = Insumo.objects.get(nombre="Ron")

        respuesta = self.client.post(
            reverse("web:barra"),
            {"producto": str(cuba.id), "medio": Pago.Medio.EFECTIVO, "cantidad": "2"},
        )
        self.assertRedirects(respuesta, reverse("web:barra"))

        with tenant_context(self.tenant):
            venta = Venta.objects.get()
            self.assertEqual(venta.estado, Venta.Estado.PAGADA)
            self.assertEqual(venta.total, Decimal("700.00"))
            self.assertEqual(saldo_calculado(barra, ron), Decimal("4900.0000"))

    def test_el_pago_por_qr_queda_pendiente(self):
        self.abrir_caja()
        with tenant_context(self.tenant):
            cuba = Producto.objects.get(nombre="Cuba libre")
        self.client.post(
            reverse("web:barra"),
            {"producto": str(cuba.id), "medio": Pago.Medio.QR, "cantidad": "1"},
        )
        with tenant_context(self.tenant):
            venta = Venta.objects.get()
            self.assertEqual(venta.estado, Venta.Estado.PENDIENTE)

    def test_cerrar_caja_sin_diferencia(self):
        self.abrir_caja()
        with tenant_context(self.tenant):
            cuba = Producto.objects.get(nombre="Cuba libre")
        self.client.post(
            reverse("web:barra"),
            {"producto": str(cuba.id), "medio": Pago.Medio.EFECTIVO, "cantidad": "1"},
        )
        respuesta = self.client.post(
            reverse("web:caja"),
            {"accion": "cerrar", "declarado_efectivo": "3350",
             "declarado_qr": "0", "declarado_tarjeta": "0", "declarado_transferencia": "0"},
        )
        self.assertRedirects(respuesta, reverse("web:panel"))
        with tenant_context(self.tenant):
            self.assertEqual(SesionCaja.objects.filter(estado="abierta").count(), 0)

    def test_cerrar_con_diferencia_sin_autorizante_falla(self):
        self.abrir_caja()
        respuesta = self.client.post(
            reverse("web:caja"),
            {"accion": "cerrar", "declarado_efectivo": "1000",
             "declarado_qr": "0", "declarado_tarjeta": "0", "declarado_transferencia": "0"},
        )
        self.assertRedirects(respuesta, reverse("web:caja"))
        with tenant_context(self.tenant):
            self.assertEqual(SesionCaja.objects.filter(estado="abierta").count(), 1)

    def test_el_panel_muestra_la_varianza(self):
        self.abrir_caja()
        respuesta = self.client.get(reverse("web:panel"))
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Varianza")
        # Sin conteos cerrados, el sistema dice que no hay, no inventa un cero.
        self.assertContains(respuesta, "no hay varianza")

    def test_el_panel_marca_los_puntos_sin_conteo(self):
        self.abrir_caja()
        respuesta = self.client.get(reverse("web:panel"))
        self.assertContains(respuesta, "No medido no es cero")


class PermisosDeInterfazTests(BaseWebTests):
    def test_un_cantinero_no_ve_el_panel(self):
        with tenant_context(self.tenant):
            from apps.accounts.models import MembresiaUsuario, Rol
            local = Local.objects.get()
            cantinero = Usuario.objects.create_user(
                email=None, nombre="Carla", tenant=self.tenant, numero="0009"
            )
            cantinero.set_pin("9999")
            cantinero.save()
            MembresiaUsuario.objects.create(
                usuario=cantinero, local=local,
                rol=Rol.objects.get(codigo="cantinero"),
            )
        self.ingresar(numero="0009", pin="9999")
        respuesta = self.client.get(reverse("web:panel"), follow=True)
        self.assertContains(respuesta, "No tenes permiso")

    def test_un_cantinero_no_puede_anular(self):
        with tenant_context(self.tenant):
            from apps.accounts.models import MembresiaUsuario, Rol
            local = Local.objects.get()
            cantinero = Usuario.objects.create_user(
                email=None, nombre="Carla", tenant=self.tenant, numero="0009"
            )
            cantinero.set_pin("9999")
            cantinero.save()
            MembresiaUsuario.objects.create(
                usuario=cantinero, local=local,
                rol=Rol.objects.get(codigo="cantinero"),
            )
        self.ingresar(numero="0009", pin="9999")
        respuesta = self.client.post(
            reverse("web:anular", args=["00000000-0000-0000-0000-000000000000"]),
            follow=True,
        )
        self.assertContains(respuesta, "No tenes permiso")
