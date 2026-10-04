// Mobile menu sidebar
function toggleMobileMenu() {
  const menu = document.getElementById('mobileMenu');
  const btn = document.getElementById('hamburgerBtn');
  const backdrop = document.getElementById('mobileBackdrop');
  if (!menu) return;
  const isOpen = menu.classList.toggle('open');
  if (backdrop) backdrop.classList.toggle('open', isOpen);
  document.body.style.overflow = isOpen ? 'hidden' : '';
  if (btn) {
    btn.classList.toggle('open', isOpen);
    btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  }
  menu.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
}

function closeMobileMenu() {
  const menu = document.getElementById('mobileMenu');
  const btn = document.getElementById('hamburgerBtn');
  const backdrop = document.getElementById('mobileBackdrop');
  if (menu) { menu.classList.remove('open'); menu.setAttribute('aria-hidden', 'true'); }
  if (backdrop) backdrop.classList.remove('open');
  if (btn) { btn.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); }
  document.body.style.overflow = '';
}

// Close on backdrop click
const _mobileBackdrop = document.getElementById('mobileBackdrop');
if (_mobileBackdrop) _mobileBackdrop.addEventListener('click', closeMobileMenu);

// Close on Escape key
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeMobileMenu();
});

// Notification polling
let notifPollInterval = null;

function toggleNotifDropdown() {
  const dropdown = document.getElementById('notifDropdown');
  const btn = document.getElementById('notifBtn');
  if (!dropdown) return;
  const isOpen = dropdown.style.display !== 'none';
  dropdown.style.display = isOpen ? 'none' : 'block';
  if (btn) btn.setAttribute('aria-expanded', isOpen ? 'false' : 'true');
  if (!isOpen) loadNotifications();
}

function loadNotifications() {
  const list = document.getElementById('notifList');
  fetch('/api/notifications/', {headers: {'X-Requested-With': 'XMLHttpRequest'}})
    .then(r => r.json())
    .then(data => {
      // Always render the dropdown list on explicit open
      if (list) {
        if (data.notifications.length === 0) {
          list.innerHTML = '<p class="notif-empty"><i class="bi bi-bell-slash" aria-hidden="true" style="display:block;font-size:28px;margin-bottom:6px;"></i>No notifications</p>';
        } else {
          list.innerHTML = data.notifications.map(n => {
            const safeLink = n.link && isSafeRelativeLink(n.link) ? escapeHtml(n.link) : null;
            return `<div class="notif-item ${n.is_read ? '' : 'unread'}">
              ${safeLink ? `<a href="${safeLink}" style="color:inherit;">` : ''}
              ${escapeHtml(n.message)}
              ${safeLink ? '</a>' : ''}
              <br><small style="color:#94a3b8;">${formatDate(n.created_at)}</small>
            </div>`;
          }).join('');
        }
      }
      _applyNotifData(data);
    })
    .catch(() => {});
}

function markAllRead() {
  const csrfToken = getCsrfToken();
  fetch('/api/notifications/read/', {
    method: 'POST',
    headers: {'X-CSRFToken': csrfToken, 'X-Requested-With': 'XMLHttpRequest'}
  }).then(() => loadNotifications()).catch(() => {});
}

function getCsrfToken() {
  const cookie = document.cookie.split(';').find(c => c.trim().startsWith('csrftoken='));
  return cookie ? cookie.split('=')[1] : '';
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.appendChild(document.createTextNode(str));
  return div.innerHTML;
}

// Same-origin relative path only: rejects "//host" and "/\host" (protocol-relative).
function isSafeRelativeLink(link) {
  return typeof link === 'string' && link.charAt(0) === '/' && link.charAt(1) !== '/' && link.charAt(1) !== '\\';
}

function formatDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString('en-PH', {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
}

function showNotifToast(message) {
  const existing = document.getElementById('notifToast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.id = 'notifToast';
  toast.setAttribute('role', 'alert');
  toast.style.cssText = [
    'position:fixed', 'bottom:24px', 'right:24px', 'z-index:9999',
    'background:#0E9AA7', 'color:#fff', 'padding:13px 18px',
    'border-radius:12px', 'font-size:14px', 'font-weight:600',
    'box-shadow:0 6px 24px rgba(0,0,0,.18)', 'max-width:320px',
    'display:flex', 'align-items:flex-start', 'gap:10px',
    'animation:toastIn .3s ease', 'cursor:pointer'
  ].join(';');
  toast.innerHTML = `<i class="bi bi-bell-fill" style="font-size:16px;flex-shrink:0;margin-top:1px;"></i><span>${escapeHtml(message)}</span>`;
  toast.addEventListener('click', function() { toast.remove(); });
  document.body.appendChild(toast);
  setTimeout(() => { if (toast.parentNode) toast.remove(); }, 6000);
}

// ── Notification delivery: SSE with polling fallback ──────────────────────
// -1 means "no baseline yet" — the first response sets the baseline without
// firing a toast, so existing unread notifications don't pop on every refresh.
var _lastUnreadCount = -1;
var _sseSource = null;
var _sseFallbackTimer = null;
var _sseFailCount = 0;

function _applyNotifData(data) {
  const badge  = document.getElementById('notifBadge');
  const srCount = document.getElementById('notifCount');
  if (!badge) return;

  if (data.unread_count > 0) {
    badge.textContent = data.unread_count;
    badge.style.display = 'inline';
    if (srCount) srCount.textContent = `${data.unread_count} unread notification${data.unread_count !== 1 ? 's' : ''}`;
  } else {
    badge.style.display = 'none';
    if (srCount) srCount.textContent = '';
  }

  // Toast only when count rises during the session (new notification arrived).
  // Skip on the very first response so we don't pop existing unread items every refresh.
  if (_lastUnreadCount >= 0 && data.unread_count > _lastUnreadCount) {
    const newest = data.notifications.find(n => !n.is_read);
    if (newest) showNotifToast(newest.message);
  }
  _lastUnreadCount = data.unread_count;

  // Refresh open dropdown list in place
  const dropdown = document.getElementById('notifDropdown');
  const list = document.getElementById('notifList');
  if (list && dropdown && dropdown.style.display !== 'none') {
    if (data.notifications.length === 0) {
      list.innerHTML = '<p class="notif-empty"><i class="bi bi-bell-slash" aria-hidden="true" style="display:block;font-size:28px;margin-bottom:6px;"></i>No notifications</p>';
    } else {
      list.innerHTML = data.notifications.map(n => {
        const safeLink = n.link && isSafeRelativeLink(n.link) ? escapeHtml(n.link) : null;
        return `<div class="notif-item ${n.is_read ? '' : 'unread'}">
          ${safeLink ? `<a href="${safeLink}" style="color:inherit;">` : ''}
          ${escapeHtml(n.message)}
          ${safeLink ? '</a>' : ''}
          <br><small style="color:#94a3b8;">${formatDate(n.created_at)}</small>
        </div>`;
      }).join('');
    }
  }
}

function _startPollingFallback() {
  if (notifPollInterval) return;
  loadNotifications();
  notifPollInterval = setInterval(() => {
    fetch('/api/notifications/', {headers: {'X-Requested-With': 'XMLHttpRequest'}})
      .then(r => r.json())
      .then(_applyNotifData)
      .catch(() => {});
  }, 30000);
}

function connectNotifSSE() {
  if (!document.getElementById('notifBadge')) return;

  if (!window.EventSource || document.body.dataset.notifSse === 'off') {
    // No SSE support, or disabled server-side (NOTIFICATIONS_SSE=False) — use polling
    _startPollingFallback();
    return;
  }

  if (_sseSource) {
    _sseSource.close();
    _sseSource = null;
  }

  _sseSource = new EventSource('/api/notifications/stream/');

  _sseSource.onopen = function () {
    _sseFailCount = 0;
    // SSE is live — stop any polling fallback timer
    if (notifPollInterval) {
      clearInterval(notifPollInterval);
      notifPollInterval = null;
    }
    if (_sseFallbackTimer) {
      clearTimeout(_sseFallbackTimer);
      _sseFallbackTimer = null;
    }
  };

  _sseSource.onmessage = function (e) {
    try {
      _applyNotifData(JSON.parse(e.data));
    } catch (_) {}
  };

  _sseSource.onerror = function () {
    _sseFailCount++;
    _sseSource.close();
    _sseSource = null;

    if (_sseFailCount <= 5) {
      // Exponential back-off: 2s, 4s, 8s, 16s, 30s
      const delay = Math.min(2000 * Math.pow(2, _sseFailCount - 1), 30000);
      _sseFallbackTimer = setTimeout(connectNotifSSE, delay);
    } else {
      // SSE keeps failing — fall back to 30-second polling permanently
      _startPollingFallback();
    }
  };
}

if (document.getElementById('notifBadge')) {
  // The stream's first message is the current snapshot, so no separate fetch is needed
  // (the polling fallback and opening the dropdown still call loadNotifications()).
  connectNotifSSE();

  // Each open stream holds a server thread and polls the DB every 3 s. Close it while the
  // tab is in the background for a while; reconnect (with a fresh snapshot) when it's back.
  let _notifHiddenTimer = null;
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      _notifHiddenTimer = setTimeout(() => {
        if (_sseSource) { _sseSource.close(); _sseSource = null; }
        if (_sseFallbackTimer) { clearTimeout(_sseFallbackTimer); _sseFallbackTimer = null; }
        if (notifPollInterval) { clearInterval(notifPollInterval); notifPollInterval = null; }
      }, 60000);
    } else {
      clearTimeout(_notifHiddenTimer);
      if (!_sseSource && !notifPollInterval && !_sseFallbackTimer) connectNotifSSE();
    }
  });
}

window.addEventListener('beforeunload', function() {
  if (_sseSource) { _sseSource.close(); _sseSource = null; }
  if (notifPollInterval) { clearInterval(notifPollInterval); notifPollInterval = null; }
  if (_sseFallbackTimer) { clearTimeout(_sseFallbackTimer); _sseFallbackTimer = null; }
});

// Close notification dropdown when clicking outside
document.addEventListener('click', function(e) {
  const wrapper = document.querySelector('.notif-wrapper');
  if (wrapper && !wrapper.contains(e.target)) {
    const dropdown = document.getElementById('notifDropdown');
    const btn = document.getElementById('notifBtn');
    if (dropdown) dropdown.style.display = 'none';
    if (btn) btn.setAttribute('aria-expanded', 'false');
  }
});

// Order status polling for tracking page
const TRACKING_STALE_MS = 2 * 60 * 1000;   // older than this: warn the position may be outdated

function describeAge(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 45) return 'just now';
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  return `${h} hr${h === 1 ? '' : 's'} ago`;
}

function renderTrackingStatus(updatedAtIso) {
  const el = document.getElementById('trackingStatus');
  if (!el) return;
  if (!updatedAtIso) {
    el.className = 'tracking-status is-waiting';
    el.textContent = "Waiting for the rider's location…";
    return;
  }
  const age = Date.now() - new Date(updatedAtIso).getTime();
  const stale = age > TRACKING_STALE_MS;
  el.className = 'tracking-status' + (stale ? ' is-stale' : ' is-live');
  el.textContent = stale
    ? `Last known location, updated ${describeAge(age)} — the rider's app may be paused.`
    : `Rider location updated ${describeAge(age)}`;
}

if (typeof trackingOrderId !== 'undefined') {
  let lastUpdatedAt = (document.getElementById('trackingMap') || {}).dataset?.riderUpdatedAt || null;
  renderTrackingStatus(lastUpdatedAt);

  const pollTracking = () => {
    if (document.hidden) return;                    // nobody is watching: skip this 10 s poll
    fetch(`/api/orders/${trackingOrderId}/status/`)
      .then(r => r.json())
      .then(data => {
        // Tracking only exists while out for delivery; show the new state once it changes.
        if (data.status && data.status !== 'out_for_delivery') {
          clearInterval(trackingTimer);
          window.location.reload();
          return;
        }
        if (data.rider_lat && data.rider_lng && typeof updateRiderMarker === 'function') {
          updateRiderMarker(data.rider_lat, data.rider_lng);
        }
        if (data.rider_updated_at) lastUpdatedAt = data.rider_updated_at;
        renderTrackingStatus(lastUpdatedAt);
      })
      .catch(() => {});
  };
  pollTracking();                                   // no 10-second blank start
  document.addEventListener('visibilitychange', () => { if (!document.hidden) pollTracking(); });
  const trackingTimer = setInterval(pollTracking, 10000);
  // Keep the "x min ago" text honest between polls too.
  setInterval(() => renderTrackingStatus(lastUpdatedAt), 30000);
}

// Rider location broadcast
if (typeof isRiderPage !== 'undefined' && isRiderPage) {
  function broadcastLocation() {
    if (!navigator.geolocation) return;
    navigator.geolocation.getCurrentPosition(pos => {
      fetch(`/api/rider/${riderId}/location/`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': getCsrfToken()},
        body: JSON.stringify({latitude: pos.coords.latitude, longitude: pos.coords.longitude})
      }).catch(() => {});
    });
  }
  broadcastLocation();
  setInterval(broadcastLocation, 15000);
}
