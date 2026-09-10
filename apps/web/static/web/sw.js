/* Service worker: permite que la pantalla de barra abra sin conexion.
 *
 * Se sirve desde la raiz para poder controlar /barra/: el alcance de un service
 * worker esta limitado a su propia ruta.
 *
 * Requiere HTTPS (o localhost). Sin TLS el navegador no lo registra, y entonces
 * la pantalla no abre si se cae la red. Es la misma razon por la que la camara
 * de la puerta necesita HTTPS.
 */

var CACHE = "boliche-sig-v1";
var ESENCIALES = ["/barra/", "/ingresar/"];

self.addEventListener("install", function (evento) {
  evento.waitUntil(
    caches.open(CACHE).then(function (cache) { return cache.addAll(ESENCIALES); })
      .catch(function () {})
  );
  self.skipWaiting();
});

self.addEventListener("activate", function (evento) {
  evento.waitUntil(
    caches.keys().then(function (claves) {
      return Promise.all(claves.map(function (c) {
        return c === CACHE ? null : caches.delete(c);
      }));
    })
  );
  self.clients.claim();
});

self.addEventListener("fetch", function (evento) {
  var pedido = evento.request;
  if (pedido.method !== "GET") { return; }

  var url = new URL(pedido.url);
  if (url.origin !== self.location.origin) { return; }
  // La API nunca se cachea: son datos vivos.
  if (url.pathname.indexOf("/api/") === 0) { return; }

  evento.respondWith(
    fetch(pedido).then(function (respuesta) {
      var copia = respuesta.clone();
      caches.open(CACHE).then(function (cache) { cache.put(pedido, copia); });
      return respuesta;
    }).catch(function () {
      return caches.match(pedido).then(function (guardada) {
        return guardada || caches.match("/barra/");
      });
    })
  );
});