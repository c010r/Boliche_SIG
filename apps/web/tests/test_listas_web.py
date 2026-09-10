"""Interfaz de listas: alta, puerta y comisiones."""

from datetime import date, time

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import MembresiaUsuario, Rol, Usuario
from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.tenancy.models import Local
from apps.ticketing.listas import agregar_invitado, crear_lista, crear_promotor
from apps.ticketing.models import ComisionPromotor, Lista, UsoLista
from apps.ticketing.services import agregar_tipo, crear_evento, publicar_evento


class BaseListasWebTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Listas Web", slug="lw")
        cls.tenant = cls.datos["tenant"]
        cls.admin = cls.datos["admin"]

    def setUp(self):
        with tenant_context(self.tenant):
            self.local = Local.objects.get()
            self.evento = crear_evento(
                local=self.local, nombre="Viernes", fecha=date(2026, 10, 3),
                aforo=50, hora_apertura=time(23, 0),
            )
            agregar_tipo(evento=self.evento, nombre="General", precio="500", cupo=50)
            publicar_evento(self.evento)

    def ingresar(self, numero="0001", pin="1234"):
        self.client.post(reverse("web:ingresar"), {"numero": numero, "pin": pin})

    def url_listas(self):
        return reverse("web:listas", args=[self.evento.id])


class PantallaDeListasTests(BaseListasWebTests):
    def test_la_pantalla_carga_y_explica_las_reglas(self):
        self.ingresar()
        respuesta = self.client.get(self.url_listas())
        self.assertEqual(respuesta.status_code, 200)
        self.assertContains(respuesta, "El corte de cada lista lo aplica el sistema")

    def test_crear_una_lista_desde_la_pantalla(self):
        self.ingresar()
        self.client.post(self.url_listas(), {
            "accion": "crear_lista", "nombre": "Lista del DJ",
            "tipo": Lista.Tipo.DJ, "cupo": "15", "hora_de_corte": "01:00",
        })
        with tenant_context(self.tenant):
            lista = Lista.objects.get()
        self.assertEqual(lista.nombre, "Lista del DJ")
        self.assertEqual(lista.cupo, 15)
        self.assertEqual(lista.hora_de_corte, time(1, 0))

    def test_agregar_un_invitado_desde_la_pantalla(self):
        with tenant_context(self.tenant):
            lista = crear_lista(evento=self.evento, nombre="Casa", cupo=10)
            lista_id = lista.id
        self.ingresar()
        self.client.post(self.url_listas(), {
            "accion": "agregar_invitado", "lista": str(lista_id),
            "nombre": "Ana", "personas": "3",
        })
        with tenant_context(self.tenant):
            from apps.ticketing.models import ListaInvitado
            invitado = ListaInvitado.objects.get()
        self.assertEqual(invitado.personas_que_cubre, 3)

    def test_liquidar_y_aprobar_comisiones_desde_la_pantalla(self):
        with tenant_context(self.tenant):
            promotor = crear_promotor(nombre="Rocio", valor_comision="150")
            lista = crear_lista(
                evento=self.evento, nombre="Rocio", cupo=10,
                tipo=Lista.Tipo.PROMOTOR, promotor=promotor,
            )
            invitado = agregar_invitado(lista=lista, nombre="Ana", personas=2)
            from apps.ticketing.listas import validar_ingreso_de_lista
            validar_ingreso_de_lista(
                evento=self.evento, texto=invitado.qr_token, personas=2,
                usuario=self.admin,
            )

        self.ingresar()
        self.client.post(self.url_listas(), {"accion": "liquidar"})
        with tenant_context(self.tenant):
            comision = ComisionPromotor.objects.get()
            self.assertEqual(comision.personas, 2)
            self.assertEqual(comision.estado, ComisionPromotor.Estado.CALCULADA)
            comision_id = comision.id

        self.client.post(
            self.url_listas(), {"accion": "aprobar", "comision": str(comision_id)}
        )
        with tenant_context(self.tenant):
            comision = ComisionPromotor.objects.get()
            self.assertEqual(comision.estado, ComisionPromotor.Estado.APROBADA)
            self.assertEqual(comision.aprobada_por, self.admin)

        self.client.post(
            self.url_listas(), {"accion": "pagar", "comision": str(comision_id)}
        )
        with tenant_context(self.tenant):
            comision = ComisionPromotor.objects.get()
            self.assertEqual(comision.estado, ComisionPromotor.Estado.PAGADA)

    def test_un_cantinero_no_ve_las_listas(self):
        with tenant_context(self.tenant):
            cantinero = Usuario.objects.create_user(
                email=None, nombre="Carla", tenant=self.tenant, numero="0009"
            )
            cantinero.set_pin("9999")
            cantinero.save()
            MembresiaUsuario.objects.create(
                usuario=cantinero, local=self.local,
                rol=Rol.objects.get(codigo="cantinero"),
            )
        self.ingresar(numero="0009", pin="9999")
        respuesta = self.client.get(self.url_listas(), follow=True)
        self.assertContains(respuesta, "No tenes permiso")


class PuertaConListaTests(BaseListasWebTests):
    def setUp(self):
        super().setUp()
        with tenant_context(self.tenant):
            self.lista = crear_lista(
                evento=self.evento, nombre="Casa", cupo=10, hora_de_corte=time(1, 0)
            )
            self.ana = agregar_invitado(lista=self.lista, nombre="Ana", personas=3)

    def validar_lista(self, **datos):
        base = {"evento": str(self.evento.id)}
        base.update(datos)
        return self.client.post(reverse("web:validar_lista"), base)

    def test_pasa_un_invitado_por_nombre(self):
        self.ingresar()
        datos = self.validar_lista(nombre="Ana", personas="2").json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["aforo_en_vivo"], 2)
        with tenant_context(self.tenant):
            self.assertEqual(UsoLista.objects.count(), 1)

    def test_pasa_un_invitado_por_qr(self):
        self.ingresar()
        datos = self.validar_lista(texto=self.ana.qr_token, personas="1").json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["invitado"], "Ana")

    def test_un_nombre_desconocido_no_pasa(self):
        self.ingresar()
        datos = self.validar_lista(nombre="Nadie").json()
        self.assertFalse(datos["ok"])
        self.assertEqual(datos["motivo"], "no_existe")

    def test_forzar_exige_permiso(self):
        with tenant_context(self.tenant):
            cantinero = Usuario.objects.create_user(
                email=None, nombre="Carla", tenant=self.tenant, numero="0009"
            )
            cantinero.set_pin("9999")
            cantinero.save()
            MembresiaUsuario.objects.create(
                usuario=cantinero, local=self.local,
                rol=Rol.objects.get(codigo="cantinero"),
            )
        self.ingresar(numero="0009", pin="9999")
        respuesta = self.validar_lista(nombre="Ana", forzar="1")
        self.assertEqual(respuesta.status_code, 403)

    def test_quien_no_puede_validar_accesos_no_usa_el_endpoint(self):
        with tenant_context(self.tenant):
            contador = Usuario.objects.create_user(
                email=None, nombre="Conta", tenant=self.tenant, numero="0010"
            )
            contador.set_pin("8888")
            contador.save()
            MembresiaUsuario.objects.create(
                usuario=contador, local=self.local,
                rol=Rol.objects.get(codigo="contador"),
            )
        self.ingresar(numero="0010", pin="8888")
        respuesta = self.validar_lista(nombre="Ana")
        self.assertEqual(respuesta.status_code, 403)

    def test_el_encargado_puede_forzar(self):
        with tenant_context(self.tenant):
            encargado = Usuario.objects.create_user(
                email=None, nombre="Enzo", tenant=self.tenant, numero="0002"
            )
            encargado.set_pin("2222")
            encargado.save()
            MembresiaUsuario.objects.create(
                usuario=encargado, local=self.local,
                rol=Rol.objects.get(codigo="encargado"),
            )
            from apps.ticketing.services import registrar_ingreso_manual
            registrar_ingreso_manual(
                evento=self.evento, cantidad=50, motivo="Lleno", usuario=self.admin
            )
        self.ingresar(numero="0002", pin="2222")
        datos = self.validar_lista(nombre="Ana", forzar="1").json()
        self.assertTrue(datos["ok"])
        with tenant_context(self.tenant):
            self.assertIn("forzado", UsoLista.objects.get().observaciones)
