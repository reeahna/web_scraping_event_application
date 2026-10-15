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

  var note = document.getElementById("event-map-note");
  var centerLat = parseFloat(el.getAttribute("data-center-lat"));
  var centerLng = parseFloat(el.getAttribute("data-center-lng"));
  var hasCenter = isFinite(centerLat) && isFinite(centerLng);

  var map = L.map(el, { scrollWheelZoom: false });
  // Start on the town when it has a known centre, otherwise the whole US;
  // zoomToTown refines either once markers arrive.
  if (hasCenter) {
    map.setView([centerLat, centerLng], 13);
  } else {
    map.setView([39.5, -98.35], 4);
  }
  L.tileLayer(tileUrl, { attribution: attribution, maxZoom: 19 }).addTo(map);

  // Events further than this from the town still get a pin, but they do not
  // drive the zoom: one mis-geocoded venue in another state would otherwise
  // pull the view out to the whole country.
  var TOWN_RADIUS_KM = 40;

  function distanceKm(a, b) {
    var rad = Math.PI / 180;
    var dLat = (b[0] - a[0]) * rad;
    var dLng = (b[1] - a[1]) * rad;
    var h =
      Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(a[0] * rad) * Math.cos(b[0] * rad) *
        Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
  }

  function median(values) {
    var sorted = values.slice().sort(function (x, y) {
      return x - y;
    });
    var mid = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  }

  // Zoom to the town the visitor chose: its own centre when the town has one,
  // otherwise the middle of its events (the median ignores far-off outliers).
  // Only the events near that point decide how far in to go.
  function zoomToTown(points) {
    if (!points.length) {
      return;
    }
    var center = hasCenter
      ? [centerLat, centerLng]
      : [
          median(points.map(function (p) { return p[0]; })),
          median(points.map(function (p) { return p[1]; })),
        ];
    var nearby = points.filter(function (p) {
      return distanceKm(center, p) <= TOWN_RADIUS_KM;
    });
    if (nearby.length) {
      map.fitBounds(nearby, { padding: [30, 30], maxZoom: 15 });
    } else {
      map.setView(center, 13);
    }
  }

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
        // Say so rather than leave an empty map that looks broken.
        if (note) {
          note.textContent =
            "None of these events has a location yet, so there is nothing to " +
            "pin on the map. The list below has every matching event.";
        }
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
      zoomToTown(bounds);
    })
    .catch(function () {
      /* The list remains the authoritative, accessible view. */
    });
})();
