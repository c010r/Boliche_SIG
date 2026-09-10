"""Libera las reservas vencidas sin pago.

Pensado para correr cada minuto desde cron o desde un servicio. Si no corre, igual
no se sobrevende: las reservas vencidas dejan de ocupar cupo solas, porque el
calculo del disponible excluye las que ya pasaron su hora. Esta limpieza es para
que los estados queden prolijos y para que el reporte no muestre como "activa"
algo que ya no lo es.
"""

from django.core.management.base import BaseCommand

from apps.core.context import tenant_context
from apps.tenancy.models import Tenant
from apps.ticketing.reservas import expirar_reservas


class Command(BaseCommand):
    help = "Marca como expiradas las reservas cuyo plazo vencio sin pago."

    def handle(self, *args, **opciones):
        total = 0
        for tenant in Tenant.objects.filter(estado=Tenant.Estado.ACTIVO):
            with tenant_context(tenant):
                expiradas = expirar_reservas()
                total += expiradas
                if expiradas:
                    self.stdout.write(f"  {tenant.slug}: {expiradas} reserva(s) expirada(s)")
        self.stdout.write(self.style.SUCCESS(f"Reservas expiradas: {total}"))
