from django.db import connection
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Chequeo de salud: la app responde y la base contesta."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()

    return Response(
        {
            "estado": "ok",
            "tenant": getattr(request.tenant, "slug", None),
            "base_de_datos": "ok",
        }
    )
