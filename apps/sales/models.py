"""Caja y ventas (ANALISIS.md seccion 5).

Tres cosas que este modulo resuelve y que son las que rompen un sabado a la noche:

1. Una sola sesion de caja abierta por terminal (invariante en la base, no en el
   codigo: si vive en el codigo, algun camino la va a saltear).
2. El pago es ASINCRONICO. Un cobro por QR se confirma por webhook minutos
   despues; una venta con "medio de pago" como campo plano no lo soporta.
3. El arqueo es por medio de pago, con la diferencia y quien la autoriza.
"""

from __future__ import annotations

from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models

from apps.core.models import TenantModel


class Turno(TenantModel):
    """La noche o el evento. Agrupa las sesiones de caja."""

    class Estado(models.TextChoices):
        ABIERTO = "abierto", "Abierto"
        CERRADO = "cerrado", "Cerrado"

    local = models.ForeignKey(
        "tenancy.Local", on_delete=models.PROTECT, related_name="turnos"
    )
    nombre = models.CharField(max_length=120)
    fecha = models.DateField()
    abierto_en = models.DateTimeField(null=True, blank=True)
    cerrado_en = models.DateTimeField(null=True, blank=True)
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.ABIERTO
    )

    class Meta:
        verbose_name = "Turno"
        verbose_name_plural = "Turnos"
        ordering = ["-fecha", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "local", "fecha", "nombre"],
                name="turno_unico_por_local_y_fecha",
            )
        ]

    def __str__(self) -> str:
        return f"{self.fecha} - {self.nombre}"


class SesionCaja(TenantModel):
    """Caja abierta por una persona en una terminal, con su fondo inicial."""

    class Estado(models.TextChoices):
        ABIERTA = "abierta", "Abierta"
        CERRADA = "cerrada", "Cerrada"

    turno = models.ForeignKey(Turno, on_delete=models.PROTECT, related_name="sesiones")
    terminal = models.ForeignKey(
        "tenancy.Terminal", on_delete=models.PROTECT, related_name="sesiones_caja"
    )
    usuario = models.ForeignKey(
        "accounts.Usuario", on_delete=models.PROTECT, related_name="sesiones_caja"
    )
    fondo_inicial = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )
    abierta_en = models.DateTimeField()
    cerrada_en = models.DateTimeField(null=True, blank=True)
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.ABIERTA
    )

    class Meta:
        verbose_name = "Sesion de caja"
        verbose_name_plural = "Sesiones de caja"
        ordering = ["-abierta_en"]
        constraints = [
            # La invariante vive en la base: una sola sesion abierta por terminal.
            models.UniqueConstraint(
                fields=["tenant", "terminal"],
                condition=models.Q(estado="abierta"),
                name="una_sesion_abierta_por_terminal",
            )
        ]

    def __str__(self) -> str:
        return f"{self.terminal.nombre} - {self.usuario} ({self.estado})"


class Venta(TenantModel):
    """Venta de barra."""

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente de pago"
        PAGADA = "pagada", "Pagada"
        ANULADA = "anulada", "Anulada"

    sesion_caja = models.ForeignKey(
        SesionCaja, on_delete=models.PROTECT, related_name="ventas"
    )
    usuario = models.ForeignKey(
        "accounts.Usuario", on_delete=models.PROTECT, related_name="ventas"
    )
    terminal = models.ForeignKey(
        "tenancy.Terminal", on_delete=models.PROTECT, related_name="ventas"
    )
    numero = models.PositiveIntegerField(help_text="Folio correlativo por sesion.")
    estado = models.CharField(
        max_length=16, choices=Estado.choices, default=Estado.PENDIENTE
    )
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    descuento = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    total = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0"))
    # Una venta de entradas no tiene productos: tiene concepto. "3 entradas
    # General - Fiesta del viernes". Igual entra en la caja y en el arqueo.
    concepto = models.CharField(max_length=150, blank=True)

    # Reloj del dispositivo y clave de idempotencia: sin los dos, la cola
    # offline duplica ventas o las ordena mal (seccion 4).
    creada_en_cliente = models.DateTimeField(null=True, blank=True)
    idempotency_key = models.CharField(max_length=160, blank=True)

    anulada_en = models.DateTimeField(null=True, blank=True)
    anulada_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ventas_anuladas",
    )
    motivo_anulacion = models.CharField(max_length=250, blank=True)

    class Meta:
        verbose_name = "Venta"
        verbose_name_plural = "Ventas"
        ordering = ["-creado_en"]
        indexes = [
            models.Index(fields=["tenant", "-creado_en"]),
            models.Index(fields=["tenant", "sesion_caja"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "idempotency_key"],
                condition=~models.Q(idempotency_key=""),
                name="venta_idempotencia_unica",
            ),
            models.UniqueConstraint(
                fields=["tenant", "sesion_caja", "numero"], name="venta_folio_unico"
            ),
        ]

    def __str__(self) -> str:
        return f"Venta #{self.numero} ({self.estado})"

    @property
    def total_pagado(self) -> Decimal:
        total = self.pagos.filter(
            estado__in=[Pago.Estado.APROBADO, Pago.Estado.CONTRACARGO]
        ).aggregate(t=models.Sum("monto"))["t"]
        return total or Decimal("0")

    @property
    def saldo(self) -> Decimal:
        return self.total - self.total_pagado

    def clean(self):
        super().clean()
        if self.total is not None and self.total < 0:
            raise ValidationError({"total": "El total no puede ser negativo."})


class ItemVenta(TenantModel):
    """Linea de venta.

    Guarda el precio del momento a proposito: si manana cambia la lista, la venta
    de hoy no se tiene que reescribir.
    """

    venta = models.ForeignKey(Venta, on_delete=models.CASCADE, related_name="items")
    producto = models.ForeignKey(
        "catalog.Producto", on_delete=models.PROTECT, related_name="items_vendidos"
    )
    cantidad = models.PositiveIntegerField(default=1)
    precio_unitario = models.DecimalField(max_digits=12, decimal_places=2)
    descuento = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )
    total = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = "Item de venta"
        verbose_name_plural = "Items de venta"
        ordering = ["venta", "creado_en"]

    def __str__(self) -> str:
        return f"{self.cantidad} x {self.producto.nombre}"

    @property
    def deposito(self):
        """De donde sale el stock: el punto asociado a la terminal de la venta."""
        return self.venta.sesion_caja.terminal.deposito

    def clean(self):
        super().clean()
        if self.cantidad is not None and self.cantidad <= 0:
            raise ValidationError({"cantidad": "La cantidad tiene que ser mayor que cero."})


class Pago(TenantModel):
    """Cobro asociado a una venta.

    N por venta, con estado propio: es lo que permite cobrar en dos medios y
    esperar la confirmacion asincronica del QR sin trabar la barra.
    """

    class Medio(models.TextChoices):
        EFECTIVO = "efectivo", "Efectivo"
        TARJETA = "tarjeta", "Tarjeta"
        QR = "qr", "QR"
        TRANSFERENCIA = "transferencia", "Transferencia"

    class Estado(models.TextChoices):
        PENDIENTE = "pendiente", "Pendiente"
        APROBADO = "aprobado", "Aprobado"
        RECHAZADO = "rechazado", "Rechazado"
        REEMBOLSADO = "reembolsado", "Reembolsado"
        CONTRACARGO = "contracargo", "Contracargo"

    venta = models.ForeignKey(Venta, on_delete=models.PROTECT, related_name="pagos")
    medio = models.CharField(max_length=20, choices=Medio.choices)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    estado = models.CharField(
        max_length=20, choices=Estado.choices, default=Estado.PENDIENTE
    )
    # Identificador del pago en el adquirente. La tarjeta NUNCA pasa por aca:
    # el sistema registra medio y monto, jamas el numero (seccion 4 bis).
    referencia_externa = models.CharField(max_length=120, blank=True)
    confirmado_en = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(null=True, blank=True)
    # Un pago puede aprobarse DESPUES de que el cajero cerro la caja. No se
    # edita un arqueo cerrado: el cobro se marca y se reporta aparte, en vez de
    # producir un faltante que nadie puede explicar (seccion 5).
    cobro_tardio = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Pago"
        verbose_name_plural = "Pagos"
        ordering = ["-creado_en"]
        indexes = [models.Index(fields=["tenant", "referencia_externa"])]

    def __str__(self) -> str:
        return f"{self.medio} {self.monto} ({self.estado})"

    def clean(self):
        super().clean()
        if self.monto is not None and self.monto <= 0:
            raise ValidationError({"monto": "El monto tiene que ser mayor que cero."})


class EgresoCaja(TenantModel):
    """Retiro parcial del encargado o gasto menor pagado de la caja."""

    class Tipo(models.TextChoices):
        RETIRO = "retiro", "Retiro"
        GASTO = "gasto", "Gasto"

    sesion_caja = models.ForeignKey(
        SesionCaja, on_delete=models.PROTECT, related_name="egresos"
    )
    tipo = models.CharField(max_length=16, choices=Tipo.choices)
    monto = models.DecimalField(max_digits=12, decimal_places=2)
    motivo = models.CharField(max_length=250)
    usuario = models.ForeignKey(
        "accounts.Usuario", on_delete=models.PROTECT, related_name="egresos_caja"
    )
    autorizado_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="egresos_autorizados",
    )

    class Meta:
        verbose_name = "Egreso de caja"
        verbose_name_plural = "Egresos de caja"
        ordering = ["-creado_en"]

    def __str__(self) -> str:
        return f"{self.tipo} {self.monto}"

    def clean(self):
        super().clean()
        if self.monto is not None and self.monto <= 0:
            raise ValidationError({"monto": "El monto tiene que ser mayor que cero."})


class Arqueo(TenantModel):
    """Cierre de caja: esperado contra declarado, por medio de pago."""

    sesion_caja = models.OneToOneField(
        SesionCaja, on_delete=models.PROTECT, related_name="arqueo"
    )
    cerrado_en = models.DateTimeField()
    autorizado_por = models.ForeignKey(
        "accounts.Usuario",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="arqueos_autorizados",
    )
    observaciones = models.CharField(max_length=500, blank=True)

    class Meta:
        verbose_name = "Arqueo"
        verbose_name_plural = "Arqueos"
        ordering = ["-cerrado_en"]

    def __str__(self) -> str:
        return f"Arqueo de {self.sesion_caja}"

    @property
    def diferencia_total(self) -> Decimal:
        total = self.lineas.aggregate(t=models.Sum("diferencia"))["t"]
        return total or Decimal("0")


class ArqueoLinea(TenantModel):
    """Una linea por medio de pago: esperado, declarado y diferencia."""

    arqueo = models.ForeignKey(Arqueo, on_delete=models.CASCADE, related_name="lineas")
    medio = models.CharField(max_length=20, choices=Pago.Medio.choices)
    esperado = models.DecimalField(max_digits=12, decimal_places=2)
    declarado = models.DecimalField(max_digits=12, decimal_places=2)
    diferencia = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        verbose_name = "Linea de arqueo"
        verbose_name_plural = "Lineas de arqueo"
        ordering = ["arqueo", "medio"]
        constraints = [
            models.UniqueConstraint(
                fields=["arqueo", "medio"], name="arqueo_linea_unica_por_medio"
            )
        ]

    def __str__(self) -> str:
        return f"{self.medio}: esperado {self.esperado} / declarado {self.declarado}"
