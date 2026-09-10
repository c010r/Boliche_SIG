# Producto para pubs y discotecas — Análisis consolidado

> Documento de análisis. **No contiene código.** Consolida el trabajo de las rondas 1 a 6
> sobre el plan de replicar GBol, con las correcciones, los números verificados y las
> decisiones pendientes. Reemplaza la necesidad de releer el hilo de conversación.
>
> Fecha: 2026-09-09 · Estado: análisis cerrado, pendiente de decisión para construir.

---

## 1. Resumen ejecutivo

El plan es sólido en lo esencial: recorta bien el alcance del MVP, ordena las fases
correctamente y acierta al identificar la adquisición del primer cliente como el paso
crítico. Pero **subestima la operación en vivo y sobreestima el valor del cumplimiento
fiscal como diferencial**. Cuatro correcciones lo vuelven vendible y usable:

1. **El alquiler de hardware a USD 10/mes/dispositivo no cierra.** Se recupera la
   inversión en ~40 meses. El hardware se vende, no se alquila.
2. **USD 100/mes de licencia es insuficiente** si se incluye facturación electrónica
   (que sola cuesta USD 10-30/mes) y soporte de madrugada. El precio correcto para el
   segmento nocturno es **USD 180-250/mes**.
3. **El pago asincrónico y el stock por punto de venta** son requisitos de arquitectura
   que el modelo de datos del plan no cubre, y que rompen la caja el primer sábado.
4. **La e-factura no es un diferencial**: varios adquirentes uruguayos ya la incluyen con
   su POS. El diferencial real es la capa operativa del nocturno.

**Veredicto:** es vendible y usable, pero el mercado uruguayo lo corona como un negocio
de nicho part-time (~20-25 clientes con un operador). La escalabilidad exige regionalizar
después, y eso obliga a rehacer el cumplimiento fiscal por país.

---

## 2. Premisas del plan evaluadas

| Premisa del plan | Veredicto |
|---|---|
| "No hace nada tecnológicamente exótico" | **Correcto.** La dificultad es operativa, no algorítmica |
| "CRM/POS multi-tenant existente sobre Django + NestJS" | No verificable en este entorno; se trató como premisa dada |
| "El control de stock por receta es el algoritmo de GBol" | **Correcto**, y es la pieza que define el modelo de datos |
| "e-factura como ventaja competitiva real" | **Parcialmente falso.** Ver §8 |
| "Cashless/VIP/multi-sede fuera del MVP" | **Correcto**, y es la mejor decisión del plan |
| "El primer cliente es el paso crítico" | **Correcto.** Ver §13 |

---

## 3. Alcance del MVP y criterios de aceptación

El alcance del plan (barra/POS, gestión, entradas con QR) se mantiene. Se le agregan
**criterios de aceptación medibles**, que el plan no tenía y sin los cuales "funciona"
no es verificable:

| Criterio | Umbral |
|---|---|
| Venta completa en barra | <= 3 toques, <= 5 s |
| Cierre de caja | <= 15 min, con diferencia por medio de pago |
| Validación en puerta | <= 1 s con feedback audible y visual |
| Stock teórico vs real por evento | < 2 % de diferencia |
| Sobreventa de aforo | 0 |
| Ventas duplicadas con 30 min de cola offline | 0 |

---

## 4. Arquitectura: decisiones y correcciones

Se mantiene **Django + DRF + PostgreSQL** del plan, con estas correcciones:

- **No sumar NestJS.** El tiempo real requerido (aforo, cajas en vivo, reportes del
  dueño) se cubre con polling cada pocos segundos: un solo deploy, un solo sistema de
  autenticación. Si hace falta push real, Django Channels.
- **Auth para operación nocturna:** nadie tipea email y contraseña en una tablet a las
  3 AM. Se necesita usuario + **PIN de 4-6 dígitos** atado al turno, con sesión de caja
  persistente.
- **Pago asincrónico obligatorio.** El cobro por QR se confirma por webhook minutos
  después. Una venta con "medio de pago" como campo plano no lo soporta.
- **Idempotencia** en todo POST de cobro, para que el reintento de la cola offline no
  duplique ventas.
- **Append-only.** Ventas, pagos, movimientos de stock y movimientos de aforo son
  asientos inmutables. Anular = contra-asiento, nunca borrar. Esto resuelve de una sola
  vez arqueo, reportes, sincronización offline y —más adelante— cashless.
- **Particionado: prematuro.** Con los volúmenes del MVP no se justifica. Lo que sí va
  desde el día 1: índices compuestos `(tenant_id, fecha)` y el ledger inmutable.

### Requisitos operativos que el plan omite

- **HTTPS obligatorio.** El acceso a cámara del celular exige *secure context*. Sirviendo
  por IP en la red del local, la validación por QR de la Fase 2 no arranca. Dominio +
  certificado válido desde el primer despliegue.
- **La impresión pasa por el backend.** Una PWA no puede abrir un socket TCP crudo a una
  impresora ESC/POS de red.
- **QR con fallback:** `BarcodeDetector` tiene soporte irregular en iOS; se necesita
  respaldo con `html5-qrcode`.

---

## 4 bis. Seguridad y confianza en el cliente

### El PIN y la higiene de sesión son controles antifraude

El §11 ter atribuye varianza y excepciones **a personas**. Eso sólo vale si la sesión
identifica de verdad a quien está vendiendo:

- **PIN por persona, nunca login compartido.** Un usuario "Barra 1" compartido destruye la
  atribución, que es la base de todo el control.
- **PIN de 4 dígitos son 10.000 combinaciones** en una tablet compartida. Hacen falta
  límite de intentos, bloqueo temporal y escalado al encargado. De 6 dígitos es mejor, pero
  sólo si no empuja al personal a anotarlo en el borde de la barra.
- **Bloqueo por inactividad con reingreso en un toque.** El equilibrio es delicado: bloquear
  demasiado cuesta ventas, no bloquear destruye la atribución. La regla operativa es
  **bloquear por inactividad, nunca en medio de una venta**.
- Una sesión abierta y abandonada es, en los hechos, un agujero de fraude: cualquiera
  vende a nombre de otro.

### El cliente no es confiable: los precios se resuelven en el servidor

La cola offline vive en el navegador y **puede ser editada**. Consecuencia dura: el servidor
**nunca** debe aceptar del cliente el precio ni el total; los recalcula contra el catálogo.

Eso abre un problema real: si el precio cambió entre la venta y la sincronización, ¿cuál
vale? La respuesta es **versionar el catálogo**: la venta offline referencia la versión de
catálogo vigente en el momento, y el servidor valida contra esa versión. Sin versionado, la
cola offline es o insegura o injusta.

### Dispositivos

- **Vinculación de dispositivo:** una terminal pertenece a un dispositivo registrado, y el
  proveedor puede **revocar remotamente**. Una tablet robada con sesión válida es una fuga
  de datos, no sólo una pérdida de hardware.
- **Tokens de vida corta** y revocación remota. Consecuencia asumida: el almacenamiento local
  de una PWA (IndexedDB) **no está cifrado**. Se mitiga con tokens cortos y revocación, no
  con cifrado propio.
- **La tarjeta nunca pasa por el sistema.** El cobro con tarjeta ocurre en la terminal del
  adquirente: el sistema registra el medio y el monto, nunca el número. Esto mantiene al
  producto **fuera del alcance de PCI** — y hay que cuidarlo como una restricción de
  diseño, no como una casualidad.

## 5. Modelo de datos — Fase 1

Corrige y completa el §3 del plan original.

**Plataforma y accesos**
- `Tenant` — nombre, slug/subdominio, plan, estado, **RUT** y datos fiscales.
- `Local` — tenant, dirección, aforo, zona horaria.
- `Terminal` — tenant, local, nombre ("Barra 1", "Puerta"), tipo, activo.
  Sirve para caja, para puerta y para facturar por dispositivo.
- `Usuario` + `MembresiaUsuario` (usuario x local x rol) + `Rol` + `Permiso` + `RolPermiso`.
- `AuditoriaLog` — quién, qué, cuándo, entidad, antes/después, terminal.

**Catálogo, insumos y stock**
- `UnidadMedida` + `UnidadMedidaConversion` — **sin esto no se puede recetar "50 ml de
  ron" contra stock en botellas**, y el stock teórico nunca cuadra.
- `Insumo` — costo promedio ponderado, stock mínimo, código de barras.
- `Producto` (con `es_combo`) + `Receta`/`RecetaItem` + `ComboItem`.
  *Un combo agrupa productos vendibles; una receta descuenta insumos. Son cosas distintas
  y el plan las confunde.*
- `Deposito` — **por punto** (Depósito, Barra 1, Barra 2, VIP), no por local.
- `StockItem` — saldo por insumo x depósito (proyección).
- `StockMovimiento` — append-only, con tipo (compra, venta, traslado, merma, consumo
  interno, ajuste, inventario) y cantidad firmada.

**Ventas y caja**
- `Turno` — la noche/evento.
- `SesionCaja` — turno, terminal, usuario, fondo inicial.
  **Invariante: una sola sesión abierta por terminal** (índice único parcial).
- `Venta` — sesión, cantinero, terminal, folio correlativo, estado, IVA, total,
  **UUID generado en el cliente**, **clave de idempotencia** y **dos timestamps
  separados** (`creada_en` servidor / `creada_en_cliente`): es lo que permite ordenar
  correctamente las ventas que llegaron tarde por la cola offline.
- `ItemVenta` — guarda el precio del momento, no una referencia al precio actual.
- `Pago` — N por venta, con medio, monto, **estado propio**, referencia externa y
  payload crudo.
- `EgresoCaja` — retiros parciales y gastos, con autorizante.
- `Arqueo` — esperado vs declarado **por medio de pago**, diferencia y autorizante.

**Ticketing (Fase 2)**
- `Evento` — fecha, horario, **aforo**, estado, precios.
  *El plan no lo tenía y sin él no hay aforo por fecha, ni precio por fecha, ni regla de
  rechazo de entradas de otro evento. Es el hueco más grande del modelo original.*
- `TipoEntrada` — general, damas, VIP, con cupo propio.
- `Entrada` — folio, estado, **`qr_token` aleatorio de alta entropía** (nunca el ID),
  canal, pago vinculado y datos de uso.
- `MovimientoAforo` — append-only; el aforo en vivo es una proyección, no un contador
  mutable.

### Invariantes a testear
1. Una sola sesión de caja abierta por terminal.
2. Una entrada se consume una sola vez (consumo atómico verificando filas afectadas).
3. Toda venta pagada: suma de pagos aprobados = total.
4. Ninguna consulta devuelve datos de otro tenant.
5. Todo POST de cobro es idempotente ante reintento.
6. Por insumo x depósito: suma de movimientos = saldo del `StockItem`.

### La regla que evita el descuadre más probable
Un pago puede aprobarse **después de que el cajero cerró la caja**. No se edita un arqueo
cerrado: el cobro tardío se asienta como línea propia referenciando el turno anterior.

---

## 6. Multi-tenancy

Se adopta **row-based con `tenant_id`**, como recomienda el plan, pero con mecanismo de
cumplimiento — sin él, la migración es un `ALTER TABLE` y una fuga de datos es un bug de
producción:

- **Fail-closed, no fail-open:** Manager por defecto que **falla si no hay tenant en
  contexto**, en vez de devolver todo. Un `objects` sin scope debe reventar, no filtrar.
- Manager `unscoped` explícito para tareas de plataforma, únicas que cruzan tenants.
- `TenantMiddleware` que resuelve el tenant por subdominio (staff), token (API) y
  **dominio público** (la tienda de entradas es anónima).
- **Tests de aislamiento generados por introspección**: por cada modelo de negocio, crear
  datos en A y B y verificar que A no ve B. Generarlos automáticamente impide que al
  agregar una feature quede un modelo sin cubrir.
- Medios y archivos aislados por prefijo de tenant.

---

## 7. Operación nocturna

| Hora | Quién | Qué pasa |
|---|---|---|
| 21:00 | Encargado | Abre turno, verifica impresoras y stock inicial |
| 21:15 | Cantineros | Cada uno abre su caja con fondo, con PIN |
| 22:00-03:00 | Cantineros | Venta en 3 toques; merma y consumo interno se registran en el momento |
| 22:00-04:00 | Puerta | Escaneo < 1 s con sonido y color |
| 03:00 | Cantineros | Conteo, arqueo por medio de pago, cierre de caja |
| 03:15 | Encargado | Cierre de turno y reporte al celular del dueño |

**Caminos de excepción** (lo que define si el producto sirve):
wifi caído -> barra sigue en cola local, puerta pasa a modo lista · tablet sin batería ->
repuesto conservando la sesión · impresora muerta -> comanda en pantalla · QR ilegible ->
ingreso manual por folio registrado · pago demorado -> venta pendiente y cobro tardío.

**Principio rector:** todo camino de excepción deja rastro. El riesgo no es el error, es
el error invisible.

### La UX del entorno real

El criterio de "3 toques y 5 segundos" no se logra con una interfaz linda: se logra
respetando las condiciones del lugar. Un boliche a las 3 AM impone restricciones que
ninguna guía de diseño de POS de oficina contempla:

| Restricción | Consecuencia de diseño |
|---|---|
| **Ruido por encima de los 90 dB** | El feedback audible **no sirve**. Todo aviso es visual, o háptico si el dispositivo lo permite |
| **Oscuridad** | Tema oscuro por defecto y brillo acotado de noche. **Una pantalla blanca de 100 % encandila al cantinero y a los clientes, y termina con la tablet apagada o dada vuelta** |
| **Manos mojadas, pegajosas, con hielo** | Las pantallas capacitivas fallan con dedos húmedos: blancos táctiles grandes, sin gestos de precisión, sin pulsación larga |
| **Una sola mano libre** | Toda acción crítica alcanzable con el pulgar, sin soltar la botella |
| **Guantes en la puerta (invierno)** | Los guantes gruesos no funcionan con capacitivo: en la puerta hay que ofrecer **lector físico** como opción |
| **El cantinero mira de reojo** | El estado se lee de un vistazo: color y tamaño antes que texto |

Reglas concretas:

1. **Tema oscuro y brillo acotado por horario.** Sin destellos blancos a pantalla completa.
2. **Blancos táctiles grandes**, con separación suficiente para evitar toques cruzados.
3. **Máximo 3 toques**, con los productos más vendidos primero — **ordenados por venta
   real, no alfabéticamente**.
4. **Ordenamiento aprendido por franja horaria.** A la 1 AM y a las 4 AM no se vende lo
   mismo; el orden de la botonera debería seguirlo.
5. **Deshacer en lugar de confirmar.** A las 3 AM un diálogo de confirmación cuesta una
   venta. La anulación se permite, pero queda como contra-asiento y se reporta (ver §11 ter).
6. **Nada de tipear en el camino crítico.**
7. **El indicador de sin conexión tiene que ser tranquilizador, no alarmante.** El modo
   offline no falla por un problema técnico: falla porque **el cantinero deja de confiar
   y para de vender**. La pantalla tiene que decir que se sigue vendiendo y que se
   sincroniza solo, sin rojos ni íconos de advertencia.

En la **puerta** la lógica se invierte: el sonido **sí** sirve —el operador mira de reojo
mientras hay cola y el ruido en el acceso es menor que en la barra—, así que ahí el
feedback es **sonoro y cromático a la vez**, y la decisión tiene que resolverse en menos
de un segundo.

### Offline: dónde sí y dónde no
Las **ventas** toleran offline (asientos inmutables + idempotencia). La **validación de QR
en puerta no**: dos puertas sin conexión aceptan la misma entrada y el aforo se rompe.
Offline-first en barra, online-siempre en puerta.

---

## 8. Cumplimiento fiscal (DGI / CFE)

- **La obligación ya está vigente.** Desde 2024-2025 prácticamente todos los
  contribuyentes activos —incluidos IVA mínimo— deben emitir CFE. El alta dejó de ser
  opcional y exige certificado digital.
- **Consecuencia sobre la propuesta:** si el MVP no emite CFE, el boliche debe mantener
  otro sistema para facturar, y "todo en un solo sistema" se cae en la primera reunión
  con el contador.
- **Obligación propia:** Resolución DGI **167/021** obliga a los proveedores de software
  que intervienen en facturación y/o conservación de CFE a informar todas sus soluciones
  y **a cada emisor electrónico que las usa**, dentro del mes siguiente al inicio de la
  provisión, declarando que el software no permite emitir fuera del estándar. Incumplir:
  sanciones e **inhabilitación** si se está en el Registro de Proveedores Habilitados.
  => El alta de cada cliente debe registrar **RUT y fecha**. DGI publica el listado
  mensualmente: estar en él es un sello de legitimidad que un competidor extranjero no
  tiene.
- **Arquitectura:** integrarse vía un **proveedor ya inscripto en el Registro de
  Proveedores Habilitados**, no hablar SOAP contra DGI con un certificado por tenant.
- **Granularidad de emisión: resuelta.** El régimen define el **e-Ticket** como el
  comprobante para operaciones con consumidores finales (Res. DGI 798/2012, num. 1º).
  Un boliche vende bebidas a consumidor final: **corresponde un e-Ticket por venta**.
  No hay régimen de emisión consolidada por noche ni por caja.
- **La facturación electrónica NO es el diferencial.** Varios adquirentes uruguayos ya la
  incluyen con su POS (§9). Es table stakes, no ventaja.

### Contingencia: el hallazgo que habilita el modo offline

Resolución DGI **798/2012, numeral 17** (con las modificaciones de las Res. 4.464/2013 y
2.281/2013):

> *"No corresponderá emitir CFC cuando se produzcan **exclusivamente fallas en la
> comunicación** que impidan la remisión de la información correspondiente a la Dirección
> General Impositiva."*

Es decir: **una caída de internet no obliga a emitir comprobantes de contingencia.** El
CFC —papel preimpreso, autorizado por DGI, con la leyenda "contingencia" en caracteres no
menores a 3 mm, impreso por imprenta autorizada— aplica cuando **no se puede usar el
sistema**, no cuando falla la conexión.

Esto resuelve la pregunta abierta más importante del análisis, y la resuelve a favor del
diseño:

1. **El modo offline es legalmente viable.** Si se cae el wifi y el software sigue
   funcionando, el sistema continúa emitiendo CFE con normalidad y encola la transmisión.
   No se degrada el cumplimiento.
2. **La firma y la numeración son locales.** Como no se puede esperar a DGI para asignar
   el número, el sistema firma con el certificado del emisor y asigna la numeración en el
   origen; DGI recibe después. Esto define la arquitectura de integración: la dependencia
   del proveedor de CFE es para el certificado, el formato y el envío, **no para la
   emisión en sí**.
3. **Hace falta un runbook de CFC en papel.** El caso que sí exige contingencia —se cayó
   el servidor, no hay luz, se rompió la tablet— se cubre con un talonario preimpreso que
   **DGI otorga en el domicilio fiscal y manda a imprimir en imprenta autorizada**. Tiene
   plazos. Un local que adopta el sistema sin los CFC en la caja queda incumpliendo la
   primera noche que se le cae el servidor. Es un ítem obligatorio de la puesta en marcha.
4. **El ticket impreso es una representación del CFE y lleva sello digital.** Numeral 8:
   cuando hay receptor no electrónico **o movimiento físico de bienes**, el emisor debe
   imprimir y entregar una representación del CFE con sello digital. En un boliche se
   entregan bebidas, así que aplica: la impresora térmica debe imprimir el sello digital
   —típicamente un QR—, y eso es un requisito del módulo de impresión, no un extra.

---

## 8 bis. Protección de datos personales

El plan menciona e-factura y BPS, pero no la **Ley 18.331 de Protección de Datos
Personales**, que aplica desde el primer día porque el sistema guarda datos personales de
terceros.

### Qué datos personales toca el sistema

| Origen | Datos |
|---|---|
| Entradas online | Nombre, contacto, medio de pago del comprador |
| Empleados | **Cédula de identidad**, datos contractuales, salarios, marcaciones horarias |
| Personal del local | Usuario y PIN |
| Fase 3 (check-in por cédula) | Datos de identificación; eventualmente **datos sensibles** |

### Obligación de registro

**Ley 18.331, artículo 28:** *"Las personas físicas o jurídicas privadas que creen,
modifiquen o supriman bases de datos de carácter personal, deberán registrarse"* ante la
URCDP. La [página oficial de obligaciones de la URCDP](https://www.gub.uy/unidad-reguladora-control-datos-personales/politicas-y-gestion/obligaciones)
agrega: solicitar consentimiento (salvo excepciones legales), cumplir los principios de la
norma, y facilitar el ejercicio de los derechos de **acceso, rectificación, cancelación y
oposición**.

En un SaaS esto se reparte: el **boliche es el responsable** del tratamiento; **el
proveedor es el encargado**. El contrato con cada local necesita una cláusula de
tratamiento de datos, y el local debe informar a su personal que sus datos —incluida la
cédula y el salario— se procesan en el sistema.

### El punto que toca la infraestructura

**El plan asume un VPS de Hostinger.** Ese servidor está fuera de Uruguay, así que
implica una **transferencia internacional de datos**, que la URCDP regula con las
Resoluciones 23/021, 63/023 y 70/023: hay que informar al titular el destino de los datos,
el rol del importador, el plazo de la transferencia, la base de legitimación y las
operaciones que realiza el importador. Las transferencias a países no considerados
adecuados requieren **cláusulas contractuales con garantías adecuadas autorizadas por la
URCDP**, u otra base legitimadora como el consentimiento.

Consecuencias prácticas, en orden de esfuerzo:

1. **Verificar en qué país está el servidor** antes de decidir el hosting. No todos los
   planes de un mismo proveedor están en la misma jurisdicción, y algunos ofrecen región
   Brasil o EE. UU. adherida al marco de privacidad UE-EE. UU.
2. Si no se puede mover, **adaptar la política de privacidad** e incluir las cláusulas, o
   apoyarse en el consentimiento informado.
3. Lo mismo aplica si más adelante se mueve PostgreSQL a un servicio gestionado, que era
   una de las ideas del plan: **cambiar de proveedor es cambiar de jurisdicción.**

### El costo real del CFE para un boliche: no son los planes baratos

Los planes publicados para emisores chicos parten de **UYU 404-493 + IVA por mes** —unos
USD 10-12 al cambio de 40,22 UYU/USD— pero **incluyen cupos de 120 comprobantes al año o 20
al mes**. Están pensados para un profesional que emite decenas de documentos, no para una
barra.

Un boliche que emite **un e-Ticket por venta** (§8) puede generar **miles de comprobantes
por mes**. Para ese volumen el plan aplicable es el **ilimitado, del orden de USD 55 + IVA
por mes** — casi el doble del techo de lo que este documento estimó en su primera versión.

**Consecuencia de producto:** a ese costo, integrar CFE como módulo propio **no es negocio y
tampoco es diferencial** (§8: los adquirentes ya lo incluyen). La decisión correcta es que
**cada local contrate su proveedor** y que el sistema se ocupe de lo que sí agrega valor. Es
la misma conclusión del §15, ahora con el número detrás.

### Consecuencias de diseño

- **Minimización:** no guardar lo que no se necesita. Para vender una entrada alcanza
  nombre y contacto; **la cédula no hace falta en el MVP**, y el plan la deja para fases
  posteriores. Bien.
- **Retención:** definir plazos de purga de datos de compradores.
- **Derechos:** el producto tiene que poder responder una solicitud de acceso o
  cancelación. Es un requisito de producto, no un trámite.
- **Fase 3 (check-in por cédula):** introduce riesgo de datos sensibles. Conviene
  reevaluarlo antes de comprometerlo, y no prometerlo en la propuesta comercial.
- **Encargado de tratamiento:** el onboarding debe firmar la cláusula, igual que debe
  registrar el RUT para la Res. 167/021.

### Consecuencia operativa del multi-tenancy: restaurar un solo cliente

El plan anticipa que un cliente grande puede pedir *"quiero mis datos en un backup
aparte"*. Con **row-based** eso es exactamente lo difícil: una restauración a un punto en
el tiempo afecta a todos los tenants del mismo esquema, y aislar los datos de uno exige
extracción e importación quirúrgica.

Opciones, ninguna gratis:

1. **Export/import lógico por tenant** — la más razonable: un export completo del tenant
   que sirve tanto para backup aislado como para responder el derecho de acceso, y sirve
   para la portabilidad si el cliente se va.
2. **Esquemas por tenant** — resuelve el aislamiento por construcción, pero es la
   migración que el plan quería evitar.
3. **Nada** — aceptar que la promesa "backup aparte" no se puede cumplir, y no ofrecerla.

Recomendación: construir el **export lógico por tenant desde el inicio**, porque resuelve
a la vez el backup aislado, el derecho de acceso y la portabilidad al salir. Es un módulo
pequeño si se diseña temprano y muy caro si se agrega después.

---

## 8 ter. Cumplimiento operativo ante la IMM

La habilitación de un baile/dancing exige, entre otros,
**“Formulario del Servicio de Espectáculos Públicos de la Intendencia”**, inspección de
Bomberos, plano del local, **nómina del personal con fotocopia de las cédulas**, INAU,
DGI, BPS, Bromatología y AGADU ([trámite oficial](https://www.gub.uy/tramites/autorizacion-habilitacion-boites-dancings-prostibulos)).

Y en la operación, la Intendencia controla **aforo, salidas de emergencia, higiene y
horarios de finalización**. En una sola noche de fiscalización desplegó 70 inspectores
para verificar exactamente esos puntos.

**Consecuencia comercial:** el contador de aforo del sistema deja de ser una función de
control interno y pasa a ser **prueba de cumplimiento**. Un local que puede mostrar el
registro de ingresos por evento, con hora y terminal, tiene algo que exhibir ante una
inspección. Eso es un argumento de venta para el dueño —y para su abogado— que ningún POS
genérico ofrece.

**Dos requisitos que se derivan:**

- El **aforo tiene que ser configurable por local y por evento**, tomado de la habilitación,
  y el sistema debe impedir la venta al alcanzarlo (ya estaba en §3) y **registrar la
  evidencia**: cuándo se alcanzó, cuándo se bloqueó, quién autorizó una excepción.
- La **hora de finalización del turno** queda registrada y es auditable: es uno de los
  puntos que la IMM fiscaliza.

**Dato de tamaño de mercado (ver §12):** en la Noche de la Nostalgia sólo **ocho eventos de
Montevideo superaron las 1.000 personas**; el resto fueron boliches y salones más chicos.
El cliente objetivo es el local **pequeño y mediano**, no el estadio.

---

## 9. Pagos

- **Mercado Pago: usar la Orders API, no la legacy.** La documentación oficial declara
  que la API de QR actual será discontinuada y que las integraciones nuevas deben usar la
  nueva Orders API.
- **Mercado Pago tiene las tasas más altas de los comparables uruguayos.** Datos
  publicados por ANDE:

| Proveedor | Costo POS | Facturación electrónica |
|---|---|---|
| GetNet | 1,13 % a 4,8 % | Sí |
| Scanntech | 1,15 % a 4,9 % | No |
| **Mercado Pago** | **2,25 % a 11,99 %** | **No** |
| Geocom | sin información | Sí |
| OCA | sin información | Sí |

  => **No atar el software a un solo adquirente.** Para un local con cientos de
  transacciones por noche la diferencia es material y el cliente querrá elegir.
- **Point vs QR:** para el MVP, QR dinámico / link de pago. Point agrega emparejamiento
  de dispositivos por local y su propio soporte.
- **Argumento de venta disponible:** la Ley de Inclusión Financiera y la rebaja de IVA
  por pago con débito. El sistema puede reportar cuánto ahorra el local empujando débito.

---

## 9 bis. Venta de entradas online: concurrencia, reserva y contracargos

El plan trata la venta online de entradas como una función más. Es **la parte de mayor
concurrencia del sistema** y la que más fácil pierde plata.

### Primero: ¿de quién es la cuenta de Mercado Pago?

Decisión que el plan nunca plantea y que condiciona todo lo demás.

| Opción | Cómo funciona | Costo real |
|---|---|---|
| **A. Cuenta propia de cada local** | El boliche conecta *su* cuenta; el software factura la licencia por separado | El local es el comerciante: asume contracargos y comisiones. Fricción de alta: necesita cuenta con el KYC que exige MP |
| **B. Marketplace (Split 1:1)** | El proveedor es el marketplace y cobra `marketplace_fee` por transacción | Permite cobrar por entrada, pero **exige que el vendedor tenga cuenta MP con nivel KYC 6** y flujo OAuth por local. El modelo 1:N está reservado a carteras asistidas |
| **C. El proveedor como comerciante** | Todo entra en la cuenta del proveedor y éste le paga al local | El proveedor asume contracargos, se convierte en intermediario de pagos y necesita hacer *payouts*. **Evitar** |

Según la documentación de MP, en el modelo marketplace *"la comisión de Mercado Pago se
descuenta del monto que recibe el vendedor"*, y el vendedor debe autorizar por **OAuth**
para obtener su propio `access_token`.

**Recomendación para el MVP: opción A.** Mantiene la responsabilidad del contracargo donde
corresponde —el local vende la entrada— y evita la fricción del KYC 6 en el alta, que es una
barrera real para un boliche chico. La opción B es la que habilita cobrar por entrada, y
**conviene validar con locales reales si tienen ese nivel de KYC antes de apostar a ese
modelo de ingreso.**

### El patrón: reserva con expiración

Vender entradas contra un cupo tiene una carrera clásica: si el sistema "consulta si hay
lugar, y después cobra", **sobrevende**. El orden correcto es al revés:

1. **Reservar** — se toma el cupo de forma atómica (`UPDATE ... WHERE disponibles > 0`,
   verificando filas afectadas). Si no hay cupo, falla en el momento, no después de cobrar.
2. **Cobrar** — la reserva tiene vencimiento (10 minutos).
3. **Confirmar** — el webhook de pago emite la entrada y convierte la reserva en confirmada.
4. **Expirar** — un proceso libera las reservas vencidas sin pago.

**Invariantes:** el aforo **nunca** se sobrevende; una reserva vencida **nunca** emite
entrada; y el webhook es **idempotente por identificador de pago**, porque MP puede
notificar el mismo pago más de una vez.

### El caso que rompe todo: pago aprobado sobre reserva vencida

El comprador paga en el minuto 10:30 y su reserva venció en el 10:00. Ya no hay cupo. Las
salidas son tres, y sólo una es aceptable:

- Sobrevender "para que funcione" → **no**. Rompe el aforo, que es lo que la IMM controla (§8 ter).
- **Reembolsar y avisar** → correcto. Nunca reembolsar en silencio: el comprador tiene que
  enterarse, y queda registrado.
- Avisar **antes** del vencimiento ("tu reserva expira en 2 minutos") reduce el caso y además
  baja el abandono.

Un colchón de sobreventa es tentador y hay que **decidirlo explícitamente**, no dejarlo
implícito: es vender de más a sabiendas, y en un local con aforo fiscal es un riesgo legal.

### Contracargos: el agujero que hace mentir a los reportes

Un contracargo llega **semanas después del evento**, cuando la caja ya cerró y los reportes
ya se miraron. Si el estado de la entrada no lo contempla, **los números del local mienten
en silencio**.

- El estado de la entrada necesita **contracargo / en disputa**, además de vendida, usada y
  anulada.
- El reporte del evento tiene que mostrarlo por separado, no diluido.
- Con la opción A, **el contracargo lo asume el local**, que es quien vendió. Es otra razón
  para no elegir la opción C sin necesidad.

### Nota sobre escala

Con el tamaño de mercado del §12 —locales chicos y medianos, ocho eventos de más de 1.000
personas en toda una noche pico de Montevideo— **el problema no es la carga, es la
corrección del bloqueo.** PostgreSQL sobra; lo que rompe es el "leer, chequear y escribir".

---

## 9 ter. Listas, promotores y reservados

El plan lista "promotor" como rol y "comisiones de personal" en Fase 3. Pero **la lista es
el mecanismo con el que un boliche llena la sala**, y el promotor es el canal por el que
llega la mayoría de las reservas: en los clubes premium, las reservas directas por web son
una minoría y el resto entra por promotores y bookers, cuya comisión es típicamente **un
porcentaje del consumo de la mesa**, pagada por el local.

No es una función de fidelización. Es **acceso, aforo y plata**, y por eso no debería
esperar a la Fase 3.

### La pregunta que el dueño hoy no puede responder

*¿Cuánto me trae cada promotor, y cuánto me cuesta?* Hoy se responde con memoria y
planillas. Es la métrica que justifica el módulo entero.

### Modelo

```
Promotor         — tenant, local, nombre, contacto, tipo de comisión, valores, activo
Lista            — evento, tipo (casa / DJ / sponsor / staff / cumpleaños / promotor),
                   dueño (promotor o usuario interno), cupo, beneficio,
                   hora de corte, estado
ListaInvitado    — lista, nombre, contacto, personas que cubre (1+N), qr_token, estado
UsoLista         — invitado, cuántas personas, cuándo, terminal, usuario de puerta
Reservado        — evento, mesa, mínimo de consumo, promotor, estado
ComisionPromotor — evento, promotor, base, monto, estado (calculada / aprobada / pagada)
```

### Las tres reglas que evitan el robo

1. **El corte de lista lo aplica el sistema, no el de la puerta.** La hora de corte es una
   regla del evento. Si queda a criterio del personal, **el criterio es exactamente donde
   se va la plata**.
2. **La atribución ocurre en la puerta, en el momento del ingreso, y no se puede editar
   después.** Un promotor no puede agregar gente de forma retroactiva. Sin esta regla,
   todos los promotores reclaman a todos los que entraron — y no hay forma de dirimir.
3. **La lista consume aforo.** Cada invitado ocupa capacidad. Sin control, el local se
   llena de invitados gratis y **rompe el aforo legal**, que es lo que la IMM fiscaliza
   (§8 ter). La lista no es un beneficio ilimitado: es cupo.

### El flujo de puerta con lista

- **Búsqueda por nombre rápida y tolerante**: apellidos mal escritos, a las 2 AM, con cola.
  El sistema tiene que funcionar con nombre aunque el invitado no traiga QR.
- **Si no está en la lista, se agrega en la puerta con autorizante y auditoría.** Nunca
  libre: el alta sin control en la puerta es el equivalente del cobro no registrado.
- El invitado puede traer QR o no; el flujo no puede depender de eso.

### Cómo se calcula la comisión sin discutir

- La base **nunca** es lo que declara el promotor: es **el registro de ingresos y consumos
  del evento**.
- Se liquida por evento, con estado, y se paga después.
- El promotor **ve su propio reporte**. Eso solo elimina la mayor parte de la discusión
  administrativa, que es el costo real de trabajar con promotores.

### Corrección de alcance

El plan manda "VIP/mesas" a la Fase 3, y con eso arrastra las listas. **Recomiendo separar
las dos cosas:** los *reservados con plano de mesas* son Fase 3, pero **las listas y los
promotores pertenecen a la Fase 2, junto con las entradas**, porque son una función de
acceso. Una lista mal gestionada es un problema de aforo y de dinero **desde la primera
noche**, no una mejora de confort.

---

## 10. Hardware

| Componente | Precio verificado | Nota |
|---|---|---|
| Impresora 80 mm **Ethernet**, autocorte (3nStar RPT004) | **USD 173** | Ficha y título coinciden en Ethernet |
| Impresora Ocom OCPP-80K | USD 128 | **Descartada**: la descripción dice "Ethernet 100M" pero la ficha técnica dice `Interface: Usb`. Contradictorio |
| Tablet Android 10" 4/64 (Dialn S10) | **USD 189** | 2 GB de RAM queda corto para la PWA con cola offline |
| Router 4G (Mercusys MB115-4G) | pendiente | 4 puertos RJ45; Teltonika RUTX11 para doble SIM |

**Costo por terminal de barra: ~USD 400** (tablet + impresora + accesorios).

Reglas: confirmar el puerto con el vendedor **por escrito** (las fichas mezclan marketing
con especificación real) · comprar **una tablet de repuesto** (USD 189 < perder una noche
de facturación) · soportar **2 configuraciones como máximo**.

---

## 11. Economía

| Costo por cliente/mes | Estimación |
|---|---|
| Infraestructura (VPS, dominio, TLS, backups) | USD 2-4 |
| Proveedor de CFE | **~USD 55** para un local con volumen real (ver §8 bis: los planes baratos tienen cupo de comprobantes) |
| Soporte en régimen | ~2,5 h -> USD 50 |
| Soporte en rampa (primer trimestre) | ~8 h -> USD 160 |

| Escenario | Ingreso | Margen bruto |
|---|---|---|
| 1 cliente, primer trimestre | 100 | **-64** |
| 1 cliente, en régimen (4 terminales) | 130 | +76 |
| 20 clientes, en régimen | 2.600 | +1.520 |
| 30 clientes, en régimen | 3.900 | +2.280 |

**Conclusiones:**
1. La rampa es negativa: **el onboarding se cobra**, no es un costo de adquisición.
2. **USD 100/mes no cubre CFE + soporte.** La facturación sola consume 10-30 % de ese
   precio.
3. Precio recomendado para el nocturno: **USD 180-250/mes**, justificado por el tamaño
   del ticket y por el SLA de las 4 AM. El precio bajo atrae a los clientes que más
   soporte consumen.

### El costo invisible
2,5 h/mes son 0,6 h/semana, pero **caen viernes y sábado de 00:00 a 04:00**. Con 20
clientes se está de guardia todos los fines de semana del año, indefinidamente. De ahí:
telemetría y autodiagnóstico (que el sistema avise antes de que el cliente llame),
runbook dentro de la app, y un SLA honesto (respuesta por WhatsApp, no teléfono 24/7).

### Techo de capacidad
| Dedicación | Clientes con soporte productizado |
|---|---|
| Part-time (~20 h/semana) | 20-25 |
| Full-time | 50-60 |

---

## 11 bis. Telemetría y observabilidad

En §11 se afirma que la telemetría reduce el costo de soporte, pero nunca se diseñó. Esto
es lo que tiene que existir:

**Qué se monitorea por cliente:** última sincronización exitosa · profundidad de la cola
offline pendiente · impresora alcanzable · **webhooks de pago fallidos** · tasa de error y
latencia · versión desplegada respecto de la última.

**La alerta va al proveedor, no al cliente.** El objetivo entero es **llamar antes de que
llamen**. Una cola offline que crece, o webhooks de Mercado Pago que fallan desde hace una
hora, son cosas que el proveedor tiene que ver primero.

**Panel de salud por tenant.** Cuando un cliente llama, el diagnóstico ya está hecho: es la
diferencia entre cinco minutos y cuarenta.

**La telemetría no lleva datos personales.** Son métricas operativas, no contenido: coherente
con la minimización del §8 bis.

### Y una corrección honesta a la economía

La telemetría **no elimina las llamadas de madrugada: las acorta.** El incidente sigue
existiendo y el teléfono sigue sonando a las 3 AM; lo que cambia es que se diagnostica en
cinco minutos en vez de cuarenta. La estimación de ~2,5 h/mes por cliente y la conclusión
de precio del §11 **se mantienen**, pero por otro motivo del que sugerí: no porque el
soporte baje, sino porque cada interrupción cuesta menos. El costo en libertad —estar de
guardia todos los viernes y sábados— **no se reduce con telemetría**.

## 11 ter. Antirrobo y control de mermas

### Los números del rubro

- **La merma estándar de un bar es del 20-30 % del valor del inventario.** Para
  licores y cerveza de barril el promedio en Norteamérica es **23 %**; vino ~10 %;
  cerveza embotellada ~2 %.
- **Composición de la merma:** free pours y sobre-servido **40 %** · errores de conteo
  **20 %** · robo y uso indebido **15 %** · errores del sistema **5 %**.
- Los bares que **miden y gestionan** la merma la reducen entre **30 % y 40 %**.

### El hallazgo que reencuadra la propuesta

**El robo no es la causa principal de la pérdida: es el cuarto factor.** El 40 % se va en
servir de más, y otro 20 % en contar mal. Un producto que se venda como "antirrobo" está
atacando el 15 % del problema y va a decepcionar.

La promesa correcta es otra: **el sistema mide, atribuye y muestra dónde se va la plata** —
y con eso el dueño recupera entre un 30 % y un 40 %. Vender prevención es una promesa que
el software no puede cumplir; vender **medición** sí.

### El cuello de botella real: el conteo

El motivo por el que los bares no miden es concreto: **un conteo tradicional lleva 3 horas
o más**, así que se hace cada dos semanas o una vez al mes. Contando cada dos semanas se
pierde la mitad de la varianza; contando cada mes, el 75 %. Cuando el conteo llega, ya no
se sabe a qué turno ni a qué persona atribuir la diferencia.

**Ahí está la cuña del producto, y es específica del nocturno:** un boliche está cerrado
casi toda la semana, así que puede contar **por evento** —el momento natural es la mañana
siguiente o antes de abrir— pero solo si contar deja de ser un castigo.

- **Meta medible: bajar el conteo de 3 horas a 20 minutos.** Ese es el número que hace
  posible la varianza por evento, y es el argumento de venta.
- **Atribución automática**: cada varianza queda ligada al evento, al punto de venta y a
  **los cantineros que trabajaron ese turno**.

### Diseño del conteo guiado

**El problema difícil no son las botellas cerradas: son las abiertas.** En una barra, la
mayoría de los insumos están parcialmente consumidos, y un conteo que solo registra
botellas llenas no puede calcular varianza. De ahí el modelo:

**1. Conteo ciego (corrección a la versión anterior de este documento).**
El teórico **no se muestra durante el conteo**. Si el que cuenta ve el número esperado,
cuenta para que coincida — y el control se destruye. Antes de este análisis este documento
decía "cantidades pre-cargadas con el teórico": era un error. El teórico se revela
**después**, en el paso de conciliación.

**2. Botellas cerradas exactas + fracción de la abierta.**
Cada insumo se cuenta en dos partes: unidades selladas (número exacto) y la botella en uso
por **fracción** (lleno / 3-4 / 1-2 / 1-4 / vacío). La fracción se convierte a mililitros
según el tamaño de botella del insumo, así que el conteo queda en la misma unidad que el
teórico.

**3. Granularidad según el valor del insumo.**
Cinco cubetas dan ±12,5 % de error por botella, que en una botella de 750 ml son ~94 ml.
Aceptable para un insumo barato, inaceptable para un destilado de alta gama. El sistema
permite **cubetas más finas para los insumos caros**, y el error aleatorio se cancela al
agregar entre muchas botellas —lo que no se cancela es el sesgo, y de ahí el conteo ciego.

**4. Lista ordenada como la estantería.**
El local configura el orden de sus estantes una vez y la tablet lista los insumos en ese
orden. Es lo que elimina la búsqueda, que es donde se van las 3 horas.

**5. Conteo en paralelo, por punto y por persona.**
Cada cantinero cuenta su barra en su tablet al mismo tiempo; el encargado cuenta el
depósito. El conteo queda con dueño, hora de inicio y duración.

**6. Conteo express (80/20).**
Si la noche fue larga, se cuenta solo el subconjunto de insumos que concentra el valor.
Medir el 20 % de los insumos que explican el 80 % del valor es infinitamente mejor que no
medir nada, y es la diferencia entre un hábito sostenible y un conteo que se abandona a la
tercera semana.

**7. El sistema cronometra el conteo.**
La promesa "3 horas -> 20 minutos" tiene que ser **verificable por el cliente**, no una
afirmación de venta. El sistema registra la duración de cada conteo y la muestra.

**8. "No medido" no es "cero".**
Si un punto no se contó, el reporte dice **sin conteo**, no cero. Confundir ambos produce
varianzas falsas y destruye la confianza en el reporte más rápido que cualquier bug.

**Modelo de datos que agrega:**
```
Conteo      — evento, punto, usuario, iniciado_en, cerrado_en, duracion_seg
ConteoItem  — conteo, insumo, unidades_cerradas, fraccion_abierta, cantidad_calculada
AjusteInventario — conteo, insumo, diferencia, motivo, autorizado_por
```
La diferencia se aplica como un `StockMovimiento` de tipo `inventario`: nunca se edita el
saldo, se asienta la diferencia.

### Que esto funcione depende de dos correcciones anteriores

El cálculo de varianza es *ventas registradas -> consumo teórico de insumos -> comparación
con el conteo físico*. Solo es posible si el modelo tiene:

1. **Stock por punto de venta** (Barra 1, Barra 2), no por local — si no, no hay a quién
   atribuir la diferencia.
2. **Unidades con conversión** — sin poder recetar "50 ml de ron" contra stock en botellas,
   el consumo teórico no existe.

Es decir: **la promesa comercial de antirrobo se sostiene únicamente sobre las correcciones
al modelo de datos de los §5 y §6.** No es un módulo extra; es una consecuencia.

### Reporte de excepciones: lo que se mide por persona y por turno

El estándar de la industria (exception-based reporting) se construye sobre la transacción,
no sobre el inventario. En un boliche:

| Excepción | Señal de qué |
|---|---|
| **Anulación después del cobro** | Se cobró en efectivo y se anuló para quedarse el dinero |
| **Descuento sin autorizante** | Descuento aplicado por el propio cantinero |
| **Cortesía / consumo interno** | Tragos gratis, habitualmente para conocidos |
| **Merma declarada** | Rotura y derrame como cobertura de faltante. Concentración al final del turno es la señal |
| **Varianza de efectivo** | Diferencia entre esperado y declarado en el arqueo |
| **Varianza de insumo por turno** | El consumo teórico menos el real, atribuido |
| **Excepción de puerta** | Ingreso manual sin escaneo: en la puerta, el equivalente del cobro no registrado |
| **Desvío de precio** | Cobrar por encima de la lista y quedarse con la diferencia |

**Diseño de los controles:**

- **Nada se borra:** anular es un contra-asiento (append-only, §4). El historial es la base
  probatoria y es lo que hace que el reporte sirva.
- **Separación de funciones:** el cantinero declara su arqueo, **el encargado lo aprueba**.
  Un cantinero nunca cierra y autoriza su propia caja. Es el control más viejo del rubro y
  el más efectivo.
- **Umbrales con aprobación:** descuentos, anulaciones y mermas por encima de un monto
  exigen autorizante y motivo obligatorio.
- **Baseline por persona, no umbral absoluto:** cada cantinero se compara contra su propio
  historial y contra sus pares. Un valor absoluto produce falsos positivos y el dueño deja
  de mirar el reporte.
- **Deterrencia visible:** el personal tiene que saber que el sistema mide. La mitad del
  efecto es que exista.

### Lo que el producto NO debe prometer

- **No promete eliminar el robo.** Ninguna política ni ningún software lo logra; "en algún
  momento, casi todo cantinero considera quedarse con algo".
- **No reemplaza al conteo disciplinado.** Si el conteo es desprolijo, la varianza es ruido
  y el reporte es inútil. El producto tiene que hacer fácil el conteo, no simularlo.
- **No es un sistema de videovigilancia.** El estándar de la industria integra video con
  las excepciones; eso queda fuera de alcance y conviene decirlo desde el principio.

### La contrapartida comercial

Un boliche que pierde 25 % de su inventario y factura, por ejemplo, USD 20.000 al mes en
bebida, se está dejando del orden de **USD 5.000 mensuales**. Recuperar entre 30 % y 40 %
de eso son **USD 1.500-2.000 por mes** — contra una licencia de USD 200. Ese es el
argumento de venta, y no tiene nada que ver con la facturación electrónica.

---

## 11 quater. Empaquetado, precio y caja del primer año

En §11 se recomendó un rango de precio, pero nunca se armó una lista. Sin lista no hay venta.

### Lista de precios

| Concepto | Precio | Razón |
|---|---|---|
| **Base** — 2 terminales: barra, caja, stock por receta, conteo, reportes de cierre y varianza | **USD 150/mes** | Comparable con GBol (~USD 140 por 4 terminales, §5), pero sin alquilar hardware |
| Terminal adicional | USD 20/mes c/u | Reemplaza el ingreso del alquiler de hardware sin su capital inmovilizado |
| **Módulo Acceso** — entradas, QR, puerta, aforo, listas y promotores | USD 70/mes | Es el módulo que llena la sala (§9 ter) |
| **CFE integrado** | **no ofrecer** | A volumen de barra el proveedor cuesta ~USD 55/mes (§8): no es negocio cubrirlo ni es diferencial. El local lo contrata por su cuenta |
| **SLA nocturno** (viernes y sábado 00:00-04:00) | USD 50/mes | **Convierte el costo en libertad del §13 bis en un ingreso, y filtra: el que exige guardia, la paga** |
| **Puesta en marcha** (alta, plantilla, capacitación, primer conteo acompañado) | USD 350 única | La rampa del primer trimestre es negativa (§11): se cobra |
| Capacitación por rotación de personal | USD 60 por sesión | Es trabajo real y recurrente |

**Configuración típica de entrada** (2 terminales, sólo barra): **USD 150/mes**.
**Configuración completa** (4 terminales, acceso, CFE y SLA nocturno): **USD 335/mes**.

Estrategia: **entrar al mismo precio que el competidor por lo básico, y cobrar más por lo
que agrega valor.** No competir en precio por el POS —ahí el producto se parece a diez
otros— sino por el control de mermas, las listas y el cumplimiento.

### Caja del primer año

Supuestos: desarrollo propio (sin costo de nómina), arranque de un piloto con precio
reducido, y un cliente nuevo cada 6-8 semanas al principio.

| Período | Clientes | MRR | Únicos | Costos del mes | Flujo del mes |
|---|---|---|---|---|---|
| Mes 1-2 | 0 | 0 | 0 | ~30 | -30 |
| Mes 3 (piloto) | 1 | 75 | 350 | ~60 | **+365** |
| Mes 5 | 2 | 190 | 0 | ~80 | +110 |
| Mes 8 | 4 | 450 | 350 | ~180 | +620 |
| Mes 12 | 7 | 900 | 0 | ~330 | +570 |

**Dos conclusiones que ordenan la estrategia:**

1. **La caja se vuelve positiva casi de inmediato**, porque el costo dominante —el desarrollo—
   no es un desembolso. El negocio no necesita inversión: necesita tiempo.
2. **Pero el punto de equilibrio de un sueldo está en el techo de capacidad.** Con un margen
   de USD 100-200 por cliente, reemplazar un sueldo modesto exige **25-30 clientes**, que es
   justo el límite de un operador part-time (§11).

Es decir: **la caja cierra rápido y el sueldo no cierra nunca** —con este diseño, en este
mercado. Es la trampa clásica del proyecto lateral: se paga solo, y no paga un sueldo.

Las tres salidas honestas son: **subir el precio** (el SLA nocturno y el módulo de acceso van
en esa dirección), **apuntar a locales más grandes o multi-sede** (mismo soporte, más
ingreso), o **regionalizar** aceptando rehacer el cumplimiento por país (§12).

---

## 12. Competencia y posicionamiento

- **Gastronomía: saturado.** Sistema POS Gastrobar, NOVACAJA, Sipos, Popapp, Toteat,
  Poster, Gour-net, Maxirest, Posip, BR Bars & Restaurants.
- **Nocturno: sin oferta local.** Lo sirven GBol desde Argentina y los cashless
  regionales (Sacoa instaló en Canelones, Uruguay).
- **Posicionamiento:** entrar por **nocturno** (acceso + barra + entradas en un solo
  sistema), no por gastronomía. El ángulo local es el soporte presencial en Montevideo,
  no el cumplimiento fiscal.

### Tamaño del mercado
El nocturno de Montevideo se concentra en **Centro y Ciudad Vieja**, más Maldonado y Punta
del Este en temporada. Con USD 130-250/mes por local, incluso 20 locales dan USD
2.600-5.000/mes. **El mercado uruguayo corona esto como negocio de nicho.** Uruguay es la
cabeza de playa donde iterar con soporte presencial; la escalabilidad exige regionalizar
después, rehaciendo el cumplimiento por país.

---

## 13. Piloto

### Observación (el artefacto de mayor valor)
Dos fines de semana, rol de observador silencioso y no participante. No se corrige al
personal. Se mide: tiempo por venta (cronómetro, 20 ventas en hora pico) · toques por
venta · tiempo de escaneo en puerta y fallas · cola máxima cada 15 min · cada interrupción
y quién la resolvió · **cada workaround: si el personal hace algo por fuera del sistema
(papel, memoria, WhatsApp), eso es un requisito no descubierto** · cierre de caja
cronometrado · diferencia de stock teórico vs conteo real · frases textuales del personal.

**Criterio de salida:** lista de fricciones ordenada por *frecuencia x costo operativo*.
Esa lista es el backlog real de la Fase 2.

Formato del backlog: | Fricción | Frecuencia/noche | Costo operativo | Severidad | Esfuerzo |
Prioridad |

### Adquisición del primer cliente
- El argumento de entrada no es software: es *"te cierro la caja en 15 minutos y te digo
  cuánta bebida se fue"*. El dueño compra control, no tecnología.
- **Cuándo:** nunca viernes ni sábado. Lunes o martes a la tarde.
- **Por dónde:** por el **contador** o el encargado antes que por el dueño. El contador
  sufre la falta de trazabilidad y tiene incentivo para recomendar.
- **Oferta:** dos noches de observación gratis, se paga solo si convence. El proceso de
  descubrimiento **es** la venta.
- **Filtrar:** descartar locales por debajo del volumen donde el problema no existe.

### Acuerdo piloto
2-3 meses · precio simbólico pero **pagado desde el día 1** (lo gratis no se usa) ·
hardware comprado por el local · datos del local, con exportación completa al salir ·
salida con 15 días de aviso y sin penalidad · compromiso del local: responsable designado
y contacto de 00:00 a 04:00.

---

## 13 bis. Soporte, horarios y responsabilidad contractual

Dos temas que no son de software y que deciden si esto se puede sostener.

### El horario es el problema, no la cantidad de horas

En §11 el soporte se estimó en ~2,5 h/mes por cliente. El promedio engaña: **esas horas caen
viernes y sábado entre las 00:00 y las 04:00.** Y el plan asume desarrollo part-time
compaginado con un trabajo diurno.

La aritmética es dura: cubrir hasta las 4 AM y trabajar el lunes no es un problema de
voluntad, es un problema fisiológico, y **se acumula**. Dos noches por semana, indefinidamente,
no son compatibles con un empleo de jornada completa.

Opciones reales, con su costo:

| Opción | Costo | Cuándo conviene |
|---|---|---|
| **SLA honesto: soporte de día, manual para la noche** | USD 0 | **Piloto y primer cliente.** El local sabe cuándo se lo atiende y el runbook cubre la madrugada |
| **Primera línea contratada** (un cantinero o estudiante técnico de guardia los fines de semana) | ~USD 200-400/mes | A partir del segundo o tercer cliente. Hace triage y escala; no resuelve |
| **Socio nocturno** | Equity | Cuando el volumen lo justifique y haya alguien adecuado |

**Recomendación:** empezar con el SLA honesto —un piloto con un local amigo no necesita
guardia 24/7— y pasar a primera línea contratada **antes** del tercer cliente. Lo que no se
puede hacer es **prometer cobertura nocturna al primer cliente si no hay quien la cubra**:
la primera promesa que se rompe es la última que se puede hacer.

La telemetría del §11 bis ayuda, pero no cambia esto: acorta el diagnóstico, no elimina la
llamada.

### Limitación de responsabilidad: hay que firmarla antes del primer incidente

Un boliche puede facturar decenas de miles de dólares en una noche. Si el sistema se cae un
sábado, la discusión sobre quién paga esa noche llega sola.

Un contrato de SaaS sin capa de responsabilidad es un problema existencial, no un detalle
legal. Mínimo:

- **Limitación de responsabilidad** al monto de las licencias pagadas (estándar de la
  industria, y defendible frente a un cliente).
- **Exclusión del lucro cesante.** Sin esta cláusula, el techo lo pone la facturación
  perdida del local, no su contrato.
- **Compromiso de disponibilidad expresado como créditos de servicio**, no como
  indemnización.
- **Caso fortuito**, incluyendo expresamente la conectividad y el suministro eléctrico.

Y un punto que se apoya en el §8: como una **falla de conexión no obliga a comprobante de
contingencia**, la exposición legal del local ante una caída de wifi es acotada. Eso conviene
**escribirlo en el contrato**, porque desactiva de antemano la discusión más probable.

Del otro lado, el contrato debe dejar constancia de que **es obligación del local tener su
talonario de comprobantes de contingencia** (§8) y de que el runbook de la noche está
entregado y aceptado. Un servidor caído es un riesgo compartido, no del proveedor solo — pero
sólo si está escrito.

### Nota de simetría

Para vender la licencia en Uruguay **el proveedor también necesita emitir CFE a sus
clientes**. Es la misma obligación que el producto resuelve, y la salida razonable es la
misma que se recomienda a los locales: contratar un proveedor ya inscripto (§8), por los
mismos USD 10-30/mes que se presupuestaron en §11.

---

## 14. Riesgos

Del plan, todos válidos: subestimar la operación en vivo · hardware · adquisición del
primer cliente.

Agregados en este análisis:
1. **Fuga de datos entre tenants** por row-based mal aplicado.
2. **Pago confirmado después del cierre de caja** -> arqueo descuadrado e inexplicable.
3. **Validación de QR sin conexión** -> doble ingreso y aforo roto.
4. **La cámara no arranca sin HTTPS** -> la función estrella de la Fase 2 no existe.
5. **La e-factura resulta no ser diferencial** porque los adquirentes ya la incluyen.
6. **El horario del soporte contra el trabajo diurno** (§13 bis): el problema no son las horas
   promedio, es que caen de madrugada los fines de semana.

---

## 15. Fases

- **Fase 1 (4-6 semanas):** Barra/POS + Gestión, un local, sin control de acceso.
  *DoD:* un cantinero abre caja, vende con efectivo y con QR, el stock baja según receta,
  y cierra con arqueo que muestra la diferencia por medio de pago.
- **Fase 2 (3-4 semanas):** Entradas + QR (venta online + validación en puerta) **+ listas y
  promotores** (ver §9 ter: son acceso, no VIP).
  *DoD:* un cliente compra, recibe un QR, entra una vez; el segundo escaneo falla; el
  aforo sube; una entrada de otro evento es rechazada.
- **Fase 3:** cashless, VIP/mesas, multi-sede, offline robusto, comisiones.

**Ajuste al orden:** sumar `Evento` al primer diseño, porque `Entrada` depende de él.
Y construir **una rebanada vertical fina** (producto -> receta -> venta -> cierre de caja)
antes que módulos horizontales.

El cronograma del plan asume desarrollo part-time; es optimista si se parte de nada y
plausible si existe una base reutilizable.

---

## 15 bis. Secuencia de construcción

El análisis acumula decisiones pero no un orden. Este es el orden, con puertas de salida
verificables.

### La regla que ordena todo: primero el esquema, después la función

Hay decisiones que **cuestan una migración sobre datos vivos** si se posponen, y otras que
sólo cuestan escribir el código después. Las primeras van en el commit inicial **aunque su
funcionalidad no exista todavía**:

- `tenant_id` en toda tabla de negocio y manager **fail-closed** (§6)
- Ledgers **append-only** para ventas, pagos, stock y aforo (§4)
- **UUID generado en el cliente** + **clave de idempotencia** (§5)
- **Versionado del catálogo** (§4 bis)
- **Unidades con conversión** (§5)

Ninguna de esas cinco se puede agregar después sin dolor. Todas se pueden agregar antes sin
costo.

### La rebanada que prueba la tesis

La tesis del producto es: *"te cierro la caja en 15 minutos y te digo dónde se va la plata"*.
La rebanada mínima que la prueba termina en **varianza atribuida**, no en la venta:

abrir caja -> vender con receta -> cerrar con arqueo -> **contar -> varianza por turno y por
persona**.

Ese es el demo que cierra la venta, y es más corto de lo que parece: no incluye acceso,
ni facturación, ni impresión, ni offline.

### Etapas y puertas

| Etapa | Contenido | Puerta de salida |
|---|---|---|
| **0. Cimientos** | Proyecto, dominio + TLS desde el día 1, tenancy fail-closed, auth por PIN, auditoría, catálogo con unidades y conversión, ledger de stock | **Tests de aislamiento entre dos tenants en verde.** Sin eso no se sigue |
| **1. Barra** | Turno, sesión de caja, venta en 3 toques, efectivo y QR, descuento por receta, arqueo por medio de pago | Venta medida <= 3 toques y <= 5 s; cierre <= 15 min; los 6 invariantes del §5 en verde |
| **2. La prueba** | Conteo guiado ciego, ajuste, reporte de varianza y de excepciones | **Conteo de ~120 insumos en <= 20 min, cronometrado por el sistema**; varianza atribuida a turno y persona |
| **3. Primera noche** | Piloto real, sin módulo de acceso | Observación de 2 fines de semana (§13); backlog priorizado por frecuencia x costo |
| **4. Acceso** | Evento, entrada, QR de un solo uso, puerta, aforo, **listas y promotores** | Una entrada se usa una vez; el segundo escaneo falla; el aforo no se sobrevende; una entrada de otro evento se rechaza |
| **5. Venta online** | Reserva con expiración, webhook idempotente, contracargos | **Prueba de concurrencia que intenta sobrevender y falla** |
| **6. Endurecimiento** | CFE + representación impresa, export por tenant, telemetría, offline, PWA | Venta con el wifi caído que sincroniza sin duplicar |

**El riesgo está bien ubicado:** las cuatro cosas que pueden arruinar el producto —aislamiento
entre tenants, stock por punto con unidades, pago asincrónico y conteo ciego— caen todas en
las etapas 0 a 2, antes de cualquier noche real.

### Cortes de alcance explícitos para el MVP

Cada uno es una decisión de no hacer, con su razón:

- **Impresión: fuera.** En una barra (a diferencia de un restaurante) **no hay comanda a
  cocina**: la venta es el pedido y el trago se sirve en el momento. Sin CFE obligatorio
  tampoco hay representación impresa que entregar. Se ahorra el módulo ESC/POS, el servicio
  de impresión y **USD 173 por terminal**. Entra cuando entra la facturación, que es cuando
  es obligatoria.
- **Offline: fuera hasta la etapa 6.** Con una barra sin CFE, la caída de conexión es una
  molestia operativa, no un problema de cumplimiento (§8).
- **CFE: fuera del MVP**, con la consecuencia asumida de que el local sigue necesitando otro
  sistema para facturar (§8). Es el precio de recortar, y hay que decirlo antes de venderlo.
- **Multi-local: fuera.** El modelo lo soporta desde el día 1, la interfaz arranca con un
  local (§3).
- **Reportes: sólo dos.** Cierre de caja por turno y varianza por evento. El resto espera.

### Lo que no se recorta

El **conteo guiado**. Es tentador mandarlo a Fase 3 junto con los reportes "avanzados", y es
exactamente al revés: sin conteo no hay varianza, sin varianza el producto no tiene nada que
vender más allá de una caja registradora linda. La etapa 2 es la que justifica el precio del
§11.

---

## 16. Huecos abiertos

1. ~~Granularidad de emisión de CFE~~ — **resuelta**: e-Ticket por venta, y la caída de
   conexión no exige comprobante de contingencia (§8). Falta confirmar con el contador del
   piloto si el local ya está inscripto como emisor.
2. ~~Costo exacto del proveedor de CFE~~ — **resuelto y corregido al alza: ~USD 55/mes** para volumen
   de barra (§8 bis). Queda la comisión de cobro aplicada al SaaS.
3. **Precio del router 4G** y cierre del BOM con dos proveedores.
4. **Costeo del paquete de soporte** de madrugada.
5. **Cambio de alcance:** el plan describe facturación por hitos, pero esto es un SaaS
   con costos recurrentes. El modelo de negocio pide una estructura de precios
   recurrente, no un plan de proyecto.

---

## 17. Fuentes

- Mercado Pago, API de QR (discontinuación de la legacy): https://www.mercadopago.com.uy/developers/es/docs/qr-code-legacy/overview.md
- Mercado Pago, pagos automáticos y suscripciones: https://www.mercadopago.com.uy/developers/es/docs/automatic-payments/overview
- Resolución DGI 167/021 (obligación de informar proveedores de software): http://www.impo.com.uy/bases/resoluciones-dgi/167-2021
- Obligatoriedad de ser emisor electrónico: https://calculame.uy/facturacion-electronica-alta-emisor
- **Resolución DGI 798/2012 (régimen de CFE: e-Ticket, representación impresa, contingencias)**: http://www.impo.com.uy/bases/resoluciones-dgi-interes-general/798-2012
- Tasas de adquirentes en Uruguay (datos de ANDE): https://tiendli.com/blog/cobrar-con-tarjeta-uruguay
- Guía CFE Uruguay 2026: https://tiendli.com/blog/facturacion-electronica-uruguay
- Impresora 3nStar RPT004 80 mm Ethernet: https://baudinequipamientos.com/impresora-termica-3nstar-rpt004-80mm/
- Impresora Ocom OCPP-80K (ficha contradictoria): https://magatec.com.uy/product/impresora-termica-ocom-ocpp-80k-80mm/
- Tablet Dialn S10 10" 4/64: https://albanes.com.uy/catalogo/tablet-dialn-s10-10-4-64_08867_08867
- Planes de proveedor CFE: https://memory.com.uy/precios-siigo-memory/
- Competencia POS en Uruguay: https://www.comparasoftware.com.uy/sistema-pos-gastrobar
- Sacoa cashless en Uruguay: https://sacoacard.com/2026/07/02/jumpx-selects-sacoa-cashless-system-for-its-new-family-entertainment-venue-in-uruguay/
- Mecánica de promotores y reservas en clubes premium (comisión sobre consumo): https://londonluxurynightlife.com/blog/how-london-vip-clubs-work-behind-scenes
- Mercado Pago, Split Payments 1:1 y requisitos (KYC 6, OAuth): https://www.mercadopago.com.uy/developers/en/docs/split-payments/split-1-1/prerequisites
- Mercado Pago, integración de checkout en marketplace (marketplace_fee / application_fee): https://www.mercadopago.com.uy/developers/en/docs/split-payments/split-1-1/integration-configuration/integrate-marketplace
- Habilitación de boites y dancings (requisitos): https://www.gub.uy/tramites/autorizacion-habilitacion-boites-dancings-prostibulos
- Controles de la IMM en eventos nocturnos (aforo, horarios): https://www.subrayado.com.uy/fiestas-la-noche-la-nostalgia-134-propuestas-autorizadas-y-los-controles-que-hara-la-imm-n985996
- Costos reales de proveedores de CFE en Uruguay (planes y cupos): https://www.aeu.org.uy/Contenidos/Facturacion-electronica-Beneficios-especiales-para-socios-uc6935
- Cotización del dólar (40,22 UYU/USD al 9/9/2026): https://www.infobae.com/noticias/2026/09/09/precio-del-dolar-hoy-en-uruguay-cotizacion-de-apertura-del-9-de-septiembre/
- Ley 18.331, bases de datos de titularidad privada (art. 28): https://impo.com.uy/bases/leyes/18331-2008/28
- Obligaciones de la Ley de Protección de Datos (URCDP): https://www.gub.uy/unidad-reguladora-control-datos-personales/politicas-y-gestion/obligaciones
- Transferencias internacionales de datos (Res. URCDP 70/023): https://ferrere.com/en/news/nuevos-requerimientos-para-la-transferencia-internacional-de-datos/
- Merma de bebidas en bares (composición y magnitud): https://www.lixor.ai/blog/bar-inventory-shrinkage-causes-prevention
- Shrinkage de licores y cerveza (promedios de la industria): https://alcoholcontrols.com/printh.html
- Reporte de excepciones en POS para detección de fraude: https://blog.agilenceinc.com/pos-exception-reporting-how-lp-teams-use-transaction-data-to-detect-fraud
- Zonas nocturnas de Montevideo: https://www.elobservador.com.uy/nota/policia-hara-intervenciones-de-madrugada-en-centro-y-ciudad-vieja-para-evitar-conflictos-en-boliches-2024110131056
