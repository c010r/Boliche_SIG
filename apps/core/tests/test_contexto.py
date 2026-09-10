from django.test import SimpleTestCase

from apps.core.context import (
    clear_current_tenant,
    get_current_tenant,
    set_current_tenant,
    tenant_context,
)
from apps.core.middleware import TenantMiddleware


class ContextoDeTenantTests(SimpleTestCase):
    def test_sin_contexto_devuelve_none(self):
        self.assertIsNone(get_current_tenant())

    def test_set_y_clear_restauran_el_valor_previo(self):
        token = set_current_tenant("A")
        self.assertEqual(get_current_tenant(), "A")
        clear_current_tenant(token)
        self.assertIsNone(get_current_tenant())

    def test_tenant_context_anida_y_restaura(self):
        with tenant_context("A"):
            self.assertEqual(get_current_tenant(), "A")
            with tenant_context("B"):
                self.assertEqual(get_current_tenant(), "B")
            self.assertEqual(get_current_tenant(), "A")
        self.assertIsNone(get_current_tenant())

    def test_tenant_context_restaura_aunque_falle(self):
        with self.assertRaises(ValueError):
            with tenant_context("A"):
                raise ValueError("boom")
        self.assertIsNone(get_current_tenant())


class ResolucionDeSubdominioTests(SimpleTestCase):
    def test_extrae_el_slug_del_subdominio(self):
        self.assertEqual(
            TenantMiddleware.slug_from_host("boliche-a.ejemplo.com"), "boliche-a"
        )

    def test_ignora_el_puerto(self):
        self.assertEqual(
            TenantMiddleware.slug_from_host("boliche-a.ejemplo.com:8000"), "boliche-a"
        )

    def test_sin_subdominio_devuelve_vacio(self):
        self.assertEqual(TenantMiddleware.slug_from_host("ejemplo.com"), "")
        self.assertEqual(TenantMiddleware.slug_from_host("localhost:8000"), "")
