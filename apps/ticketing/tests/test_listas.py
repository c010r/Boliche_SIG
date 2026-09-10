"""Listas, promotores y comisiones (ANALISIS.md seccion 9 ter).

Las tres reglas que se verifican:
- el corte lo aplica el sistema, no el de la puerta;
- la atribucion se registra al ingresar y no se edita;
- la lista consume aforo.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.tenancy.models import Local, Tenant
from apps.ticketing.listas import (
    InvitadoInvalido,
    agregar_invitado,
    anular_invitado,
    aprobar_comision,
    corte_efectivo,
    crear_lista,
    crear_promotor,
    liquidar_comisiones,
    lista_vigente,
    pagar_comision,
    resumen_de_listas,
    validar_ingreso_de_lista,
)
from apps.ticketing.models import (
    ComisionPromotor,
    Lista,
    ListaInvitado,
    MovimientoAforo,
    UsoLista,
)
from apps.ticketing.services import (
    aforo_en_vivo,
    agregar_tipo,
    crear_evento,
    publicar_evento,
    registrar_ingreso_manual,
)


def momento_en(anio, mes, dia, hora, minuto=0):
    return timezone.make_aware(datetime(anio, mes, dia, hora, minuto))


class BaseListasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Listas", slug="listas")
        cls.tenant = cls.datos["tenant"]

    def setUp(self):
        self.ctx = tenant_context(self.tenant)
        self.ctx.__enter__()
        self.local = Local.objects.get()
        self.usuario = self.datos["admin"]
        self.evento = crear_evento(
            local=self.local,
            nombre="Viernes",
            fecha=date(2026, 10, 3),
            aforo=50,
            hora_apertura=time(23, 0),
        )
        agregar_tipo(evento=self.evento, nombre="General", precio="500", cupo=50)
        publicar_evento(self.evento)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)


class CorteDeListaTests(BaseListasTests):
    def test_sin_corte_la_lista_vale_toda_la_noche(self):
        lista = crear_lista(evento=self.evento, nombre="Casa", cupo=10)
        self.assertIsNone(corte_efectivo(lista))
        self.assertTrue(lista_vigente(lista, momento_en(2026, 10, 4, 5, 0)))

    def test_el_corte_posterior_a_la_medianoche_cae_al_dia_siguiente(self):
        lista = crear_lista(
            evento=self.evento, nombre="Cumple", cupo=10, hora_de_corte=time(1, 0)
        )
        corte = corte_efectivo(lista)
        self.assertEqual(corte.date(), date(2026, 10, 4))
        self.assertEqual(corte.time(), time(1, 0))

    def test_el_corte_antes_de_medianoche_es_el_mismo_dia(self):
        temprano = crear_evento(
            local=self.local, nombre="Temprano", fecha=date(2026, 10, 10),
            aforo=50, hora_apertura=time(20, 0),
        )
        lista = crear_lista(
            evento=temprano, nombre="Casa", cupo=10, hora_de_corte=time(22, 0)
        )
        self.assertEqual(corte_efectivo(lista).date(), date(2026, 10, 10))

    def test_una_lista_cortada_ya_no_deja_entrar(self):
        lista = crear_lista(
            evento=self.evento, nombre="Cumple", cupo=10, hora_de_corte=time(1, 0)
        )
        invitado = agregar_invitado(lista=lista, nombre="Ana")

        # 00:30: todavia vale.
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, usuario=self.usuario,
            momento=momento_en(2026, 10, 4, 0, 30),
        )
        self.assertTrue(resultado["ok"])

        otro = agregar_invitado(lista=lista, nombre="Beto")
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=otro.qr_token, usuario=self.usuario,
            momento=momento_en(2026, 10, 4, 1, 30),
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "lista_cortada")

    def test_una_lista_cerrada_a_mano_no_deja_entrar(self):
        lista = crear_lista(evento=self.evento, nombre="Casa", cupo=10)
        invitado = agregar_invitado(lista=lista, nombre="Ana")
        lista.estado = Lista.Estado.CERRADA
        lista.save(update_fields=["estado"])
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, usuario=self.usuario
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "lista_cortada")


class IngresosDeListaTests(BaseListasTests):
    def setUp(self):
        super().setUp()
        self.promotor = crear_promotor(nombre="Rocio", valor_comision="150")
        self.lista = crear_lista(
            evento=self.evento, nombre="Lista de Rocio", cupo=5,
            tipo=Lista.Tipo.PROMOTOR, promotor=self.promotor,
            creada_por=self.usuario,
        )
        self.ana = agregar_invitado(lista=self.lista, nombre="Ana", personas=3)

    def test_un_invitado_entra_y_consume_aforo(self):
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=self.ana.qr_token, personas=3,
            usuario=self.usuario,
        )
        self.assertTrue(resultado["ok"])
        self.assertEqual(resultado["aforo_en_vivo"], 3)
        self.assertEqual(aforo_en_vivo(self.evento), 3)

    def test_el_invitado_puede_entrar_en_dos_tandas(self):
        validar_ingreso_de_lista(
            evento=self.evento, nombre="Ana", personas=1, usuario=self.usuario
        )
        resultado = validar_ingreso_de_lista(
            evento=self.evento, nombre="Ana", personas=2, usuario=self.usuario
        )
        self.assertTrue(resultado["ok"])
        self.assertEqual(aforo_en_vivo(self.evento), 3)

    def test_no_puede_pasar_mas_gente_de_la_que_cubre(self):
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=self.ana.qr_token, personas=4,
            usuario=self.usuario,
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "sin_personas")
        self.assertEqual(aforo_en_vivo(self.evento), 0)

    def test_la_lista_tiene_su_propio_cupo(self):
        beto = agregar_invitado(lista=self.lista, nombre="Beto", personas=5)
        validar_ingreso_de_lista(
            evento=self.evento, texto=self.ana.qr_token, personas=3,
            usuario=self.usuario,
        )
        # Quedan 2 lugares en la lista, y Beto trae 5.
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=beto.qr_token, personas=5, usuario=self.usuario
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "cupo_de_lista")

    def test_la_lista_consume_aforo_y_lo_agota(self):
        # 50 de aforo: entran 48 por la puerta comun y quedan 2.
        registrar_ingreso_manual(
            evento=self.evento, cantidad=48, motivo="Entradas", usuario=self.usuario
        )
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=self.ana.qr_token, personas=3,
            usuario=self.usuario,
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "aforo_completo")

    def test_con_autorizacion_el_ingreso_forzado_pasa_y_queda_marcado(self):
        registrar_ingreso_manual(
            evento=self.evento, cantidad=50, motivo="Lleno", usuario=self.usuario
        )
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=self.ana.qr_token, personas=1,
            usuario=self.usuario, forzar=True,
        )
        self.assertTrue(resultado["ok"])
        self.assertIn("forzado", resultado["uso"].observaciones)

    def test_se_puede_buscar_por_nombre_sin_qr(self):
        resultado = validar_ingreso_de_lista(
            evento=self.evento, nombre="ana", personas=1, usuario=self.usuario
        )
        self.assertTrue(resultado["ok"])

    def test_un_nombre_ambiguo_pide_el_qr(self):
        otra = crear_lista(evento=self.evento, nombre="Otra", cupo=5)
        agregar_invitado(lista=otra, nombre="Ana Maria")
        resultado = validar_ingreso_de_lista(
            evento=self.evento, nombre="Ana", personas=1, usuario=self.usuario
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "ambiguo")

    def test_un_invitado_anulado_no_entra(self):
        anular_invitado(invitado=self.ana, motivo="Se bajo", usuario=self.usuario)
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto=self.ana.qr_token, usuario=self.usuario
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "anulado")

    def test_un_codigo_desconocido_no_esta_en_la_lista(self):
        resultado = validar_ingreso_de_lista(
            evento=self.evento, texto="no-existe", usuario=self.usuario
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "no_existe")

    def test_un_invitado_de_otro_evento_se_rechaza(self):
        otro = crear_evento(
            local=self.local, nombre="Sabado", fecha=date(2026, 10, 11), aforo=50
        )
        resultado = validar_ingreso_de_lista(
            evento=otro, texto=self.ana.qr_token, usuario=self.usuario
        )
        self.assertFalse(resultado["ok"])
        self.assertEqual(resultado["motivo"], "otro_evento")

    def test_anular_exige_motivo(self):
        with self.assertRaises(ValidationError):
            anular_invitado(invitado=self.ana, motivo="", usuario=self.usuario)

    def test_no_se_anula_a_quien_ya_ingreso(self):
        validar_ingreso_de_lista(
            evento=self.evento, texto=self.ana.qr_token, personas=1, usuario=self.usuario
        )
        with self.assertRaises(InvitadoInvalido):
            anular_invitado(invitado=self.ana, motivo="Tarde", usuario=self.usuario)


class AtribucionTests(BaseListasTests):
    def setUp(self):
        super().setUp()
        self.promotor = crear_promotor(nombre="Rocio", valor_comision="150")
        self.lista = crear_lista(
            evento=self.evento, nombre="Lista de Rocio", cupo=10,
            tipo=Lista.Tipo.PROMOTOR, promotor=self.promotor,
        )

    def test_la_atribucion_no_se_puede_editar(self):
        invitado = agregar_invitado(lista=self.lista, nombre="Ana", personas=2)
        validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, personas=2, usuario=self.usuario
        )
        uso = UsoLista.objects.get()
        uso.personas = 9
        with self.assertRaises(ValidationError):
            uso.save()

    def test_la_atribucion_no_se_puede_borrar(self):
        invitado = agregar_invitado(lista=self.lista, nombre="Ana", personas=1)
        validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, usuario=self.usuario
        )
        with self.assertRaises(ValidationError):
            UsoLista.objects.get().delete()

    def test_queda_registrado_quien_dejo_pasar(self):
        invitado = agregar_invitado(lista=self.lista, nombre="Ana", personas=1)
        validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, usuario=self.usuario
        )
        self.assertEqual(UsoLista.objects.get().registrado_por, self.usuario)

    def test_el_aforo_que_mueve_la_lista_es_un_asiento_mas(self):
        invitado = agregar_invitado(lista=self.lista, nombre="Ana", personas=2)
        validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, personas=2, usuario=self.usuario
        )
        movimiento = MovimientoAforo.objects.get()
        self.assertEqual(movimiento.delta, 2)
        self.assertIn("Lista de Rocio", movimiento.motivo)


class ComisionesTests(BaseListasTests):
    def setUp(self):
        super().setUp()
        self.rocio = crear_promotor(nombre="Rocio", valor_comision="150")
        self.luis = crear_promotor(nombre="Luis", valor_comision="200")
        self.lista_rocio = crear_lista(
            evento=self.evento, nombre="Rocio", cupo=20,
            tipo=Lista.Tipo.PROMOTOR, promotor=self.rocio,
        )
        self.lista_luis = crear_lista(
            evento=self.evento, nombre="Luis", cupo=20,
            tipo=Lista.Tipo.PROMOTOR, promotor=self.luis,
        )
        self.lista_casa = crear_lista(evento=self.evento, nombre="Casa", cupo=20)

    def entrar(self, lista, nombre, personas):
        invitado = agregar_invitado(lista=lista, nombre=nombre, personas=personas)
        validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, personas=personas,
            usuario=self.usuario,
        )

    def test_la_comision_sale_de_los_ingresos_registrados(self):
        self.entrar(self.lista_rocio, "Ana", 3)
        self.entrar(self.lista_rocio, "Beto", 2)
        self.entrar(self.lista_luis, "Carla", 4)
        self.entrar(self.lista_casa, "Dani", 5)

        comisiones = {c.promotor.nombre: c for c in liquidar_comisiones(evento=self.evento)}
        self.assertEqual(comisiones["Rocio"].personas, 5)
        self.assertEqual(comisiones["Rocio"].monto, Decimal("750.00"))
        self.assertEqual(comisiones["Luis"].personas, 4)
        self.assertEqual(comisiones["Luis"].monto, Decimal("800.00"))
        # La lista de la casa no genera comision.
        self.assertNotIn("Casa", comisiones)

    def test_lo_que_no_ingreso_no_se_paga(self):
        agregar_invitado(lista=self.lista_rocio, nombre="Fantasma", personas=10)
        comisiones = liquidar_comisiones(evento=self.evento)
        self.assertEqual(comisiones, [])

    def test_liquidar_dos_veces_no_duplica(self):
        self.entrar(self.lista_rocio, "Ana", 2)
        liquidar_comisiones(evento=self.evento)
        liquidar_comisiones(evento=self.evento)
        self.assertEqual(ComisionPromotor.objects.count(), 1)
        self.assertEqual(ComisionPromotor.objects.get().personas, 2)

    def test_la_comision_se_aprueba_y_despues_se_paga(self):
        self.entrar(self.lista_rocio, "Ana", 2)
        comision = liquidar_comisiones(evento=self.evento)[0]
        self.assertEqual(comision.estado, ComisionPromotor.Estado.CALCULADA)

        with self.assertRaises(ValidationError):
            pagar_comision(comision=comision)

        aprobar_comision(comision=comision, usuario=self.usuario)
        comision.refresh_from_db()
        self.assertEqual(comision.estado, ComisionPromotor.Estado.APROBADA)
        self.assertEqual(comision.aprobada_por, self.usuario)

        pagar_comision(comision=comision)
        comision.refresh_from_db()
        self.assertEqual(comision.estado, ComisionPromotor.Estado.PAGADA)
        self.assertIsNotNone(comision.pagada_en)


class ResumenDeListasTests(BaseListasTests):
    def test_el_resumen_trae_cupo_uso_y_vigencia(self):
        lista = crear_lista(
            evento=self.evento, nombre="Casa", cupo=10, hora_de_corte=time(1, 0)
        )
        invitado = agregar_invitado(lista=lista, nombre="Ana", personas=4)
        validar_ingreso_de_lista(
            evento=self.evento, texto=invitado.qr_token, personas=4, usuario=self.usuario
        )
        resumen = resumen_de_listas(self.evento)
        fila = resumen["filas"][0]
        self.assertEqual(fila["cupo"], 10)
        self.assertEqual(fila["usados"], 4)
        self.assertEqual(fila["disponibles"], 6)
        self.assertTrue(fila["vigente"])
        self.assertIsNotNone(fila["corte"])
        self.assertEqual(resumen["personas_por_lista"], 4)


class AislamientoDeListasTests(BaseListasTests):
    def test_las_listas_de_otro_boliche_no_se_ven(self):
        crear_lista(evento=self.evento, nombre="Casa", cupo=5)
        otro = Tenant.objects.create(nombre="Boliche B", slug="boliches-b")
        with tenant_context(otro):
            self.assertEqual(Lista.objects.count(), 0)
            self.assertEqual(ListaInvitado.objects.count(), 0)
            self.assertEqual(UsoLista.objects.count(), 0)
