"""Invariantes del ledger de stock (ANALISIS.md seccion 5).

Los tres que importan:
1. Un movimiento no se modifica ni se borra.
2. El saldo del StockItem es la suma del ledger.
3. Un reintento idempotente no duplica el descuento.
"""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.catalog.models import (
    CategoriaProducto,
    ComboItem,
    Insumo,
    Magnitud,
    Producto,
    Receta,
    RecetaItem,
    UnidadMedida,
)
from apps.core.context import TenantNotSetError, tenant_context
from apps.stock.models import (
    AppendOnlyError,
    Deposito,
    StockItem,
    StockMovimiento,
)
from apps.stock.services import (
    descontar_venta,
    insumos_de_producto,
    registrar_movimiento,
    saldo_calculado,
)
from apps.tenancy.models import Local, Tenant


class BaseStockTests(TestCase):
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
        self.ron = Insumo.objects.create(nombre="Ron", unidad_base=self.ml)
        self.categoria = CategoriaProducto.objects.create(nombre="Tragos")

    def tearDown(self):
        self.ctx.__exit__(None, None, None)


class AppendOnlyTests(BaseStockTests):
    def test_no_se_puede_modificar_un_movimiento(self):
        mov = registrar_movimiento(
            deposito=self.barra,
            insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA,
            cantidad=Decimal("750"),
        )
        mov.cantidad = Decimal("1")
        with self.assertRaises(AppendOnlyError):
            mov.save()

    def test_no_se_puede_borrar_un_movimiento(self):
        mov = registrar_movimiento(
            deposito=self.barra,
            insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA,
            cantidad=Decimal("750"),
        )
        with self.assertRaises(AppendOnlyError):
            mov.delete()

    def test_no_se_puede_borrar_en_masa(self):
        registrar_movimiento(
            deposito=self.barra,
            insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA,
            cantidad=Decimal("750"),
        )
        with self.assertRaises(AppendOnlyError):
            StockMovimiento.objects.all().delete()

    def test_no_se_puede_actualizar_en_masa(self):
        registrar_movimiento(
            deposito=self.barra,
            insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA,
            cantidad=Decimal("750"),
        )
        with self.assertRaises(AppendOnlyError):
            StockMovimiento.objects.all().update(cantidad=Decimal("0"))

    def test_anular_es_asentar_el_contrario(self):
        registrar_movimiento(
            deposito=self.barra,
            insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA,
            cantidad=Decimal("750"),
        )
        registrar_movimiento(
            deposito=self.barra,
            insumo=self.ron,
            tipo=StockMovimiento.Tipo.MERMA,
            cantidad=Decimal("-750"),
            motivo="Botella rota",
        )
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("0"))
        self.assertEqual(StockMovimiento.objects.count(), 2)


class SaldoTests(BaseStockTests):
    def test_el_saldo_es_la_suma_del_ledger(self):
        registrar_movimiento(
            deposito=self.barra, insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("1500"),
        )
        registrar_movimiento(
            deposito=self.barra, insumo=self.ron,
            tipo=StockMovimiento.Tipo.VENTA, cantidad=Decimal("-50"),
        )
        item = StockItem.objects.get(deposito=self.barra, insumo=self.ron)
        self.assertEqual(item.cantidad, Decimal("1450"))
        self.assertEqual(item.cantidad, saldo_calculado(self.barra, self.ron))

    def test_el_saldo_arranca_en_cero(self):
        registrar_movimiento(
            deposito=self.barra, insumo=self.ron,
            tipo=StockMovimiento.Tipo.VENTA, cantidad=Decimal("-50"),
        )
        item = StockItem.objects.get(deposito=self.barra, insumo=self.ron)
        self.assertEqual(item.cantidad, Decimal("-50"))

    def test_el_signo_se_valida_segun_el_tipo(self):
        with self.assertRaises(ValidationError):
            registrar_movimiento(
                deposito=self.barra, insumo=self.ron,
                tipo=StockMovimiento.Tipo.VENTA, cantidad=Decimal("50"),
            )
        with self.assertRaises(ValidationError):
            registrar_movimiento(
                deposito=self.barra, insumo=self.ron,
                tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("-50"),
            )

    def test_no_se_admite_cantidad_cero(self):
        with self.assertRaises(ValidationError):
            registrar_movimiento(
                deposito=self.barra, insumo=self.ron,
                tipo=StockMovimiento.Tipo.AJUSTE, cantidad=Decimal("0"),
            )

    def test_el_ajuste_admite_los_dos_signos(self):
        registrar_movimiento(
            deposito=self.barra, insumo=self.ron,
            tipo=StockMovimiento.Tipo.AJUSTE, cantidad=Decimal("100"),
            motivo="Correccion",
        )
        registrar_movimiento(
            deposito=self.barra, insumo=self.ron,
            tipo=StockMovimiento.Tipo.AJUSTE, cantidad=Decimal("-40"),
            motivo="Correccion",
        )
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("60"))


class IdempotenciaTests(BaseStockTests):
    def test_la_misma_clave_no_duplica_el_movimiento(self):
        primero = registrar_movimiento(
            deposito=self.barra, insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("750"),
            idempotency_key="venta-123",
        )
        segundo = registrar_movimiento(
            deposito=self.barra, insumo=self.ron,
            tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("750"),
            idempotency_key="venta-123",
        )
        self.assertEqual(primero.id, segundo.id)
        self.assertEqual(StockMovimiento.objects.count(), 1)
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("750"))

    def test_claves_distintas_si_duplican(self):
        for clave in ("a", "b"):
            registrar_movimiento(
                deposito=self.barra, insumo=self.ron,
                tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("750"),
                idempotency_key=clave,
            )
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("1500"))


class VentaPorRecetaTests(BaseStockTests):
    def setUp(self):
        super().setUp()
        self.cola = Insumo.objects.create(nombre="Cola", unidad_base=self.ml)
        self.cuba = Producto.objects.create(
            categoria=self.categoria, nombre="Cuba libre", precio=Decimal("350.00")
        )
        self.receta = Receta.objects.create(producto=self.cuba)
        RecetaItem.objects.create(
            receta=self.receta, insumo=self.ron, cantidad=Decimal("50"), unidad=self.ml
        )
        RecetaItem.objects.create(
            receta=self.receta, insumo=self.cola, cantidad=Decimal("150"), unidad=self.ml
        )

    def test_vender_descuenta_los_insumos_de_la_receta(self):
        descontar_venta(
            deposito=self.barra, producto=self.cuba, cantidad=1,
            idempotency_key="venta-1",
        )
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("-50"))
        self.assertEqual(saldo_calculado(self.barra, self.cola), Decimal("-150"))

    def test_el_reintento_offline_no_descuenta_dos_veces(self):
        for _ in range(3):
            descontar_venta(
                deposito=self.barra, producto=self.cuba, cantidad=1,
                idempotency_key="venta-1",
            )
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("-50"))

    def test_un_producto_sin_receta_no_mueve_stock(self):
        agua = Producto.objects.create(
            categoria=self.categoria, nombre="Agua", precio=Decimal("100.00")
        )
        descontar_venta(deposito=self.barra, producto=agua)
        self.assertEqual(StockMovimiento.objects.count(), 0)

    def test_la_receta_inactiva_no_descuenta(self):
        self.receta.activa = False
        self.receta.save(update_fields=["activa"])
        self.assertEqual(insumos_de_producto(self.cuba), {})


class ComboTests(BaseStockTests):
    def setUp(self):
        super().setUp()
        self.cola = Insumo.objects.create(nombre="Cola", unidad_base=self.ml)
        self.vaso = Insumo.objects.create(nombre="Vaso", unidad_base=self.ml)

        self.trago = Producto.objects.create(
            categoria=self.categoria, nombre="Cuba libre", precio=Decimal("350.00")
        )
        receta = Receta.objects.create(producto=self.trago)
        RecetaItem.objects.create(
            receta=receta, insumo=self.ron, cantidad=Decimal("50"), unidad=self.ml
        )

        self.papas = Producto.objects.create(
            categoria=self.categoria, nombre="Papas", precio=Decimal("180.00")
        )
        receta_papas = Receta.objects.create(producto=self.papas)
        RecetaItem.objects.create(
            receta=receta_papas, insumo=self.vaso, cantidad=Decimal("1"), unidad=self.ml
        )

        self.combo = Producto.objects.create(
            categoria=self.categoria, nombre="Trago + papas",
            precio=Decimal("480.00"), es_combo=True,
        )
        ComboItem.objects.create(combo=self.combo, producto_hijo=self.trago, cantidad=1)
        ComboItem.objects.create(combo=self.combo, producto_hijo=self.papas, cantidad=1)

    def test_el_combo_expande_a_los_insumos_de_sus_hijos(self):
        consumos = insumos_de_producto(self.combo)
        self.assertEqual(consumos[str(self.ron.id)], Decimal("50"))
        self.assertEqual(consumos[str(self.vaso.id)], Decimal("1"))

    def test_vender_un_combo_descuenta_todo(self):
        descontar_venta(deposito=self.barra, producto=self.combo)
        self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("-50"))
        self.assertEqual(saldo_calculado(self.barra, self.vaso), Decimal("-1"))

    def test_las_cantidades_se_multiplican(self):
        consumos = insumos_de_producto(self.combo, cantidad=3)
        self.assertEqual(consumos[str(self.ron.id)], Decimal("150"))

    def test_un_combo_ciclico_se_detecta(self):
        combo_b = Producto.objects.create(
            categoria=self.categoria, nombre="Combo B", precio=Decimal("100.00"), es_combo=True
        )
        ComboItem.objects.create(combo=self.combo, producto_hijo=combo_b, cantidad=1)
        ComboItem.objects.create(combo=combo_b, producto_hijo=self.combo, cantidad=1)
        with self.assertRaises(ValueError):
            insumos_de_producto(self.combo)


class AislamientoDeStockTests(BaseStockTests):
    def test_stock_de_otro_boliche_no_se_ve(self):
        otro = Tenant.objects.create(nombre="Boliche B", slug="boliches-b")

        with tenant_context(otro):
            local_b = Local.objects.create(nombre="Barra B")
            deposito_b = Deposito.objects.create(local=local_b, nombre="Barra 1")
            ron_b = Insumo.objects.create(nombre="Ron", unidad_base=self.ml)
            registrar_movimiento(
                deposito=deposito_b, insumo=ron_b,
                tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("999"),
            )
            self.assertEqual(StockMovimiento.objects.count(), 1)

        # De vuelta en el boliche A.
        self.assertEqual(StockMovimiento.objects.count(), 0)
        self.assertEqual(Deposito.objects.count(), 1)

    def test_operar_stock_sin_tenant_revienta(self):
        self.ctx.__exit__(None, None, None)
        with self.assertRaises(TenantNotSetError):
            registrar_movimiento(
                deposito=self.barra, insumo=self.ron,
                tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("1"),
            )
        self.ctx = tenant_context(self.boliche)
        self.ctx.__enter__()
