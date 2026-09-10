from django.core.management.base import BaseCommand

from apps.core.provisioning import provisionar_boliche


class Command(BaseCommand):
    help = "Da de alta un boliche completo: local, puntos de stock, terminales, roles y dueno."

    def add_arguments(self, parser):
        parser.add_argument("--nombre", required=True, help="Nombre comercial.")
        parser.add_argument("--slug", required=True, help="Subdominio, por ejemplo boliche-a.")
        parser.add_argument("--rut", default="", help="RUT (lo exige la Res. DGI 167/021).")
        parser.add_argument("--aforo", type=int, default=300)
        parser.add_argument("--local", default="Principal")
        parser.add_argument("--terminales", default="Barra 1,Barra 2")
        parser.add_argument("--admin-nombre", default="Dueno")
        parser.add_argument("--admin-numero", default="0001")
        parser.add_argument("--admin-pin", default="1234")
        parser.add_argument(
            "--sin-plantilla",
            action="store_true",
            help="No cargar los datos tipicos del vertical.",
        )

    def handle(self, *args, **opciones):
        terminales = [t.strip() for t in opciones["terminales"].split(",") if t.strip()]
        resultado = provisionar_boliche(
            nombre=opciones["nombre"],
            slug=opciones["slug"],
            rut=opciones["rut"],
            aforo=opciones["aforo"],
            nombre_local=opciones["local"],
            terminales=terminales,
            usuario_admin=opciones["admin_nombre"],
            numero_admin=opciones["admin_numero"],
            pin_admin=opciones["admin_pin"],
            con_plantilla=not opciones["sin_plantilla"],
        )

        self.stdout.write(self.style.SUCCESS("Boliche dado de alta."))
        self.stdout.write(f"  Boliche:    {resultado['tenant'].nombre} ({resultado['tenant'].slug})")
        self.stdout.write(f"  Local:      {resultado['local'].nombre}")
        self.stdout.write(f"  Terminales: {', '.join(t for t in terminales)} y Puerta")
        self.stdout.write(f"  Roles:      {', '.join(resultado['roles'].keys())}")
        self.stdout.write(
            f"  Productos:  {resultado['productos']} | Insumos: {resultado['insumos']}"
        )
        self.stdout.write(
            self.style.WARNING(
                f"  Ingreso:    numero {opciones['admin_numero']} con PIN "
                f"{opciones['admin_pin']} (cambialo antes de operar)"
            )
        )
