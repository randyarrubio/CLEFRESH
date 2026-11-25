/* CLEFRESH maps — Leaflet + OpenStreetMap tiles (no API key, no billing).
 *
 * base.html loads Leaflet only on pages with a map, then calls initMapsIfNeeded().
 * Maps: #shopsMap (shop list), #shopMap (shop detail), #trackingMap (customer live tracking),
 * #riderNavMap (rider route). main.js calls updateRiderMarker(lat, lng) from its tracking poll.
 * Road routes come from the public OSRM router; if it's unavailable a straight dashed line is drawn.
 */
let map, riderMarker;
const DEFAULT_CENTER = [9.3350, 125.9770];       // Cantilan, Surigao del Sur
const TILE_URL = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png';
const TILE_ATTRIBUTION = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

function baseMap(el, center, zoom) {
  const m = L.map(el, { scrollWheelZoom: false }).setView(center, zoom);
  L.tileLayer(TILE_URL, { maxZoom: 19, attribution: TILE_ATTRIBUTION }).addTo(m);
  return m;
}

// Coloured pin made of a circle, so no image files are needed.
function dot(color) {
  return L.divIcon({
    className: 'cf-map-pin',
    html: `<span style="background:${color}"></span>`,
    iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -12],
  });
}
function riderIcon() {
  return L.divIcon({
    className: 'cf-map-rider',
    html: '<i class="bi bi-bicycle" aria-hidden="true"></i>',
    iconSize: [34, 34], iconAnchor: [17, 17],
  });
}
function pin(m, latlng, color, title) {
  return L.marker(latlng, { icon: dot(color), title, alt: title }).addTo(m).bindTooltip(title);
}
const num = (v) => { const n = parseFloat(v); return isNaN(n) ? null : n; };
const point = (lat, lng) => (num(lat) === null || num(lng) === null ? null : [num(lat), num(lng)]);

function readShopMarkers() {
  const el = document.getElementById('shop-markers');
  return el ? JSON.parse(el.textContent) : [];
}

function initMapsIfNeeded() {
  if (typeof L === 'undefined') return;

  const listMapEl = document.getElementById('shopsMap');
  const shopMarkers = readShopMarkers();
  if (listMapEl && shopMarkers.length > 0) initShopsMap(listMapEl, shopMarkers);

  const shopMapEl = document.getElementById('shopMap');
  if (shopMapEl) {
    const p = point(shopMapEl.dataset.lat, shopMapEl.dataset.lng);
    if (p) {
      const m = baseMap(shopMapEl, p, 16);
      pin(m, p, '#0c70b8', shopMapEl.dataset.name || 'Shop');
    }
  }

  const trackingMapEl = document.getElementById('trackingMap');
  if (trackingMapEl) initTrackingMap(trackingMapEl);

  const riderNavEl = document.getElementById('riderNavMap');
  if (riderNavEl) initRiderNavMap(riderNavEl);
}

function initShopsMap(el, shopMarkers) {
  const m = baseMap(el, [shopMarkers[0].lat, shopMarkers[0].lng], 14);
  const bounds = L.latLngBounds([]);
  shopMarkers.forEach((s) => {
    const marker = pin(m, [s.lat, s.lng], '#0c70b8', s.name);
    // Build nodes instead of an HTML string: shop names are owner-controlled.
    const link = document.createElement('a');
    link.href = s.url;
    const strong = document.createElement('strong');
    strong.textContent = s.name;
    link.appendChild(strong);
    marker.bindPopup(link);
    bounds.extend([s.lat, s.lng]);
  });
  if (shopMarkers.length > 1) m.fitBounds(bounds, { padding: [32, 32], maxZoom: 16 });
}

function initTrackingMap(el) {
  const delivery = point(el.dataset.deliveryLat, el.dataset.deliveryLng);
  map = baseMap(el, delivery || DEFAULT_CENTER, 15);
  if (delivery) pin(map, delivery, '#18a463', 'Delivery address');

  // Show the last known rider position immediately instead of waiting for the first poll.
  const rider = point(el.dataset.riderLat, el.dataset.riderLng);
  if (rider) updateRiderMarker(rider[0], rider[1]);
}

let trackingViewFitted = false;

// First fix: frame both the rider and the delivery address.
function fitTrackingView() {
  if (!map || !riderMarker) return;
  const el = document.getElementById('trackingMap');
  const delivery = point(el.dataset.deliveryLat, el.dataset.deliveryLng);
  if (!delivery) {
    map.setView(riderMarker.getLatLng(), 16);
  } else {
    map.fitBounds(L.latLngBounds([riderMarker.getLatLng(), delivery]), { padding: [48, 48], maxZoom: 17 });
  }
  trackingViewFitted = true;
}

function updateRiderMarker(lat, lng) {
  if (!map) return;
  const pos = [lat, lng];
  if (!riderMarker) {
    riderMarker = L.marker(pos, { icon: riderIcon(), title: 'Rider', alt: 'Rider', zIndexOffset: 500 })
      .addTo(map).bindTooltip('Your rider');
  } else {
    riderMarker.setLatLng(pos);
  }
  if (!trackingViewFitted) fitTrackingView();
  else if (!map.getBounds().contains(pos)) map.panTo(pos);   // only move when the rider leaves the view
}

function drawRoute(m, from, to, onDone) {
  // Public OSRM demo router: free, no key. Falls back to a straight dashed line.
  const url = `https://router.project-osrm.org/route/v1/driving/${from[1]},${from[0]};${to[1]},${to[0]}?overview=full&geometries=geojson`;
  fetch(url)
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((data) => {
      const coords = data.routes && data.routes[0] && data.routes[0].geometry.coordinates;
      if (!coords) throw new Error('no route');
      const line = L.polyline(coords.map(([lng, lat]) => [lat, lng]), { color: '#0c70b8', weight: 5, opacity: 0.85 }).addTo(m);
      onDone(line.getBounds());
    })
    .catch(() => {
      const line = L.polyline([from, to], { color: '#0c70b8', weight: 3, dashArray: '6 8' }).addTo(m);
      onDone(line.getBounds());
    });
}

function initRiderNavMap(el) {
  const stops = {
    pickup: { pos: point(el.dataset.pickupLat, el.dataset.pickupLng), title: 'Pickup', color: '#0c70b8' },
    shop: { pos: point(el.dataset.shopLat, el.dataset.shopLng), title: 'Shop', color: '#d99a06' },
    delivery: { pos: point(el.dataset.deliveryLat, el.dataset.deliveryLng), title: 'Delivery', color: '#d64545' },
  };
  const target = stops[el.dataset.stage] || stops.pickup;
  const anyStop = Object.values(stops).find((s) => s.pos);
  if (!anyStop) return;                       // nothing geocoded yet: keep the illustrated map

  el.closest('.rw-map')?.classList.add('is-live');
  const m = baseMap(el, target.pos || anyStop.pos, 15);
  const bounds = L.latLngBounds([]);
  Object.values(stops).forEach((s) => {
    if (!s.pos) return;
    pin(m, s.pos, s.color, s.title);
    bounds.extend(s.pos);
  });
  if (bounds.isValid()) m.fitBounds(bounds, { padding: [32, 32], maxZoom: 16 });

  // Route from the rider's position to the current stop.
  if (!navigator.geolocation || !target.pos) return;
  navigator.geolocation.getCurrentPosition((pos) => {
    const you = [pos.coords.latitude, pos.coords.longitude];
    L.marker(you, { icon: riderIcon(), title: 'You', alt: 'You' }).addTo(m).bindTooltip('You');
    drawRoute(m, you, target.pos, (routeBounds) => {
      m.fitBounds(routeBounds.extend(you).extend(target.pos), { padding: [32, 32], maxZoom: 17 });
    });
  }, () => {}, { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 });
}
