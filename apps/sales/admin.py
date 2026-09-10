from django.contrib import admin

from apps.sales.models import (
    Arqueo,
    ArqueoLinea,
    EgresoCaja,
    ItemVenta,
    Pago,
    SesionCaja,
    Turno,
    Venta,
)


class ItemVentaInline(admin.TabularInline):
    model = ItemVenta
    extra = 0
    readonly_fields = ("producto", "cantidad", "precio_unitario", "descuento", "total")


class PagoInline(admin.TabularInline):
    model = Pago
    extra = 0


@admin.register(Venta)
class VentaAdmin(admin.ModelAdmin):
    list_display = ("numero", "estado", "total", "total_pagado", "usuario", "creado_en")
    list_filter = ("tenant", "estado", "terminal")
    search_fields = ("numero", "idempotency_key")
    inlines = [ItemVentaInline, PagoInline]
    readonly_fields = ("numero", "subtotal", "total")


class ArqueoLineaInline(admin.TabularInline):
    model = ArqueoLinea
    extra = 0
    readonly_fields = ("medio", "esperado", "declarado", "diferencia")


@admin.register(Arqueo)
class ArqueoAdmin(admin.ModelAdmin):
    list_display = ("sesion_caja", "cerrado_en", "diferencia_total", "autorizado_por")
    inlines = [ArqueoLineaInline]


@admin.register(SesionCaja)
class SesionCajaAdmin(admin.ModelAdmin):
    list_display = ("terminal", "usuario", "estado", "fondo_inicial", "abierta_en", "cerrada_en")
    list_filter = ("tenant", "estado", "terminal")


@admin.register(Turno)
class TurnoAdmin(admin.ModelAdmin):
    list_display = ("nombre", "local", "fecha", "estado", "abierto_en", "cerrado_en")
    list_filter = ("tenant", "estado", "local")


admin.site.register(EgresoCaja)
