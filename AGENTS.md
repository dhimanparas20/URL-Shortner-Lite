# AGENTS.md — LLM Agent Guide for URL-Shortner-Lite

This document provides everything an LLM agent needs to understand, navigate, and safely operate on this codebase.

---

## Project Overview

**MST URL Shortener** — a self-hosted Flask web app that shortens URLs, tracks clicks, generates QR codes, and provides an admin dashboard. Uses MongoDB (primary) or a local JSON file (dev fallback) for storage.

**Stack:** Python 3.14, Flask 3.1.0, Flask-RESTful 0.3.10, PyMongo 4.6.0, qrcode 8.2, Pillow 11.3.0, Bootstrap 5, Gunicorn 23.0.0, uv package manager.

---

## File Structure

```
app.py                  ← Single-file Flask backend (all routes, logic, API)
templates/
  index.html            ← Main URL shortening form (dark purple UI)
  not_found.html        ← 404 error page
  admin.html            ← Admin panel dashboard (table, search, filter, sort, pagination, bulk delete)
  admin_login.html      ← Admin login page
static/
  link.png              ← Favicon
  ss.png                ← Screenshot for README
pyproject.toml          ← Python project metadata + dependencies
uv.lock                 ← Locked dependency versions (do not edit manually)
Dockerfile              ← Production Docker image (Python 3.14-slim + uv + gunicorn)
docker-compose.yml      ← Docker Compose config
vercel.json             ← Vercel serverless deployment config
.env                    ← Runtime environment variables (gitignored, never commit)
env_sample              ← Template for .env
README.md               ← Human-facing documentation
AGENTS.md               ← This file (LLM agent guide)
```

---

## Architecture

### Backend (`app.py` — single file, ~600 lines)

All backend logic lives in `app.py`. Key sections in order:

1. **Imports & Config** (lines 1–45): Load `.env`, initialize Flask, read env vars (`USE_JSON_FILE`, `MONGO_URI`, `ADMIN_USERNAME`, `ADMIN_PASSWORD`).
2. **MongoDB Init** (lines 38–45): Conditionally connects to MongoDB only when `USE_JSON_FILE=false`. Raises `RuntimeError` if `MONGO_URI` is missing or unreachable.
3. **JSON File Helpers** (lines 44–61): `load_database()` / `save_database()` — atomic writes via temp file + `os.replace()`.
4. **Utility Functions** (lines 64–100): `generate_short_url()`, `trim_keyword()`, `is_valid_url()`, `is_valid_keyword()`, `calculate_expires_at()`.
5. **Resource Classes** (lines 103–408): Flask-RESTful resources:
   - `URLShortener` — POST `/shorten`
   - `URLRedirect` — GET `/<short_url>` (catch-all, registered LAST)
   - `ReverseLookup` — POST `/unshorten`
   - `ClickStats` — GET `/stats/<short_url>`
   - `QRCodeGen` — GET `/qr/<short_url>`
6. **Admin Routes** (lines 411–510): Session-based auth with `@admin_required` decorator:
   - GET/POST `/admin/login`
   - GET `/admin/logout`
   - GET `/admin` — serves `admin.html`
   - GET `/api/admin/urls` — returns all URLs as JSON
   - DELETE `/api/admin/urls/delete-expired` — bulk delete expired/limit-hit URLs
   - DELETE `/api/admin/urls/<short_url>` — delete single URL
7. **Error Handlers** (lines 510–536): 404, 405, 413, 500 handlers.
8. **Security Headers** (lines 530–536): `@app.after_request` adds `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`.
9. **Route Registration** (lines 539–550): `URLRedirect` catch-all is registered **last** so admin routes take precedence.

### Frontend (`templates/`)

- **`index.html`** — Public form. Vanilla JS fetches `/shorten` (POST JSON), displays result + QR code + click count. Includes unshorten form.
- **`admin.html`** — Single-page admin dashboard. All JS is inline in a single IIFE. Fetches `/api/admin/urls`, renders table client-side with search, filter (all/active/expired), sort, pagination (10/page). Delete via `/api/admin/urls/<short_url>` (DELETE). Bulk delete via `/api/admin/urls/delete-expired` (DELETE).
- **`admin_login.html`** — Simple POST form to `/admin/login`.
- **`not_found.html`** — 404 page with link back to home.

### Storage Modes

| Mode | `USE_JSON_FILE` | Data Location | Notes |
|------|-----------------|---------------|-------|
| MongoDB | `false` | MongoDB Atlas or local | Production default. Requires `MONGO_URI`. |
| JSON File | `true` | `./url_database.json` | Dev/testing. Atomic writes via temp file. |

---

## API Endpoints Reference

| Method | Endpoint | Auth | Body/Params | Response |
|--------|----------|------|-------------|----------|
| POST | `/shorten` | None | `{ url, custom_keyword?, expiry_value?, expiry_unit?, max_views? }` | `{ short_url }` 201 |
| GET | `/<short_url>` | None | — | 302 redirect to long URL |
| POST | `/unshorten` | None | `{ keyword }` | `{ long_url, clicks }` 200 |
| GET | `/stats/<short_url>` | None | — | `{ clicks }` 200 |
| GET | `/qr/<short_url>` | None | — | PNG image |
| GET | `/admin` | Session | — | HTML admin panel |
| POST | `/admin/login` | None | Form: `username`, `password` | Redirect to `/admin` |
| GET | `/admin/logout` | Session | — | Redirect to `/admin/login` |
| GET | `/api/admin/urls` | Session | — | JSON array of all URLs |
| DELETE | `/api/admin/urls/delete-expired` | Session | — | `{ deleted: N }` |
| DELETE | `/api/admin/urls/<short_url>` | Session | — | `{ success: true }` |

---

## Environment Variables

| Variable | Required | Default | Notes |
|----------|----------|---------|-------|
| `MONGO_URI` | When `USE_JSON_FILE=false` | — | MongoDB connection string |
| `USE_JSON_FILE` | No | `false` | Toggle JSON file storage |
| `ADMIN_USERNAME` | No | `admin` | Admin login username |
| `ADMIN_PASSWORD` | No | `changeme` | Admin login password |
| `SECRET_KEY` | No | `secrets.token_hex(32)` | Flask session key. **Set in production.** |
| `SESSION_COOKIE_SECURE` | No | `false` | Set `true` behind HTTPS |
| `FLASK_DEBUG` | No | `false` | Debug mode |

---

## Critical Rules for LLM Agents

### DO NOT
- **Never commit `.env`** — it contains live MongoDB credentials and admin passwords.
- **Never modify the `/<short_url>` catch-all route registration order** — it MUST be registered last via `api.add_resource(URLRedirect, '/<string:short_url>')` so Flask admin routes take precedence.
- **Never remove the `is_valid_url()` check** in `URLShortener.post` — this prevents `javascript:`, `data:`, and other dangerous URL schemes.
- **Never remove the `is_valid_keyword()` check** — custom keywords must be validated for length (3–30 chars) and character set (`[a-zA-Z0-9_-]`).
- **Never hardcode credentials** — always read from environment variables.
- **Never run Flask dev server in production** — use Gunicorn via Docker or directly.
- **Never change `debug=True` in the `__main__` block** — it reads from `FLASK_DEBUG` env var.

### ALWAYS
- **Validate URLs** before shortening: must be `http://` or `https://` scheme, must have a valid domain (contains `.`).
- **Sanitize output** — the frontend JS uses `esc()` to prevent XSS when rendering user-provided URLs in the table.
- **Use atomic file writes** when modifying `save_database()` — write to `.tmp` then `os.replace()`.
- **Keep routes before the catch-all** — any new Flask route must be defined before `api.add_resource(URLRedirect, '/<string:short_url>')`.
- **Add `@admin_required`** to any new admin API endpoint.
- **Register new Flask-RESTful resources** via `api.add_resource()` before the catch-all.
- **Test with both storage modes** — `USE_JSON_FILE=true` and `USE_JSON_FILE=false`.

### When Adding Features
- Backend: Add routes in `app.py` between the error handlers and `api.add_resource(URLShortener, ...)`.
- Frontend: Edit the relevant template in `templates/`. All JS is inline (no build step).
- New env vars: Add to both `.env` and `env_sample`.
- Dependencies: Add to `pyproject.toml` and run `uv lock`.

### Security Checklist
- [ ] URL validation (`is_valid_url`) blocks non-http(s) schemes
- [ ] Custom keyword validation (`is_valid_keyword`) prevents injection
- [ ] Session cookies: `HttpOnly`, `SameSite=Lax`, optional `Secure`
- [ ] Security headers: `X-Content-Type-Options`, `X-Frame-Options`, `X-XSS-Protection`, `Referrer-Policy`
- [ ] Error handlers for 404, 405, 413, 500
- [ ] `MAX_CONTENT_LENGTH` set to 1MB
- [ ] Input length limits (`MAX_URL_LENGTH=2048`, `MAX_CUSTOM_KEYWORD_LENGTH=30`)
- [ ] Admin auth uses session-based auth with `session.regenerate`
- [ ] MongoDB client init is conditional and fails fast if unreachable
- [ ] No `debug=True` in production path

---

## Testing

### Manual Test Flow
1. Start the app: `USE_JSON_FILE=true python app.py`
2. Open `http://localhost:5000` — shorten a URL, verify redirect works
3. Open `http://localhost:5000/admin` — log in, verify table loads
4. Test search, filter, sort, pagination
5. Create an expired URL (expiry_value=1, expiry_unit=seconds), wait, verify it disappears
6. Click "Delete All Expired", verify count decreases
7. Test with MongoDB: set `USE_JSON_FILE=false` and provide a valid `MONGO_URI`

### Key Edge Cases
- Duplicate long URL should return existing short URL (200, not 201)
- Custom keyword collision should return 409
- Invalid URL schemes (`javascript:`, `ftp://`) should return 400
- Expired URLs should return 404 on redirect
- Max-view URLs should auto-delete after reaching limit
- Empty/missing `MONGO_URI` with `USE_JSON_FILE=false` should raise `RuntimeError` at startup

---

## Common Tasks

| Task | Where to Edit |
|------|---------------|
| Change short URL length | `SHORT_URL_LENGTH` constant in `app.py` |
| Add new URL validation rule | `is_valid_url()` in `app.py` |
| Modify admin table columns | `admin.html` — `<thead>` + `renderTable()` JS function |
| Change pagination page size | `perPage` constant in `admin.html` JS |
| Add new API endpoint | Add `Resource` class + `api.add_resource()` in `app.py` (before catch-all) |
| Modify expiry units | `multipliers` dict in `calculate_expires_at()` |
| Change admin credentials | `.env` → `ADMIN_USERNAME` / `ADMIN_PASSWORD` |
| Add new env variable | `.env` + `env_sample` + `os.getenv()` in `app.py` |
