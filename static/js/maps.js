let map, riderMarker;

function readShopMarkers() {
  const el = document.getElementById('shop-markers');
  return el ? JSON.parse(el.textContent) : [];
}

function initMapsIfNeeded() {
  // Shop list map
  const listMapEl = document.getElementById('shopsMap');
  const shopMarkers = readShopMarkers();
  if (listMapEl && shopMarkers.length > 0) {
    initShopsMap(listMapEl, shopMarkers);
  }

  // Shop detail map
  const shopMapEl = document.getElementById('shopMap');
  if (shopMapEl) {
    const lat = parseFloat(shopMapEl.dataset.lat);
    const lng = parseFloat(shopMapEl.dataset.lng);
    if (!isNaN(lat) && !isNaN(lng)) {
      const m = new google.maps.Map(shopMapEl, {zoom: 15, center: {lat, lng}});
      new google.maps.Marker({position: {lat, lng}, map: m, title: shopMapEl.dataset.name});
    }
  }

  // Order tracking map
  const trackingMapEl = document.getElementById('trackingMap');
  if (trackingMapEl) {
    initTrackingMap(trackingMapEl);
  }

  // Rider nav map
  const riderNavEl = document.getElementById('riderNavMap');
  if (riderNavEl) {
    initRiderNavMap(riderNavEl);
  }
}

function initShopsMap(el, shopMarkers) {
  const center = shopMarkers.length > 0
    ? {lat: shopMarkers[0].lat, lng: shopMarkers[0].lng}
    : {lat: 9.34, lng: 125.98};

  const m = new google.maps.Map(el, {zoom: 13, center});

  if (navigator.geolocation) {
    navigator.geolocation.getCurrentPosition(pos => {
      m.setCenter({lat: pos.coords.latitude, lng: pos.coords.longitude});
    });
  }

  shopMarkers.forEach(s => {
    const marker = new google.maps.Marker({position: {lat: s.lat, lng: s.lng}, map: m, title: s.name});
    // Build nodes instead of an HTML string: shop names are owner-controlled.
    const link = document.createElement('a');
    link.href = s.url;
    const strong = document.createElement('strong');
    strong.textContent = s.name;
    link.appendChild(strong);
    const iw = new google.maps.InfoWindow({content: link});
    marker.addListener('click', () => iw.open(m, marker));
  });
}

function initTrackingMap(el) {
  const deliveryLat = parseFloat(el.dataset.deliveryLat);
  const deliveryLng = parseFloat(el.dataset.deliveryLng);
  const center = (!isNaN(deliveryLat) && !isNaN(deliveryLng))
    ? {lat: deliveryLat, lng: deliveryLng}
    : {lat: 9.34, lng: 125.98};

  map = new google.maps.Map(el, {zoom: 14, center});

  if (!isNaN(deliveryLat) && !isNaN(deliveryLng)) {
    new google.maps.Marker({
      position: center,
      map,
      title: 'Delivery Address',
      icon: 'https://maps.google.com/mapfiles/ms/icons/green-dot.png'
    });
  }

  riderMarker = new google.maps.Marker({map, title: 'Rider', icon: 'https://maps.google.com/mapfiles/ms/icons/motorcycling.png'});

  // Show the last known rider position immediately instead of waiting for the first poll.
  const riderLat = parseFloat(el.dataset.riderLat);
  const riderLng = parseFloat(el.dataset.riderLng);
  if (!isNaN(riderLat) && !isNaN(riderLng)) {
    riderMarker.setPosition({lat: riderLat, lng: riderLng});
    fitTrackingView();
  }
}

let trackingViewFitted = false;

// First fix: frame both the rider and the delivery address.
function fitTrackingView() {
  if (!map || !riderMarker || !riderMarker.getPosition()) return;
  const deliveryLat = parseFloat(document.getElementById('trackingMap').dataset.deliveryLat);
  const deliveryLng = parseFloat(document.getElementById('trackingMap').dataset.deliveryLng);
  if (isNaN(deliveryLat) || isNaN(deliveryLng)) {
    map.setCenter(riderMarker.getPosition());
  } else {
    const bounds = new google.maps.LatLngBounds();
    bounds.extend(riderMarker.getPosition());
    bounds.extend({lat: deliveryLat, lng: deliveryLng});
    map.fitBounds(bounds, 48);
  }
  trackingViewFitted = true;
}

function updateRiderMarker(lat, lng) {
  if (!riderMarker) return;
  const pos = {lat, lng};
  riderMarker.setPosition(pos);
  if (!map) return;
  if (!trackingViewFitted) {
    fitTrackingView();
  } else if (map.getBounds() && !map.getBounds().contains(pos)) {
    map.panTo(pos);   // only move the map when the rider leaves the visible area
  }
}

function initRiderNavMap(el) {
  const point = (lat, lng) => {
    const p = {lat: parseFloat(lat), lng: parseFloat(lng)};
    return isNaN(p.lat) || isNaN(p.lng) ? null : p;
  };
  const stops = {
    pickup: {pos: point(el.dataset.pickupLat, el.dataset.pickupLng), title: 'Pickup', icon: 'blue-dot'},
    shop: {pos: point(el.dataset.shopLat, el.dataset.shopLng), title: 'Shop', icon: 'yellow-dot'},
    delivery: {pos: point(el.dataset.deliveryLat, el.dataset.deliveryLng), title: 'Delivery', icon: 'red-dot'},
  };
  const target = stops[el.dataset.stage] || stops.pickup;
  const anyStop = Object.values(stops).find(s => s.pos);
  if (!anyStop) return;                       // nothing geocoded: keep the illustrated map

  el.closest('.rw-map')?.classList.add('is-live');
  const m = new google.maps.Map(el, {zoom: 14, center: (target.pos || anyStop.pos)});
  const bounds = new google.maps.LatLngBounds();
  Object.values(stops).forEach(s => {
    if (!s.pos) return;
    new google.maps.Marker({position: s.pos, map: m, title: s.title,
      icon: `https://maps.google.com/mapfiles/ms/icons/${s.icon}.png`});
    bounds.extend(s.pos);
  });

  // Route from the rider's position to the current stop (falls back to just the markers).
  if (!navigator.geolocation || !target.pos) return;
  navigator.geolocation.getCurrentPosition(pos => {
    const you = {lat: pos.coords.latitude, lng: pos.coords.longitude};
    new google.maps.Marker({position: you, map: m, title: 'You',
      icon: 'https://maps.google.com/mapfiles/ms/icons/motorcycling.png'});
    const dr = new google.maps.DirectionsRenderer({map: m, suppressMarkers: true});
    new google.maps.DirectionsService().route({
      origin: you, destination: target.pos, travelMode: google.maps.TravelMode.DRIVING,
    }, (result, status) => {
      if (status === 'OK') dr.setDirections(result);
      else { bounds.extend(you); m.fitBounds(bounds); }
    });
  }, () => { if (!bounds.isEmpty()) m.fitBounds(bounds); });
}
