from django.contrib import admin

from apps.stock.models import Deposito, StockItem, StockMovimiento


@admin.register(Deposito)
class DepositoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "local", "es_principal", "activo")
    list_filter = ("tenant", "local", "activo")


@admin.register(StockItem)
class StockItemAdmin(admin.ModelAdmin):
    list_display = ("insumo", "deposito", "cantidad")
    list_filter = ("tenant", "deposito")
    search_fields = ("insumo__nombre",)


@admin.register(StockMovimiento)
class StockMovimientoAdmin(admin.ModelAdmin):
    """El ledger es de solo lectura: no se carga ni se edita a mano."""

    list_display = ("creado_en", "tipo", "insumo", "deposito", "cantidad", "usuario")
    list_filter = ("tenant", "tipo", "deposito")
    search_fields = ("insumo__nombre", "motivo")
    readonly_fields = [f.name for f in StockMovimiento._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
