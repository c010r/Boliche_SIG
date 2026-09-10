"""El stock teorico solo cuadra si las unidades cierran (ANALISIS.md seccion 5)."""

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from apps.catalog.models import (
    CategoriaProducto,
    ComboItem,
    Insumo,
    InsumoPresentacion,
    Magnitud,
    Producto,
    Receta,
    RecetaItem,
    UnidadMedida,
)
from apps.core.context import tenant_context
from apps.tenancy.models import Tenant


class BaseCatalogTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.boliche = Tenant.objects.create(nombre="Boliche A", slug="boliche-a")
        cls.ml = UnidadMedida.objects.create(
            codigo="ml", nombre="Mililitro", magnitud=Magnitud.VOLUMEN, factor_a_base=1
        )
        cls.litro = UnidadMedida.objects.create(
            codigo="l", nombre="Litro", magnitud=Magnitud.VOLUMEN, factor_a_base=1000
        )
        cls.gramo = UnidadMedida.objects.create(
            codigo="g", nombre="Gramo", magnitud=Magnitud.MASA, factor_a_base=1
        )
        cls.unidad = UnidadMedida.objects.create(
            codigo="u", nombre="Unidad", magnitud=Magnitud.UNIDAD, factor_a_base=1
        )


class ConversionTests(BaseCatalogTests):
    def setUp(self):
        self.ctx = tenant_context(self.boliche)
        self.ctx.__enter__()
        self.ron = Insumo.objects.create(nombre="Ron", unidad_base=self.ml)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_convierte_litros_a_mililitros(self):
        self.assertEqual(self.ron.convertir_a_base(Decimal("1.5"), self.litro), Decimal("1500"))

    def test_misma_unidad_no_cambia(self):
        self.assertEqual(self.ron.convertir_a_base(Decimal("50"), self.ml), Decimal("50"))

    def test_rechaza_magnitudes_incompatibles(self):
        with self.assertRaises(ValidationError):
            self.ron.convertir_a_base(Decimal("50"), self.gramo)

    def test_presentacion_botella_a_unidad_base(self):
        botella = InsumoPresentacion.objects.create(
            insumo=self.ron, nombre="Botella", factor_a_unidad_base=Decimal("750")
        )
        self.assertEqual(
            self.ron.convertir_presentacion_a_base(2, botella), Decimal("1500")
        )

    def test_insumo_medido_en_litros_convierte_desde_mililitros(self):
        cola = Insumo.objects.create(nombre="Cola", unidad_base=self.litro)
        self.assertEqual(cola.convertir_a_base(Decimal("150"), self.ml), Decimal("0.15"))


class RecetaTests(BaseCatalogTests):
    def setUp(self):
        self.ctx = tenant_context(self.boliche)
        self.ctx.__enter__()
        self.categoria = CategoriaProducto.objects.create(nombre="Tragos")
        self.ron = Insumo.objects.create(nombre="Ron", unidad_base=self.ml)
        self.cola = Insumo.objects.create(nombre="Cola", unidad_base=self.ml)
        self.cuba = Producto.objects.create(
            categoria=self.categoria, nombre="Cuba libre", precio=Decimal("350.00")
        )
        self.receta = Receta.objects.create(producto=self.cuba)

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_item_de_receta_se_convierte_a_unidad_base(self):
        item = RecetaItem.objects.create(
            receta=self.receta, insumo=self.ron, cantidad=Decimal("50"), unidad=self.ml
        )
        self.assertEqual(item.cantidad_en_unidad_base(), Decimal("50"))

    def test_la_merma_se_suma(self):
        item = RecetaItem.objects.create(
            receta=self.receta,
            insumo=self.ron,
            cantidad=Decimal("50"),
            unidad=self.ml,
            merma_pct=Decimal("10"),
        )
        self.assertEqual(item.cantidad_en_unidad_base(), Decimal("55"))

    def test_receta_con_magnitud_incompatible_no_valida(self):
        item = RecetaItem(
            receta=self.receta, insumo=self.ron, cantidad=Decimal("50"), unidad=self.gramo
        )
        with self.assertRaises(ValidationError):
            item.full_clean()

    def test_cantidad_cero_no_valida(self):
        item = RecetaItem(
            receta=self.receta, insumo=self.ron, cantidad=Decimal("0"), unidad=self.ml
        )
        with self.assertRaises(ValidationError):
            item.full_clean()


class ComboModeloTests(BaseCatalogTests):
    def setUp(self):
        self.ctx = tenant_context(self.boliche)
        self.ctx.__enter__()
        self.categoria = CategoriaProducto.objects.create(nombre="Promos")
        self.cerveza = Producto.objects.create(
            categoria=self.categoria, nombre="Cerveza", precio=Decimal("200.00")
        )
        self.pizza = Producto.objects.create(
            categoria=self.categoria, nombre="Pizza", precio=Decimal("400.00")
        )
        self.combo = Producto.objects.create(
            categoria=self.categoria,
            nombre="Cerveza + pizza",
            precio=Decimal("520.00"),
            es_combo=True,
        )

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def test_combo_agrupa_productos_y_no_insumos(self):
        ComboItem.objects.create(combo=self.combo, producto_hijo=self.cerveza, cantidad=1)
        ComboItem.objects.create(combo=self.combo, producto_hijo=self.pizza, cantidad=1)
        self.assertEqual(self.combo.combo_items.count(), 2)
        # El combo no lleva receta propia: descuenta a traves de sus hijos.
        self.assertFalse(Receta.objects.filter(producto=self.combo).exists())

    def test_un_producto_simple_puede_tener_receta(self):
        receta = Receta.objects.create(producto=self.cerveza)
        self.assertEqual(receta.producto, self.cerveza)
