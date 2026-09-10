from django.urls import path

from apps.web import views

app_name = "web"

urlpatterns = [
    path("", views.inicio, name="inicio"),
    path("ingresar/", views.ingresar, name="ingresar"),
    path("salir/", views.salir, name="salir"),
    path("barra/", views.barra, name="barra"),
    path("barra/anular/<uuid:venta_id>/", views.anular, name="anular"),
    path("caja/", views.caja, name="caja"),
    path("panel/", views.panel, name="panel"),
    # Entradas y control de acceso
    path("eventos/", views.eventos, name="eventos"),
    path("eventos/<uuid:evento_id>/", views.evento, name="evento"),
    path("eventos/<uuid:evento_id>/vender/", views.vender_entradas, name="vender_entradas"),
    path("entradas/<uuid:entrada_id>/qr/", views.entrada_qr, name="entrada_qr"),
    path("puerta/", views.puerta, name="puerta"),
    path("puerta/<uuid:evento_id>/", views.puerta, name="puerta_evento"),
    path("validar/", views.validar, name="validar"),
    path("validar-lista/", views.validar_lista, name="validar_lista"),
    path("eventos/<uuid:evento_id>/listas/", views.listas, name="listas"),
    # Tienda publica (anonima: el boliche sale del slug)
    path("tienda/<slug:slug>/", views.tienda, name="tienda"),
    path("tienda/<slug:slug>/<uuid:evento_id>/", views.tienda_evento, name="tienda_evento"),
    path("tienda/<slug:slug>/<uuid:evento_id>/reservar/", views.reservar, name="reservar"),
    path("reserva/<str:token>/", views.reserva_pago, name="reserva_pago"),
    path("reserva/<str:token>/confirmar/", views.reserva_confirmar, name="reserva_confirmar"),
    path("entrada/<str:token>/", views.entrada_publica, name="entrada_publica"),
]
