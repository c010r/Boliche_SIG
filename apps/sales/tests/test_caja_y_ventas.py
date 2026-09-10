"""Invariantes de caja y venta (ANALISIS.md seccion 5).

Los que rompen un sabado a la noche si fallan:
- una sola caja abierta por terminal,
- una venta pagada tiene pagos aprobados por el total,
- el cobro no se traba esperando al adquirente,
- un pago aprobado sobre caja cerrada NO reescribe el arqueo,
- anular repone el stock en vez de borrar.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Usuario
from apps.catalog.models import (
    CategoriaProducto,
    Insumo,
    Magnitud,
    Producto,
    Receta,
    RecetaItem,
    UnidadMedida,
)
from apps.core.context import tenant_context
from apps.sales.models import (
    Arqueo,
    EgresoCaja,
    Pago,
    SesionCaja,
    Turno,
    Venta,
)
from apps.sales.services import (
    CajaYaAbierta,
    abrir_caja,
    abrir_turno,
    anular_venta,
    calcular_esperado,
    cerrar_caja,
    confirmar_pago,
    registrar_egreso,
    registrar_pago,
    registrar_venta,
    resumen_de_ventas,
)
from apps.stock.models import Deposito, StockMovimiento
from apps.stock.services import saldo_calculado
from apps.tenancy.models import Local, Tenant, Terminal


class BaseVentasTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.boliche = Tenant.objects.create(nombre="Boliche A", slug="boliche-a")
        cls.ml = UnidadMedida.objects.create(
            codigo="ml", nombre="Mililitro", magnitud=Magnitud.VOLUMEN, factor_a_base=1
        )

    def setUp(self):
        self.ctx = tenant_context(self.boliche)
        self.ctx.__enter__()

        self.local = Local.objects.create(nombre="Barra A")
        self.barra = Deposito.objects.create(local=self.local, nombre="Barra 1")
        self.terminal = Terminal.objects.create(
            local=self.local,
            nombre="Barra 1",
            tipo=Terminal.Tipo.BARRA,
            deposito=self.barra,
        )
        self.ana = Usuario.objects.create_user(
            email=None, nombre="Ana", tenant=self.boliche, numero="0001"
        )
        self.encargado = Usuario.objects.create_user(
            email=None, nombre="Enzo", tenant=self.boliche, numero="0002"
        )

        self.ron = Insumo.objects.create(nombre="Ron", unidad_base=self.ml)
        self.cola = Insumo.objects.create(nombre="Cola", unidad_base=self.ml)
        self.categoria = CategoriaProducto.objects.create(nombre="Tragos")
        self.cuba = Producto.objects.create(
            categoria=self.categoria, nombre="Cuba libre", precio=Decimal("350.00")
        )
        receta = Receta.objects.create(producto=self.cuba)
        RecetaItem.objects.create(
            receta=receta, insumo=self.ron, cantidad=Decimal("50"), unidad=self.ml
        )
        RecetaItem.objects.create(
            receta=receta, insumo=self.cola, cantidad=Decimal("150"), unidad=self.ml
        )

        self.turno = abrir_turno(local=self.local, nombre="Viernes")
        self.sesion = abrir_caja(
            turno=self.turno,
            terminal=self.terminal,
            usuario=self.ana,
            fondo_inicial=Decimal("2000"),
        )

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def vender(self, cantidad=1, **kw):
        return registrar_venta(
            sesion=self.sesion,
            items=[{"producto": self.cuba, "cantidad": cantidad}],
            usuario=self.ana,
            **kw,
        )


class AperturaDeCajaTests(BaseVentasTests):
    def test_no_puede_haber_dos_cajas_abiertas_en_la_misma_terminal(self):
        with self.assertRaises(CajaYaAbierta):
            abrir_caja(turno=self.turno, terminal=self.terminal, usuario=self.encargado)

    def test_la_invariante_tambien_esta_en_la_base(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SesionCaja.objects.create(
                    tenant=self.boliche,
                    turno=self.turno,
                    terminal=self.terminal,
                    usuario=self.encargado,
                    abierta_en=timezone.now(),
                    estado=SesionCaja.Estado.ABIERTA,
                )

    def test_otra_terminal_si_puede_abrir(self):
        otra = Terminal.objects.create(
            local=self.local, nombre="Barra 2", tipo=Terminal.Tipo.BARRA
        )
        segunda = abrir_caja(turno=self.turno, terminal=otra, usuario=self.encargado)
        self.assertEqual(segunda.estado, SesionCaja.Estado.ABIERTA)


class VentaTests(BaseVentasTests):
    def test_la_venta_descuenta_el_stock_por_receta(self):
        self.vender()
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("-50"))
        self.assertEqual(saldo_calculado(self.barra, self.cola), Decimal("-150"))

    def test_el_folio_es_correlativo_por_sesion(self):
        primera = self.vender()
        segunda = self.vender()
        self.assertEqual((primera.numero, segunda.numero), (1, 2))

    def test_la_venta_nace_pendiente_hasta_cobrar(self):
        venta = self.vender()
        self.assertEqual(venta.estado, Venta.Estado.PENDIENTE)
        self.assertEqual(venta.total, Decimal("350.00"))

    def test_la_idempotencia_evita_duplicar_la_venta(self):
        primera = self.vender(idempotency_key="offline-1")
        segunda = self.vender(idempotency_key="offline-1")
        self.assertEqual(primera.id, segunda.id)
        self.assertEqual(Venta.objects.count(), 1)
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("-50"))

    def test_la_cantidad_multiplica_el_consumo(self):
        self.vender(cantidad=3)
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("-150"))

    def test_no_se_vende_con_la_caja_cerrada(self):
        cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("2000")},
            usuario=self.ana,
        )
        with self.assertRaises(ValidationError):
            self.vender()

    def test_una_venta_sin_items_no_existe(self):
        with self.assertRaises(ValidationError):
            registrar_venta(sesion=self.sesion, items=[], usuario=self.ana)

    def test_guarda_el_precio_del_momento(self):
        venta = self.vender()
        self.cuba.precio = Decimal("500.00")
        self.cuba.save(update_fields=["precio"])
        self.assertEqual(venta.items.first().precio_unitario, Decimal("350.00"))

    def test_vender_sin_punto_de_stock_falla_en_vez_de_mentir(self):
        sin_punto = Terminal.objects.create(
            local=self.local, nombre="Puerta", tipo=Terminal.Tipo.PUERTA
        )
        sesion = abrir_caja(turno=self.turno, terminal=sin_punto, usuario=self.ana)
        with self.assertRaises(ValidationError):
            registrar_venta(
                sesion=sesion,
                items=[{"producto": self.cuba, "cantidad": 1}],
                usuario=self.ana,
            )


class PagoTests(BaseVentasTests):
    def test_un_pago_por_el_total_deja_la_venta_pagada(self):
        venta = self.vender()
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("350"),
            estado=Pago.Estado.APROBADO,
        )
        venta.refresh_from_db()
        self.assertEqual(venta.estado, Venta.Estado.PAGADA)
        self.assertEqual(venta.saldo, Decimal("0"))

    def test_un_pago_parcial_deja_la_venta_pendiente(self):
        venta = self.vender()
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("200"),
            estado=Pago.Estado.APROBADO,
        )
        venta.refresh_from_db()
        self.assertEqual(venta.estado, Venta.Estado.PENDIENTE)
        self.assertEqual(venta.saldo, Decimal("150.00"))

    def test_se_puede_pagar_en_dos_medios(self):
        venta = self.vender()
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("150"),
            estado=Pago.Estado.APROBADO,
        )
        registrar_pago(
            venta=venta, medio=Pago.Medio.QR, monto=Decimal("200"),
            estado=Pago.Estado.APROBADO,
        )
        venta.refresh_from_db()
        self.assertEqual(venta.estado, Venta.Estado.PAGADA)

    def test_el_qr_es_asincronico(self):
        venta = self.vender()
        pago = registrar_pago(
            venta=venta, medio=Pago.Medio.QR, monto=Decimal("350"),
            referencia_externa="mp-123",
        )
        venta.refresh_from_db()
        self.assertEqual(venta.estado, Venta.Estado.PENDIENTE)

        confirmar_pago(pago=pago)
        venta.refresh_from_db()
        self.assertEqual(venta.estado, Venta.Estado.PAGADA)

    def test_confirmar_dos_veces_no_duplica_nada(self):
        venta = self.vender()
        pago = registrar_pago(venta=venta, medio=Pago.Medio.QR, monto=Decimal("350"))
        confirmar_pago(pago=pago)
        confirmar_pago(pago=pago)
        venta.refresh_from_db()
        self.assertEqual(venta.total_pagado, Decimal("350.00"))

    def test_no_se_cobra_una_venta_anulada(self):
        venta = self.vender()
        anular_venta(venta=venta, usuario=self.encargado, motivo="Error de carga")
        with self.assertRaises(ValidationError):
            registrar_pago(venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("350"))

    def test_los_pagos_aprobados_suman_el_total(self):
        venta = self.vender(cantidad=2)
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("400"),
            estado=Pago.Estado.APROBADO,
        )
        registrar_pago(
            venta=venta, medio=Pago.Medio.QR, monto=Decimal("300"),
            estado=Pago.Estado.APROBADO,
        )
        venta.refresh_from_db()
        self.assertEqual(venta.total_pagado, venta.total)


class AnulacionTests(BaseVentasTests):
    def test_anular_exige_motivo(self):
        venta = self.vender()
        with self.assertRaises(ValidationError):
            anular_venta(venta=venta, usuario=self.encargado, motivo="")

    def test_anular_repone_el_stock(self):
        venta = self.vender()
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("-50"))
        anular_venta(venta=venta, usuario=self.encargado, motivo="Error de carga")
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("0"))

    def test_anular_no_borra_nada(self):
        venta = self.vender()
        anular_venta(venta=venta, usuario=self.encargado, motivo="Error de carga")
        self.assertTrue(Venta.objects.filter(pk=venta.pk).exists())
        self.assertEqual(StockMovimiento.objects.count(), 4)

    def test_anular_dos_veces_no_hace_nada_la_segunda(self):
        venta = self.vender()
        anular_venta(venta=venta, usuario=self.encargado, motivo="Error")
        anular_venta(venta=venta, usuario=self.encargado, motivo="Error")
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("0"))

    def test_quien_anula_y_el_motivo_quedan_registrados(self):
        venta = self.vender()
        anular_venta(venta=venta, usuario=self.encargado, motivo="Pidio mal")
        venta.refresh_from_db()
        self.assertEqual(venta.anulada_por, self.encargado)
        self.assertEqual(venta.motivo_anulacion, "Pidio mal")
        self.assertIsNotNone(venta.anulada_en)


class ArqueoTests(BaseVentasTests):
    def test_el_esperado_suma_fondo_y_ventas_y_resta_egresos(self):
        venta = self.vender()
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("350"),
            estado=Pago.Estado.APROBADO,
        )
        registrar_egreso(
            sesion=self.sesion, tipo=EgresoCaja.Tipo.RETIRO, monto=Decimal("500"),
            motivo="Retiro del encargado", usuario=self.ana,
        )
        esperado = calcular_esperado(self.sesion)
        self.assertEqual(esperado[Pago.Medio.EFECTIVO], Decimal("1850.00"))

    def test_cierre_sin_diferencia_no_exige_autorizante(self):
        venta = self.vender()
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("350"),
            estado=Pago.Estado.APROBADO,
        )
        arqueo = cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("2350")},
            usuario=self.ana,
        )
        self.assertEqual(arqueo.diferencia_total, Decimal("0"))
        self.sesion.refresh_from_db()
        self.assertEqual(self.sesion.estado, SesionCaja.Estado.CERRADA)

    def test_una_diferencia_exige_autorizante(self):
        with self.assertRaises(ValidationError):
            cerrar_caja(
                sesion=self.sesion,
                declarado_por_medio={"efectivo": Decimal("1900")},
                usuario=self.ana,
            )

    def test_nadie_autoriza_su_propia_diferencia(self):
        with self.assertRaises(ValidationError):
            cerrar_caja(
                sesion=self.sesion,
                declarado_por_medio={"efectivo": Decimal("1900")},
                usuario=self.ana,
                autorizado_por=self.ana,
            )

    def test_una_diferencia_autorizada_por_otro_queda_registrada(self):
        arqueo = cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("1900")},
            usuario=self.ana,
            autorizado_por=self.encargado,
        )
        self.assertEqual(arqueo.diferencia_total, Decimal("-100.00"))
        self.assertEqual(arqueo.autorizado_por, self.encargado)

    def test_el_arqueo_desglosa_por_medio_de_pago(self):
        venta = self.vender()
        registrar_pago(
            venta=venta, medio=Pago.Medio.EFECTIVO, monto=Decimal("150"),
            estado=Pago.Estado.APROBADO,
        )
        registrar_pago(
            venta=venta, medio=Pago.Medio.QR, monto=Decimal("200"),
            estado=Pago.Estado.APROBADO,
        )
        arqueo = cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("2150"), "qr": Decimal("200")},
            usuario=self.ana,
        )
        self.assertEqual(arqueo.lineas.count(), len(Pago.Medio.choices))
        self.assertEqual(
            arqueo.lineas.get(medio=Pago.Medio.QR).esperado, Decimal("200.00")
        )

    def test_no_se_cierra_dos_veces(self):
        cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("2000")},
            usuario=self.ana,
        )
        with self.assertRaises(ValidationError):
            cerrar_caja(
                sesion=self.sesion,
                declarado_por_medio={"efectivo": Decimal("2000")},
                usuario=self.ana,
            )

    def test_al_cerrar_la_ultima_caja_se_cierra_el_turno(self):
        cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("2000")},
            usuario=self.ana,
        )
        self.turno.refresh_from_db()
        self.assertEqual(self.turno.estado, Turno.Estado.CERRADO)


class CobroTardioTests(BaseVentasTests):
    def test_un_pago_confirmado_despues_del_cierre_no_reescribe_el_arqueo(self):
        venta = self.vender()
        pago = registrar_pago(
            venta=venta, medio=Pago.Medio.QR, monto=Decimal("350"),
            referencia_externa="mp-999",
        )

        arqueo = cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("2000")},
            usuario=self.ana,
        )
        antes = arqueo.lineas.get(medio=Pago.Medio.QR).esperado
        self.assertEqual(antes, Decimal("0"))

        confirmar_pago(pago=pago)
        pago.refresh_from_db()
        self.assertTrue(pago.cobro_tardio)

        arqueo.refresh_from_db()
        self.assertEqual(arqueo.lineas.get(medio=Pago.Medio.QR).esperado, Decimal("0"))
        self.assertEqual(arqueo.diferencia_total, Decimal("0"))

    def test_el_cobro_tardio_queda_en_el_resumen(self):
        venta = self.vender()
        pago = registrar_pago(venta=venta, medio=Pago.Medio.QR, monto=Decimal("350"))
        cerrar_caja(
            sesion=self.sesion,
            declarado_por_medio={"efectivo": Decimal("2000")},
            usuario=self.ana,
        )
        confirmar_pago(pago=pago)
        resumen = resumen_de_ventas(self.sesion)
        self.assertEqual(resumen["por_medio"][Pago.Medio.QR], Decimal("350.00"))


class ResumenTests(BaseVentasTests):
    def test_el_resumen_separa_vendido_de_anulado(self):
        primera = self.vender()
        registrar_pago(
            venta=primera, medio=Pago.Medio.EFECTIVO, monto=Decimal("350"),
            estado=Pago.Estado.APROBADO,
        )
        segunda = self.vender()
        anular_venta(venta=segunda, usuario=self.encargado, motivo="Error")

        resumen = resumen_de_ventas(self.sesion)
        self.assertEqual(resumen["cantidad_ventas"], 1)
        self.assertEqual(resumen["total_vendido"], Decimal("350.00"))
        self.assertEqual(resumen["cantidad_anuladas"], 1)
        self.assertEqual(resumen["total_anulado"], Decimal("350.00"))


class AislamientoDeCajaTests(BaseVentasTests):
    def test_las_ventas_de_otro_boliche_no_se_ven(self):
        self.vender()
        otro = Tenant.objects.create(nombre="Boliche B", slug="boliche-b")
        with tenant_context(otro):
            self.assertEqual(Venta.objects.count(), 0)
            self.assertEqual(SesionCaja.objects.count(), 0)
            self.assertEqual(Arqueo.objects.count(), 0)
