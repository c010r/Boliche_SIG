"""Stock: depositos por punto, saldo proyectado y ledger de movimientos.

Decision central (ANALISIS.md seccion 4): los movimientos son un LEDGER
append-only. Nunca se editan ni se borran; anular es asentar el contrario. Eso
resuelve de una sola vez arqueo, reportes, sincronizacion offline y -mas
adelante- cashless.

El stock por PUNTO (Barra 1, Barra 2, Deposito) y no por local es lo que permite
atribuir una varianza a un turno y a una persona (seccion 11 ter).
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.managers import TenantManager, UnscopedManager
from apps.core.models import TenantModel


class AppendOnlyError(RuntimeError):
    """Se intento modificar o borrar un asiento del ledger."""


class MovimientoQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise AppendOnlyError(
            "Los movimientos de stock no se modifican. Asienta el movimiento contrario."
        )

    def delete(self):
        raise AppendOnlyError(
            "Los movimientos de stock no se borran. Asienta el movimiento contrario."
        )


class MovimientoManager(TenantManager.from_queryset(MovimientoQuerySet)):
    """Manager fail-closed del ledger, sin update ni delete."""


class Deposito(TenantModel):
    """Punto de stock: Deposito, Barra 1, Barra 2, VIP."""

    local = models.ForeignKey(
        "tenancy.Local", on_delete=models.PROTECT, related_name="depositos"
    )
    nombre = models.CharField(max_length=100)
    es_principal = models.BooleanField(
        default=False, help_text="El deposito central del local."
    )
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Deposito"
        verbose_name_plural = "Depositos"
        ordering = ["local__nombre", "nombre"]
        base_manager_name = "unscoped"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "local", "nombre"], name="deposito_unico_por_local"
            )
        ]

    def __str__(self) -> str:
        return f"{self.local.nombre} / {self.nombre}"


class StockItem(TenantModel):
    """Saldo por insumo x deposito. Es una PROYECCION del ledger.

    Se puede recalcular desde los movimientos, y los tests verifican que
    coincidan. Si alguna vez difieren, el ledger es la verdad.
    """

    deposito = models.ForeignKey(
        Deposito, on_delete=models.PROTECT, related_name="items"
    )
    insumo = models.ForeignKey(
        "catalog.Insumo", on_delete=models.PROTECT, related_name="stocks"
    )
    cantidad = models.DecimalField(
        max_digits=16, decimal_places=4, default=Decimal("0")
    )

    class Meta:
        verbose_name = "Saldo de stock"
        verbose_name_plural = "Saldos de stock"
        ordering = ["deposito__nombre", "insumo__nombre"]
        base_manager_name = "unscoped"
        default_manager_name = "objects"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "deposito", "insumo"], name="stock_item_unico"
            )
        ]

    def __str__(self) -> str:
        return f"{self.insumo.nombre} en {self.deposito.nombre}: {self.cantidad}"


class StockMovimiento(TenantModel):
    """Asiento inmutable del ledger de stock.

    cantidad va con signo: positiva suma al deposito, negativa resta.
    """

    class Tipo(models.TextChoices):
        COMPRA = "compra", "Compra"
        VENTA = "venta", "Venta"
        TRASLADO_ENTRADA = "traslado_entrada", "Traslado (entrada)"
        TRASLADO_SALIDA = "traslado_salida", "Traslado (salida)"
        MERMA = "merma", "Merma o rotura"
        CONSUMO_INTERNO = "consumo_interno", "Consumo interno"
        AJUSTE = "ajuste", "Ajuste"
        INVENTARIO = "inventario", "Conteo de inventario"

    SUMAN = {Tipo.COMPRA, Tipo.TRASLADO_ENTRADA}
    RESTAN = {Tipo.VENTA, Tipo.TRASLADO_SALIDA, Tipo.MERMA, Tipo.CONSUMO_INTERNO}

    deposito = models.ForeignKey(
        Deposito, on_delete=models.PROTECT, related_name="movimientos"
    )
    insumo = models.ForeignKey(
        "catalog.Insumo", on_delete=models.PROTECT, related_name="movimientos"
    )
    tipo = models.CharField(max_length=24, choices=Tipo.choices)
    cantidad = models.DecimalField(
        max_digits=16, decimal_places=4, help_text="Con signo, en unidad base."
    )
    costo_unitario = models.DecimalField(
        max_digits=14, decimal_places=4, null=True, blank=True
    )
    motivo = models.CharField(max_length=250, blank=True)
    referencia_tipo = models.CharField(max_length=60, blank=True)
    referencia_id = models.CharField(max_length=64, blank=True)

    usuario = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimientos_stock",
    )
    terminal = models.ForeignKey(
        "tenancy.Terminal",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="movimientos_stock",
    )
    # Clave de idempotencia: un reintento de la cola offline no puede duplicar
    # el descuento de stock (seccion 4). El largo contempla claves compuestas
    # del tipo "<operacion>:<uuid venta>:<uuid insumo>".
    idempotency_key = models.CharField(max_length=160, blank=True)

    objects = MovimientoManager()
    unscoped = UnscopedManager()

    class Meta:
        verbose_name = "Movimiento de stock"
        verbose_name_plural = "Movimientos de stock"
        ordering = ["-creado_en"]
        base_manager_name = "unscoped"
        default_manager_name = "objects"
        indexes = [
            models.Index(fields=["tenant", "-creado_en"]),
            models.Index(fields=["tenant", "deposito", "insumo"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="movimiento_idempotencia_unica",
            ),
            models.CheckConstraint(
                condition=~models.Q(cantidad=0), name="movimiento_cantidad_no_cero"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tipo} {self.cantidad} {self.insumo.nombre}"

    def clean(self):
        super().clean()
        if self.cantidad is None:
            return
        if self.tipo in self.SUMAN and self.cantidad < 0:
            raise ValidationError(
                {"cantidad": f"Un movimiento de tipo {self.tipo} no puede restar stock."}
            )
        if self.tipo in self.RESTAN and self.cantidad > 0:
            raise ValidationError(
                {"cantidad": f"Un movimiento de tipo {self.tipo} no puede sumar stock."}
            )

    # --- append-only ------------------------------------------------------

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise AppendOnlyError(
                "Los movimientos de stock no se modifican. Asienta el movimiento contrario."
            )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise AppendOnlyError(
            "Los movimientos de stock no se borran. Asienta el movimiento contrario."
        )
