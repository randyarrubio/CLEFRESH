# CLAUDE.md — CLEFRESH

## Project
Laundry shop booking platform. Customers find nearby laundry shops, book pickup/delivery slots, pay online. Shop owners manage services and orders. Riders handle logistics.

## Stack
- Django 4.2.30, Python 3.13, SQLite (dev)
- django-allauth 65.9.0 (email + Google SSO only — Facebook SSO removed)
- PayMongo (payments), Google Maps API (geocoding + rider tracking)
- Formspree (email), vanilla JS + Django templates
- Bootstrap Icons 1.11.3 (self-hosted in `static/vendor/bootstrap-icons-1.11.3/`, loaded in `base.html`)

## Apps
| App | Purpose |
|-----|---------|
| `accounts` | Custom User model, SSO completion, decorators, forgot password, rider doc serving |
| `shops` | Shop, Service, DeliverySlot |
| `orders` | Order, OrderService, OrderStatusLog, PhotoProof, EmailLog |
| `payments` | PayMongoTransaction, Refund, WebhookEvent |
| `maps` | Rider dashboard + location tracking |
| `reviews` | Shop reviews |
| `notifications` | In-app notifications API |
| `analytics` | Admin panel — dashboard, shops, users, orders, transactions, analytics, broadcast |
| `utils` | email_notifications.py, geocode.py, validators.py |

## Models (quick ref)
- **User** → roles: `customer | shop_owner | rider | admin`
- **Shop** → `status: pending|approved|rejected|suspended`, lat/lng auto-geocoded on save
  - `suspended` shops are invisible to customers; shop owner loses dashboard access; admin can reinstate
- **Service** → types: `wash|dry|fold|iron|dry_clean`, price per kg or per piece
- **DeliverySlot** → date+time window, max_capacity, current_bookings
- **Order** → status flow: `pending→accepted→picked_up→washing→drying→folding→ready→out_for_delivery→delivered`; terminal states: `declined`, `cancelled`
- **Order.payment_method** → `gcash|grab_pay|maya|card|online_banking|cod`
- **Order.total_amount** → capped at ₱50,000 (enforced in `place_order_view`)
- **OrderStatusLog.status** → `CharField(max_length=30)` with `choices=Order.STATUS_CHOICES` — use `|humanize_status` filter (from `shop_extras`) for display; `get_status_display()` now works but `|humanize_status` is preferred in templates
- **PayMongoTransaction** → amounts in **centavos** (PHP × 100)
- **WebhookEvent** → idempotency log for PayMongo webhooks; keyed on `event_id`
- **All file fields** → use UUID-based upload paths (prevents sequential enumeration)

## URL Structure
| Prefix | App |
|--------|-----|
| `/` | home (landing page; riders redirected to `/rider/` when logged in) |
| `/shops/` | shop listing + detail |
| `/auth/` | accounts (login, register, logout, profile, forgot password) |
| `/orders/` | orders + reviews |
| `/payment/` | PayMongo checkout/webhook |
| `/rider/` | rider dashboard |
| `/shop-dashboard/` | shop owner dashboard |
| `/admin-panel/` | admin dashboard, shops, users, orders, transactions, analytics, broadcast |
| `/api/` | notifications |
| `/info/<slug>/` | help, privacy, terms, refunds (under root shops.urls, not a separate app) |

### Auth URLs (under `/auth/`)
| Name | Path |
|------|------|
| `forgot_password` | `forgot-password/` |
| `password_reset_done` | `forgot-password/done/` |
| `password_reset_confirm` | `reset-password/<uidb64>/<token>/` |
| `serve_rider_doc` | `media/rider-docs/<filename>` |

## Key Config
- `AUTH_USER_MODEL = 'accounts.User'`, `TIME_ZONE = 'Asia/Manila'`, `SITE_ID = 1`
- All API keys via `.env` + python-decouple: `PAYMONGO_SECRET_KEY`, `PAYMONGO_PUBLIC_KEY`, `PAYMONGO_WEBHOOK_SECRET`, `GOOGLE_MAPS_API_KEY`, `FORMSPREE_ENDPOINT`, `GOOGLE_CLIENT_ID/SECRET`
- `DJANGO_ADMIN_URL` env var controls the Django admin path (default: `django-admin/`) — change in production
- **Google Maps API key** only injected into templates on `/`, `/orders/`, `/rider/`, `/shops/`, `/shop-dashboard/` — not global
- **PayMongo public key** only injected on `/payment/` paths — not global
- `NOTIFICATIONS_SSE` (default True) — set False on PythonAnywhere; bell falls back to 30 s polling. Deploy guide: `deploy/PYTHONANYWHERE.md`
- **SMTP is required for transactional email** — password resets and verification codes are never sent through Formspree. Formspree is used only for non-sensitive notification fallback; configure SMTP in production (`utils/email_notifications.py`).

## Security Hardening (implemented)
| Feature | Location |
|---|---|
| Login rate limiting | 10 attempts / 15 min per IP via Django cache (`accounts/views.py`) |
| Webhook idempotency | `WebhookEvent` model; duplicate events return 200 immediately |
| File magic byte validation | PIL verify for images, `%PDF` header for PDFs (`utils/validators.py`) |
| UUID upload paths | All `ImageField` / `FileField` fields across all apps |
| Rider doc auth serving | `/auth/media/rider-docs/<filename>` — login + ownership check required |
| Session security | `register_form_data` session key stores only non-sensitive fields (no passwords) |
| Rider location gating | Coordinates only returned when `order.status == 'out_for_delivery'` |
| Geocode rate limit | 30 calls / hour per user (`maps/views.py`) |
| Duplicate payment guard | Blocks new intent if one is already `awaiting_payment_method` or `processing` |
| Security headers | `SESSION_COOKIE_HTTPONLY`, `X_FRAME_OPTIONS='DENY'`, `SECURE_CONTENT_TYPE_NOSNIFF`, `SECURE_REFERRER_POLICY='strict-origin-when-cross-origin'` |
| Upload size cap | `DATA_UPLOAD_MAX_MEMORY_SIZE` and `FILE_UPLOAD_MAX_MEMORY_SIZE` = 10 MB |
| Order value cap | ₱50,000 max enforced in `place_order_view` |
| Notification XSS | `n.link` validated as relative path and HTML-escaped before `href` insertion |

**Production deployment checklist:**
- Set `SESSION_COOKIE_SECURE=True`, `CSRF_COOKIE_SECURE=True`, `SECURE_SSL_REDIRECT=True` in `.env`
- Set `DJANGO_ADMIN_URL` to a non-obvious path
- Configure web server to block direct access to `/media/rider_docs/` — route through `/auth/media/rider-docs/` instead
- Run `pip-audit` against pinned `requirements.txt` before deploy
- Set `STATIC_HASHED_FILENAMES=True` and run `python manage.py collectstatic --noinput` on every deploy
- Use `deploy/nginx.conf.example` (gzip, 1-year static caching, media caching, private-doc blocking, SSE proxying)
- Set `CACHE_BACKEND=redis|db` and `CLIENT_IP_HEADER=HTTP_X_REAL_IP` behind nginx
- Existing oversized uploads: back up `media/` + DB, then `python manage.py optimize_media` (new uploads are shrunk automatically)

## Frontend / UI

### Design system
- **Primary color:** `#1287d9` (CSS var `--primary`)
- **Accent/CTA color:** `#f9ca47` (CSS var `--accent`) — yellow, used for primary buttons
- **Fonts:** Open Sans (headings, nav), DM Sans (body), Outfit (numbers/display) — all from Google Fonts
- **Icons:** Bootstrap Icons (`bi bi-*` classes) — self-hosted, loaded in `base.html`
- **Google SSO icon:** use inline SVG (real Google "G" logo), NOT `bi bi-google`

### Performance conventions
- Static images are **WebP** sized ~2× their largest display size (PNG originals kept in `static/img/` but not served); favicon is `favicon-32.png` / `favicon-180.png`
- Below-the-fold `<img>`: add `loading="lazy" decoding="async"`; the LCP/hero image gets `fetchpriority="high"` (never lazy)
- Uploads are shrunk on save (`utils/images.py`, hooked in `Shop.save`, `User.save`, `PhotoProof.save`): PNG → WebP, capped per field
- Google Maps JS is injected only when `#shopsMap/#trackingMap/#riderNavMap` exists and is near the viewport (loader in `base.html`)
- Google Fonts CSS loads non-blocking (`rel=preload` + `onload`); don't add `@import` fonts to `style.css`
- DB indexes on `Order` (created_at, shop+created_at, status, payment intent, payment_due_at partial) and `Notification` (user+created_at, user+is_read) — check `EXPLAIN QUERY PLAN` before adding more

### CSS conventions
- Global styles: `static/css/style.css` — `:root` CSS variables
- Home page styles scoped in `{% block extra_head %}` with `.tlp-*` prefix
- **Shared layout utilities** (added to `style.css`):
  - `.page-shell` — standard page wrapper (`max-width:1200px; margin:0 auto; padding:32px 40px 48px`)
  - `.detail-layout` / `.detail-main` / `.detail-sidebar` — 2-col detail page layout
  - `.split-layout` / `.split-aside` / `.split-main` — form + table side-by-side
  - `.info-grid` / `.info-label` / `.info-value` / `.info-value-primary` — order info fields
  - `.pm-option` / `.pm-options` — payment method radio selector (CSS `:has()`, no JS)
  - `.slot-bar` / `.slot-bar-fill.low|mid|full` — capacity progress bar
  - `.order-row-link` — block `<a>` wrapper for clickable order rows
  - `.rider-*` classes — rider dashboard component system (in `style.css`, not inline)
  - `.rw-*` classes — rider task detail component system (in `style.css`, not inline)
  - `.broadcast-modal` / `.broadcast-card` / `.broadcast-backdrop` — broadcast modal (in `style.css`)
  - `.ba-*` classes — Business Analytics page component system (in `{% block extra_head %}` of `analytics.html`)
- Buttons: `.tlp-btn` base + `.btn-yellow` / `.btn-teal` / `.btn-teal-outline` / `.btn-white-outline` / `.btn-warning` (amber outline, used for Suspend) / `.btn-danger` / `.btn-success`
- Shop status badges: `.badge-shop-pending` / `.badge-shop-approved` / `.badge-shop-rejected` / `.badge-shop-suspended`

### base.html structure
- Sticky white topbar with glassmorphism effect
- Logo: `<span class="brand-cle">CLE</span><span class="brand-fresh">FRESH</span>` two-tone text
- Topbar right: notification bell, avatar initials, Login/Logout, Register button
- **Global login + register modals** embedded in `base.html` — these also contain SSO buttons and the login form. Any changes to the login form must be applied to `base.html`, `home.html`, AND `accounts/login.html`
- **Broadcast modal** embedded in `base.html` (admin/superuser only) — triggered by the Broadcast nav link via `data-broadcast-modal`. POSTs to `admin_broadcast_view` which always redirects (no standalone broadcast page).
- Footer: 4-column grid — brand blurb, Explore links, Workspace links, Support & Legal

### Topbar nav — role priority order
The `{% block topbar_nav %}` in `base.html` resolves in this order (first match wins):

| Condition | Links shown |
|-----------|-------------|
| `user.role == 'customer'` | Home, My Orders — shown on **every** page including shop pages |
| `is_shop_owner_area or is_shop_owner_home` | Dashboard, Orders, Delivery Slots, Services, Analytics, Payments |
| `user.role == 'rider'` | *(empty)* |
| `user.role == 'admin'` | Dashboard, Shops, Users, Orders, Transactions, Analytics, Broadcast |
| `user.is_superuser` | same as admin |
| `is_shop_site and shop` | Shop name, Services, How It Works, About Us — anonymous visitors only |
| else (anonymous) | Home, Services, How It Works, About Us |

**Important:** `shops/site.html` overrides `{% block topbar_nav %}` — it shows customer nav for logged-in customers, shop links for everyone else. This is the only template that overrides this block.

### home.html (landing page)
Contains embedded **login modal** and **register modal** (separate from the global base.html modals — shown only on the home page). Both modals include SSO buttons and full forms.

Role-based rendering in `home_view`:
- `rider` → redirect to `rider_dashboard` (does not see landing page)
- `customer` → sees shop search/filter UI
- `shop_owner` → sees owner stats + recent orders
- anonymous → sees public landing page

## Important Patterns
- Role guard decorator: `@role_required('shop_owner')` from `accounts/decorators.py`
- Email sending: always use `utils/email_notifications.py` + log with `EmailLog` model. SMTP is preferred and required for password resets and verification codes; Formspree is not transactional email.
- Geocoding: `utils/geocode.py` — called automatically in `Shop.save()`
- Status display for `OrderStatusLog`: use `{% load shop_extras %}` then `{{ log.status|humanize_status }}` — preferred over `get_status_display()` in templates
- Rider doc URLs: use `{% url 'serve_rider_doc' filename %}` — never link to `/media/rider_docs/` directly
- Seed data: `python manage.py seed_data`
- PayMongo amounts: **always centavos** — never store raw PHP floats
- Currency display: always `₱` symbol — never `PHP`
- Order status transitions are enforced server-side; `shops/views.py` has a `valid_transitions` dict — don't bypass it
- Admin shop actions: approve / reject / **suspend** / **unsuspend** / **delete** (delete is permanent and cascades to orders)
- Analytics revenue: `Sum('total_amount')` on `Order` where `payment_status='paid'` and `created_at__gte=period_start` — stored in ₱ (not centavos)
- Analytics date range: `?days=7|30|90` GET param (default 30); computes current + previous period for trend deltas
- Broadcast: modal only — `admin_broadcast_view` always redirects after POST, never renders `broadcast.html`

## Known Limitations / To Do
- Forgot-password emails and shop-owner verification codes require SMTP (`EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` in `.env`). Formspree does not deliver transactional messages to arbitrary recipients.
- Rider docs auth view works in dev; in production the web server (nginx) must block `/media/rider_docs/` direct access.
- `SECURE_HSTS_SECONDS` defaults to 0 — enable in production via `.env`.

---

## My Preferences
- **Concise.** No preamble, no filler, no "Great question!"
- **Code changes:** show only the changed lines + minimal surrounding context unless I ask for the full file
- **Fixes:** just fix it — one sentence on the cause only if non-obvious
- **Django conventions only** — don't suggest switching patterns or frameworks
- **Currency:** PHP, stored as centavos in PayMongo models

---

## Shorthand
| I say | You do |
|-------|--------|
| `just code` | Code only, no explanation |
| `show diff only` | Changed lines + ~3 lines of context |
| `prod-ready` | Add error handling, edge cases |
| `quick draft` | First pass, don't over-engineer |
| `fix this` | Fix it, one-sentence cause |
| `review this` | Check for N+1 queries, missing auth decorators, centavo errors |
| `list options` | 2-3 approaches with tradeoffs |
| `pick best` | Single best approach, one-line reason |
| `think first` | Reason before writing code |
| `ELI5` | Plain language, no jargon |

---

## Task Recipes

### Bug Fix
- Show fixed lines + minimal context
- State the root cause in one sentence

### New Feature
- Follow existing view/url/template pattern in the relevant app
- Sequence: model changes → migration reminder → view → url → template
- Keep templates consistent with `base.html`

### New Django App
- Scaffold: `models.py views.py urls.py apps.py admin.py migrations/__init__.py`
- Register in `settings.INSTALLED_APPS`
- Wire into `clefresh/urls.py`

### PayMongo Work
- Amounts always in centavos (PHP × 100)
- Use `PayMongoTransaction` for tracking
- Webhook must verify with `PAYMONGO_WEBHOOK_SECRET`
- `WebhookEvent` handles idempotency — do not process the same `event_id` twice

### Auth & Permissions
- `@login_required` from `django.contrib.auth.decorators`
- `@role_required('role')` from `accounts/decorators.py`
- SSO completion view: `accounts/views.sso_complete_view`
- Forgot password: custom flow at `/auth/forgot-password/` using `default_token_generator` + SMTP (`utils/email_notifications.py`)

### Code Review
- Flag: N+1 queries, missing `@login_required` / `@role_required`, centavo unit errors, unvalidated input, `PHP` currency symbol (should be `₱`), `|title|cut:"_"` on status fields (use `|humanize_status`), direct `/media/rider_docs/` links (use `serve_rider_doc`), bypassing `valid_transitions` in order status updates
- Output: bullet list of issues, then offer to fix

### UI / Templates
- Use Bootstrap Icons (`bi bi-*`) for all icons — already loaded globally
- **Exception:** Google SSO button uses inline SVG, not `bi bi-google`
- Match teal/yellow color palette from design tokens above
- Use `.page-shell` wrapper on all new pages — no inline `style="max-width:..."` wrappers
- New page styles go in `{% block extra_head %}` scoped to a page-specific class prefix
- Home page uses `.tlp-*` prefix; don't reuse it on other pages
- Login form changes must be applied in 3 places: `base.html`, `home.html`, `accounts/login.html`
- Icon styling: use `.stat-icon` (yellow bg, yellow icon) for card metric icons; `.glyph-icon` on `<i>` inside section headings (`<h3>`) for inline yellow icons — do not use custom colored icon containers
- Analytics page (`admin_panel/analytics.html`) uses Chart.js 4.4.0 via CDN for line + donut charts; extra JS goes in `{% block extra_js %}`
