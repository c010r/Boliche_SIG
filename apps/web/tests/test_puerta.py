"""La puerta: vender entradas y validarlas desde el celular."""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import Usuario
from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.tenancy.models import Local, Terminal
from apps.ticketing.models import Entrada, MovimientoAforo
from apps.ticketing.services import agregar_tipo, crear_evento, publicar_evento


class BasePuertaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Puerta", slug="puerta")
        cls.tenant = cls.datos["tenant"]
        cls.admin = cls.datos["admin"]

    def setUp(self):
        with tenant_context(self.tenant):
            local = Local.objects.get()
            self.evento = crear_evento(
                local=local, nombre="Viernes", fecha=date(2026, 9, 12), aforo=100
            )
            self.general = agregar_tipo(
                evento=self.evento, nombre="General", precio="500", cupo=50
            )
            agregar_tipo(evento=self.evento, nombre="VIP", precio="1200", cupo=5)
            publicar_evento(self.evento)

    def ingresar(self):
        self.client.post(reverse("web:ingresar"), {"numero": "0001", "pin": "1234"})

    def vender(self, cantidad=1, tipo=None):
        self.ingresar()
        return self.client.post(
            reverse("web:vender_entradas", args=[self.evento.id]),
            {"tipo": str((tipo or self.general).id), "cantidad": str(cantidad)},
        )


class PantallasTests(BasePuertaTests):
    def test_la_lista_de_eventos_muestra_el_evento(self):
        self.ingresar()
        respuesta = self.client.get(reverse("web:eventos"))
        self.assertContains(respuesta, "Viernes")

    def test_el_detalle_muestra_ocupacion_y_precios(self):
        self.ingresar()
        respuesta = self.client.get(reverse("web:evento", args=[self.evento.id]))
        self.assertContains(respuesta, "General")
        self.assertContains(respuesta, "Adentro ahora")

    def test_la_puerta_carga_con_el_evento_publicado(self):
        self.ingresar()
        respuesta = self.client.get(reverse("web:puerta"))
        self.assertContains(respuesta, "Listo para escanear")
        self.assertContains(respuesta, "Ingreso manual")

    def test_la_puerta_avisa_que_sin_https_no_hay_camara(self):
        self.ingresar()
        respuesta = self.client.get(reverse("web:puerta"))
        self.assertContains(respuesta, "HTTPS")


class VentaDeEntradasTests(BasePuertaTests):
    def test_vender_emite_y_muestra_el_qr(self):
        respuesta = self.vender()
        with tenant_context(self.tenant):
            entrada = Entrada.objects.get()
        self.assertRedirects(
            respuesta, reverse("web:entrada_qr", args=[entrada.id])
        )
        qr = self.client.get(reverse("web:entrada_qr", args=[entrada.id]))
        self.assertContains(qr, "<svg")
        self.assertContains(qr, entrada.folio)

    def test_vender_varias_lleva_al_evento(self):
        respuesta = self.vender(cantidad=3)
        self.assertRedirects(respuesta, reverse("web:evento", args=[self.evento.id]))
        with tenant_context(self.tenant):
            self.assertEqual(Entrada.objects.count(), 3)

    def test_no_se_puede_vender_mas_que_el_cupo(self):
        self.vender()
        respuesta = self.client.post(
            reverse("web:vender_entradas", args=[self.evento.id]),
            {"tipo": str(self.general.id), "cantidad": "999"},
            follow=True,
        )
        self.assertContains(respuesta, "quedan")
        with tenant_context(self.tenant):
            self.assertEqual(Entrada.objects.count(), 1)

    def test_vender_sin_caja_avisa_que_no_entra_al_arqueo(self):
        respuesta = self.vender()
        self.client.get(respuesta["Location"])
        respuesta = self.client.get(reverse("web:evento", args=[self.evento.id]))
        self.assertContains(respuesta, "no entra al arqueo")

    def test_un_cantinero_no_puede_vender_entradas(self):
        from apps.accounts.models import MembresiaUsuario, Rol

        with tenant_context(self.tenant):
            local = Local.objects.get()
            cantinero = Usuario.objects.create_user(
                email=None, nombre="Carla", tenant=self.tenant, numero="0009"
            )
            cantinero.set_pin("9999")
            cantinero.save()
            MembresiaUsuario.objects.create(
                usuario=cantinero, local=local, rol=Rol.objects.get(codigo="cantinero")
            )
        self.client.post(reverse("web:ingresar"), {"numero": "0009", "pin": "9999"})
        respuesta = self.client.post(
            reverse("web:vender_entradas", args=[self.evento.id]),
            {"tipo": str(self.general.id), "cantidad": "1"},
            follow=True,
        )
        self.assertContains(respuesta, "No tenes permiso")


class ValidacionTests(BasePuertaTests):
    def validar(self, texto, manual=None, motivo=None, cantidad=None):
        datos = {"evento": str(self.evento.id), "texto": texto or ""}
        if manual:
            datos["manual"] = manual
        if motivo:
            datos["motivo"] = motivo
        if cantidad:
            datos["cantidad"] = cantidad
        return self.client.post(reverse("web:validar"), datos)

    def test_valida_una_entrada_y_devuelve_json(self):
        self.vender()
        self.ingresar()
        with tenant_context(self.tenant):
            entrada = Entrada.objects.get()
        respuesta = self.validar(entrada.qr_token)
        datos = respuesta.json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["aforo_en_vivo"], 1)
        self.assertEqual(datos["folio"], entrada.folio)

    def test_el_segundo_escaneo_rechaza(self):
        self.vender()
        self.ingresar()
        with tenant_context(self.tenant):
            entrada = Entrada.objects.get()
        self.assertTrue(self.validar(entrada.qr_token).json()["ok"])
        segundo = self.validar(entrada.qr_token).json()
        self.assertFalse(segundo["ok"])
        self.assertEqual(segundo["motivo"], "ya_usada")
        with tenant_context(self.tenant):
            self.assertEqual(MovimientoAforo.objects.count(), 1)

    def test_un_codigo_desconocido_se_rechaza(self):
        self.ingresar()
        datos = self.validar("no-existe").json()
        self.assertFalse(datos["ok"])
        self.assertEqual(datos["motivo"], "no_existe")

    def test_el_ingreso_manual_exige_motivo(self):
        self.ingresar()
        respuesta = self.validar("", manual="1", cantidad="2")
        self.assertEqual(respuesta.status_code, 400)
        self.assertIn("motivo", respuesta.json()["mensaje"])

    def test_el_ingreso_manual_con_motivo_registra_aforo(self):
        self.ingresar()
        datos = self.validar("", manual="1", motivo="Lista del DJ", cantidad="3").json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["aforo_en_vivo"], 3)

    def test_el_aforo_completo_se_avisa_en_la_respuesta(self):
        self.ingresar()
        self.validar("", manual="1", motivo="Llenar", cantidad="100")
        self.vender()
        with tenant_context(self.tenant):
            entrada = Entrada.objects.get()
        datos = self.validar(entrada.qr_token).json()
        self.assertTrue(datos["ok"])
        self.assertTrue(datos["aforo_completo"])

    def test_un_cantinero_no_puede_validar(self):
        from apps.accounts.models import MembresiaUsuario, Rol

        with tenant_context(self.tenant):
            local = Local.objects.get()
            cantinero = Usuario.objects.create_user(
                email=None, nombre="Carla", tenant=self.tenant, numero="0009"
            )
            cantinero.set_pin("9999")
            cantinero.save()
            MembresiaUsuario.objects.create(
                usuario=cantinero, local=local, rol=Rol.objects.get(codigo="cantinero")
            )
        self.client.post(reverse("web:ingresar"), {"numero": "0009", "pin": "9999"})
        respuesta = self.validar("cualquiera")
        self.assertEqual(respuesta.status_code, 403)
