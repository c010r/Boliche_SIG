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
]
