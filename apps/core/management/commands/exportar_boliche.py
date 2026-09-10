"""Exportacion logica de un boliche.

Resuelve tres cosas con un solo mecanismo (ANALISIS.md seccion 8 bis):

1. El backup aislado que un cliente grande puede pedir por contrato.
2. El derecho de acceso de la Ley 18.331: hay que poder entregarle a alguien todo
   lo que el sistema guarda sobre el.
3. La portabilidad si el boliche se va: los datos son suyos.

Se usa el serializador de Django a proposito: el archivo queda en un formato que
loaddata puede volver a cargar, asi que sirve de respaldo y no solo de informe.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.core import serializers

from apps.core.context import tenant_context
from apps.tenancy.models import Tenant

# Apps cuyos modelos pertenecen a un boliche. Se excluyen las globales (unidades de
# medida y permisos) porque no son datos del cliente.
APPS_DEL_BOLICHE = ["tenancy", "accounts", "catalog", "stock", "sales", "ticketing", "pagos"]


def modelos_del_boliche():
    """Modelos a exportar, en un orden que respeta las dependencias."""
    encontrados = []
    for etiqueta in APPS_DEL_BOLICHE:
        try:
            app = apps.get_app_config(etiqueta)
        except LookupError:
            continue
        for modelo in app.get_models():
            campos = {campo.name for campo in modelo._meta.get_fields()}
            if modelo._meta.model_name == "tenant" or "tenant" in campos:
                encontrados.append(modelo)
    return encontrados


def consulta_del_boliche(modelo, tenant):
    """Consulta de los datos de un boliche, sin depender del manager de cada modelo.

    Los modelos de negocio exponen unscoped (fail-closed + escape explicito), pero
    Usuario tiene su propio manager porque la autenticacion ocurre antes de que
    exista contexto de tenant.
    """
    manager = getattr(modelo, "unscoped", None) or modelo._base_manager
    return manager.filter(tenant=tenant)


def exportar(tenant, *, incluir_tenant: bool = True) -> dict:
    """Devuelve el export completo del boliche como estructura serializable."""
    partes = []
    manifiesto = {}

    with tenant_context(tenant):
        for modelo in modelos_del_boliche():
            etiqueta = modelo._meta.label

            if modelo._meta.model_name == "tenant":
                if not incluir_tenant:
                    continue
                consulta = modelo.objects.filter(pk=tenant.pk)
            else:
                consulta = consulta_del_boliche(modelo, tenant)

            filas = list(consulta.order_by("pk"))
            manifiesto[etiqueta] = len(filas)
            partes.append(serializers.serialize("json", filas))

    objetos = []
    for parte in partes:
        objetos.extend(json.loads(parte))

    return {
        "formato": "boliche-sig/export/1",
        "generado_en": datetime.now().isoformat(timespec="seconds"),
        "boliche": {
            "nombre": tenant.nombre,
            "slug": tenant.slug,
            "rut": tenant.rut,
            "id": str(tenant.id),
        },
        "modelos": manifiesto,
        "total_de_objetos": len(objetos),
        # Mismo formato que loaddata: el respaldo tambien sirve para restaurar.
        "objetos": objetos,
    }


class Command(BaseCommand):
    help = "Exporta todos los datos de un boliche a un archivo JSON."

    def add_arguments(self, parser):
        parser.add_argument("--slug", required=True)
        parser.add_argument("--salida", help="Archivo de destino. Si falta, va a stdout.")
        parser.add_argument(
            "--sin-boliche",
            action="store_true",
            help="No incluir la fila del propio boliche.",
        )

    def handle(self, *args, **opciones):
        tenant = Tenant.objects.filter(slug=opciones["slug"]).first()
        if tenant is None:
            raise CommandError(f"No existe un boliche con slug {opciones['slug']}.")

        datos = exportar(tenant, incluir_tenant=not opciones["sin_boliche"])
        texto = json.dumps(datos, indent=2, ensure_ascii=False, default=str)

        salida = opciones.get("salida")
        if salida:
            ruta = Path(salida)
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(texto, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Exportado a {ruta}"))
        else:
            self.stdout.write(texto)

        self.stdout.write(f"  Boliche: {tenant.nombre} ({tenant.slug})")
        self.stdout.write(f"  Objetos: {datos['total_de_objetos']}")
        for etiqueta, cantidad in sorted(datos["modelos"].items()):
            if cantidad:
                self.stdout.write(f"    {etiqueta}: {cantidad}")
