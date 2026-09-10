"""Tienda publica: comprar una entrada sin estar autenticado."""

from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.tenancy.models import Local, Tenant
from apps.ticketing.models import Entrada, Reserva
from apps.ticketing.services import agregar_tipo, crear_evento, publicar_evento


class BaseTiendaTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Online", slug="online")
        cls.tenant = cls.datos["tenant"]

    def setUp(self):
        with tenant_context(self.tenant):
            local = Local.objects.get()
            self.evento = crear_evento(
                local=local, nombre="Fiesta Grande", fecha=date(2026, 11, 7), aforo=80
            )
            self.general = agregar_tipo(
                evento=self.evento, nombre="General", precio="700", cupo=5
            )
            publicar_evento(self.evento)

    def url_tienda(self):
        return reverse("web:tienda", args=[self.tenant.slug])

    def url_evento(self):
        return reverse("web:tienda_evento", args=[self.tenant.slug, self.evento.id])

    def url_reservar(self):
        return reverse("web:reservar", args=[self.tenant.slug, self.evento.id])


class CarteleraTests(BaseTiendaTests):
    def test_la_cartelera_es_publica(self):
        respuesta = self.client.get(self.url_tienda())
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "Fiesta Grande")

    def test_un_boliche_inexistente_da_404(self):
        respuesta = self.client.get(reverse("web:tienda", args=["no-existe"]))
        self.assertEqual(respuesta.status_code, 404)

    def test_un_evento_en_borrador_no_se_muestra(self):
        with tenant_context(self.tenant):
            borrador = crear_evento(
                local=Local.objects.get(), nombre="Sin publicar",
                fecha=date(2026, 12, 1), aforo=50,
            )
        respuesta = self.client.get(self.url_tienda())
        self.assertNotContains(respuesta, "Sin publicar")

    def test_el_detalle_muestra_precio_y_disponibles(self):
        respuesta = self.client.get(self.url_evento())
        self.assertContains(respuesta, "General")
        self.assertContains(respuesta, "quedan 5")

    def test_la_tienda_de_un_boliche_no_muestra_el_evento_de_otro(self):
        otro = Tenant.objects.create(nombre="Otro Boliche", slug="otro-boliche")
        respuesta = self.client.get(reverse("web:tienda", args=[otro.slug]))
        self.assertNotContains(respuesta, "Fiesta Grande")


class ReservaOnlineTests(BaseTiendaTests):
    def reservar(self, cantidad="1", nombre="Ana"):
        return self.client.post(
            self.url_reservar(),
            {"tipo": str(self.general.id), "cantidad": cantidad, "nombre": nombre,
             "contacto": "099123456"},
        )

    def test_reservar_toma_el_cupo_y_lleva_a_pagar(self):
        respuesta = self.reservar()
        with tenant_context(self.tenant):
            reserva = Reserva.objects.get()
        self.assertRedirects(
            respuesta, reverse("web:reserva_pago", args=[reserva.token])
        )
        self.assertEqual(reserva.estado, Reserva.Estado.ACTIVA)
        self.assertEqual(reserva.cantidad, 1)
        self.assertTrue(reserva.referencia_pago.startswith("SIM-"))

    def test_la_pantalla_de_pago_muestra_el_reloj(self):
        self.reservar()
        with tenant_context(self.tenant):
            reserva = Reserva.objects.get()
        respuesta = self.client.get(
            reverse("web:reserva_pago", args=[reserva.token])
        )
        self.assertContains(respuesta, "guardado por")
        self.assertContains(respuesta, "Mercado Pago todavia no esta integrado")

    def test_no_se_puede_reservar_mas_que_el_cupo(self):
        respuesta = self.reservar(cantidad="6", nombre="Ana")
        self.assertRedirects(respuesta, self.url_evento())
        with tenant_context(self.tenant):
            self.assertEqual(Reserva.objects.count(), 0)

    def test_confirmar_emite_las_entradas_y_muestra_el_qr(self):
        self.reservar(cantidad="2")
        with tenant_context(self.tenant):
            reserva = Reserva.objects.get()
        respuesta = self.client.post(
            reverse("web:reserva_confirmar", args=[reserva.token]),
            {"referencia": reserva.referencia_pago, "estado": "aprobado"},
        )
        with tenant_context(self.tenant):
            entradas = list(Entrada.objects.all())
        self.assertEqual(len(entradas), 2)
        # Entrada ordena por -creado_en, asi que no se asume cual es "la primera":
        # se verifica que el destino sea una de las emitidas.
        destino = respuesta["Location"]
        self.assertTrue(
            any(e.qr_token in destino for e in entradas),
            f"El destino {destino} no es ninguna de las entradas emitidas.",
        )

    def test_la_entrada_publica_muestra_el_qr(self):
        self.reservar()
        with tenant_context(self.tenant):
            reserva = Reserva.objects.get()
        self.client.post(
            reverse("web:reserva_confirmar", args=[reserva.token]), {"estado": "aprobado"}
        )
        with tenant_context(self.tenant):
            entrada = Entrada.objects.get()
        respuesta = self.client.get(
            reverse("web:entrada_publica", args=[entrada.qr_token])
        )
        self.assertContains(respuesta, "<svg")
        self.assertContains(respuesta, "Se usa una sola vez")

    def test_la_entrada_publica_marca_las_usadas(self):
        self.reservar()
        with tenant_context(self.tenant):
            reserva = Reserva.objects.get()
        self.client.post(
            reverse("web:reserva_confirmar", args=[reserva.token]), {"estado": "aprobado"}
        )
        with tenant_context(self.tenant):
            entrada = Entrada.objects.get()
            entrada.estado = Entrada.Estado.USADA
            entrada.save(update_fields=["estado"])
        respuesta = self.client.get(
            reverse("web:entrada_publica", args=[entrada.qr_token])
        )
        self.assertContains(respuesta, "ya fue usada")

    def test_un_token_desconocido_da_404(self):
        respuesta = self.client.get(
            reverse("web:entrada_publica", args=["token-que-no-existe"])
        )
        self.assertEqual(respuesta.status_code, 404)


class PagoTardioEnLaTiendaTests(BaseTiendaTests):
    def test_el_pago_tardio_muestra_la_pantalla_de_devolucion(self):
        momento = timezone.now()
        with tenant_context(self.tenant):
            from apps.ticketing.reservas import crear_reserva

            reserva = crear_reserva(tipo=self.general, cantidad=2, momento=momento)
            reserva.expira_en = momento - timedelta(minutes=1)
            reserva.save(update_fields=["expira_en"])

        respuesta = self.client.post(
            reverse("web:reserva_confirmar", args=[reserva.token]),
            {"estado": "aprobado"},
        )
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "vencio")
        self.assertContains(respuesta, "devolver")
        with tenant_context(self.tenant):
            self.assertEqual(Entrada.objects.count(), 0)


class WebhookTests(BaseTiendaTests):
    def test_el_webhook_confirma_la_reserva(self):
        with tenant_context(self.tenant):
            from apps.ticketing.reservas import crear_reserva

            reserva = crear_reserva(
                tipo=self.general, cantidad=1, comprador_nombre="Ana"
            )
            referencia = f"SIM-{reserva.token[:16]}"

        respuesta = self.client.post(
            reverse("webhook_de_pago"), {"referencia": referencia, "estado": "aprobado"}
        )
        datos = respuesta.json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["estado"], "confirmada")
        self.assertEqual(len(datos["entradas"]), 1)

    def test_el_webhook_es_idempotente(self):
        with tenant_context(self.tenant):
            from apps.ticketing.reservas import crear_reserva

            reserva = crear_reserva(tipo=self.general, cantidad=2)
            referencia = f"SIM-{reserva.token[:16]}"

        for _ in range(3):
            self.client.post(
                reverse("webhook_de_pago"),
                {"referencia": referencia, "estado": "aprobado"},
            )
        with tenant_context(self.tenant):
            self.assertEqual(Entrada.objects.count(), 2)

    def test_un_webhook_rechazado_se_ignora(self):
        with tenant_context(self.tenant):
            from apps.ticketing.reservas import crear_reserva

            reserva = crear_reserva(tipo=self.general, cantidad=1)
            referencia = f"SIM-{reserva.token[:16]}"

        datos = self.client.post(
            reverse("webhook_de_pago"),
            {"referencia": referencia, "estado": "rechazado"},
        ).json()
        self.assertEqual(datos["estado"], "ignorado")
        with tenant_context(self.tenant):
            self.assertEqual(Entrada.objects.count(), 0)

    def test_un_webhook_sin_referencia_falla(self):
        respuesta = self.client.post(reverse("webhook_de_pago"), {"estado": "aprobado"})
        self.assertEqual(respuesta.status_code, 400)

    def test_un_webhook_de_una_reserva_desconocida_da_404(self):
        respuesta = self.client.post(
            reverse("webhook_de_pago"),
            {"referencia": "SIM-inexistente", "estado": "aprobado"},
        )
        self.assertEqual(respuesta.status_code, 404)
