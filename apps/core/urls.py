from django.urls import path

from apps.core import views
from apps.web import api
from apps.web import views as web_views

urlpatterns = [
    path("health/", views.health, name="health"),
    # Notificacion del adquirente. Va bajo /api/ porque lo llama un tercero, no
    # un navegador con sesion.
    path("pagos/webhook/", web_views.webhook_de_pago, name="webhook_de_pago"),
    # Sincronizacion de la barra cuando vuelve la conexion.
    path("barra/sincronizar/", api.sincronizar_ventas, name="sincronizar_ventas"),
]
