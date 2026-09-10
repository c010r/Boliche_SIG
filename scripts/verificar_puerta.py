
import http.cookiejar, json, re, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8011"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
CSRF = [""]

def get(path):
    with op.open(BASE + path, timeout=20) as r:
        cuerpo = r.read().decode("utf-8", "replace")
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', cuerpo)
        if m: CSRF[0] = m.group(1)
        return r.status, cuerpo, r.geturl()

def post(path, datos, json_out=False):
    cuerpo = urllib.parse.urlencode(datos).encode()
    req = urllib.request.Request(BASE + path, data=cuerpo)
    req.add_header("X-CSRFToken", CSRF[0])
    req.add_header("Referer", BASE + path)
    # Un 4xx es una respuesta valida de la API, no una caida del script.
    try:
        respuesta = op.open(req, timeout=20)
    except urllib.error.HTTPError as error:
        respuesta = error
    with respuesta as r:
        texto = r.read().decode("utf-8", "replace")
        if json_out: return r.status, json.loads(texto), r.geturl()
        m = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', texto)
        if m: CSRF[0] = m.group(1)
        return r.status, texto, r.geturl()

fallas = []
def ok(cond, texto):
    print(("  OK     " if cond else "  FALLA  ") + texto)
    if not cond: fallas.append(texto)

print("1) Ingreso y apertura de caja")
_, html, _ = get("/ingresar/")
post("/ingresar/", {"csrfmiddlewaretoken": CSRF[0], "numero": "0001", "pin": "1234"})
_, html, _ = get("/caja/")
opciones = re.findall(r'<option value="([0-9a-f-]+)">([^<]+)</option>', html)
if opciones:
    puerta_id = [i for i, n in opciones if "Puerta" in n]
    elegido = puerta_id[0] if puerta_id else opciones[0][0]
    _, html2, url = post("/caja/", {"csrfmiddlewaretoken": CSRF[0], "accion": "abrir",
                                    "terminal": elegido, "fondo_inicial": "2000",
                                    "turno": "Verificacion puerta"})
    ok("barra" in url or "caja" in url, "caja de puerta abierta -> " + url)

print("2) Lista de eventos")
_, html, _ = get("/eventos/")
ok("Fiesta de demo" in html, "el evento de la demo aparece")

print("3) Detalle del evento")
m = re.search(r'/eventos/([0-9a-f-]+)/', html)
ev = m.group(1)
_, html, _ = get("/eventos/" + ev + "/")
ok("Adentro ahora" in html, "muestra ocupacion")
ok("Vender entradas" in html, "ofrece vender entradas")

print("4) Vender una entrada")
# El precio viene localizado con coma (es-UY): 500,00
tipos = re.findall(r'<option value="([0-9a-f-]+)">\s*([A-Za-z]+) · \$ ([\d.,]+) · quedan (\d+)', html)
ok(len(tipos) > 0, "tipos disponibles: " + str([t[1] for t in tipos]))
entrada_id = None
if tipos:
    _, html2, url = post("/eventos/" + ev + "/vender/",
                         {"csrfmiddlewaretoken": CSRF[0], "tipo": tipos[0][0], "cantidad": "1"})
    m = re.search(r'/entradas/([0-9a-f-]+)/qr/', url)
    if m:
        entrada_id = m.group(1)
    ok("qr" in url, "redirige al QR -> " + url)

print("5) La pantalla del QR")
if entrada_id:
    _, html, _ = get("/entradas/" + entrada_id + "/qr/")
    ok("<svg" in html, "el QR se genera")
    token = re.search(r'style="word-break:break-all;">([A-Za-z0-9_\-]{30,})<', html)
    ok(token is not None, "el token viaja en la pantalla")
    token = token.group(1) if token else ""

    print("6) Validar en la puerta")
    _, html, _ = get("/puerta/" + ev + "/")
    ok("Listo para escanear" in html, "la pantalla de puerta carga")
    ok("HTTPS" in html, "avisa el requisito de HTTPS para la camara")

    _, datos, _ = post("/validar/", {"csrfmiddlewaretoken": CSRF[0], "evento": ev, "texto": token}, json_out=True)
    ok(datos.get("ok") is True, "primer escaneo acepta: " + str(datos.get("mensaje")))
    ok(datos.get("aforo_en_vivo") is not None, "devuelve el aforo en vivo: " + str(datos.get("aforo_en_vivo")))

    _, datos2, _ = post("/validar/", {"csrfmiddlewaretoken": CSRF[0], "evento": ev, "texto": token}, json_out=True)
    ok(datos2.get("ok") is False, "segundo escaneo rechaza")
    ok(datos2.get("motivo") == "ya_usada", "el motivo es ya_usada -> " + str(datos2.get("motivo")))

    print("7) Ingreso manual")
    _, d3, _ = post("/validar/", {"csrfmiddlewaretoken": CSRF[0], "evento": ev,
                                  "manual": "1", "cantidad": "3", "motivo": "Lista del DJ"}, json_out=True)
    ok(d3.get("ok") is True, "registra el ingreso manual")
    _, d4, _ = post("/validar/", {"csrfmiddlewaretoken": CSRF[0], "evento": ev,
                                  "manual": "1", "cantidad": "1"}, json_out=True)
    ok(d4.get("ok") is False, "el ingreso manual sin motivo se rechaza")

print("8) Listas de invitados")
st, html, _ = get("/eventos/" + ev + "/listas/")
ok(st == 200, "la pantalla de listas carga")
ok("El corte de cada lista lo aplica el sistema" in html, "explica la regla del corte")

_, html2, url = post("/eventos/" + ev + "/listas/", {
    "csrfmiddlewaretoken": CSRF[0], "accion": "crear_lista",
    "nombre": "Lista de verificacion", "tipo": "promotor",
    "promotor": "", "cupo": "10", "hora_de_corte": "01:00",
})
st, html, _ = get("/eventos/" + ev + "/listas/")
ok("Lista de verificacion" in html, "la lista quedo creada")

_, html, url = post("/eventos/" + ev + "/listas/", {
    "csrfmiddlewaretoken": CSRF[0], "accion": "agregar_invitado",
    "lista": re.search(r'<option value="([0-9a-f-]+)">Lista de verificacion', html).group(1),
    "nombre": "Verificada", "personas": "2",
})

_, datos, _ = post("/validar-lista/", {
    "csrfmiddlewaretoken": CSRF[0], "evento": ev,
    "nombre": "Verificada", "personas": "2",
}, json_out=True)
ok(datos.get("ok") is True, "el invitado pasa: " + str(datos.get("mensaje")))

_, datos2, _ = post("/validar-lista/", {
    "csrfmiddlewaretoken": CSRF[0], "evento": ev,
    "nombre": "Verificada", "personas": "5",
}, json_out=True)
ok(datos2.get("ok") is False, "no pasa mas gente de la que cubre")
ok(datos2.get("motivo") == "sin_personas", "el motivo es sin_personas")

_, datos3, _ = post("/validar-lista/", {
    "csrfmiddlewaretoken": CSRF[0], "evento": ev,
    "nombre": "No Existe",
}, json_out=True)
ok(datos3.get("ok") is False, "un nombre desconocido no pasa")

print("")
if fallas:
    print("RESULTADO: " + str(len(fallas)) + " verificacion(es) fallaron")
    raise SystemExit(1)
print("RESULTADO: todas las verificaciones pasaron")
