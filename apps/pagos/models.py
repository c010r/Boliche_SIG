"""Credenciales del adquirente, por boliche.

Cada local es el COMERCIANTE y cobra en SU cuenta de Mercado Pago: el proveedor
del software no es intermediario de pagos (ANALISIS.md seccion 9 bis). Eso define
quien asume los contracargos y evita convertirse en agregador.

Consecuencia de seguridad: aca se guardan tokens de acceso ajenos. El campo no se
muestra en el admin y el __repr__ no lo expone, para que no termine en un log. En
produccion deberia ir cifrado en reposo; mientras no lo este, la proteccion es la
de la base y la del servidor.
"""

from __future__ import annotations

from django.db import models

from apps.core.models import TenantModel


class Proveedor(models.TextChoices):
    MERCADO_PAGO = "mercadopago", "Mercado Pago"
    SIMULADO = "simulado", "Simulado (desarrollo)"


class CredencialDePago(TenantModel):
    """Credenciales del adquirente de un boliche."""

    proveedor = models.CharField(
        max_length=24, choices=Proveedor.choices, default=Proveedor.MERCADO_PAGO
    )
    access_token = models.CharField(max_length=250, blank=True)
    # Caja de Mercado Pago a la que se asocian las ordenes del QR.
    external_pos_id = models.CharField(max_length=64, blank=True)
    # Clave para validar que la notificacion la mando el adquirente.
    clave_de_firma = models.CharField(max_length=250, blank=True)
    activo = models.BooleanField(default=False)

    class Meta:
        verbose_name = "Credencial de pago"
        verbose_name_plural = "Credenciales de pago"
        ordering = ["tenant", "proveedor"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "proveedor"], name="credencial_unica_por_proveedor"
            )
        ]

    def __str__(self) -> str:
        return f"{self.tenant.slug} / {self.get_proveedor_display()}"

    def __repr__(self) -> str:
        # Nunca filtrar el token por un log.
        return f"<CredencialDePago {self.tenant_id} {self.proveedor}>"

    @property
    def configurada(self) -> bool:
        return bool(self.activo and self.access_token)
