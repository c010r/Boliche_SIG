# Boliche SIG

Sistema de gestión para pubs y discotecas: **barra/POS, control de acceso y gestión**,
multi-tenant, para el mercado uruguayo.

El diseño completo —decisiones, por qué, y qué se descartó— está en
[`ANALISIS.md`](ANALISIS.md). Ese documento es la fuente de verdad del proyecto:
si el código y el análisis se contradicen, hay que resolver la contradicción, no ignorarla.

## Estado

En construcción. Secuencia de hitos en `ANALISIS.md` §15 bis.

- [x] **Hito 1** — esqueleto del proyecto (Django + DRF + PostgreSQL)
- [ ] **Hito 2** — multi-tenancy *fail-closed* con tests de aislamiento
- [ ] **Hito 3** — auth por PIN, roles/permisos y auditoría
- [ ] **Hito 4** — catálogo con unidades/conversión y ledger de stock append-only
- [ ] **Hito 5** — caja y ventas con invariantes

## Stack

Django + Django REST Framework + PostgreSQL. La decisión de no sumar un segundo runtime
(por ejemplo NestJS para tiempo real) está fundamentada en `ANALISIS.md` §4.

## Puesta en marcha

```bash
docker compose up -d db
python -m venv .venv && .venv/Scripts/activate   # Windows
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py test
```

Para el detalle operativo ver `ANALISIS.md`.
