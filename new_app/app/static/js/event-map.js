/* Phase 12: the public events map.
 *
 * Progressive enhancement only — the event list is always rendered and is the
 * full, accessible equivalent of this map. This script fetches the same
 * filters' matching events (those with coordinates) and plots them with
 * marker clustering and keyboard-accessible popups. It reads its configuration
 * from data attributes and never inlines untrusted values into HTML.
 */
(function () {
  "use strict";

  var el = document.getElementById("event-map");
  if (!el || typeof L === "undefined") {
    return;
  }

  var mapUrl = el.getAttribute("data-map-url");
  var tileUrl = el.getAttribute("data-tile-url");
  var attribution = el.getAttribute("data-attribution") || "";

  var map = L.map(el, { scrollWheelZoom: false });
  // A sensible default view until markers arrive (fitBounds overrides it).
  map.setView([39.5, -98.35], 4);
  L.tileLayer(tileUrl, { attribution: attribution, maxZoom: 19 }).addTo(map);

  function popupHtml(point) {
    // Build with the DOM, not string concatenation, so titles/venues can never
    // be interpreted as markup.
    var wrap = document.createElement("div");
    wrap.className = "map-popup";
    var link = document.createElement("a");
    link.href = point.url;
    link.textContent = point.title;
    var h = document.createElement("strong");
    h.appendChild(link);
    wrap.appendChild(h);
    if (point.start_date) {
      var d = document.createElement("div");
      d.textContent = point.start_date;
      wrap.appendChild(d);
    }
    if (point.venue) {
      var v = document.createElement("div");
      v.textContent = point.venue;
      wrap.appendChild(v);
    }
    return wrap;
  }

  fetch(mapUrl, { headers: { Accept: "application/json" } })
    .then(function (r) {
      return r.ok ? r.json() : { points: [] };
    })
    .then(function (data) {
      var points = (data && data.points) || [];
      if (!points.length) {
        return;
      }
      var cluster =
        typeof L.markerClusterGroup === "function"
          ? L.markerClusterGroup()
          : L.layerGroup();
      var bounds = [];
      points.forEach(function (p) {
        if (typeof p.latitude !== "number" || typeof p.longitude !== "number") {
          return;
        }
        var marker = L.marker([p.latitude, p.longitude], {
          title: p.title,
          alt: p.title,
          keyboard: true,
        });
        marker.bindPopup(popupHtml(p));
        cluster.addLayer(marker);
        bounds.push([p.latitude, p.longitude]);
      });
      map.addLayer(cluster);
      if (bounds.length) {
        map.fitBounds(bounds, { padding: [30, 30], maxZoom: 15 });
      }
    })
    .catch(function () {
      /* The list remains the authoritative, accessible view. */
    });
})();
