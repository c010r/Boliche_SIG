# Manual de Boliche SIG

Sistema de gestión para pubs y discotecas: **barra, caja y control de mermas**.
El diseño y el porqué de cada decisión están en [`ANALISIS.md`](ANALISIS.md).

---

## 1. Qué resuelve

| Problema del boliche | Cómo lo resuelve |
|---|---|
| Cierres de caja eternos | Arqueo por medio de pago, esperado calculado por el sistema |
| No se sabe cuánta bebida se fue | Consumo teórico por receta contra conteo físico: **varianza valorizada** |
| Diferencias que nadie explica | Ledger inmutable: nada se borra, todo deja rastro |
| Cuentas que se pierden entre turnos | Una caja por terminal, cierre por persona |
| No se sabe a quién atribuir un faltante | Varianza por punto de stock y por turno |

**Lo que el sistema NO promete:** no elimina el robo. El 40 % de la merma de un
bar es servir de más, no robo (ver `ANALISIS.md` §11 ter). El sistema **mide**;
la decisión es del dueño.

---

## 2. Instalación en producción (recomendada)

Requiere un servidor con Docker, un dominio apuntando a su IP, y puertos 80 y 443 abiertos.

```bash
git clone https://github.com/c010r/Boliche_SIG.git
cd Boliche_SIG
cp .env.example .env
```

Editar `.env`:

```ini
DJANGO_SECRET_KEY=<clave larga y aleatoria>
POSTGRES_PASSWORD=<clave fuerte>
DOMINIO=boliches.midominio.com
```

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

El proxy **saca el certificado TLS automáticamente**. Esto no es una formalidad:
sin HTTPS la cámara del celular no arranca y la validación en puerta no funciona.

Verificar: `curl https://boliches.midominio.com/api/health/`

### Respaldos

El servicio `backup` corre solo: un volcado diario en `./backups` con retención de
14 días. Restaurar:

```bash
docker compose -f docker-compose.prod.yml exec -T db \
  pg_restore -U boliche -d boliche_sig --clean --if-exists < backups/boliche_20260101_0400.dump
```

**Probá la restauración antes de necesitarla.** Un respaldo que nunca se restauró
no es un respaldo.

---

## 3. Instalación para desarrollo

Requiere PostgreSQL 14 o superior.

```sql
CREATE ROLE boliche LOGIN PASSWORD 'boliche' CREATEDB;
CREATE DATABASE boliche_sig OWNER boliche;
```

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py test
python manage.py runserver
```

Abrir <http://127.0.0.1:8000/>.

---

## 4. Alta de un boliche

```bash
python manage.py provisionar_boliche \
  --nombre "Boliche Nuevo" --slug nuevo --rut 210000000000 --aforo 300
```

Deja el local operativo en un paso:

- Local con aforo, y puntos de stock **Deposito, Barra 1, Barra 2 y Puerta**
- Una terminal por punto, cada una ligada a su punto de stock
- Los siete roles con sus permisos
- El usuario dueño, número `0001` y PIN `1234`
- La plantilla del vertical: insumos con sus presentaciones de compra y tragos con receta

**Cambiar el PIN del dueño antes de operar.**

Para ver el sistema con datos creíbles en un minuto:

```bash
python manage.py demo
```

---

## 5. Operación de una noche

### Abrir caja

1. Ingresar en la tablet con **número de operador y PIN** (no email ni contraseña).
2. **Caja → elegir terminal → fondo inicial → Abrir caja.**

Una terminal no puede tener dos cajas abiertas a la vez: el sistema lo impide.

### Vender

**Barra → tocar el producto → tocar el medio de pago.** Dos toques.

- La botonera pone primero lo más vendido de la sesión.
- **Efectivo** cobra en el acto. **QR y tarjeta** quedan pendientes hasta que el
  adquirente confirme: la barra no se traba esperando.
- El stock se descuenta en el momento, según la receta del producto.

### Anular

Requiere el permiso `ventas.anular` (el rol **cantinero no lo tiene**). Exige
motivo y **no borra nada**: queda la venta anulada, quién la anuló y el
movimiento contrario en el stock.

### Cerrar caja

1. **Caja → Cierre y arqueo.**
2. El sistema muestra el **esperado por medio de pago**. Vos escribís lo que contaste.
3. Si hay diferencia, hay que poner el **número del autorizante**, y no puede ser
   el mismo que cierra la caja. Nadie autoriza su propia diferencia.

### Contar stock (lo que hace que el sistema valga la pena)

1. Cada punto se cuenta por separado: cada cantinero su barra, el encargado el depósito.
2. Se cuenta en dos partes: **botellas selladas exactas** y la **botella empezada por
   fracción** (vacía, 1/4, 1/2, 3/4, llena).
3. **El conteo es ciego**: el sistema no muestra el teórico mientras contás. Si lo
   mostraras, contarías para que coincida y el control no serviría.
4. Al cerrar, el sistema revela el teórico, calcula la diferencia y la valoriza en plata.

Un ajuste que supera el umbral configurado exige un autorizante.

---

## 6. Panel del dueño

**Panel** (requiere permiso `reportes.ver`) muestra:

- **Varianza valorizada** por insumo y punto, ordenada por impacto en plata
- **Puntos sin conteo**, marcados como tales: *no medido no es cero*
- Duración promedio del conteo, medida por el sistema
- Cobrado por medio de pago
- Anulaciones recientes con su motivo y su autor
- Últimos turnos cerrados

---

## 7. Runbook de noche

Lo que el personal tiene que saber sin llamar a nadie. Imprimilo y dejalo al lado
de la caja.

| Qué pasa | Qué hacer |
|---|---|
| **Se cayó el internet** | Se sigue vendiendo. Los cobros por QR quedan pendientes y se confirman solos cuando vuelve. **No reinicies nada.** |
| **No imprime** | Todavía no hay impresión en esta versión: la comanda es la pantalla. |
| **La tablet se quedó sin batería** | Cambiala por la de repuesto y volvé a ingresar con el mismo número y PIN: la caja sigue abierta. |
| **No me acuerdo el PIN** | Tres intentos fallidos y el operador queda bloqueado. Lo desbloquea el encargado. |
| **Cobré mal y ya cerré la venta** | Se anula con motivo. Queda registrado; no se borra. |
| **La caja no me cierra** | Escribí lo que contaste. Si hay diferencia, la autoriza el encargado con su número. **No ajustes los números para que den.** |
| **Se cayó el servidor** | Avisar al proveedor. El local necesita su talonario de **comprobantes de contingencia** en papel (obligación del local, ver §8). |

---

## 8. Cumplimiento (Uruguay)

- **Facturación electrónica:** obligatoria para todos los contribuyentes de IVA. **Esta
  versión no emite CFE.** El local sigue necesitando su proveedor de facturación:
  a volumen de barra cuesta del orden de USD 55/mes (ver `ANALISIS.md` §8).
- **Comprobantes de contingencia (CFC):** es obligación del local tener el talonario
  autorizado por DGI. Se necesita cuando **no se puede usar el sistema**, no cuando
  falla la conexión.
- **Protección de datos (Ley 18.331):** el local es responsable del tratamiento y el
  proveedor es encargado. Hay que firmar la cláusula correspondiente. No se guardan
  datos de tarjetas: solo medio y monto.
- **Aforo:** lo controla la Intendencia. El aforo del local se carga en el alta.

---

## 9. Roles y permisos

| Rol | Puede |
|---|---|
| **Dueño** | Todo |
| **Encargado de noche** | Vender, anular, autorizar diferencias y ajustes, ver reportes, contar |
| **Cantinero** | Vender, abrir y cerrar su caja, contar. **No puede anular ni autorizar** |
| **Cajero de puerta** | Vender entradas y validar accesos |
| **Seguridad** | Solo validar accesos |
| **Promotor** | Solo gestionar listas |
| **Contador** | Solo ver reportes y stock |

La distinción es deliberada: **quien cobra, quien supervisa y quien administra no se
mezclan en la misma sesión.**

---

## 10. Estado del desarrollo

Ver el detalle en [`README.md`](README.md). Resumen: el núcleo de **Fase 1** está
completo y verificado con **157 tests** más una verificación de punta a punta por HTTP.

**Falta:** módulo de entradas y control de acceso con QR, listas y promotores
(Fase 2), y modo offline en la barra (Fase 3).
