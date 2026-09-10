"""Export de un boliche: backup aislado, derecho de acceso y portabilidad."""

import json

from django.core import serializers
from django.core.management import call_command
from django.test import TestCase

from apps.accounts.models import Usuario
from apps.core.context import tenant_context
from apps.core.management.commands.exportar_boliche import exportar
from apps.core.provisioning import provisionar_boliche
from apps.tenancy.models import Local, Tenant
from apps.ticketing.models import Lista
from apps.ticketing.services import agregar_tipo, crear_evento, publicar_evento


class BaseExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Export", slug="export")
        cls.tenant = cls.datos["tenant"]
        with tenant_context(cls.tenant):
            local = Local.objects.get()
            evento = crear_evento(
                local=local, nombre="Fiesta", fecha="2026-12-05", aforo=100
            )
            agregar_tipo(evento=evento, nombre="General", precio="500", cupo=50)
            publicar_evento(evento)
            Lista.objects.create(
                tenant=cls.tenant, evento=evento, nombre="Casa", cupo=10
            )

    def test_el_export_trae_los_datos_del_boliche(self):
        datos = exportar(self.tenant)
        self.assertEqual(datos["formato"], "boliche-sig/export/1")
        self.assertEqual(datos["boliche"]["slug"], "export")
        self.assertGreater(datos["total_de_objetos"], 0)
        self.assertIn("ticketing.Evento", datos["modelos"])
        self.assertIn("catalog.Producto", datos["modelos"])
        self.assertIn("stock.StockMovimiento", datos["modelos"])

    def test_el_formato_es_el_mismo_que_usa_loaddata(self):
        """Asi el export sirve tambien de respaldo, no solo de informe."""
        datos = exportar(self.tenant)
        for objeto in datos["objetos"][:50]:
            self.assertIn("model", objeto)
            self.assertIn("pk", objeto)
            self.assertIn("fields", objeto)

    def test_los_objetos_se_pueden_volver_a_leer(self):
        datos = exportar(self.tenant)
        crudo = json.dumps(datos["objetos"])
        recuperados = list(serializers.deserialize("json", crudo))
        self.assertEqual(len(recuperados), len(datos["objetos"]))

    def test_se_puede_dejar_afuera_la_fila_del_boliche(self):
        datos = exportar(self.tenant, incluir_tenant=False)
        self.assertNotIn("tenancy.Tenant", datos["modelos"])

    def test_el_manifiesto_cuenta_bien(self):
        datos = exportar(self.tenant)
        self.assertEqual(
            sum(datos["modelos"].values()), datos["total_de_objetos"]
        )


class AislamientoDelExportTests(TestCase):
    """Lo mas importante: el export de un boliche no puede traer datos de otro."""

    def test_no_se_filtran_datos_de_otro_boliche(self):
        a = provisionar_boliche(nombre="Boliche A", slug="exp-a")["tenant"]
        b = provisionar_boliche(nombre="Boliche B", slug="exp-b")["tenant"]

        with tenant_context(a):
            Lista.objects.create(
                tenant=a,
                evento=crear_evento(
                    local=Local.objects.first(), nombre="Solo de A",
                    fecha="2026-12-05", aforo=50,
                ),
                nombre="Lista de A", cupo=5,
            )
        with tenant_context(b):
            Lista.objects.create(
                tenant=b,
                evento=crear_evento(
                    local=Local.objects.first(), nombre="Solo de B",
                    fecha="2026-12-06", aforo=50,
                ),
                nombre="Lista de B", cupo=5,
            )

        crudo = json.dumps(exportar(a))
        self.assertIn("Lista de A", crudo)
        self.assertNotIn("Lista de B", crudo)
        self.assertNotIn("Solo de B", crudo)
        self.assertIn("Solo de A", crudo)

    def test_el_export_no_incluye_usuarios_de_otro_boliche(self):
        a = provisionar_boliche(nombre="Boliche A", slug="exp-c")["tenant"]
        b = provisionar_boliche(nombre="Boliche B", slug="exp-d")["tenant"]

        with tenant_context(b):
            Usuario.objects.create_user(
                email=None, nombre="Ajeno", tenant=b, numero="0099"
            )

        datos = exportar(a)
        usuarios = [
            o for o in datos["objetos"] if o["model"] == "accounts.usuario"
        ]
        for objeto in usuarios:
            self.assertEqual(objeto["fields"]["tenant"], str(a.id))
        self.assertNotIn("Ajeno", json.dumps(datos))


class ComandoExportTests(TestCase):
    def test_el_comando_avisa_si_el_boliche_no_existe(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("exportar_boliche", slug="no-existe")

    def test_el_comando_escribe_el_archivo(self):
        import tempfile
        from pathlib import Path

        provisionar_boliche(nombre="Boliche Archivo", slug="exp-archivo")
        with tempfile.TemporaryDirectory() as carpeta:
            destino = Path(carpeta) / "sub" / "export.json"
            call_command(
                "exportar_boliche", slug="exp-archivo", salida=str(destino)
            )
            self.assertTrue(destino.exists())
            datos = json.loads(destino.read_text(encoding="utf-8"))
            self.assertEqual(datos["boliche"]["slug"], "exp-archivo")
