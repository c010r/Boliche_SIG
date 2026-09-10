
import http.cookiejar, re, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8010"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

def get(path):
    with op.open(BASE + path, timeout=15) as r:
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()

def post(path, datos):
    req = urllib.request.Request(BASE + path, data=urllib.parse.urlencode(datos).encode())
    with op.open(req, timeout=15) as r:
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()

def token(html):
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    return m.group(1) if m else ""

fallas = []
def ok(cond, texto):
    print(("  OK     " if cond else "  FALLA  ") + texto)
    if not cond:
        fallas.append(texto)

print("1) Ingreso por numero 0001 + PIN 1234")
_, html, _ = get("/ingresar/")
st, html, url = post("/ingresar/", {"csrfmiddlewaretoken": token(html), "numero": "0001", "pin": "1234"})
ok("ingresar" not in url, "sesion iniciada, redirige a " + url)

print("2) Abrir caja en Barra 1")
st, html, _ = get("/caja/")
terminales = re.findall(r'<option value="([0-9a-f-]+)">([^<]+)</option>', html)
ok(len(terminales) > 0, "terminales ofrecidas: " + str(len(terminales)))
if terminales:
    st, html2, url = post("/caja/", {
        "csrfmiddlewaretoken": token(html),
        "accion": "abrir",
        "terminal": terminales[0][0],
        "fondo_inicial": "3000",
        "turno": "Verificacion",
    })
    ok("barra" in url, "caja abierta -> " + url)

print("3) La barra muestra la botonera")
st, html, url = get("/barra/")
productos = re.findall(r'data-producto="([0-9a-f-]+)"[^>]*data-nombre="([^"]+)"', html)
ok(len(productos) > 3, "productos en pantalla: " + str(len(productos)))
ok("Tema oscuro" if False else "--fondo: #0d0f12" in html, "tema oscuro aplicado")

print("4) Vender una Cuba libre con efectivo")
if productos:
    pid = [p for p, n in productos if n == "Cuba libre"]
    pid = pid[0] if pid else productos[0][0]
    st, html2, url = post("/barra/", {
        "csrfmiddlewaretoken": token(html),
        "producto": pid,
        "medio": "efectivo",
        "cantidad": "1",
    })
    ok("Vendido" in html2, "venta confirmada en pantalla")

print("5) La caja refleja la venta")
st, html, _ = get("/caja/")
ok("Cierre y arqueo" in html, "la caja muestra el arqueo")
m = re.search(r"Total vendido</th><td class=\"num\">\$ ([\d.]+)", html)
if m:
    print("       Total vendido: " + m.group(1))
    ok(float(m.group(1).replace(",", "")) > 0, "el total vendido es mayor que cero")

print("6) El panel del dueno muestra la varianza")
st, html, _ = get("/panel/")
ok("Varianza" in html, "panel accesible")
ok("Ron" in html, "el reporte trae el ron del conteo de la demo")
ok("Varianza valorizada" in html, "valoriza la diferencia en moneda")
ok("No medido no es cero" in html, "marca los puntos sin conteo")

print("7) Cerrar sesion")
st, html, url = get("/salir/")
ok("ingresar" in url, "sesion cerrada -> " + url)

print("")
if fallas:
    print("RESULTADO: " + str(len(fallas)) + " verificación(es) fallaron")
    raise SystemExit(1)
print("RESULTADO: todas las verificaciones pasaron")
