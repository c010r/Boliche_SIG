"""Alta de un boliche completo.

Objetivo medible (ANALISIS.md seccion 11 quater): un boliche nuevo operativo en
menos de 30 minutos, sin tocar la base de datos a mano.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from apps.accounts.permisos import crear_roles_del_tenant, sincronizar_permisos
from apps.accounts.models import MembresiaUsuario, Usuario
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
from apps.core import verticales
from apps.core.context import tenant_context
from apps.stock.models import Deposito
from apps.tenancy.models import Local, Tenant, Terminal


@transaction.atomic
def sincronizar_unidades() -> dict:
    """Catalogo global de unidades."""
    unidades = {}
    for codigo, nombre, magnitud, factor in verticales.UNIDADES:
        unidad, _ = UnidadMedida.objects.update_or_create(
            codigo=codigo,
            defaults={
                "nombre": nombre,
                "magnitud": magnitud,
                "factor_a_base": Decimal(factor),
            },
        )
        unidades[codigo] = unidad
    return unidades


@transaction.atomic
def provisionar_boliche(
    *,
    nombre: str,
    slug: str,
    rut: str = "",
    aforo: int = 300,
    nombre_local: str = "Principal",
    terminales=("Barra 1", "Barra 2"),
    usuario_admin: str = "Dueno",
    numero_admin: str = "0001",
    pin_admin: str = "1234",
    con_plantilla: bool = True,
) -> dict:
    """Crea tenant, local, puntos de stock, terminales, roles y usuario dueno."""
    sincronizar_unidades()
    sincronizar_permisos()

    # Tenant es la raiz del aislamiento: no esta scopeado, su manager ya es global.
    tenant, _ = Tenant.objects.update_or_create(
        slug=slug,
        defaults={"nombre": nombre, "rut": rut, "estado": Tenant.Estado.ACTIVO},
    )

    with tenant_context(tenant):
        local, _ = Local.objects.update_or_create(
            nombre=nombre_local,
            defaults={"aforo": aforo, "zona_horaria": "America/Montevideo"},
        )

        deposito_central, _ = Deposito.objects.update_or_create(
            local=local, nombre="Deposito", defaults={"es_principal": True}
        )

        puntos = {"Deposito": deposito_central}
        for nombre_terminal in terminales:
            deposito, _ = Deposito.objects.update_or_create(
                local=local, nombre=nombre_terminal, defaults={"es_principal": False}
            )
            puntos[nombre_terminal] = deposito
            Terminal.objects.update_or_create(
                local=local,
                nombre=nombre_terminal,
                defaults={"tipo": Terminal.Tipo.BARRA, "deposito": deposito},
            )

        puerta, _ = Deposito.objects.update_or_create(
            local=local, nombre="Puerta", defaults={"es_principal": False}
        )
        puntos["Puerta"] = puerta
        Terminal.objects.update_or_create(
            local=local,
            nombre="Puerta",
            defaults={"tipo": Terminal.Tipo.PUERTA, "deposito": puerta},
        )

        roles = crear_roles_del_tenant(tenant)

        admin = Usuario.objects.filter(tenant=tenant, numero=numero_admin).first()
        if admin is None:
            admin = Usuario.objects.create_user(
                email=None, nombre=usuario_admin, tenant=tenant, numero=numero_admin
            )
        admin.set_pin(pin_admin)
        admin.save()

        MembresiaUsuario.objects.update_or_create(
            usuario=admin, local=local, rol=roles["dueno"], defaults={"activo": True}
        )

        resumen = {"productos": 0, "insumos": 0}
        if con_plantilla:
            resumen = _cargar_plantilla()

    return {
        "tenant": tenant,
        "local": local,
        "depositos": puntos,
        "roles": roles,
        "admin": admin,
        **resumen,
    }


def _cargar_plantilla() -> dict:
    """Carga insumos, productos y combos tipicos del vertical.

    Se ejecuta dentro del contexto del tenant, por eso no recibe el tenant.
    """
    unidades = {
        u.codigo: u for u in UnidadMedida.objects.all()
    }

    insumos = {}
    for nombre, unidad_base, costo, minimo, presentacion, factor in verticales.INSUMOS:
        insumo, _ = Insumo.objects.update_or_create(
            nombre=nombre,
            defaults={
                "unidad_base": unidades[unidad_base],
                "costo_promedio": Decimal(costo),
                "stock_minimo": Decimal(minimo),
                "activo": True,
            },
        )
        InsumoPresentacion.objects.update_or_create(
            insumo=insumo,
            nombre=presentacion,
            defaults={"factor_a_unidad_base": Decimal(factor)},
        )
        insumos[nombre] = insumo

    categorias = {}
    for nombre, orden in verticales.CATEGORIAS:
        categoria, _ = CategoriaProducto.objects.update_or_create(
            nombre=nombre, defaults={"orden": orden, "activo": True}
        )
        categorias[nombre] = categoria

    productos = {}
    for categoria, nombre, precio, receta in verticales.PRODUCTOS:
        producto, _ = Producto.objects.update_or_create(
            nombre=nombre,
            defaults={
                "categoria": categorias[categoria],
                "precio": Decimal(precio),
                "es_combo": False,
                "activo": True,
            },
        )
        receta_obj, _ = Receta.objects.update_or_create(
            producto=producto, defaults={"activa": True}
        )
        receta_obj.items.all().delete()
        for insumo_nombre, cantidad, unidad in receta:
            RecetaItem.objects.create(
                receta=receta_obj,
                insumo=insumos[insumo_nombre],
                cantidad=Decimal(cantidad),
                unidad=unidades[unidad],
            )
        productos[nombre] = producto

    for nombre, precio, hijos in verticales.COMBOS:
        combo, _ = Producto.objects.update_or_create(
            nombre=nombre,
            defaults={
                "categoria": categorias["Tragos"],
                "precio": Decimal(precio),
                "es_combo": True,
                "activo": True,
            },
        )
        combo.combo_items.all().delete()
        for hijo, cantidad in hijos:
            ComboItem.objects.create(
                combo=combo, producto_hijo=productos[hijo], cantidad=cantidad
            )
        productos[nombre] = combo

    return {"productos": len(productos), "insumos": len(insumos)}
