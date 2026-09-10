"""Invariantes de entradas, aforo y control de acceso (ANALISIS.md secciones 5 y 9 bis).

Los que rompen una puerta real:
- el aforo no se sobrevende,
- una entrada se usa UNA sola vez,
- una entrada de otro evento se rechaza,
- el aforo es la suma de un ledger, no un contador mutable.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Usuario
from apps.core.context import TenantNotSetError, tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.sales.models import Pago, SesionCaja, Venta
from apps.sales.services import abrir_caja, abrir_turno, registrar_pago
from apps.tenancy.models import Local, Tenant, Terminal
from apps.ticketing.models import Entrada, Evento, MovimientoAforo, TipoEntrada
from apps.ticketing.services import (
    CupoAgotado,
    EntradaInvalida,
    aforo_disponible,
    aforo_en_vivo,
    agregar_tipo,
    anular_entrada,
    contenido_qr,
    crear_evento,
    publicar_evento,
    qr_svg,
    registrar_egreso,
    registrar_ingreso_manual,
    resumen_de_evento,
    token_desde_texto,
    validar_entrada,
    vender_entrada,
)


class BaseTicketingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Fiesta", slug="fiesta")
        cls.tenant = cls.datos["tenant"]

    def setUp(self):
        self.ctx = tenant_context(self.tenant)
        self.ctx.__enter__()

        self.local = Local.objects.get()
        self.puerta = Terminal.objects.get(nombre="Puerta")
        self.usuario = self.datos["admin"]

        self.evento = crear_evento(
            local=self.local,
            nombre="Viernes",
            fecha=date(2026, 9, 12),
            aforo=100,
        )
        self.general = agregar_tipo(
            evento=self.evento, nombre="General", precio="500", cupo=60, orden=1
        )
        self.vip = agregar_tipo(
            evento=self.evento, nombre="VIP", precio="1200", cupo=10, orden=2
        )
        publicar_evento(self.evento)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)


class EventoTests(BaseTicketingTests):
    def test_un_evento_sin_tipos_no_se_publica(self):
        otro = crear_evento(
            local=self.local, nombre="Vacio", fecha=date(2026, 9, 19), aforo=50
        )
        with self.assertRaises(ValidationError):
            publicar_evento(otro)

    def test_el_cupo_del_tipo_limita_la_venta(self):
        with self.assertRaises(CupoAgotado):
            vender_entrada(tipo=self.vip, cantidad=11, usuario=self.usuario)

    def test_el_aforo_del_evento_limita_el_total(self):
        # El aforo es 100 y la suma de cupos es 70: subimos el cupo para probar.
        self.general.cupo = 200
        self.general.save(update_fields=["cupo"])
        with self.assertRaises(CupoAgotado):
            vender_entrada(tipo=self.general, cantidad=101, usuario=self.usuario)

    def test_los_folios_son_correlativos_por_evento(self):
        entradas = vender_entrada(tipo=self.general, cantidad=3, usuario=self.usuario)
        self.assertEqual([e.folio for e in entradas], ["000001", "000002", "000003"])
        mas = vender_entrada(tipo=self.vip, cantidad=1, usuario=self.usuario)
        self.assertEqual(mas[0].folio, "000004")

    def test_cada_evento_lleva_su_propia_numeracion(self):
        vender_entrada(tipo=self.general, cantidad=2, usuario=self.usuario)
        otro = crear_evento(
            local=self.local, nombre="Sabado", fecha=date(2026, 9, 13), aforo=50
        )
        otro_tipo = agregar_tipo(evento=otro, nombre="General", precio="500", cupo=50)
        entradas = vender_entrada(tipo=otro_tipo, cantidad=1, usuario=self.usuario)
        self.assertEqual(entradas[0].folio, "000001")

    def test_el_cupo_disponible_baja_al_vender(self):
        self.assertEqual(self.general.disponibles, 60)
        vender_entrada(tipo=self.general, cantidad=5, usuario=self.usuario)
        self.general.refresh_from_db()
        self.assertEqual(self.general.disponibles, 55)


class QrTests(BaseTicketingTests):
    def test_el_token_no_es_el_identificador(self):
        entrada = vender_entrada(tipo=self.general, usuario=self.usuario)[0]
        self.assertNotEqual(entrada.qr_token, str(entrada.id))
        self.assertNotIn(str(entrada.id), entrada.qr_token)

    def test_los_tokens_son_unicos_y_largos(self):
        entradas = vender_entrada(tipo=self.general, cantidad=5, usuario=self.usuario)
        tokens = {e.qr_token for e in entradas}
        self.assertEqual(len(tokens), 5)
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_el_contenido_del_qr_es_el_token(self):
        entrada = vender_entrada(tipo=self.general, usuario=self.usuario)[0]
        self.assertEqual(contenido_qr(entrada), entrada.qr_token)

    def test_el_qr_se_genera_como_svg(self):
        entrada = vender_entrada(tipo=self.general, usuario=self.usuario)[0]
        svg = qr_svg(entrada)
        self.assertIn("<svg", svg)

    def test_acepta_el_token_pelado_y_una_url(self):
        self.assertEqual(token_desde_texto("abc123"), "abc123")
        self.assertEqual(token_desde_texto("https://x.uy/puerta/e/abc123"), "abc123")
        self.assertEqual(token_desde_texto("https://x.uy/puerta/e/abc123/"), "abc123")


class AforoTests(BaseTicketingTests):
    def test_el_aforo_arranca_en_cero(self):
        self.assertEqual(aforo_en_vivo(self.evento), 0)
        self.assertEqual(aforo_disponible(self.evento), 100)

    def test_el_ingreso_consume_aforo(self):
        entrada = vender_entrada(tipo=self.general, usuario=self.usuario)[0]
        resultado = validar_entrada(
            evento=self.evento, texto_qr=entrada.qr_token,
            terminal=self.puerta, usuario=self.usuario,
        )
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["aforo_en_vivo"], 1)

    def test_el_ingreso_manual_exige_motivo(self):
        with self.assertRaises(ValidationError):
            registrar_ingreso_manual(
                evento=self.evento, motivo="", usuario=self.usuario
            )

    def test_el_ingreso_manual_suma_aforo(self):
        registrar_ingreso_manual(
            evento=self.evento, cantidad=3, motivo="Lista del DJ",
            terminal=self.puerta, usuario=self.usuario,
        )
        self.assertEqual(aforo_en_vivo(self.evento), 3)

    def test_el_egreso_resta_aforo(self):
        registrar_ingreso_manual(
            evento=self.evento, cantidad=2, motivo="Lista", usuario=self.usuario
        )
        registrar_egreso(evento=self.evento, motivo="Se fue", usuario=self.usuario)
        self.assertEqual(aforo_en_vivo(self.evento), 1)

    def test_el_aforo_es_la_suma_del_ledger(self):
        registrar_ingreso_manual(
            evento=self.evento, cantidad=5, motivo="Lista", usuario=self.usuario
        )
        registrar_egreso(evento=self.evento, cantidad=2, motivo="Se fueron", usuario=self.usuario)
        suma = sum(
            m.delta for m in MovimientoAforo.objects.filter(evento=self.evento)
        )
        self.assertEqual(aforo_en_vivo(self.evento), suma)

    def test_los_movimientos_de_aforo_no_se_modifican(self):
        registrar_ingreso_manual(
            evento=self.evento, cantidad=1, motivo="Lista", usuario=self.usuario
        )
        movimiento = MovimientoAforo.objects.get()
        movimiento.delta = 5
        with self.assertRaises(ValidationError):
            movimiento.save()

    def test_los_movimientos_de_aforo_no_se_borran(self):
        registrar_ingreso_manual(
            evento=self.evento, cantidad=1, motivo="Lista", usuario=self.usuario
        )
        with self.assertRaises(ValidationError):
            MovimientoAforo.objects.get().delete()

    def test_el_ingreso_no_puede_restar(self):
        with self.assertRaises(ValidationError):
            registrar_ingreso_manual(
                evento=self.evento, cantidad=-1, motivo="x", usuario=self.usuario
            )


class ControlDeAccesoTests(BaseTicketingTests):
    def setUp(self):
        super().setUp()
        self.entrada = vender_entrada(
            tipo=self.general, usuario=self.usuario
        )[0]

    def validar(self, texto):
        return validar_entrada(
            evento=self.evento, texto_qr=texto,
            terminal=self.puerta, usuario=self.usuario,
        )

    def test_una_entrada_valida_ingresa(self):
        resultado = self.validar(self.entrada.qr_token)
        self.assertTrue(resultado["ok"])
        self.entrada.refresh_from_db()
        self.assertEqual(self.entrada.estado, Entrada.Estado.USADA)
        self.assertEqual(self.entrada.usada_en_terminal, self.puerta)

    def test_la_misma_entrada_no_entra_dos_veces(self):
        self.assertTrue(self.validar(self.entrada.qr_token)["ok"])
        segundo = self.validar(self.entrada.qr_token)
        self.assertFalse(segundo["ok"])
        self.assertEqual(segundo["motivo"], "ya_usada")
        self.assertEqual(aforo_en_vivo(self.evento), 1)

    def test_una_entrada_de_otro_evento_se_rechaza(self):
        otro = crear_evento(
            local=self.local, nombre="Otro", fecha=date(2026, 9, 20), aforo=50
        )
        agregar_tipo(evento=otro, nombre="General", precio="500", cupo=50)
        resultado = validar_entrada(
            evento=otro, texto_qr=self.entrada.qr_token,
            terminal=self.puerta, usuario=self.usuario,
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "otro_evento")

    def test_una_entrada_anulada_se_rechaza(self):
        anular_entrada(entrada=self.entrada, usuario=self.usuario, motivo="Se arrepintio")
        resultado = self.validar(self.entrada.qr_token)
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "anulada")

    def test_un_codigo_inexistente_se_rechaza(self):
        resultado = self.validar("token-que-no-existe")
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "no_existe")

    def test_anular_exige_motivo(self):
        with self.assertRaises(ValidationError):
            anular_entrada(entrada=self.entrada, usuario=self.usuario, motivo="")

    def test_no_se_anula_una_entrada_que_ya_ingreso(self):
        self.validar(self.entrada.qr_token)
        with self.assertRaises(EntradaInvalida):
            anular_entrada(
                entrada=self.entrada, usuario=self.usuario, motivo="Tarde"
            )

    def test_la_anulacion_queda_registrada(self):
        anular_entrada(entrada=self.entrada, usuario=self.usuario, motivo="Duplicada")
        self.entrada.refresh_from_db()
        self.assertEqual(self.entrada.anulada_por, self.usuario)
        self.assertEqual(self.entrada.motivo_anulacion, "Duplicada")


class VentaEnPuertaTests(BaseTicketingTests):
    def test_vender_en_puerta_registra_la_venta_en_la_caja(self):
        turno = abrir_turno(local=self.local, nombre="Viernes")
        sesion = abrir_caja(
            turno=turno, terminal=self.puerta, usuario=self.usuario,
            fondo_inicial=Decimal("1000"),
        )
        entradas = vender_entrada(
            tipo=self.general, cantidad=2, usuario=self.usuario,
            sesion_caja=sesion, terminal=self.puerta,
        )
        venta = Venta.objects.get()
        self.assertEqual(venta.total, Decimal("1000.00"))
        self.assertEqual(venta.concepto, "2 x General - Viernes")
        self.assertEqual([e.venta_id for e in entradas], [venta.id, venta.id])

    def test_la_venta_de_entradas_entra_al_arqueo(self):
        turno = abrir_turno(local=self.local, nombre="Viernes")
        sesion = abrir_caja(
            turno=turno, terminal=self.puerta, usuario=self.usuario,
            fondo_inicial=Decimal("1000"),
        )
        vender_entrada(
            tipo=self.general, cantidad=2, usuario=self.usuario, sesion_caja=sesion
        )
        venta = Venta.objects.get()
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=venta.total,
            estado=Pago.Estado.APROBADO,
        )
        from apps.sales.services import calcular_esperado

        esperado = calcular_esperado(sesion)
        self.assertEqual(esperado[Pago.Medio.EFECTIVO], Decimal("2000.00"))

    def test_sin_caja_la_venta_queda_sin_venta_asociada(self):
        entradas = vender_entrada(tipo=self.general, usuario=self.usuario)
        self.assertIsNone(entradas[0].venta)
        self.assertEqual(Venta.objects.count(), 0)


class ResumenTests(BaseTicketingTests):
    def test_el_resumen_trae_lo_que_el_dueno_quiere(self):
        entradas = vender_entrada(tipo=self.general, cantidad=3, usuario=self.usuario)
        validar_entrada(
            evento=self.evento, texto_qr=entradas[0].qr_token,
            terminal=self.puerta, usuario=self.usuario,
        )
        anular_entrada(entrada=entradas[1], usuario=self.usuario, motivo="Duplicada")

        resumen = resumen_de_evento(self.evento)
        self.assertEqual(resumen["vendidas"], 2)  # la anulada no cuenta
        self.assertEqual(resumen["usadas"], 1)
        self.assertEqual(resumen["anuladas"], 1)
        self.assertEqual(resumen["dentro"], 1)
        self.assertEqual(resumen["recaudado"], Decimal("1000.00"))
        self.assertEqual(resumen["disponible"], 99)


class AislamientoDeTicketingTests(BaseTicketingTests):
    def test_las_entradas_de_otro_boliche_no_se_ven(self):
        vender_entrada(tipo=self.general, usuario=self.usuario)
        otro = Tenant.objects.create(nombre="Boliche B", slug="boliches-b")
        with tenant_context(otro):
            self.assertEqual(Entrada.objects.count(), 0)
            self.assertEqual(Evento.objects.count(), 0)
            self.assertEqual(MovimientoAforo.objects.count(), 0)

    def test_operar_entradas_sin_tenant_revienta(self):
        self.ctx.__exit__(None, None, None)
        with self.assertRaises(TenantNotSetError):
            vender_entrada(tipo=self.general, usuario=self.usuario)
        self.ctx = tenant_context(self.tenant)
        self.ctx.__enter__()
