import http.cookiejar, json, re, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8014"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
CSRF = [""]

def guardar(html):
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if m: CSRF[0] = m.group(1)

def get(path):
    with op.open(BASE + path, timeout=20) as r:
        texto = r.read().decode("utf-8", "replace")
        guardar(texto)
        return r.status, texto, dict(r.headers)

def post_json(path, datos):
    cuerpo = json.dumps(datos).encode()
    req = urllib.request.Request(BASE + path, data=cuerpo)
    req.add_header("Content-Type", "application/json")
    req.add_header("X-CSRFToken", CSRF[0])
    req.add_header("Referer", BASE + path)
    try:
        respuesta = op.open(req, timeout=20)
    except urllib.error.HTTPError as error:
        respuesta = error
    with respuesta as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))

fallas = []
def ok(cond, texto):
    print(("  OK     " if cond else "  FALLA  ") + texto)
    if not cond: fallas.append(texto)

print("1) El service worker se sirve desde la raiz")
st, js, cabeceras = get("/sw.js")
ok(st == 200, "GET /sw.js -> " + str(st))
ok("javascript" in cabeceras.get("Content-Type", ""), "se sirve como javascript")
ok(cabeceras.get("Service-Worker-Allowed") == "/", "permite controlar /barra/")
ok("boliche-sig-v1" in js, "el contenido es el service worker")

print("2) La pantalla de barra trae la cola local")
st, html, _ = get("/ingresar/")
op.open(urllib.request.Request(BASE + "/ingresar/", data=urllib.parse.urlencode(
    {"csrfmiddlewaretoken": CSRF[0], "numero": "0001", "pin": "1234"}).encode()), timeout=20)
st, html, _ = get("/caja/")
opciones = re.findall(r'<option value="([0-9a-f-]+)">([^<]+)</option>', html)
puerta = [i for i, n in opciones if "Barra 1" in n]
if puerta and "Abrir caja" in html:
    op.open(urllib.request.Request(BASE + "/caja/", data=urllib.parse.urlencode(
        {"csrfmiddlewaretoken": CSRF[0], "accion": "abrir", "terminal": puerta[0],
         "fondo_inicial": "1000", "turno": "Offline"}).encode()), timeout=20)
st, html, _ = get("/barra/")
ok("estado-conexion" in html, "muestra el indicador de conexion")
ok("Todo al dia" in html, "el indicador arranca tranquilo, no alarmante")
ok("barra.js" in html, "carga la cola local")
m = re.search(r'data-catalogo-version="(\d+)"', html)
ok(m is not None, "manda la version del catalogo: " + (m.group(1) if m else "-"))
version = int(m.group(1)) if m else 1
elegido = re.search(r'data-producto="([0-9a-f-]+)"[^>]*data-nombre="([^"]*)"[^>]*data-precio="([^"]*)"', html)
producto, nombre, precio_texto = elegido.group(1), elegido.group(2), elegido.group(3)
# El precio viene localizado con coma: 120,00
precio = float(precio_texto.replace(".", "").replace(",", "."))

print("3) Sincronizar un lote hecho sin conexion")
st, datos = post_json("/api/barra/sincronizar/", {"catalogo_version": version, "ventas": [
    {"id_local": "v1", "idempotency_key": "off-1", "producto": producto,
     "cantidad": 2, "medio": "efectivo",
     "creada_en_cliente": "2026-09-09T23:30:00-03:00"},
]})
ok(st == 200 and datos["ok"], "el lote se acepta")
r0 = datos["resultados"][0]
ok(r0["estado"] == "registrada", "la venta se registro")
esperado = "%.2f" % (precio * 2)
ok(
    r0["total"] == esperado,
    "el servidor calculo 2 x " + nombre + " = " + r0["total"],
)
ok(r0["precio_desactualizado"] is False, "la lista estaba al dia")

print("4) Reintentar el mismo lote no duplica")
for _ in range(3):
    st, datos2 = post_json("/api/barra/sincronizar/", {"catalogo_version": version, "ventas": [
        {"id_local": "v1", "idempotency_key": "off-1", "producto": producto,
         "cantidad": 2, "medio": "efectivo"},
    ]})
ok(datos2["resultados"][0]["venta"] == r0["venta"], "devuelve la MISMA venta, no una nueva")

print("5) Una lista vieja queda marcada")
st, datos3 = post_json("/api/barra/sincronizar/", {"catalogo_version": version + 7, "ventas": [
    {"id_local": "v2", "idempotency_key": "off-2", "producto": producto,
     "cantidad": 1, "medio": "qr"},
]})
r2 = datos3["resultados"][0]
ok(r2["precio_desactualizado"] is True, "marca que se cobro con lista vieja")
ok(r2["medio_pendiente"] is True, "un cobro por QR queda pendiente sin conexion")

print("")
if fallas:
    print("RESULTADO: " + str(len(fallas)) + " verificacion(es) fallaron")
    raise SystemExit(1)
print("RESULTADO: todas las verificaciones pasaron")