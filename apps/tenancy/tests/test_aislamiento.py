"""Puerta de salida de la Etapa 0: aislamiento entre dos boliches.

Si estos tests no estan en verde, no se sigue construyendo (seccion 15 bis).
"""

from django.db.models.deletion import ProtectedError
from django.test import TestCase, override_settings

from apps.core.context import TenantNotSetError, tenant_context
from apps.core.managers import TenantMismatchError
from apps.tenancy.models import Local, Tenant, Terminal


class AislamientoEntreTenantsTests(TestCase):
    def setUp(self):
        self.boliche_a = Tenant.objects.create(nombre="Boliche A", slug="boliche-a")
        self.boliche_b = Tenant.objects.create(nombre="Boliche B", slug="boliche-b")

        with tenant_context(self.boliche_a):
            self.local_a = Local.objects.create(nombre="Barra A")
            self.terminal_a = Terminal.objects.create(
                local=self.local_a, nombre="Barra 1", tipo=Terminal.Tipo.BARRA
            )
        with tenant_context(self.boliche_b):
            self.local_b = Local.objects.create(nombre="Barra B")

    # --- fail-closed -----------------------------------------------------

    def test_sin_tenant_en_contexto_revienta(self):
        with self.assertRaises(TenantNotSetError):
            list(Local.objects.all())

        with self.assertRaises(TenantNotSetError):
            Local.objects.count()

    def test_sin_tenant_tampoco_se_puede_crear(self):
        with self.assertRaises(TenantNotSetError):
            Local.objects.create(nombre="Sin contexto")

    # --- aislamiento -----------------------------------------------------

    def test_cada_tenant_ve_solo_lo_suyo(self):
        with tenant_context(self.boliche_a):
            self.assertEqual(list(Local.objects.values_list("id", flat=True)), [self.local_a.id])
            self.assertEqual(Local.objects.count(), 1)

        with tenant_context(self.boliche_b):
            self.assertEqual(list(Local.objects.values_list("id", flat=True)), [self.local_b.id])

    def test_no_se_ve_el_detalle_de_otro_tenant(self):
        with tenant_context(self.boliche_a):
            with self.assertRaises(Local.DoesNotExist):
                Local.objects.get(id=self.local_b.id)

            self.assertEqual(
                Local.objects.filter(id=self.local_b.id).count(), 0
            )

    def test_terminales_tambien_estan_aisladas(self):
        with tenant_context(self.boliche_b):
            self.assertEqual(Terminal.objects.count(), 0)
        with tenant_context(self.boliche_a):
            self.assertEqual(Terminal.objects.count(), 1)

    # --- escape explicito -------------------------------------------------

    def test_unscoped_ve_todo(self):
        self.assertEqual(Local.unscoped.count(), 2)
        self.assertEqual(Tenant.objects.count(), 2)

    # --- no se puede escribir para otro tenant ----------------------------

    def test_crear_para_otro_tenant_revienta(self):
        with tenant_context(self.boliche_a):
            with self.assertRaises(TenantMismatchError):
                Local.objects.create(nombre="Intruso", tenant=self.boliche_b)

    def test_create_fuerza_el_tenant_del_contexto(self):
        with tenant_context(self.boliche_a):
            local = Local.objects.create(nombre="Barra 2")
        self.assertEqual(local.tenant_id, self.boliche_a.id)

    # --- proteccion de datos del tenant -----------------------------------

    def test_no_se_puede_borrar_un_tenant_con_datos(self):
        with self.assertRaises(ProtectedError):
            self.boliche_a.delete()

    def test_nombre_de_local_unico_por_tenant(self):
        with tenant_context(self.boliche_a):
            Local.objects.create(nombre="VIP")
        # El mismo nombre en otro boliche esta permitido.
        with tenant_context(self.boliche_b):
            Local.objects.create(nombre="VIP")


@override_settings(ALLOWED_HOSTS=["*"])
class MiddlewareTests(TestCase):
    def setUp(self):
        self.boliche = Tenant.objects.create(nombre="Boliche A", slug="boliche-a")
        with tenant_context(self.boliche):
            Local.objects.create(nombre="Barra A")

    def test_resuelve_el_tenant_por_subdominio(self):
        respuesta = self.client.get(
            "/api/health/", headers={"host": "boliche-a.ejemplo.com"}
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["tenant"], "boliche-a")

    def test_sin_subdominio_no_hay_tenant(self):
        respuesta = self.client.get("/api/health/")
        self.assertEqual(respuesta.status_code, 200)
        self.assertIsNone(respuesta.json()["tenant"])

    def test_tenant_suspendido_no_se_resuelve(self):
        self.boliche.estado = Tenant.Estado.SUSPENDIDO
        self.boliche.save(update_fields=["estado"])
        respuesta = self.client.get(
            "/api/health/", headers={"host": "boliche-a.ejemplo.com"}
        )
        self.assertIsNone(respuesta.json()["tenant"])

    def test_el_contexto_no_se_filtra_entre_requests(self):
        self.client.get("/api/health/", headers={"host": "boliche-a.ejemplo.com"})
        # Despues de la request, el contexto quedo limpio: consultar sin tenant
        # tiene que seguir reventando.
        with self.assertRaises(TenantNotSetError):
            list(Local.objects.all())

    def test_health_responde_ok(self):
        respuesta = self.client.get("/api/health/")
        self.assertEqual(respuesta.status_code, 200)
        self.assertEqual(respuesta.json()["base_de_datos"], "ok")
