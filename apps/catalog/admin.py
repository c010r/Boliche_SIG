from django.contrib import admin

from apps.catalog.models import (
    CategoriaProducto,
    ComboItem,
    Insumo,
    InsumoPresentacion,
    Producto,
    Receta,
    RecetaItem,
    UnidadMedida,
)


@admin.register(UnidadMedida)
class UnidadMedidaAdmin(admin.ModelAdmin):
    list_display = ("codigo", "nombre", "magnitud", "factor_a_base")
    list_filter = ("magnitud",)


class RecetaItemInline(admin.TabularInline):
    model = RecetaItem
    extra = 0


@admin.register(Receta)
class RecetaAdmin(admin.ModelAdmin):
    list_display = ("producto", "activa")
    inlines = [RecetaItemInline]


@admin.register(Insumo)
class InsumoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "unidad_base", "costo_promedio", "stock_minimo", "activo")
    list_filter = ("tenant", "activo")
    search_fields = ("nombre", "codigo_barras")


@admin.register(Producto)
class ProductoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "categoria", "precio", "es_combo", "activo")
    list_filter = ("tenant", "categoria", "es_combo", "activo")
    search_fields = ("nombre",)


admin.site.register([CategoriaProducto, InsumoPresentacion, ComboItem])
