from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import MembresiaUsuario, Permiso, Rol, Usuario
from apps.core.context import tenant_context
from apps.tenancy.models import Local, Tenant


class PinTests(TestCase):
    def setUp(self):
        self.boliche = Tenant.objects.create(nombre="Boliche A", slug="boliche-a")
        self.cantinero = Usuario.objects.create_user(
            email=None, nombre="Ana", tenant=self.boliche, numero="0007"
        )
        self.cantinero.set_pin("4821")
        self.cantinero.save()

    def test_el_pin_no_se_guarda_en_claro(self):
        self.assertNotIn("4821", self.cantinero.pin_hash)
        self.assertTrue(self.cantinero.pin_hash.startswith("pbkdf2_"))

    def test_verifica_el_pin_correcto(self):
        self.assertTrue(self.cantinero.check_pin("4821"))

    def test_rechaza_el_pin_incorrecto(self):
        self.assertFalse(self.cantinero.check_pin("0000"))

    def test_sin_pin_cargado_nunca_valida(self):
        otro = Usuario.objects.create_user(email=None, nombre="Beto", tenant=self.boliche)
        self.assertFalse(otro.check_pin(""))

    @override_settings(PIN_MAX_INTENTOS=3, PIN_BLOQUEO_MINUTOS=15)
    def test_bloquea_tras_los_intentos_maximos(self):
        for _ in range(2):
            self.cantinero.registrar_pin_fallido()
            self.assertFalse(self.cantinero.esta_bloqueado())

        self.cantinero.registrar_pin_fallido()
        self.cantinero.refresh_from_db()
        self.assertTrue(self.cantinero.esta_bloqueado())
        self.assertGreater(self.cantinero.pin_bloqueado_hasta, timezone.now())

    @override_settings(PIN_MAX_INTENTOS=3, PIN_BLOQUEO_MINUTOS=15)
    def test_el_bloqueo_expira(self):
        for _ in range(3):
            self.cantinero.registrar_pin_fallido()
        self.cantinero.refresh_from_db()
        self.cantinero.pin_bloqueado_hasta = timezone.now() - timedelta(minutes=1)
        self.cantinero.save(update_fields=["pin_bloqueado_hasta"])
        self.assertFalse(self.cantinero.esta_bloqueado())

    def test_ingreso_correcto_limpia_los_intentos(self):
        self.cantinero.registrar_pin_fallido()
        self.cantinero.registrar_pin_correcto()
        self.cantinero.refresh_from_db()
        self.assertEqual(self.cantinero.pin_intentos, 0)
        self.assertIsNone(self.cantinero.pin_bloqueado_hasta)

    def test_numero_unico_por_tenant(self):
        from django.db.utils import IntegrityError

        with self.assertRaises(IntegrityError):
            Usuario.objects.create_user(
                email=None, nombre="Otra", tenant=self.boliche, numero="0007"
            )

    def test_el_mismo_numero_en_otro_boliche_esta_permitido(self):
        otro = Tenant.objects.create(nombre="Boliche B", slug="boliche-b")
        Usuario.objects.create_user(
            email=None, nombre="Luis", tenant=otro, numero="0007"
        )


class PermisosTests(TestCase):
    def setUp(self):
        self.boliche = Tenant.objects.create(nombre="Boliche A", slug="boliche-a")
        with tenant_context(self.boliche):
            self.local = Local.objects.create(nombre="Barra A")
            self.rol = Rol.objects.create(codigo="cantinero", nombre="Cantinero")

        self.p_venta = Permiso.objects.create(codigo="ventas.vender", nombre="Vender")
        self.p_anular = Permiso.objects.create(
            codigo="ventas.anular", nombre="Anular venta"
        )
        self.rol.permisos.add(self.p_venta)

        self.ana = Usuario.objects.create_user(
            email=None, nombre="Ana", tenant=self.boliche, numero="0001"
        )
        with tenant_context(self.boliche):
            MembresiaUsuario.objects.create(
                usuario=self.ana, local=self.local, rol=self.rol
            )

    def test_permisos_efectivos_salen_del_rol(self):
        self.assertTrue(self.ana.tiene_permiso("ventas.vender"))
        self.assertFalse(self.ana.tiene_permiso("ventas.anular"))

    def test_superusuario_tiene_todos_los_permisos(self):
        jefe = Usuario.objects.create_superuser(email="jefe@ejemplo.com", password="x")
        self.assertTrue(jefe.tiene_permiso("ventas.anular"))

    def test_usuario_sin_membresia_no_tiene_permisos(self):
        nadie = Usuario.objects.create_user(
            email=None, nombre="Nadie", tenant=self.boliche, numero="0002"
        )
        self.assertFalse(nadie.tiene_permiso("ventas.vender"))
