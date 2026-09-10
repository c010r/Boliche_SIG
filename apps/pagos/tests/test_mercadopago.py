"""Proveedor de Mercado Pago, probado sin red.

El transporte HTTP es inyectable, asi que se verifica el cuerpo y las cabeceras
que se mandan de verdad, y el mapeo de estados, sin tocar la API.

NO reemplaza una prueba contra el sandbox de Mercado Pago: eso queda pendiente y
esta dicho en el manual.
"""

import hashlib
import hmac
from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.pagos.models import CredencialDePago, Proveedor
from apps.pagos.proveedores import (
    ErrorDelProveedor,
    ProveedorMercadoPago,
    ProveedorSimulado,
    proveedor_actual,
    proveedor_para,
)
from apps.tenancy.models import Local, Tenant
from apps.ticketing.models import Reserva
from apps.ticketing.reservas import crear_reserva
from apps.ticketing.services import agregar_tipo, crear_evento, publicar_evento

SECRETO = "clave-de-firma-de-prueba"


class TransporteFalso:
    """Registra lo que se manda y devuelve lo que se le indique."""

    def __init__(self, respuestas=None):
        self.llamadas = []
        self.respuestas = list(respuestas or [])

    def __call__(self, metodo, url, cuerpo, cabeceras):
        self.llamadas.append(
            {"metodo": metodo, "url": url, "cuerpo": cuerpo, "cabeceras": cabeceras}
        )
        if self.respuestas:
            return self.respuestas.pop(0)
        return {"id": "ORD-1", "status": "created"}


class BasePagosTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Pagos", slug="pagos-test")
        cls.tenant = cls.datos["tenant"]

    def setUp(self):
        self.ctx = tenant_context(self.tenant)
        self.ctx.__enter__()
        self.local = Local.objects.get()
        self.evento = crear_evento(
            local=self.local, nombre="Fiesta", fecha=date(2026, 11, 7), aforo=100
        )
        self.tipo = agregar_tipo(
            evento=self.evento, nombre="General", precio="700", cupo=50
        )
        publicar_evento(self.evento)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def credencial(self, **extra):
        datos = {
            "tenant": self.tenant,
            "proveedor": Proveedor.MERCADO_PAGO,
            "access_token": "APP_USR-token-de-prueba",
            "external_pos_id": "CAJA001",
            "clave_de_firma": SECRETO,
            "activo": True,
        }
        datos.update(extra)
        return CredencialDePago.objects.create(**datos)

    def reserva(self, cantidad=2):
        return crear_reserva(tipo=self.tipo, cantidad=cantidad, comprador_nombre="Ana")

    def firma(self, data_id, request_id, ts):
        plantilla = f"id:{data_id};request-id:{request_id};ts:{ts};"
        v1 = hmac.new(
            SECRETO.encode(), plantilla.encode(), hashlib.sha256
        ).hexdigest()
        return f"ts={ts},v1={v1}"


class CreacionDeOrdenTests(BasePagosTests):
    def test_arma_la_orden_con_el_contrato_de_mercado_pago(self):
        transporte = TransporteFalso([{"id": "ORD-99", "status": "created"}])
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(), transporte=transporte
        )
        reserva = self.reserva(cantidad=2)

        intento = proveedor.crear_intento(
            reserva=reserva, url_retorno="https://x.uy/reserva/" + reserva.token
        )

        llamada = transporte.llamadas[0]
        self.assertEqual(llamada["metodo"], "POST")
        self.assertTrue(llamada["url"].endswith("/v1/orders"))

        cuerpo = llamada["cuerpo"]
        self.assertEqual(cuerpo["type"], "qr")
        self.assertEqual(cuerpo["total_amount"], 1400.0)
        self.assertEqual(cuerpo["config"]["qr"]["mode"], "dynamic")
        self.assertEqual(cuerpo["config"]["qr"]["external_pos_id"], "CAJA001")
        # La referencia externa es el token de la reserva: con eso se la encuentra
        # cuando vuelve la notificacion.
        self.assertEqual(cuerpo["external_reference"], reserva.token)
        self.assertEqual(cuerpo["transactions"]["payments"][0]["amount"], 1400.0)
        self.assertEqual(cuerpo["items"][0]["quantity"], 2)

    def test_manda_autorizacion_e_idempotencia(self):
        transporte = TransporteFalso([{"id": "ORD-1"}])
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(), transporte=transporte
        )
        proveedor.crear_intento(reserva=self.reserva(), url_retorno="https://x.uy/")

        cabeceras = transporte.llamadas[0]["cabeceras"]
        self.assertEqual(cabeceras["Authorization"], "Bearer APP_USR-token-de-prueba")
        self.assertTrue(cabeceras["X-Idempotency-Key"])

    def test_devuelve_la_referencia_de_la_orden(self):
        transporte = TransporteFalso([{"id": "ORD-77"}])
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(), transporte=transporte
        )
        intento = proveedor.crear_intento(
            reserva=self.reserva(), url_retorno="https://x.uy/volver"
        )
        self.assertEqual(intento["referencia"], "ORD-77")
        self.assertEqual(intento["url"], "https://x.uy/volver")

    def test_sin_credenciales_configuradas_no_arma_la_orden(self):
        transporte = TransporteFalso()
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(activo=False), transporte=transporte
        )
        with self.assertRaises(ErrorDelProveedor):
            proveedor.crear_intento(
                reserva=self.reserva(), url_retorno="https://x.uy/"
            )
        self.assertEqual(transporte.llamadas, [])

    def test_si_no_vuelve_identificador_falla_claro(self):
        transporte = TransporteFalso([{"status": "created"}])
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(), transporte=transporte
        )
        with self.assertRaises(ErrorDelProveedor):
            proveedor.crear_intento(
                reserva=self.reserva(), url_retorno="https://x.uy/"
            )


class NotificacionesTests(BasePagosTests):
    def test_sin_clave_de_firma_no_se_puede_validar(self):
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(clave_de_firma=""), transporte=TransporteFalso()
        )
        datos = proveedor.interpretar_webhook(
            payload={"data": {"id": "ORD-1"}}, cabeceras={}
        )
        self.assertEqual(datos["estado"], "pendiente")
        self.assertEqual(datos["detalle"], "sin_clave_de_firma")
        self.assertIn("clave de firma", datos["mensaje"])

    def test_una_firma_invalida_se_rechaza(self):
        transporte = TransporteFalso()
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(), transporte=transporte
        )
        datos = proveedor.interpretar_webhook(
            payload={"data": {"id": "ORD-1"}},
            cabeceras={"x-signature": "ts=1,v1=firmafalsa", "x-request-id": "r1"},
        )
        self.assertEqual(datos["estado"], "rechazado")
        self.assertEqual(datos["detalle"], "firma_invalida")
        # Con la firma invalida NO se llama a la API.
        self.assertEqual(transporte.llamadas, [])

    def test_con_firma_valida_consulta_la_orden(self):
        transporte = TransporteFalso([
            {
                "id": "ORD-1",
                "status": "processed",
                "external_reference": "token-de-la-reserva",
                "transactions": {"payments": [{"status": "processed"}]},
            }
        ])
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(), transporte=transporte
        )
        datos = proveedor.interpretar_webhook(
            payload={"data": {"id": "ORD-1"}},
            cabeceras={
                "x-signature": self.firma("ORD-1", "req-1", "123"),
                "x-request-id": "req-1",
            },
        )
        self.assertEqual(datos["estado"], "aprobado")
        self.assertEqual(datos["referencia"], "token-de-la-reserva")
        self.assertEqual(transporte.llamadas[0]["metodo"], "GET")
        self.assertTrue(transporte.llamadas[0]["url"].endswith("/v1/orders/ORD-1"))

    def test_una_notificacion_sin_orden_se_ignora(self):
        proveedor = ProveedorMercadoPago(
            credencial=self.credencial(), transporte=TransporteFalso()
        )
        datos = proveedor.interpretar_webhook(
            payload={"algo": "sin id"},
            cabeceras={
                "x-signature": self.firma("", "req-1", "123"),
                "x-request-id": "req-1",
            },
        )
        self.assertEqual(datos["detalle"], "sin_orden")


class MapeoDeEstadosTests(BasePagosTests):
    def traducir(self, orden):
        from apps.pagos.proveedores import _estado_de_la_orden

        return _estado_de_la_orden(orden)

    def test_orden_procesada_es_aprobada(self):
        estado, detalle = self.traducir(
            {"status": "processed", "transactions": {"payments": [{"status": "processed"}]}}
        )
        self.assertEqual(estado, "aprobado")
        self.assertEqual(detalle, "processed")

    def test_orden_expirada_es_rechazada(self):
        estado, _ = self.traducir({"status": "expired"})
        self.assertEqual(estado, "rechazado")

    def test_orden_creada_es_pendiente(self):
        estado, _ = self.traducir({"status": "created"})
        self.assertEqual(estado, "pendiente")

    def test_la_transaccion_manda_sobre_la_orden(self):
        """Una orden procesada con la transaccion creada NO tiene la plata adentro."""
        estado, detalle = self.traducir(
            {
                "status": "processed",
                "transactions": {"payments": [{"status": "created"}]},
            }
        )
        self.assertEqual(estado, "pendiente")
        self.assertEqual(detalle, "created")

    def test_un_estado_desconocido_no_se_toma_como_aprobado(self):
        estado, _ = self.traducir({"status": "estado-raro"})
        self.assertEqual(estado, "pendiente")

    def test_una_orden_reembolsada_es_rechazada(self):
        estado, _ = self.traducir({"status": "refunded"})
        self.assertEqual(estado, "rechazado")


class SeleccionDeProveedorTests(BasePagosTests):
    def test_sin_credenciales_cae_al_simulado(self):
        proveedor = proveedor_para(self.tenant)
        self.assertIsInstance(proveedor, ProveedorSimulado)

    def test_con_credenciales_activas_usa_mercadopago(self):
        self.credencial()
        proveedor = proveedor_para(self.tenant)
        self.assertIsInstance(proveedor, ProveedorMercadoPago)
        self.assertEqual(proveedor.credencial.access_token, "APP_USR-token-de-prueba")

    def test_una_credencial_inactiva_no_se_usa(self):
        self.credencial(activo=False)
        self.assertIsInstance(proveedor_para(self.tenant), ProveedorSimulado)

    def test_proveedor_actual_no_sirve_para_cobrar_con_mercadopago(self):
        """Sin boliche no hay credenciales: hay que usar proveedor_para."""
        from django.test import override_settings

        with override_settings(PROVEEDOR_DE_PAGO="mercadopago"):
            with self.assertRaises(ErrorDelProveedor):
                proveedor_actual()

    def test_el_token_no_aparece_en_el_repr(self):
        credencial = self.credencial()
        self.assertNotIn("APP_USR", repr(credencial))
