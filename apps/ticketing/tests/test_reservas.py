"""Reservas con vencimiento: la carrera que arruina una preventa (ANALISIS.md 9 bis).

Lo que se verifica:
- reservar toma cupo y no se puede sobrevender;
- las reservas vencidas NO ocupan cupo;
- confirmar emite entradas;
- un pago aprobado sobre una reserva VENCIDA no emite y queda marcado: no se
  sobrevende "para que funcione".
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.tenancy.models import Local, Tenant
from apps.ticketing.models import Entrada, Reserva
from apps.ticketing.pagos import ProveedorSimulado, proveedor_actual
from apps.ticketing.reservas import (
    SinCupo,
    cancelar_reserva,
    confirmar_reserva,
    crear_reserva,
    disponibles_de_tipo,
    expirar_reservas,
    reservas_activas,
    resumen_de_reservas,
)
from apps.ticketing.services import agregar_tipo, crear_evento, publicar_evento, vender_entrada


class BaseReservasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Preventa", slug="preventa")
        cls.tenant = cls.datos["tenant"]

    def setUp(self):
        self.ctx = tenant_context(self.tenant)
        self.ctx.__enter__()
        self.local = Local.objects.get()
        self.usuario = self.datos["admin"]
        self.evento = crear_evento(
            local=self.local, nombre="Preventa", fecha=date(2026, 10, 3), aforo=100
        )
        self.general = agregar_tipo(
            evento=self.evento, nombre="General", precio="600", cupo=10
        )
        publicar_evento(self.evento)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)


class CupoTests(BaseReservasTests):
    def test_reservar_toma_cupo(self):
        self.assertEqual(disponibles_de_tipo(self.general), 10)
        crear_reserva(tipo=self.general, cantidad=3)
        self.assertEqual(disponibles_de_tipo(self.general), 7)
        self.assertEqual(reservas_activas(self.general), 3)

    def test_no_se_puede_reservar_mas_que_el_cupo(self):
        with self.assertRaises(SinCupo):
            crear_reserva(tipo=self.general, cantidad=11)

    def test_vender_y_reservar_comparten_el_mismo_cupo(self):
        vender_entrada(tipo=self.general, cantidad=4, usuario=self.usuario)
        self.assertEqual(disponibles_de_tipo(self.general), 6)
        with self.assertRaises(SinCupo):
            crear_reserva(tipo=self.general, cantidad=7)

    def test_la_ultima_entrada_se_toma_una_sola_vez(self):
        crear_reserva(tipo=self.general, cantidad=10)
        self.assertEqual(disponibles_de_tipo(self.general), 0)
        with self.assertRaises(SinCupo):
            crear_reserva(tipo=self.general, cantidad=1)

    def test_una_reserva_vencida_no_ocupa_cupo(self):
        momento = timezone.now()
        crear_reserva(tipo=self.general, cantidad=10, momento=momento)
        self.assertEqual(disponibles_de_tipo(self.general, momento), 0)
        # Diez minutos despues, el cupo esta libre otra vez aunque nadie haya
        # corrido la limpieza.
        despues = momento + timedelta(minutes=11)
        self.assertEqual(disponibles_de_tipo(self.general, despues), 10)

    def test_la_cantidad_tiene_que_ser_positiva(self):
        with self.assertRaises(ValidationError):
            crear_reserva(tipo=self.general, cantidad=0)

    def test_un_evento_cancelado_no_acepta_reservas(self):
        self.evento.estado = self.evento.Estado.CANCELADO
        self.evento.save(update_fields=["estado"])
        with self.assertRaises(ValidationError):
            crear_reserva(tipo=self.general, cantidad=1)

    def test_la_reserva_vence_a_los_minutos_configurados(self):
        momento = timezone.now()
        reserva = crear_reserva(tipo=self.general, cantidad=1, momento=momento)
        delta = reserva.expira_en - momento
        self.assertAlmostEqual(delta.total_seconds(), 600, delta=5)

    @override_settings(RESERVA_MINUTOS_DE_VALIDEZ=2)
    def test_el_vencimiento_es_configurable(self):
        momento = timezone.now()
        reserva = crear_reserva(tipo=self.general, cantidad=1, momento=momento)
        self.assertAlmostEqual(
            (reserva.expira_en - momento).total_seconds(), 120, delta=5
        )


class ConfirmacionTests(BaseReservasTests):
    def test_confirmar_emite_las_entradas(self):
        reserva = crear_reserva(
            tipo=self.general, cantidad=2, comprador_nombre="Ana"
        )
        resultado = confirmar_reserva(reserva=reserva, referencia_pago="mp-1")
        self.assertEqual(resultado["estado"], "confirmada")
        self.assertEqual(len(resultado["entradas"]), 2)
        reserva.refresh_from_db()
        self.assertEqual(reserva.estado, Reserva.Estado.CONFIRMADA)
        self.assertEqual(reserva.referencia_pago, "mp-1")

    def test_las_entradas_online_quedan_marcadas_como_tales(self):
        reserva = crear_reserva(tipo=self.general, cantidad=1)
        confirmar_reserva(reserva=reserva)
        entrada = Entrada.objects.get()
        self.assertEqual(entrada.canal, Entrada.Canal.ONLINE)
        self.assertEqual(entrada.estado, Entrada.Estado.EMITIDA)

    def test_confirmar_dos_veces_no_duplica_entradas(self):
        reserva = crear_reserva(tipo=self.general, cantidad=2)
        confirmar_reserva(reserva=reserva)
        resultado = confirmar_reserva(reserva=reserva)
        self.assertEqual(resultado["estado"], "ya_confirmada")
        self.assertEqual(Entrada.objects.count(), 2)

    def test_confirmar_consume_el_cupo_de_verdad(self):
        reserva = crear_reserva(tipo=self.general, cantidad=4)
        self.assertEqual(disponibles_de_tipo(self.general), 6)
        confirmar_reserva(reserva=reserva)
        self.assertEqual(disponibles_de_tipo(self.general), 6)
        self.assertEqual(self.general.vendidas, 4)

    def test_los_folios_siguen_la_numeracion_del_evento(self):
        vender_entrada(tipo=self.general, cantidad=2, usuario=self.usuario)
        reserva = crear_reserva(tipo=self.general, cantidad=2)
        resultado = confirmar_reserva(reserva=reserva)
        self.assertEqual(
            [e.folio for e in resultado["entradas"]], ["000003", "000004"]
        )


class PagoTardioTests(BaseReservasTests):
    def test_un_pago_sobre_una_reserva_vencida_no_emite_entrada(self):
        momento = timezone.now()
        reserva = crear_reserva(tipo=self.general, cantidad=2, momento=momento)

        # El pago se acredita 11 minutos despues, cuando la reserva ya vencio.
        resultado = confirmar_reserva(
            reserva=reserva, referencia_pago="mp-tarde",
            momento=momento + timedelta(minutes=11),
        )

        self.assertEqual(resultado["estado"], "pago_tardio")
        self.assertEqual(resultado["entradas"], [])
        self.assertIn("reembolsar", resultado["mensaje"].lower())
        self.assertEqual(Entrada.objects.count(), 0)

    def test_el_pago_tardio_queda_marcado(self):
        momento = timezone.now()
        reserva = crear_reserva(tipo=self.general, cantidad=1, momento=momento)
        confirmar_reserva(reserva=reserva, momento=momento + timedelta(minutes=11))
        reserva.refresh_from_db()
        self.assertTrue(reserva.pago_tardio)
        self.assertEqual(reserva.estado, Reserva.Estado.EXPIRADA)
        self.assertEqual(reserva.referencia_pago, "")
        self.assertIsNotNone(reserva.pagado_en)

    def test_el_cupo_no_se_sobrevende_con_el_pago_tardio(self):
        momento = timezone.now()
        primera = crear_reserva(tipo=self.general, cantidad=6, momento=momento)
        segunda = crear_reserva(tipo=self.general, cantidad=4, momento=momento)
        self.assertEqual(disponibles_de_tipo(self.general, momento), 0)

        # Las dos vencen; el cupo queda libre y entra otro comprador.
        despues = momento + timedelta(minutes=11)
        confirmar_reserva(reserva=primera, momento=despues)
        confirmar_reserva(reserva=segunda, momento=despues)

        reserva_nueva = crear_reserva(tipo=self.general, cantidad=10, momento=despues)
        resultado = confirmar_reserva(reserva=reserva_nueva, momento=despues)

        self.assertEqual(resultado["estado"], "confirmada")
        self.assertEqual(Entrada.objects.count(), 10)
        self.assertEqual(self.general.vendidas, 10)
        self.assertLessEqual(self.general.vendidas, self.general.cupo)

    def test_el_resumen_cuenta_los_pagos_tardios(self):
        momento = timezone.now()
        reserva = crear_reserva(tipo=self.general, cantidad=1, momento=momento)
        confirmar_reserva(reserva=reserva, momento=momento + timedelta(minutes=11))
        self.assertEqual(resumen_de_reservas(self.evento, momento)["pagos_tardios"], 1)


class ExpiracionTests(BaseReservasTests):
    def test_expirar_libera_el_cupo(self):
        momento = timezone.now()
        crear_reserva(tipo=self.general, cantidad=10, momento=momento)
        expiradas = expirar_reservas(momento=momento + timedelta(minutes=11))
        self.assertEqual(expiradas, 1)
        self.assertEqual(disponibles_de_tipo(self.general), 10)

    def test_expirar_no_toca_las_vigentes(self):
        momento = timezone.now()
        crear_reserva(tipo=self.general, cantidad=3, momento=momento)
        expiradas = expirar_reservas(momento=momento + timedelta(minutes=5))
        self.assertEqual(expiradas, 0)
        self.assertEqual(disponibles_de_tipo(self.general, momento), 7)

    def test_expirar_no_toca_las_confirmadas(self):
        momento = timezone.now()
        reserva = crear_reserva(tipo=self.general, cantidad=2, momento=momento)
        confirmar_reserva(reserva=reserva, momento=momento)
        expirar_reservas(momento=momento + timedelta(minutes=30))
        reserva.refresh_from_db()
        self.assertEqual(reserva.estado, Reserva.Estado.CONFIRMADA)


class CancelacionTests(BaseReservasTests):
    def test_cancelar_libera_el_cupo(self):
        reserva = crear_reserva(tipo=self.general, cantidad=5)
        cancelar_reserva(reserva=reserva)
        self.assertEqual(disponibles_de_tipo(self.general), 10)

    def test_no_se_cancela_una_reserva_confirmada(self):
        reserva = crear_reserva(tipo=self.general, cantidad=1)
        confirmar_reserva(reserva=reserva)
        with self.assertRaises(ValidationError):
            cancelar_reserva(reserva=reserva)


class ProveedorDePagoTests(BaseReservasTests):
    def test_el_proveedor_por_defecto_es_el_simulado(self):
        self.assertEqual(proveedor_actual().nombre, "simulado")

    def test_el_proveedor_simulado_arma_el_intento(self):
        reserva = crear_reserva(tipo=self.general, cantidad=1)
        intento = ProveedorSimulado().crear_intento(
            reserva=reserva, url_retorno="https://x.uy/pago/"
        )
        self.assertIn("url", intento)
        self.assertTrue(intento["referencia"].startswith("SIM-"))

    def test_el_proveedor_simulado_interpreta_el_webhook(self):
        datos = ProveedorSimulado().interpretar_webhook(
            payload={"referencia": "SIM-1", "estado": "aprobado"}, cabeceras={}
        )
        self.assertEqual(datos["referencia"], "SIM-1")
        self.assertEqual(datos["estado"], "aprobado")

    def test_un_proveedor_desconocido_falla_claro(self):
        from django.test import override_settings as ot

        with ot(PROVEEDOR_DE_PAGO="mercadopago-no-existe"):
            with self.assertRaises(Exception):
                proveedor_actual()


class AislamientoDeReservasTests(BaseReservasTests):
    def test_las_reservas_de_otro_boliche_no_se_ven(self):
        crear_reserva(tipo=self.general, cantidad=1)
        otro = Tenant.objects.create(nombre="Boliche B", slug="boliches-b")
        with tenant_context(otro):
            self.assertEqual(Reserva.objects.count(), 0)
