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
sin HTTPS la cámara del celular no arranca, el service worker de la barra no se
registra, y la validación en puerta no funciona.

Verificar: `curl https://boliches.midominio.com/api/health/`

### Probar el despliegue antes de usarlo

```bash
docker compose -f docker-compose.verificar.yml up -d --build
curl http://127.0.0.1:8099/api/health/
docker compose -f docker-compose.verificar.yml down -v
```

Construye **la misma imagen** que producción contra una base real, sin el proxy TLS
(que necesita un dominio de verdad). Sirve para comprobar que la imagen construye,
migra y responde **antes** de apuntar el dominio.

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

### Si se cae el internet

**La barra sigue vendiendo.** No hay que hacer nada especial: la venta se guarda en la
tablet y se manda sola cuando vuelve la conexión. El indicador arriba de la botonera
dice *"Sin conexión: podés seguir vendiendo"* y después *"Enviando N…"*. **Nunca se pone
en rojo**, a propósito: un cartel de alarma hace que el cantinero deje de confiar y pare
de vender, que es peor que la caída.

Detalles que conviene saber:

- El **efectivo** se cobra y queda confirmado. Un cobro por **QR o tarjeta** queda
  **pendiente** hasta que vuelva la red: sin conexión no hay forma de confirmarlo con el
  adquirente, y fingir que sí sería peor.
- La tablet manda la **versión de la lista de precios** que tenía. Si cambió mientras
  estaba sin conexión, la venta entra igual —el cliente ya se llevó su trago— pero queda
  **marcada** para que el reporte lo muestre.
- Reintentar la cola **no duplica nada**: cada venta lleva su clave de idempotencia.
- La pantalla abre sin conexión porque el **service worker** la tiene en caché. Eso
  **exige HTTPS**: sin TLS el navegador no registra el service worker y la pantalla no
  abre si se cae la red. Es la misma razón por la que la cámara de la puerta necesita
  HTTPS.
- **La puerta no funciona offline, a propósito.** Dos puertas sin conexión aceptarían la
  misma entrada y el aforo se rompería.

### Anular

Requiere el permiso `ventas.anular` (el rol **cantinero no lo tiene**). Exige
motivo y **no borra nada**: queda la venta anulada, quién la anuló y el
movimiento contrario en el stock.

### Vender entradas online (preventa)

La tienda pública está en **`/tienda/<slug-del-boliche>/`** y **no requiere cuenta**:
el comprador elige el evento, la cantidad y paga.

El sistema funciona con el patrón de **reserva con expiración**, que es lo que evita
sobrevender:

1. **Reserva:** se le toma el cupo al comprador durante unos minutos (10 por defecto).
2. **Pago:** el comprador paga en ese plazo, con un reloj en pantalla.
3. **Confirmación:** recién ahí se emiten las entradas y se le muestra el QR.
4. **Expiración:** si no paga, el lugar se libera solo.

**Si el pago se acredita después de que la reserva venció, el sistema NO emite la
entrada**: marca el pago como tardío, le avisa al comprador y hay que devolverle el
dinero. Prefiere devolver un pago antes que sobrevender el aforo.

Configuración: `RESERVA_MINUTOS_DE_VALIDEZ` en el `.env`.

Para que los estados queden prolijos conviene correr la limpieza cada minuto:

```bash
* * * * * cd /ruta/al/proyecto && python manage.py expirar_reservas
```

Sin ese cron **igual no se sobrevende**: el cupo de una reserva vencida se libera
solo, porque el cálculo de disponibles excluye las que ya pasaron su hora.

#### Conectar Mercado Pago (lo que falta hacer)

El proveedor real **ya está implementado** contra la **Orders API** de Mercado Pago,
que es la vigente: la de QR legacy está en discontinuación. Lo que falta es cargar las
credenciales de cada boliche.

**Cada local cobra en SU cuenta**: el local es el comerciante y asume los contracargos.
El proveedor del software no es intermediario de pagos.

Pasos, por cada boliche:

1. Crear una aplicación en [Mercado Pago > Tus integraciones](https://www.mercadopago.com.uy/developers/panel/app).
2. Crear la **sucursal y caja** del local, y anotar su `external_pos_id`.
3. En **Webhooks**, cargar como URL de producción:
   `https://<dominio>/api/pagos/webhook/<slug-del-boliche>/` y seleccionar el evento
   **Order (Mercado Pago)**. Guardar la **clave secreta** que genera.
4. Cargar en el sistema la credencial del boliche (`CredencialDePago`): proveedor
   `mercadopago`, `access_token`, `external_pos_id`, `clave_de_firma` y `activo=True`.

> **El slug va en la URL a propósito.** Para validar la firma hay que conocer la clave
> del boliche **antes** de mirar el cuerpo, y el cuerpo no es confiable hasta que la
> firma valide.

**Antes de cobrar de verdad, comprobar contra el sandbox**: el proveedor está probado
con un transporte simulado que verifica el cuerpo y las cabeceras que se envían, y el
mapeo de estados está tomado de la documentación, pero **no se ejecutó todavía contra
la API real**. Hay que hacer una compra de prueba.

Detalles que ya están resueltos en el código:

- La orden vence a los **15 minutos** (predeterminado de Mercado Pago) y nuestra reserva
  vence a los **10**: si el pago se acredita tarde, la reserva ya venció y el sistema lo
  detecta en vez de sobrevender.
- La notificación **consulta** la orden; no se confía en el cuerpo.
- El estado que manda es el de la **transacción**, no el de la orden: una orden
  procesada con la transacción sin acreditar **no tiene la plata adentro**.
- Sin clave de firma configurada, el sistema **no valida** y lo avisa: es preferible
  fallar de forma visible antes que aceptar notificaciones sin verificar.

### Vender entradas en la puerta

1. **Eventos → abrir el evento → elegir tipo y cantidad → Emitir y cobrar.** Si hay
   una caja abierta, la venta entra al arqueo; si no, el sistema avisa que no.
2. Con **una** entrada emitida se abre la pantalla del QR, listo para mostrar.
3. El cupo por tipo y el **aforo del evento** bloquean la venta: no se sobrevende.
   El aforo es la capacidad del local que fiscaliza la Intendencia.

### Validar en la puerta

**Puerta → elegir el evento.**

- **Encender cámara** lee el QR solo (Android con Chrome). En iPhone el navegador no
  lee QR automáticamente: usar el código a mano.
- La cámara **necesita HTTPS**. Si no arranca, el campo de código a mano siempre
  funciona.
- El resultado se ve en grande, en verde o rojo, **y suena**. En la puerta el sonido
  sirve porque el operador mira de reojo mientras hay cola.
- **Una entrada se usa una sola vez.** El segundo escaneo la rechaza, aunque venga de
  otra puerta al mismo tiempo.
- Si el aforo está completo, deja entrar igual —la entrada es válida— y **avisa en
  pantalla**.

### Listas de invitados y promotores

**Eventos → abrir el evento → Listas y comisiones.**

Se crean listas con **cupo total** y **hora de corte** opcional, y se cargan invitados
que cubren **1 + acompañantes**. La pantalla de la puerta tiene un campo para buscar por
nombre (no hace falta que el invitado traiga QR) y el mismo lector de QR sirve: si el
código no es una entrada, el sistema lo busca entre las invitaciones.

Tres reglas que el sistema aplica solo:

1. **El corte lo aplica el sistema, no el de la puerta.** Si queda a criterio del
   personal, el criterio es donde se va la plata. El corte se calcula sobre la hora de
   apertura del evento, así que un corte a la 01:00 con apertura a las 23:00 cae al día
   siguiente y no en el pasado.
2. **La atribución se registra al ingresar y no se puede editar.** Un uso de lista no se
   modifica ni se borra: si el promotor pudiera agregar gente después, todos reclamarían
   a todos.
3. **La lista consume aforo.** Los invitados ocupan capacidad, y el aforo es lo que
   fiscaliza la Intendencia. Si el aforo está completo, el sistema no deja pasar salvo
   que alguien con el permiso `acceso.forzar` lo autorice, y el ingreso queda marcado.

**Comisiones:** se liquidan por evento y salen de las personas que **quedaron registradas
en la puerta** por las listas de cada promotor, nunca de lo que declara el promotor. El
circuito es *calcular → aprobar → pagar*, y cada paso queda con su responsable.

### Ingreso manual

Invitado, lista, o alguien que pasó sin escanear. **Siempre con motivo.** Es el
equivalente en la puerta del cobro no registrado: si se puede hacer sin dejar rastro,
se va a hacer.

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
| **Se cayó el internet** | Se sigue vendiendo: la tablet guarda y sincroniza sola. Los cobros por QR quedan pendientes. **No reinicies ni cierres la pantalla.** |
| **No imprime** | Todavía no hay impresión en esta versión: la comanda es la pantalla. |
| **La tablet se quedó sin batería** | Cambiala por la de repuesto y volvé a ingresar con el mismo número y PIN: la caja sigue abierta. |
| **No me acuerdo el PIN** | Tres intentos fallidos y el operador queda bloqueado. Lo desbloquea el encargado. |
| **Cobré mal y ya cerré la venta** | Se anula con motivo. Queda registrado; no se borra. |
| **La caja no me cierra** | Escribí lo que contaste. Si hay diferencia, la autoriza el encargado con su número. **No ajustes los números para que den.** |
| **El comprador pagó y la reserva venció** | El sistema le avisa y hay que devolverle el pago. **No se lo hace entrar por arriba del aforo**: es la capacidad que fiscaliza la Intendencia. |
| **El QR no lee** | Usá el campo de código a mano: el token está en la pantalla del comprador. Si tampoco, ingreso manual con motivo. |
| **La entrada ya figura usada y el cliente protesta** | Está cumpliendo su función: alguien la usó antes. No hay forma de "reusarla"; si hay que dejarlo pasar, es ingreso manual con motivo. |
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
completo y verificado con **332 tests**, cuatro verificaciones de punta a punta por
HTTP (barra y caja, puerta con listas, compra online y barra offline) y el despliegue
Docker construido y comprobado.

**Falta:** cargar las credenciales de Mercado Pago de cada boliche y hacer una compra
de prueba contra su sandbox. El código de la integración está completo y probado sin
red.
