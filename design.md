# Design Document — Embeddable Widget & Lead-Capture Platform

## Problem

Let a customer (tenant) create an embeddable form widget (signup form, CTA, or popover), get a single `<script>` tag to paste into any website, and safely receive submissions from that widget on any origin — validated, rate-limited, spam-filtered, enriched with geo data, stored, and viewable in a dashboard.

## Data model

**`tenants`**
- `id` (PK)
- `email`, `password_hash` (via Supabase Auth, reusing the pattern from BE-03)
- `created_at`

**`widgets`**
- `id` (PK)
- `tenant_id` (FK → tenants, indexed — every widget query filters by this)
- `type` (enum: `signup_form` | `cta` | `popover`)
- `title`, `description`
- `fields` (JSON — list of `{name, label, type, required}`)
- `button_text`
- `display_options` (JSON — colors, position, etc.)
- `bundle_version` (integer, incremented on config change — drives cache-busting)
- `created_at`, `updated_at`

**`submissions`**
- `id` (PK)
- `widget_id` (FK → widgets, indexed)
- `tenant_id` (FK → tenants, indexed — denormalized for fast tenant-scoped dashboard queries without a join)
- `data` (JSON — the submitted field values)
- `ip_address`
- `country`, `city` (nullable — populated by geo enrichment, absent if all providers failed)
- `spam_flag` (boolean)
- `created_at` (indexed — dashboard queries filter/group by time)

## The embed flow

1. Tenant creates a widget via the authenticated Widget Management API → gets back `widget_id`
2. Tenant pastes `<script src="https://api.example.com/widget.js?id={widget_id}"></script>` into their site
3. `widget.js` (a static, versioned, publicly-cached bundle) runs on the visitor's page, fetches `GET /widgets/{id}/config` (public, short-cache), and renders the form using that config
4. Visitor submits → `POST /submissions` (public, CORS-enabled, validated, rate-limited, spam-checked, geo-enriched, stored, side-effect fired)
5. Tenant views results via the authenticated Dashboard API

## API contracts (all three request paths)

**Path 1 — Widget owner (authenticated, `Authorization: Bearer <token>`)**
```
POST   /widgets              → create, 201
GET    /widgets              → list tenant's own widgets, 200
GET    /widgets/{id}         → get one (404 if not found OR belongs to another tenant)
PUT    /widgets/{id}         → update, 200
DELETE /widgets/{id}         → delete, 204
GET    /dashboard/{widget_id}/stats  → counts over time, geo breakdown, 200
```

**Path 2 — Customer website (public, cached, CORS)**
```
GET /widgets/{id}/config     → widget's public config, 200, Cache-Control: max-age=300
GET /widget.js               → the versioned bundle, 200, Cache-Control: max-age=31536000, immutable
```

**Path 3 — Website visitor (public, CORS, hardened)**
```
POST /submissions            → 201 on success
                                400 — malformed/missing fields
                                413 — oversized payload
                                429 — rate limit exceeded
                              (honeypot-filled submissions return 201 but are silently dropped, per the brief)
```

## Layer sketch

```
Route handlers (FastAPI)
    ↓
Service layer (validation, rate-limit check, spam check, enrichment orchestration)
    ↓
Repository layer (all SQL, tenant-scoped queries — same pattern as repository.py in Build-CRUD-API)
    ↓
PostgreSQL (Docker, same setup as BE-04)
```

Enrichment and the email side-effect are separate modules called from the service layer, each with their own try/except boundary — a failure in either must never propagate up and fail the submission.

## Explicit non-goal

**No real-time dashboard, no WebSockets/SSE.** The dashboard is a plain authenticated REST API returning aggregated stats on request (polling, not push). This is explicitly listed as an optional stretch goal in the brief, and the core requirement is fully satisfied without it.
