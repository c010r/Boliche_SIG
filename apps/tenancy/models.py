from __future__ import annotations

from django.db import models

from apps.core.managers import UnscopedManager
from apps.core.models import TenantModel, TimeStampedModel, UUIDModel


class Tenant(UUIDModel, TimeStampedModel):
    """El boliche. Raiz del aislamiento multi-tenant.

    No esta scopeado por tenant (es el propio tenant), asi que su manager es
    unscoped por definicion.
    """

    class Estado(models.TextChoices):
        ACTIVO = "activo", "Activo"
        SUSPENDIDO = "suspendido", "Suspendido"

    class Plan(models.TextChoices):
        BASE = "base", "Base"
        ACCESO = "acceso", "Acceso"
        COMPLETO = "completo", "Completo"

    nombre = models.CharField("Nombre comercial", max_length=200)
    slug = models.SlugField(
        "Subdominio",
        max_length=63,
        unique=True,
        help_text="Se usa para resolver el tenant por subdominio.",
    )
    # Dato exigido por la Resolucion DGI 167/021: hay que informar cada emisor
    # electronico que usa la solucion de software (seccion 8).
    rut = models.CharField("RUT", max_length=20, blank=True)
    plan = models.CharField(max_length=20, choices=Plan.choices, default=Plan.BASE)
    estado = models.CharField(
        max_length=20, choices=Estado.choices, default=Estado.ACTIVO
    )

    objects = UnscopedManager()

    class Meta:
        verbose_name = "Boliche"
        verbose_name_plural = "Boliches"
        ordering = ["nombre"]

    def __str__(self) -> str:
        return self.nombre


class Local(TenantModel):
    """Sede. Un boliche puede tener varias, aunque la interfaz arranque con una."""

    nombre = models.CharField(max_length=120)
    direccion = models.CharField(max_length=250, blank=True)
    # Aforo legal, tomado de la habilitacion (seccion 8 ter).
    aforo = models.PositiveIntegerField(null=True, blank=True)
    zona_horaria = models.CharField(max_length=64, default="America/Montevideo")
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Local"
        verbose_name_plural = "Locales"
        ordering = ["nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "nombre"], name="local_nombre_unico_por_tenant"
            )
        ]

    def __str__(self) -> str:
        return self.nombre


class Terminal(TenantModel):
    """Dispositivo registrado.

    Sirve para tres cosas a la vez (seccion 5): cerrar caja por terminal, validar
    entradas en cada puerta, y facturar por dispositivo.
    """

    class Tipo(models.TextChoices):
        BARRA = "barra", "Barra"
        PUERTA = "puerta", "Puerta"
        CAJA = "caja", "Caja"
        ADMIN = "admin", "Administracion"

    local = models.ForeignKey(
        Local, on_delete=models.PROTECT, related_name="terminales"
    )
    nombre = models.CharField(max_length=120)
    tipo = models.CharField(max_length=20, choices=Tipo.choices)
    # Punto de stock desde el que descarga esta terminal. "Barra 1" descarga de
    # "Barra 1"; es lo que permite atribuir despues una varianza a un turno.
    deposito = models.ForeignKey(
        "stock.Deposito",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="terminales",
        verbose_name="Punto de stock",
    )
    # Vinculacion de dispositivo: una terminal pertenece a un dispositivo
    # registrado y el proveedor puede revocarlo (seccion 4 bis).
    identificador = models.CharField(max_length=120, blank=True)
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Terminal"
        verbose_name_plural = "Terminales"
        ordering = ["local__nombre", "nombre"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "local", "nombre"],
                name="terminal_nombre_unico_por_local",
            )
        ]

    def __str__(self) -> str:
        return f"{self.local.nombre} / {self.nombre}"
