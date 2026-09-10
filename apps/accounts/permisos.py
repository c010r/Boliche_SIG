"""Catalogo de permisos y roles del vertical nocturno.

Los permisos son globales (no dependen del boliche); los roles se crean por
tenant a partir de estas plantillas.
"""

from __future__ import annotations

PERMISOS = [
    ("ventas.vender", "Vender en barra"),
    ("ventas.anular", "Anular una venta"),
    ("ventas.descontar", "Aplicar descuentos"),
    ("caja.abrir", "Abrir caja"),
    ("caja.cerrar", "Cerrar caja"),
    ("caja.autorizar_diferencia", "Autorizar diferencias de arqueo"),
    ("caja.egreso", "Registrar egresos de caja"),
    ("stock.ver", "Ver stock"),
    ("stock.contar", "Contar stock"),
    ("stock.autorizar_ajuste", "Autorizar ajustes de inventario"),
    ("stock.compra", "Registrar compras"),
    ("catalogo.gestionar", "Gestionar productos e insumos"),
    ("reportes.ver", "Ver reportes"),
    ("usuarios.gestionar", "Gestionar usuarios y roles"),
    ("acceso.validar", "Validar entradas en puerta"),
    ("entradas.vender", "Vender entradas"),
    ("entradas.anular", "Anular entradas"),
    ("listas.gestionar", "Gestionar listas e invitaciones"),
    ("promotores.gestionar", "Gestionar promotores y comisiones"),
]

# El dueño tiene todo; los demas son subconjuntos deliberados. La distincion
# clave: quien cobra, quien supervisa y quien administra no se mezclan.
ROLES = {
    "dueno": ("Dueno", [codigo for codigo, _ in PERMISOS]),
    "encargado": (
        "Encargado de noche",
        [
            "ventas.vender", "ventas.anular", "ventas.descontar",
            "caja.abrir", "caja.cerrar", "caja.autorizar_diferencia", "caja.egreso",
            "stock.ver", "stock.contar", "stock.autorizar_ajuste", "stock.compra",
            "catalogo.gestionar", "reportes.ver", "acceso.validar",
            "entradas.vender", "entradas.anular", "listas.gestionar",
            "promotores.gestionar",
        ],
    ),
    "cantinero": (
        "Cantinero",
        ["ventas.vender", "caja.abrir", "caja.cerrar", "stock.contar", "stock.ver"],
    ),
    "cajero_puerta": (
        "Cajero de puerta",
        ["entradas.vender", "acceso.validar", "caja.abrir", "caja.cerrar"],
    ),
    "seguridad": ("Control de acceso", ["acceso.validar"]),
    "promotor": ("Promotor", ["listas.gestionar"]),
    "contador": ("Contador externo", ["reportes.ver", "stock.ver"]),
}


def sincronizar_permisos() -> int:
    """Crea o actualiza el catalogo global de permisos. Devuelve cuantos hay."""
    from apps.accounts.models import Permiso

    for codigo, nombre in PERMISOS:
        Permiso.objects.update_or_create(
            codigo=codigo, defaults={"nombre": nombre}
        )
    return Permiso.objects.count()


def crear_roles_del_tenant(tenant):
    """Crea los roles del boliche con sus permisos, a partir de las plantillas."""
    from apps.accounts.models import Permiso, Rol

    creados = {}
    for codigo, (nombre, codigos_permiso) in ROLES.items():
        rol, _ = Rol.unscoped.update_or_create(
            tenant=tenant, codigo=codigo, defaults={"nombre": nombre}
        )
        permisos = Permiso.objects.filter(codigo__in=codigos_permiso)
        rol.permisos.set(permisos)
        creados[codigo] = rol
    return creados
