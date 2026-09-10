
import http.cookiejar, json, re, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8012"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
CSRF = [""]

def guardar_csrf(html):
    m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    if m: CSRF[0] = m.group(1)

def get(path):
    with op.open(BASE + path, timeout=20) as r:
        return r.status, r.read().decode("utf-8", "replace"), r.geturl()

def post(path, datos, json_out=False):
    cuerpo = urllib.parse.urlencode(datos).encode()
    req = urllib.request.Request(BASE + path, data=cuerpo)
    req.add_header("X-CSRFToken", CSRF[0])
    req.add_header("Referer", BASE + path)
    try:
        respuesta = op.open(req, timeout=20)
    except urllib.error.HTTPError as error:
        respuesta = error
    with respuesta as r:
        texto = r.read().decode("utf-8", "replace")
        if json_out: return r.status, json.loads(texto), r.geturl()
        return r.status, texto, r.geturl()

fallas = []
def ok(cond, texto):
    print(("  OK     " if cond else "  FALLA  ") + texto)
    if not cond: fallas.append(texto)

print("1) Cartelera publica (sin sesion)")
st, html, _ = get("/tienda/demo/")
ok(st == 200, "la tienda responde sin estar autenticado")
ok("Fiesta de demo" in html, "el evento publicado aparece")
ok("Comprar" in html, "ofrece comprar")

print("2) Detalle del evento")
m = re.search(r'/tienda/demo/([0-9a-f-]+)/', html)
ev = m.group(1)
st, html, _ = get("/tienda/demo/" + ev + "/")
guardar_csrf(html)
ok("Reservar y pagar" in html, "ofrece reservar")
opciones = re.findall(r'<option value="([0-9a-f-]+)"[^>]*>\s*([A-Za-z]+) · \$ ([\d.,]+) · quedan (\d+)', html)
ok(len(opciones) > 0, "tipos con disponibilidad: " + str([(o[1], o[3]) for o in opciones]))

print("3) Reservar (toma el cupo)")
st, html2, url = post("/tienda/demo/" + ev + "/reservar/",
                      {"csrfmiddlewaretoken": CSRF[0], "tipo": opciones[0][0],
                       "cantidad": "2", "nombre": "Ana", "contacto": "099123456"})
ok("/reserva/" in url, "redirige a la pantalla de pago -> " + url)
token = url.rstrip("/").split("/")[-1]

print("4) Pantalla de pago")
st, html, _ = get("/reserva/" + token + "/")
ok("guardado por" in html, "muestra el reloj de la reserva")
ok("Mercado Pago todavia no esta integrado" in html, "avisa que el cobro es simulado")
guardar_csrf(html)

print("5) Pagar y confirmar")
st, html, url = post("/reserva/" + token + "/confirmar/",
                     {"csrfmiddlewaretoken": CSRF[0], "estado": "aprobado", "referencia": "SIM-x"})
ok("/entrada/" in url, "redirige a la entrada -> " + url)
entrada_token = url.rstrip("/").split("/")[-1]

print("6) La entrada con su QR")
st, html, _ = get("/entrada/" + entrada_token + "/")
ok("<svg" in html, "el QR se genera")
ok("Se usa una sola vez" in html, "avisa que se usa una vez")
ok("Las otras entradas de esta compra" in html, "muestra las dos entradas de la compra")

print("7) Webhook del adquirente")
m = re.search(r'/entrada/([A-Za-z0-9_\-]+)/', html)
st, datos, _ = post("/api/pagos/webhook/", {"referencia": "SIM-inexistente", "estado": "aprobado"}, json_out=True)
ok(st == 404, "una referencia desconocida da 404 (no rompe)")
st, datos, _ = post("/api/pagos/webhook/", {"estado": "aprobado"}, json_out=True)
ok(st == 400, "sin referencia da 400")

print("")
if fallas:
    print("RESULTADO: " + str(len(fallas)) + " verificacion(es) fallaron")
    raise SystemExit(1)
print("RESULTADO: todas las verificaciones pasaron")
