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
]
