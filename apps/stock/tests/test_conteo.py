"""Conteo guiado ciego y varianza (ANALISIS.md seccion 11 ter).

Lo que se verifica:
- el teorico NO se revela mientras el conteo esta abierto;
- al cerrar, la diferencia se asienta y el saldo queda igual a lo contado;
- un deposito sin conteo se reporta como sin conteo, no como cero;
- la varianza se ordena por impacto en plata;
- el sistema cronometra el conteo.
"""

from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import Usuario
from apps.catalog.models import (
    Insumo,
    InsumoPresentacion,
    Magnitud,
    UnidadMedida,
)
from apps.core.context import tenant_context
from apps.stock.conteo import (
    ConteoYaCerrado,
    cerrar_conteo,
    iniciar_conteo,
    registrar_item,
)
from apps.stock.models import (
    AjusteInventario,
    Conteo,
    Deposito,
    StockMovimiento,
)
from apps.stock.reportes import reporte_de_varianza
from apps.stock.services import registrar_movimiento, saldo_calculado
from apps.tenancy.models import Local, Tenant


class BaseConteoTests(TestCase):
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
        self.deposito = Deposito.objects.create(local=self.local, nombre="Deposito")
        self.ana = Usuario.objects.create_user(
            email=None, nombre="Ana", tenant=self.boliche, numero="0001"
        )
        self.encargado = Usuario.objects.create_user(
            email=None, nombre="Enzo", tenant=self.boliche, numero="0002"
        )

        self.ron = Insumo.objects.create(
            nombre="Ron", unidad_base=self.ml, costo_promedio=Decimal("1.20")
        )
        self.botella = InsumoPresentacion.objects.create(
            insumo=self.ron, nombre="Botella", factor_a_unidad_base=Decimal("750")
        )

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def cargar_stock(self, cantidad, insumo=None, deposito=None):
        return registrar_movimiento(
            deposito=deposito or self.barra,
            insumo=insumo or self.ron,
            tipo=StockMovimiento.Tipo.COMPRA,
            cantidad=Decimal(str(cantidad)),
        )


class ConteoCiegoTests(BaseConteoTests):
    def test_mientras_esta_abierto_no_hay_teorico(self):
        self.cargar_stock(1500)  # 2 botellas
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        item = registrar_item(
            conteo=conteo,
            insumo=self.ron,
            unidades_cerradas=Decimal("1"),
            fraccion_abierta=Decimal("0.5"),
            presentacion=self.botella,
        )
        self.assertIsNone(item.cantidad_teorica)
        self.assertIsNone(item.diferencia)
        self.assertIsNone(item.cantidad_declarada)

    def test_al_cerrar_se_revela_el_teorico(self):
        self.cargar_stock(1500)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron,
            unidades_cerradas=Decimal("1"), fraccion_abierta=Decimal("0.5"),
            presentacion=self.botella,
        )
        cerrar_conteo(conteo=conteo)
        item = conteo.items.get(insumo=self.ron)
        self.assertEqual(item.cantidad_teorica, Decimal("1500"))
        self.assertEqual(item.cantidad_declarada, Decimal("1125"))
        self.assertEqual(item.diferencia, Decimal("-375"))

    def test_la_declarada_suma_selladas_y_fraccion(self):
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron,
            unidades_cerradas=Decimal("2"), fraccion_abierta=Decimal("0.25"),
            presentacion=self.botella,
        )
        item = conteo.items.get()
        self.assertEqual(item.calcular_declarada(), Decimal("1687.5000"))


class CierreDeConteoTests(BaseConteoTests):
    def test_el_saldo_queda_igual_a_lo_contado(self):
        self.cargar_stock(1500)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron,
            unidades_cerradas=Decimal("1"), fraccion_abierta=Decimal("0.5"),
            presentacion=self.botella,
        )
        cerrar_conteo(conteo=conteo)
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("1125.0000"))

    def test_la_diferencia_queda_como_movimiento_de_inventario(self):
        self.cargar_stock(1500)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron,
            unidades_cerradas=Decimal("1"), fraccion_abierta=Decimal("0.5"),
            presentacion=self.botella,
        )
        cerrar_conteo(conteo=conteo)
        ajuste = StockMovimiento.objects.get(tipo=StockMovimiento.Tipo.INVENTARIO)
        self.assertEqual(ajuste.cantidad, Decimal("-375.0000"))
        self.assertEqual(ajuste.referencia_tipo, "conteo")

    def test_se_registra_el_ajuste_con_su_motivo(self):
        self.cargar_stock(1500)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron,
            unidades_cerradas=Decimal("1"), fraccion_abierta=Decimal("0.5"),
            presentacion=self.botella,
        )
        cerrar_conteo(conteo=conteo, motivos={str(self.ron.id): "Botella rota"})
        ajuste = AjusteInventario.objects.get()
        self.assertEqual(ajuste.motivo, "Botella rota")
        self.assertEqual(ajuste.diferencia, Decimal("-375.0000"))

    def test_sin_diferencia_no_hay_ajuste(self):
        self.cargar_stock(750)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron,
            unidades_cerradas=Decimal("1"), fraccion_abierta=Decimal("0"),
            presentacion=self.botella,
        )
        cerrar_conteo(conteo=conteo)
        self.assertEqual(AjusteInventario.objects.count(), 0)
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("750"))

    def test_el_sistema_cronometra_el_conteo(self):
        inicio = timezone.now() - timedelta(minutes=18)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana, momento=inicio)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"))
        cerrado = cerrar_conteo(conteo=conteo, momento=inicio + timedelta(minutes=17))
        self.assertEqual(cerrado.duracion_seg, 17 * 60)

    def test_no_se_cierra_dos_veces(self):
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"))
        cerrar_conteo(conteo=conteo)
        with self.assertRaises(ConteoYaCerrado):
            cerrar_conteo(conteo=conteo)

    def test_no_se_cuenta_sobre_un_conteo_cerrado(self):
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"))
        cerrar_conteo(conteo=conteo)
        with self.assertRaises(ConteoYaCerrado):
            registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("1"))

    def test_la_fraccion_tiene_que_estar_entre_cero_y_uno(self):
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        with self.assertRaises(ValidationError):
            registrar_item(
                conteo=conteo, insumo=self.ron, fraccion_abierta=Decimal("1.5")
            )


class UmbralDeAutorizacionTests(BaseConteoTests):
    def test_una_diferencia_grande_exige_autorizante(self):
        self.cargar_stock(3000)  # 4 botellas, 3600 de costo
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"),
            presentacion=self.botella,
        )
        with self.assertRaises(ValidationError):
            cerrar_conteo(conteo=conteo)

    def test_con_autorizante_cierra(self):
        self.cargar_stock(3000)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(
            conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"),
            presentacion=self.botella,
        )
        cerrar_conteo(conteo=conteo, autorizado_por=self.encargado)
        ajuste = AjusteInventario.objects.get()
        self.assertEqual(ajuste.autorizado_por, self.encargado)

    @override_settings(AJUSTE_UMBRAL_AUTORIZACION="0")
    def test_con_umbral_cero_toda_diferencia_exige_autorizante(self):
        self.cargar_stock(100)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"))
        with self.assertRaises(ValidationError):
            cerrar_conteo(conteo=conteo)


class ReporteDeVarianzaTests(BaseConteoTests):
    def test_no_medido_no_es_cero(self):
        reporte = reporte_de_varianza()
        nombres = [d["deposito"] for d in reporte["depositos_sin_conteo"]]
        self.assertIn("Barra 1", nombres)
        self.assertIn("Deposito", nombres)
        self.assertEqual(reporte["filas"], [])

    def test_ordena_por_impacto_en_plata(self):
        vodka = Insumo.objects.create(
            nombre="Vodka", unidad_base=self.ml, costo_promedio=Decimal("3.00")
        )
        self.cargar_stock(100, insumo=self.ron)
        self.cargar_stock(100, insumo=vodka)

        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("50"))
        registrar_item(conteo=conteo, insumo=vodka, unidades_cerradas=Decimal("50"))
        cerrar_conteo(conteo=conteo)

        # Ron: 50 ml menos a 1,20 = -60. Vodka: 50 ml menos a 3,00 = -150.
        filas = reporte_de_varianza()["filas"]
        self.assertEqual(filas[0]["insumo"], "Vodka")
        self.assertEqual(filas[0]["valorizado"], Decimal("-150.0000"))
        self.assertEqual(filas[1]["valorizado"], Decimal("-60.0000"))

    def test_el_deposito_contado_no_aparece_como_sin_conteo(self):
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"))
        cerrar_conteo(conteo=conteo)

        reporte = reporte_de_varianza()
        nombres = [d["deposito"] for d in reporte["depositos_sin_conteo"]]
        self.assertNotIn("Barra 1", nombres)
        self.assertIn("Deposito", nombres)

    def test_el_reporte_trae_la_duracion_del_conteo(self):
        inicio = timezone.now() - timedelta(minutes=12)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana, momento=inicio)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"))
        cerrar_conteo(conteo=conteo, momento=inicio + timedelta(minutes=12))
        self.assertEqual(reporte_de_varianza()["duracion_promedio_seg"], 720)

    def test_la_varianza_valorizada_es_la_suma(self):
        self.cargar_stock(1000)
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("800"))
        cerrar_conteo(conteo=conteo)
        self.assertEqual(
            reporte_de_varianza()["varianza_valorizada"], Decimal("-240.0000")
        )


class AislamientoDeConteoTests(BaseConteoTests):
    def test_los_conteos_de_otro_boliche_no_se_ven(self):
        conteo = iniciar_conteo(deposito=self.barra, usuario=self.ana)
        registrar_item(conteo=conteo, insumo=self.ron, unidades_cerradas=Decimal("0"))
        cerrar_conteo(conteo=conteo)

        otro = Tenant.objects.create(nombre="Boliche B", slug="boliche-b")
        with tenant_context(otro):
            self.assertEqual(Conteo.objects.count(), 0)
            self.assertEqual(AjusteInventario.objects.count(), 0)
