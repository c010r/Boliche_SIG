/* Barra de Boliche SIG: venta con cola local.
 *
 * La regla que ordena todo esto: si se cae el wifi, la barra SIGUE VENDIENDO.
 * La venta se guarda en el dispositivo y se manda cuando vuelve la conexion.
 *
 * El servidor resuelve los precios: acá nunca se manda cuanto sale un trago.
 * Cada venta lleva su clave de idempotencia, así que reintentar la cola entera
 * las veces que haga falta no duplica nada.
 */

(function () {
  "use strict";

  var API = "/api/barra/sincronizar/";
  var DB = "boliche-sig";
  var ALMACEN = "ventas_pendientes";
  var versionCatalogo = null;
  var seleccionado = null;
  var sincronizando = false;

  // --- IndexedDB --------------------------------------------------------

  function abrirBase() {
    return new Promise(function (resolver, rechazar) {
      var pedido = indexedDB.open(DB, 1);
      pedido.onupgradeneeded = function () {
        var base = pedido.result;
        if (!base.objectStoreNames.contains(ALMACEN)) {
          base.createObjectStore(ALMACEN, { keyPath: "idempotency_key" });
        }
      };
      pedido.onsuccess = function () { resolver(pedido.result); };
      pedido.onerror = function () { rechazar(pedido.error); };
    });
  }

  function guardar(registro) {
    return abrirBase().then(function (base) {
      return new Promise(function (resolver, rechazar) {
        var tx = base.transaction(ALMACEN, "readwrite");
        tx.objectStore(ALMACEN).put(registro);
        tx.oncomplete = function () { resolver(registro); };
        tx.onerror = function () { rechazar(tx.error); };
      });
    });
  }

  function pendientes() {
    return abrirBase().then(function (base) {
      return new Promise(function (resolver, rechazar) {
        var tx = base.transaction(ALMACEN, "readonly");
        var pedido = tx.objectStore(ALMACEN).getAll();
        pedido.onsuccess = function () { resolver(pedido.result || []); };
        pedido.onerror = function () { rechazar(pedido.error); };
      });
    });
  }

  function borrar(clave) {
    return abrirBase().then(function (base) {
      return new Promise(function (resolver) {
        var tx = base.transaction(ALMACEN, "readwrite");
        tx.objectStore(ALMACEN).delete(clave);
        tx.oncomplete = function () { resolver(); };
      });
    });
  }

  // --- identificadores --------------------------------------------------

  function nuevoId() {
    if (window.crypto && crypto.randomUUID) { return crypto.randomUUID(); }
    return "v-" + Date.now() + "-" + Math.random().toString(36).slice(2);
  }

  function tokenCsrf() {
    var campo = document.querySelector("input[name=csrfmiddlewaretoken]");
    return campo ? campo.value : "";
  }

  // --- indicador --------------------------------------------------------

  /* El indicador NO puede alarmar. El modo offline no falla por un problema
   * tecnico: falla porque el cantinero deja de confiar y para de vender. */
  function pintarEstado() {
    var caja = document.getElementById("estado-conexion");
    if (!caja) { return; }
    pendientes().then(function (lista) {
      var n = lista.length;
      if (navigator.onLine) {
        if (n === 0) {
          caja.textContent = "Todo al dia";
          caja.className = "chico pos";
        } else {
          caja.textContent = "Enviando " + n + "...";
          caja.className = "chico aviso";
        }
      } else {
        caja.textContent = n === 0
          ? "Sin conexion: podes seguir vendiendo"
          : "Sin conexion: " + n + " venta(s) guardadas, se envian solas";
        caja.className = "chico aviso";
      }
    });
  }

  function avisar(texto, esError) {
    var caja = document.getElementById("aviso-venta");
    if (!caja) { return; }
    caja.textContent = texto;
    caja.className = esError ? "pos" : "aviso";
    setTimeout(function () { caja.textContent = ""; }, 4000);
  }

  // --- venta ------------------------------------------------------------

  function cobrar(medio) {
    if (!seleccionado) { return; }
    var registro = {
      id_local: nuevoId(),
      idempotency_key: nuevoId(),
      producto: seleccionado.id,
      cantidad: 1,
      medio: medio,
      creada_en_cliente: new Date().toISOString(),
      catalogo_version: versionCatalogo
    };

    guardar(registro).then(function () {
      avisar("Vendido: " + seleccionado.nombre + " (" + medio + ")", false);
      cancelar();
      pintarEstado();
      sincronizar();
    });
  }

  function cancelar() {
    seleccionado = null;
    var barra = document.getElementById("pago");
    if (barra) { barra.hidden = true; }
  }

  // --- sincronizacion ---------------------------------------------------

  function sincronizar() {
    if (sincronizando || !navigator.onLine) { return Promise.resolve(); }
    sincronizando = true;

    return pendientes().then(function (lista) {
      if (!lista.length) { return null; }
      return fetch(API, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": tokenCsrf()
        },
        body: JSON.stringify({
          catalogo_version: versionCatalogo,
          ventas: lista
        })
      }).then(function (respuesta) { return respuesta.json(); });
    }).then(function (datos) {
      if (!datos) { return; }
      if (datos.catalogo_version) { versionCatalogo = datos.catalogo_version; }
      var borradas = [];
      (datos.resultados || []).forEach(function (r) {
        // Un error de datos (producto que ya no existe) no se reintenta nunca:
        // reintentarlo para siempre traba la cola.
        if (r.estado === "registrada") { borradas.push(r.idempotency_key); }
        if (r.estado === "error" && r.mensaje && r.mensaje.indexOf("ya no existe") >= 0) {
          borradas.push(r.idempotency_key);
        }
      });
      return Promise.all(borradas.map(borrar));
    }).catch(function () {
      // Sin conexion o servidor caido: la cola queda intacta y se reintenta.
    }).then(function () {
      sincronizando = false;
      pintarEstado();
    });
  }

  // --- arranque ---------------------------------------------------------

  function iniciar() {
    var datos = document.getElementById("datos-barra");
    if (datos) {
      versionCatalogo = parseInt(datos.dataset.catalogoVersion, 10) || null;
    }

    var botones = document.querySelectorAll("[data-producto]");
    for (var i = 0; i < botones.length; i++) {
      botones[i].addEventListener("click", function (ev) {
        var b = ev.currentTarget;
        seleccionado = { id: b.dataset.producto, nombre: b.dataset.nombre };
        var detalle = document.getElementById("pd");
        if (detalle) {
          detalle.textContent = b.dataset.nombre + "  ·  $ " + b.dataset.precio;
        }
        var panel = document.getElementById("pago");
        if (panel) { panel.hidden = false; }
      });
    }

    window.addEventListener("online", function () { pintarEstado(); sincronizar(); });
    window.addEventListener("offline", pintarEstado);
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) { pintarEstado(); sincronizar(); }
    });
    setInterval(function () { sincronizar(); }, 20000);

    pintarEstado();
    sincronizar();

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(function () {});
    }
  }

  window.cobrar = cobrar;
  window.cancelar = cancelar;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", iniciar);
  } else {
    iniciar();
  }
})();