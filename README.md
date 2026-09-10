# Boliche SIG

Sistema de gestión para pubs y discotecas: **barra/POS, control de acceso y gestión**,
multi-tenant, para el mercado uruguayo.

El diseño completo —decisiones, por qué, y qué se descartó— está en
[`ANALISIS.md`](ANALISIS.md). Ese documento es la fuente de verdad del proyecto: si el
código y el análisis se contradicen, hay que resolver la contradicción, no ignorarla.

## Estado

En construcción, siguiendo la secuencia de `ANALISIS.md` §15 bis.

- [x] **Hito 1** — esqueleto Django + DRF + PostgreSQL, y **multi-tenancy fail-closed**
      con batería de aislamiento entre boliches
- [x] **Hito 2** — usuarios con doble credencial (email+password / número+PIN), roles,
      permisos, membresía usuario×local×rol y auditoría
- [x] **Hito 3** — catálogo: unidades con magnitud y factor, insumos, presentaciones,
      productos, recetas con merma y combos
- [x] **Hito 4** — depósitos por punto, **ledger de stock append-only** y saldo proyectado
- [x] **Hito 5** — turnos, sesión de caja, venta con folio, pago asincrónico, anulación
      con contra-asiento y arqueo por medio de pago
- [x] **Hito 6** — conteo guiado ciego, ajuste de inventario con autorizante y
      reporte de varianza ordenado por impacto en plata
- [x] **Hito 7** — alta de un boliche en un comando, con plantilla del vertical y
      una noche de demostración completa
- [x] **Hito 8** — interfaz web: ingreso por número y PIN, barra con botonera táctil,
      cierre de caja con arqueo y panel del dueño con la varianza
- [ ] **Hito 9** — despliegue con Docker y manual de instalación

**157 tests en verde**, más una verificación de punta a punta por HTTP real
(`scripts/verificar_interfaz.py`). La puerta de la Etapa 0 (aislamiento entre dos tenants) está
cumplida y se verifica en cada corrida.

## Stack

Django 6.1 + Django REST Framework + PostgreSQL 16. La decisión de no sumar un segundo
runtime (por ejemplo NestJS para tiempo real) está fundamentada en `ANALISIS.md` §4: el
tiempo real requerido se cubre con polling.

## Decisiones que ya están en el código

Estas son las que `ANALISIS.md` marca como caras de cambiar después:

| Decisión | Dónde vive |
|---|---|
| `AUTH_USER_MODEL` propio desde la primera migración | `apps/accounts/models.py` |
| Manager **fail-closed**: sin tenant en contexto revienta en vez de devolver todo | `apps/core/managers.py` |
| `unscoped` como escape explícito y único para tareas de plataforma | `apps/core/managers.py` |
| Claves primarias UUID generadas en el origen | `apps/core/models.py` |
| Ledger **append-only**: no se edita ni se borra, se asienta el contrario | `apps/stock/models.py` |
| Clave de idempotencia en los movimientos de stock | `apps/stock/services.py` |
| Unidades con magnitud y factor (recetar ml contra stock en botellas) | `apps/catalog/models.py` |
| Combo ≠ receta: la receta descuenta insumos, el combo agrupa productos | `apps/catalog/models.py` |

## Puesta en marcha

Requiere PostgreSQL. Con un servidor local:

```sql
CREATE ROLE boliche LOGIN PASSWORD 'boliche' CREATEDB;
CREATE DATABASE boliche_sig OWNER boliche;
```

O con Docker: `docker compose up -d db`.

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py test
python manage.py runserver
```

Chequeo de salud: `GET /api/health/`.

### Dar de alta un boliche

```bash
python manage.py provisionar_boliche --nombre "Boliche Nuevo" --slug nuevo --rut 210000000000
```

Deja el local operativo: puntos de stock (Deposito, Barra 1, Barra 2, Puerta), una
terminal por punto, los siete roles con sus permisos, el usuario dueño con PIN y la
plantilla del vertical (unidades, insumos con presentaciones, tragos con receta y combos).

### Usar el sistema

```bash
python manage.py runserver
```

Abrir <http://127.0.0.1:8000/> e ingresar con el número de operador y el PIN. La
interfaz es oscura a propósito: una pantalla blanca al 100 % en un boliche a oscuras
encandila a quien atiende.

Para verificar el flujo completo contra un servidor real (ingreso, apertura de caja,
venta, panel y salida):

```bash
python manage.py runserver 127.0.0.1:8010 &
python scripts/verificar_interfaz.py
```

### Ver el sistema funcionando en un minuto

```bash
python manage.py demo
```

Crea un boliche de demostración y simula una noche entera —carga de stock, venta,
cierre de caja y conteo ciego— y termina imprimiendo el **reporte de varianza**: qué
insumo se fue, cuánto costó y en cuánto tiempo se contó.

## Prueba manual del aislamiento

```bash
python manage.py test apps.tenancy.tests.test_aislamiento
```

Verifica que un boliche no puede **leer** ni **escribir** datos de otro, y que sin tenant
en contexto el sistema falla en vez de filtrar.
