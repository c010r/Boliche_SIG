"""Catalogo: unidades, insumos, productos, recetas y combos (ANALISIS.md seccion 5).

Dos decisiones que no se pueden posponer y que viven aca:

1. Las unidades tienen magnitud y factor, para poder recetar "50 ml de ron" contra
   stock de botellas. Sin esto el stock teorico nunca cuadra.
2. Un combo y una receta son cosas distintas: la receta descuenta INSUMOS, el combo
   agrupa PRODUCTOS vendibles (cada uno con su propia receta).
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TenantModel, TimeStampedModel, UUIDModel


class Magnitud(models.TextChoices):
    VOLUMEN = "volumen", "Volumen"
    MASA = "masa", "Masa"
    UNIDAD = "unidad", "Unidad"


class UnidadMedida(UUIDModel, TimeStampedModel):
    """Unidad de medida global (no depende del boliche).

    factor_a_base expresa la unidad en la base de su magnitud: ml, g y unidad
    valen 1; litro vale 1000 (ml); kilo vale 1000 (g).
    """

    codigo = models.CharField(max_length=16, unique=True)
    nombre = models.CharField(max_length=60)
    magnitud = models.CharField(max_length=16, choices=Magnitud.choices)
    factor_a_base = models.DecimalField(max_digits=18, decimal_places=6, default=Decimal("1"))

    class Meta:
        verbose_name = "Unidad de medida"
        verbose_name_plural = "Unidades de medida"
        ordering = ["magnitud", "factor_a_base", "codigo"]

    def __str__(self) -> str:
        return self.codigo

    def convertir_a_base_de_magnitud(self, cantidad: Decimal) -> Decimal:
        return Decimal(cantidad) * self.factor_a_base


class Insumo(TenantModel):
    """Lo que se descuenta del stock: botella, hielo, vaso descartable."""

    nombre = models.CharField(max_length=150)
    unidad_base = models.ForeignKey(
        UnidadMedida,
        on_delete=models.PROTECT,
        related_name="insumos",
        help_text="Unidad en la que se lleva el stock de este insumo.",
    )
    # Costo promedio ponderado (seccion 3).
    costo_promedio = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal("0")
    )
    stock_minimo = models.DecimalField(
        max_digits=14, decimal_places=4, default=Decimal("0")
    )
    codigo_barras = models.CharField(max_length=64, blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Insumo"
        verbose_name_plural = "Insumos"
        ordering = ["nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "nombre"], name="insumo_nombre_unico_por_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.nombre

    def convertir_a_base(self, cantidad, unidad: UnidadMedida) -> Decimal:
        """Convierte una cantidad expresada en otra unidad a la unidad base.

        Rechaza magnitudes incompatibles: no se puede convertir mililitros a
        gramos, y aceptarlo en silencio es como se arruina un stock.
        """
        cantidad = Decimal(cantidad)
        if unidad.magnitud != self.unidad_base.magnitud:
            raise ValidationError(
                f"No se puede convertir {unidad.codigo} ({unidad.magnitud}) a "
                f"{self.unidad_base.codigo} ({self.unidad_base.magnitud})."
            )
        en_base_de_magnitud = unidad.convertir_a_base_de_magnitud(cantidad)
        return en_base_de_magnitud / self.unidad_base.factor_a_base

    def convertir_presentacion_a_base(self, cantidad, presentacion) -> Decimal:
        return Decimal(cantidad) * presentacion.factor_a_unidad_base


class InsumoPresentacion(TenantModel):
    """Como se compra o se cuenta un insumo: botella de 750 ml, caja de 12.

    Es lo que permite cargar stock en unidades de compra y vender en mililitros.
    """

    insumo = models.ForeignKey(
        Insumo, on_delete=models.CASCADE, related_name="presentaciones"
    )
    nombre = models.CharField(max_length=100, help_text="Botella, caja, fardo...")
    factor_a_unidad_base = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        help_text="Cuantas unidades base entran. Botella de 750 ml = 750 si la base es ml.",
    )
    codigo_barras = models.CharField(max_length=64, blank=True)
    es_compra = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Presentacion"
        verbose_name_plural = "Presentaciones"
        ordering = ["insumo__nombre", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "insumo", "nombre"],
                name="presentacion_unica_por_insumo",
            )
        ]

    def __str__(self) -> str:
        return f"{self.insumo.nombre} / {self.nombre}"

    def clean(self):
        super().clean()
        if self.factor_a_unidad_base is not None and self.factor_a_unidad_base <= 0:
            raise ValidationError(
                {"factor_a_unidad_base": "El factor tiene que ser mayor que cero."}
            )


class CategoriaProducto(TenantModel):
    """Agrupa la botonera de la barra. El orden es configurable a proposito."""

    nombre = models.CharField(max_length=100)
    orden = models.PositiveSmallIntegerField(default=0)
    color = models.CharField(max_length=16, blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Categoria de producto"
        verbose_name_plural = "Categorias de producto"
        ordering = ["orden", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "nombre"], name="categoria_nombre_unico_por_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.nombre


class Producto(TenantModel):
    """Lo que se le vende al cliente: un trago, una cerveza, un combo."""

    categoria = models.ForeignKey(
        CategoriaProducto, on_delete=models.PROTECT, related_name="productos"
    )
    nombre = models.CharField(max_length=150)
    precio = models.DecimalField(max_digits=12, decimal_places=2)
    tasa_iva = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("22.00")
    )
    es_combo = models.BooleanField(
        default=False, help_text="Si es combo, descuenta stock via sus productos hijos."
    )
    orden = models.PositiveSmallIntegerField(default=0)
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Producto"
        verbose_name_plural = "Productos"
        ordering = ["categoria__orden", "orden", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "nombre"], name="producto_nombre_unico_por_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.nombre


class Receta(TenantModel):
    """Insumos que descuenta un producto al venderse.

    Aca vive el "algoritmo de stock" del rubro: no es magia, es descontar por
    receta en vez de por unidad de producto terminado.
    """

    producto = models.OneToOneField(
        Producto, on_delete=models.CASCADE, related_name="receta"
    )
    activa = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Receta"
        verbose_name_plural = "Recetas"

    def __str__(self) -> str:
        return f"Receta de {self.producto.nombre}"


class RecetaItem(TenantModel):
    receta = models.ForeignKey(Receta, on_delete=models.CASCADE, related_name="items")
    insumo = models.ForeignKey(
        Insumo, on_delete=models.PROTECT, related_name="receta_items"
    )
    cantidad = models.DecimalField(max_digits=14, decimal_places=4)
    unidad = models.ForeignKey(
        UnidadMedida, on_delete=models.PROTECT, related_name="receta_items"
    )
    merma_pct = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("0"),
        help_text="Porcentaje que se suma por derrame o descarte.",
    )

    class Meta:
        verbose_name = "Item de receta"
        verbose_name_plural = "Items de receta"
        ordering = ["receta__producto__nombre", "insumo__nombre"]

    def __str__(self) -> str:
        return f"{self.cantidad} {self.unidad.codigo} de {self.insumo.nombre}"

    def clean(self):
        super().clean()
        if self.insumo_id and self.unidad_id:
            if self.unidad.magnitud != self.insumo.unidad_base.magnitud:
                raise ValidationError(
                    {
                        "unidad": (
                            f"La receta usa {self.unidad.codigo} "
                            f"({self.unidad.magnitud}) pero el insumo se lleva en "
                            f"{self.insumo.unidad_base.codigo} "
                            f"({self.insumo.unidad_base.magnitud})."
                        )
                    }
                )
        if self.cantidad is not None and self.cantidad <= 0:
            raise ValidationError({"cantidad": "La cantidad tiene que ser mayor que cero."})

    def cantidad_en_unidad_base(self) -> Decimal:
        """Cantidad a descontar del stock, en la unidad base del insumo."""
        neta = self.insumo.convertir_a_base(self.cantidad, self.unidad)
        if self.merma_pct:
            neta = neta * (Decimal("1") + self.merma_pct / Decimal("100"))
        return neta


class ComboItem(TenantModel):
    """Producto hijo de un combo.

    Un combo agrupa productos vendibles; cada hijo descuenta su propia receta.
    """

    combo = models.ForeignKey(
        Producto, on_delete=models.CASCADE, related_name="combo_items"
    )
    producto_hijo = models.ForeignKey(
        Producto, on_delete=models.PROTECT, related_name="en_combos"
    )
    cantidad = models.PositiveSmallIntegerField(default=1)

    class Meta:
        verbose_name = "Item de combo"
        verbose_name_plural = "Items de combo"
        ordering = ["combo__nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "combo", "producto_hijo"], name="combo_item_unico"
            )
        ]

    def __str__(self) -> str:
        return f"{self.cantidad} x {self.producto_hijo.nombre}"

    def clean(self):
        super().clean()
        if self.combo_id and self.producto_hijo_id and self.combo_id == self.producto_hijo_id:
            raise ValidationError("Un combo no puede contenerse a si mismo.")
        if self.combo_id and not self.combo.es_combo:
            raise ValidationError({"combo": "El producto padre no esta marcado como combo."})
