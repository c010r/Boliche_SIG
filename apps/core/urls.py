from django.urls import path

from apps.core import views

urlpatterns = [
    path("health/", views.health, name="health"),
]
