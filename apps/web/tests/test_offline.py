"""Sincronizacion de la barra offline (ANALISIS.md seccion 4).

Lo que tiene que quedar garantizado:
- la cola se puede reintentar sin duplicar ventas;
- los PRECIOS los resuelve el servidor, no la terminal;
- una venta que falla no tumba el resto del lote;
- si la terminal vendio con una lista vieja, queda marcado.
"""

import json
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import Usuario
from apps.catalog.models import Insumo, Producto
from apps.catalog.version import subir_version_del_catalogo
from apps.core.context import tenant_context
from apps.core.provisioning import provisionar_boliche
from apps.sales.models import Pago, SesionCaja, Venta
from apps.sales.services import abrir_caja, abrir_turno
from apps.stock.models import Deposito, StockMovimiento
from apps.stock.services import registrar_movimiento, saldo_calculado
from apps.tenancy.models import Local, Terminal


class BaseSincronizacionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.datos = provisionar_boliche(nombre="Boliche Offline", slug="offline")
        cls.tenant = cls.datos["tenant"]
        cls.admin = cls.datos["admin"]

    def setUp(self):
        with tenant_context(self.tenant):
            self.local = Local.objects.get()
            self.barra = Deposito.objects.get(nombre="Barra 1")
            self.terminal = Terminal.objects.get(nombre="Barra 1")
            self.ron = Insumo.objects.get(nombre="Ron")
            registrar_movimiento(
                deposito=self.barra, insumo=self.ron,
                tipo=StockMovimiento.Tipo.COMPRA, cantidad=Decimal("5000"),
            )
            self.cuba = Producto.objects.get(nombre="Cuba libre")
            self.chopp = Producto.objects.get(nombre="Chopp")
            self.producto_id = str(self.cuba.id)
            self.chopp_id = str(self.chopp.id)
            self.catalogo_version = self.tenant.catalogo_version

        self.client.post(reverse("web:ingresar"), {"numero": "0001", "pin": "1234"})
        self.abrir_caja()

    def abrir_caja(self):
        with tenant_context(self.tenant):
            turno = abrir_turno(local=self.local, nombre="Viernes")
            abrir_caja(
                turno=turno, terminal=self.terminal, usuario=self.admin,
                fondo_inicial=Decimal("1000"),
            )

    def sincronizar(self, ventas, catalogo_version=None):
        cuerpo = {
            "ventas": ventas,
            "catalogo_version": (
                catalogo_version if catalogo_version is not None
                else self.catalogo_version
            ),
        }
        return self.client.post(
            reverse("sincronizar_ventas"),
            data=json.dumps(cuerpo),
            content_type="application/json",
        )

    def una_venta(self, **extra):
        base = {
            "id_local": "local-1",
            "idempotency_key": "clave-1",
            "producto": self.producto_id,
            "cantidad": 1,
            "medio": "efectivo",
            "creada_en_cliente": timezone.now().isoformat(),
        }
        base.update(extra)
        return base


class SincronizacionBasicaTests(BaseSincronizacionTests):
    def test_registra_la_venta_y_descuenta_stock(self):
        respuesta = self.sincronizar([self.una_venta()])
        datos = respuesta.json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["resultados"][0]["estado"], "registrada")
        with tenant_context(self.tenant):
            venta = Venta.objects.get()
            self.assertEqual(venta.total, Decimal("350.00"))
            self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("4950.0000"))

    def test_el_lote_vacio_responde_ok(self):
        datos = self.sincronizar([]).json()
        self.assertTrue(datos["ok"])
        self.assertEqual(datos["resultados"], [])
        self.assertIn("servidor", datos)

    def test_un_cuerpo_invalido_da_400(self):
        respuesta = self.client.post(
            reverse("sincronizar_ventas"), data="{no soy json",
            content_type="application/json",
        )
        self.assertEqual(respuesta.status_code, 400)

    def test_sin_caja_abierta_da_409(self):
        with tenant_context(self.tenant):
            SesionCaja.objects.update(estado=SesionCaja.Estado.CERRADA)
        respuesta = self.sincronizar([self.una_venta()])
        self.assertEqual(respuesta.status_code, 409)
        self.assertIn("caja abierta", respuesta.json()["mensaje"].lower())

    def test_sin_sesion_da_403(self):
        self.client.get(reverse("web:salir"))
        respuesta = self.sincronizar([self.una_venta()])
        self.assertEqual(respuesta.status_code, 403)


class IdempotenciaTests(BaseSincronizacionTests):
    def test_mandar_el_lote_dos_veces_no_duplica(self):
        ventas = [self.una_venta()]
        self.sincronizar(ventas)
        self.sincronizar(ventas)
        self.sincronizar(ventas)
        with tenant_context(self.tenant):
            self.assertEqual(Venta.objects.count(), 1)
            self.assertEqual(saldo_calculado(self.barra, self.ron), Decimal("4950.0000"))

    def test_dos_ventas_distintas_entran_las_dos(self):
        ventas = [
            self.una_venta(idempotency_key="a"),
            self.una_venta(idempotency_key="b", id_local="local-2"),
        ]
        self.sincronizar(ventas)
        with tenant_context(self.tenant):
            self.assertEqual(Venta.objects.count(), 2)

    def test_sin_ninguna_clave_se_rechaza(self):
        """Sin clave no hay forma de garantizar que no se duplique."""
        datos = self.sincronizar(
            [self.una_venta(idempotency_key="", id_local="")]
        ).json()
        resultado = datos["resultados"][0]
        self.assertEqual(resultado["estado"], "error")
        self.assertIn("idempotencia", resultado["mensaje"].lower())

    def test_si_falta_la_clave_se_usa_el_id_local(self):
        datos = self.sincronizar(
            [self.una_venta(idempotency_key="", id_local="local-9")]
        ).json()
        resultado = datos["resultados"][0]
        self.assertEqual(resultado["estado"], "registrada")
        self.assertEqual(resultado["idempotency_key"], "local-9")


class PreciosDelServidorTests(BaseSincronizacionTests):
    def test_el_precio_lo_pone_el_servidor_y_no_el_cliente(self):
        # La terminal intenta mandar un precio propio: se ignora por completo.
        datos = self.sincronizar([
            self.una_venta(precio="1", total="1", precio_unitario="1")
        ]).json()
        resultado = datos["resultados"][0]
        self.assertEqual(resultado["estado"], "registrada")
        self.assertEqual(resultado["total"], "350.00")
        with tenant_context(self.tenant):
            self.assertEqual(Venta.objects.get().total, Decimal("350.00"))

    def test_una_lista_vieja_queda_marcada(self):
        datos = self.sincronizar(
            [self.una_venta()], catalogo_version=(self.catalogo_version + 5)
        ).json()
        resultado = datos["resultados"][0]
        self.assertTrue(resultado["precio_desactualizado"])
        with tenant_context(self.tenant):
            venta = Venta.objects.get()
            self.assertTrue(venta.precio_desactualizado)
            self.assertEqual(venta.catalogo_version, self.catalogo_version + 5)

    def test_la_version_al_dia_no_marca_nada(self):
        datos = self.sincronizar([self.una_venta()]).json()
        self.assertFalse(datos["resultados"][0]["precio_desactualizado"])

    def test_subir_la_version_del_catalogo_marca_las_ventas_viejas(self):
        with tenant_context(self.tenant):
            nueva = subir_version_del_catalogo(self.tenant)
        self.assertEqual(nueva, self.catalogo_version + 1)
        datos = self.sincronizar(
            [self.una_venta()], catalogo_version=self.catalogo_version
        ).json()
        self.assertTrue(datos["resultados"][0]["precio_desactualizado"])


class LoteConErroresTests(BaseSincronizacionTests):
    def test_un_producto_inexistente_no_tumba_el_lote(self):
        ventas = [
            self.una_venta(idempotency_key="ok"),
            self.una_venta(idempotency_key="malo", producto="00000000-0000-0000-0000-000000000000"),
            self.una_venta(idempotency_key="ok2", id_local="local-3", producto=self.chopp_id),
        ]
        datos = self.sincronizar(ventas).json()
        estados = [r["estado"] for r in datos["resultados"]]
        self.assertEqual(estados, ["registrada", "error", "registrada"])
        with tenant_context(self.tenant):
            self.assertEqual(Venta.objects.count(), 2)

    def test_una_cantidad_invalida_se_rechaza_sin_registrar(self):
        datos = self.sincronizar([self.una_venta(cantidad=0)]).json()
        self.assertEqual(datos["resultados"][0]["estado"], "error")
        with tenant_context(self.tenant):
            self.assertEqual(Venta.objects.count(), 0)

    def test_un_producto_sin_receta_igual_entra(self):
        with tenant_context(self.tenant):
            agua = Producto.objects.get(nombre="Agua")
            agua_id = str(agua.id)
        datos = self.sincronizar([self.una_venta(producto=agua_id)]).json()
        self.assertEqual(datos["resultados"][0]["estado"], "registrada")


class CobroSinConexionTests(BaseSincronizacionTests):
    def test_el_efectivo_queda_aprobado(self):
        self.sincronizar([self.una_venta(medio="efectivo")])
        with tenant_context(self.tenant):
            pago = Pago.objects.get()
            self.assertEqual(pago.estado, Pago.Estado.APROBADO)
            venta = Venta.objects.get()
            self.assertEqual(venta.estado, Venta.Estado.PAGADA)

    def test_un_cobro_con_qr_queda_pendiente(self):
        """Sin conexion no se puede confirmar un cobro con el adquirente."""
        datos = self.sincronizar([self.una_venta(medio="qr")]).json()
        self.assertTrue(datos["resultados"][0]["medio_pendiente"])
        with tenant_context(self.tenant):
            pago = Pago.objects.get()
            self.assertEqual(pago.estado, Pago.Estado.PENDIENTE)
            self.assertEqual(Venta.objects.get().estado, Venta.Estado.PENDIENTE)

    def test_la_hora_del_dispositivo_se_guarda_aparte(self):
        momento = timezone.now().isoformat()
        self.sincronizar([self.una_venta(creada_en_cliente=momento)])
        with tenant_context(self.tenant):
            venta = Venta.objects.get()
            self.assertIsNotNone(venta.creada_en_cliente)
            self.assertIsNotNone(venta.creado_en)

    def test_una_hora_ilegible_no_rompe_la_venta(self):
        datos = self.sincronizar(
            [self.una_venta(creada_en_cliente="no es una fecha")]
        ).json()
        self.assertEqual(datos["resultados"][0]["estado"], "registrada")
        with tenant_context(self.tenant):
            self.assertIsNone(Venta.objects.get().creada_en_cliente)
