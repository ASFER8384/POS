"""Temp POS — a standalone VISUAL fake POS partner (Cratis-style ops console).

Deploy as its own Render web service. It behaves like a real POS partner and now
mirrors the platform's OPS dashboard layout — a left sidebar with tabs:

  * Orders    — live kitchen tickets from order.* webhooks; Accept/Preparing/
                Ready/Cancel buttons call the platform API (X-API-Key).
  * Chat      — read every WhatsApp thread for the store, reply as the POS agent
                (auto-takeover), or hand the thread back to the bot.
  * Customers — the store's customer book (incremental sync API).
  * Riders    — who's carrying what right now (derived from live order tickets).
  * Settings  — store identity + integration flags + this POS's own config.

The API key lives ONLY on this server; the browser talks to local /api/* proxy
routes so the key is never exposed. Open GET / in a browser: that's the POS.

The API key is NOT configured — it is earned at onboarding: the platform sends a
``store.connected`` webhook carrying a single-use claim token, which this POS
trades for its own key (see ``_onboard``). The key then lives only on this server.

Env vars:
  POS_BASE_URL        platform API base (default the live Render deployment)
  POS_WEBHOOK_SECRET  shared HMAC secret configured in the store's partner config
  DATABASE_URL        our OWN Postgres — where onboarded stores + their keys are kept
                      so a restart/redeploy doesn't lose them. Absent = memory only.
  PORT                injected by Render
"""
from __future__ import annotations

import hashlib
import hmac
import os
import ssl

import asyncpg
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

BASE_URL = os.environ.get(
    "POS_BASE_URL", "https://restaurant-whatsapp-service.onrender.com"
).rstrip("/")
SECRET = os.environ.get("POS_WEBHOOK_SECRET", "")
# Our OWN database — where this POS keeps the stores it has onboarded and their keys,
# so a restart/redeploy never loses them. Absent → memory-only (local dev).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

app = FastAPI(title="Temp POS", version="4.0")

# The stores' credentials, learned ONLY at onboarding (store.connected webhook).
# There is deliberately NO env fallback: a key must be earned through the onboarding
# handshake, never baked into config.
#
# MULTI-STORE: one POS partner serves MANY restaurants, each with its OWN key, so
# this is keyed by restaurant_id — never a single slot (which would make each new
# store.connected evict the previous store).
#   restaurant_id -> {api_key, source, name, phone, partner, pos_store_id}
# This is a read-through CACHE of the `stores` table; the DB is the source of truth.
_stores: dict[int, dict] = {}

# The store the UI is currently looking at (restaurant_id). Defaults to the most
# recent onboard; the user switches it from the header dropdown.
_active_id: int | None = None

# order_id -> ticket dict (each carries restaurant_id so tickets stay per-store).
_orders: dict[int, dict] = {}


# ── Our own database: where onboarded stores + their keys live ───────────────
# A real POS keeps its credentials in its OWN store, so a restart/redeploy never
# loses them. (The backend is our choice alone — the platform contract says nothing
# about storage; any partner may use MySQL/Mongo/a vault instead.)
_pool: asyncpg.Pool | None = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    restaurant_id BIGINT PRIMARY KEY,
    api_key       TEXT NOT NULL,
    key_source    TEXT,
    name          TEXT,
    phone         TEXT,
    partner       TEXT,
    pos_store_id  TEXT,
    onboarded_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def _db_connect() -> None:
    """Open the pool + ensure the schema. No DATABASE_URL → memory-only (local dev).

    Tries SSL first (Render's Postgres requires it), then falls back to plaintext (a
    local/docker Postgres refuses the SSL upgrade), so the same code runs in both.
    """
    global _pool
    if not DATABASE_URL:
        print("no DATABASE_URL — keys will live in memory only (lost on restart)")
        return
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    last: Exception | None = None
    for mode, ssl_arg in (("ssl", ctx), ("plaintext", None)):
        try:
            _pool = await asyncpg.create_pool(
                DATABASE_URL, ssl=ssl_arg, min_size=1, max_size=5
            )
            print(f"db connected ({mode})")
            break
        except Exception as exc:  # noqa: BLE001 — try the other transport
            last = exc
    if _pool is None:
        raise last or RuntimeError("could not connect to the database")
    async with _pool.acquire() as c:
        await c.execute(_SCHEMA)


async def _db_save_store(rec: dict) -> None:
    """Persist one onboarded store (upsert — re-onboarding replaces its key)."""
    if _pool is None:
        return
    async with _pool.acquire() as c:
        await c.execute(
            """INSERT INTO stores (restaurant_id, api_key, key_source, name, phone,
                                   partner, pos_store_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7)
               ON CONFLICT (restaurant_id) DO UPDATE SET
                   api_key=EXCLUDED.api_key, key_source=EXCLUDED.key_source,
                   name=EXCLUDED.name, phone=EXCLUDED.phone,
                   partner=EXCLUDED.partner, pos_store_id=EXCLUDED.pos_store_id,
                   onboarded_at=now()""",
            int(rec["restaurant_id"]), rec.get("api_key") or "", rec.get("source"),
            rec.get("name"), rec.get("phone"), rec.get("partner"), rec.get("pos_store_id"),
        )


async def _db_load_stores() -> None:
    """Repopulate the in-memory cache from the DB at startup — this is what makes a
    restart survivable: we come back already onboarded."""
    global _active_id
    if _pool is None:
        return
    async with _pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM stores ORDER BY onboarded_at")
    for r in rows:
        _stores[r["restaurant_id"]] = {
            "restaurant_id": r["restaurant_id"],
            "api_key": r["api_key"],
            "source": r["key_source"],
            "name": r["name"],
            "phone": r["phone"],
            "partner": r["partner"],
            "pos_store_id": r["pos_store_id"],
        }
    if _stores and _active_id is None:
        _active_id = list(_stores)[-1]  # most recently onboarded
    print(f"loaded {len(_stores)} store(s) from db; active=r{_active_id}")


@app.on_event("startup")
async def _startup() -> None:
    try:
        await _db_connect()
        await _db_load_stores()
    except Exception as exc:  # noqa: BLE001 — never block boot on the db
        print(f"db init failed ({exc}) — continuing in memory-only mode")


def _active_store() -> dict:
    """The store the UI is acting as, or {} when none is onboarded."""
    if _active_id is None:
        return {}
    return _stores.get(_active_id) or {}


def _api_key() -> str:
    """The key of the ACTIVE store — every platform call is made as that store."""
    return _active_store().get("api_key") or ""


def _verify(raw: bytes, header: str | None) -> bool:
    if not SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def _platform(method: str, path: str, **kw) -> httpx.Response:
    """Call the platform API as the POS (X-API-Key)."""
    headers = {"X-API-Key": _api_key()}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as c:
        return await c.request(method, path, headers=headers, **kw)


def _passthrough(r: httpx.Response) -> JSONResponse:
    ct = r.headers.get("content-type", "")
    body = r.json() if ct.startswith("application/json") else {"raw": r.text}
    return JSONResponse(body, status_code=r.status_code)


async def _onboard(data: dict) -> None:
    """Handle a ``store.connected`` event — learn the store + get our API key.

    Two delivery styles, both supported:

      * CLAIM (preferred/enterprise): the webhook carries a short-lived, single-use
        ``claim_token`` and NOT the key. We trade it once at POST /api/v1/partner/claim
        for the real key, so the long-lived secret never sits in this server's request
        logs.
      * PUSH (legacy): the webhook carries ``api_key`` directly — just store it.

    Each store is filed under its own ``restaurant_id``, so onboarding a second store
    ADDS it rather than replacing the first. The newest onboard becomes the active one.
    """
    async def _remember(rid: int, api_key: str, source: str, src: dict) -> None:
        # `global` must be declared HERE: this is its own scope, so without it the
        # assignment below would just make a local and the active store never changes.
        global _active_id
        rec = _stores.setdefault(rid, {})
        rec.update({"api_key": api_key, "source": source, "restaurant_id": rid})
        for key in ("name", "phone", "partner", "pos_store_id"):
            if src.get(key) is not None:
                rec[key] = src.get(key)
        _active_id = rid  # newest onboard becomes what the UI shows
        # Persist it — this is what makes the onboarding survive a restart. Best-effort:
        # a db hiccup must not lose the store we just claimed (it stays in memory).
        try:
            await _db_save_store(rec)
            where = "db" if _pool is not None else "memory (no db)"
        except Exception as exc:  # noqa: BLE001
            where = f"memory only — db save FAILED: {exc}"
        print(f"onboarded via {source.upper()}: r{rid} {rec.get('name')} "
              f"({len(_stores)} store(s) held) -> saved to {where}")

    rid = data.get("restaurant_id")

    claim_token = data.get("claim_token")
    if claim_token:
        try:
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as c:
                r = await c.post(
                    "/api/v1/partner/claim", json={"claim_token": claim_token}
                )
            if r.status_code == 200:
                body = r.json()
                # The claim reply is authoritative for identity (and is where the key
                # appears — the webhook never carried it).
                await _remember(
                    int(body.get("restaurant_id") or rid),
                    body.get("api_key", ""),
                    "claim",
                    {**data, **body},
                )
            else:
                print(f"claim FAILED {r.status_code}: {r.text[:200]}")
        except Exception as exc:  # noqa: BLE001
            print(f"claim ERROR: {exc}")
        return

    if data.get("api_key") and rid is not None:
        await _remember(int(rid), data["api_key"], "push", data)


# ── Inbound platform webhooks (us <- platform) ───────────────────────────────
@app.post("/hooks/whatsapp")
async def receive(request: Request) -> Response:
    raw = await request.body()
    ok = _verify(raw, request.headers.get("X-Partner-Signature"))
    event = request.headers.get("X-Partner-Event", "?")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    data = body.get("data", {}) if isinstance(body, dict) else {}
    # Envelope also carries the event name; header is a convenience.
    if event == "?" and isinstance(body, dict):
        event = body.get("event", "?")
    print(f"received {event} hmac_ok={ok}")
    if not ok:
        return Response(status_code=401, content="bad signature")

    # ── ONBOARDING: the platform is handing us this store ────────────────────
    if event == "store.connected":
        await _onboard(data)
        return Response(status_code=200, content="ok")

    oid = data.get("order_id")
    if oid is not None:
        oid = int(oid)
        t = _orders.setdefault(oid, {"order_id": oid, "events": []})
        # Stamp WHICH store this ticket belongs to, so a multi-store POS never shows
        # one restaurant's orders under another. restaurant_id is the stable key;
        # pos_store_id is the partner's own optional mirror and is often "".
        if data.get("restaurant_id") is not None:
            t["restaurant_id"] = int(data["restaurant_id"])
        t["events"].append(event)
        if event == "order.created":
            t.update(
                {
                    "order_number": data.get("order_number"),
                    "status": data.get("status", "confirmed"),
                    "customer": data.get("customer") or {},
                    "items": data.get("items") or [],
                    "total": data.get("total"),
                    "cod_due": data.get("cod_due"),
                    "address": data.get("address") or {},
                    "rider": None,
                    "delivery_status": None,
                    "cod_collected": None,
                    "late": False,
                }
            )
        elif event == "order.rider_assigned":
            t["status"] = "assigned"
            t["rider"] = data.get("rider")
            t["delivery_status"] = "assigned"
        elif event == "order.picked_up":
            t["status"] = "picked_up"
            t["delivery_status"] = "picked_up"
            t["rider"] = data.get("rider") or t.get("rider")
        elif event == "order.delivered":
            t["status"] = "delivered"
            t["delivery_status"] = "delivered"
            t["cod_collected"] = data.get("cod_collected")
            t["rider"] = data.get("rider") or t.get("rider")
        elif event == "order.late":
            t["late"] = True
        elif event == "order.cancelled":
            t["status"] = "cancelled"
            t["delivery_status"] = None
        elif event == "order.confirmed":
            # Defensive: some deployments emit a distinct confirm event.
            t["status"] = data.get("status", "confirmed")
        else:
            # Any other event that carries a status field — keep the ticket in
            # sync so we never show a stale status the platform has moved past.
            st = data.get("status")
            if st:
                t["status"] = st
    return Response(status_code=200, content="ok")


# ── Order actions (us -> platform) ───────────────────────────────────────────
@app.post("/pos/{order_id}/action")
async def pos_action(order_id: int, request: Request) -> JSONResponse:
    if not _api_key():
        return JSONResponse(
            {"error": "no API key — store not onboarded yet (awaiting store.connected)"},
            status_code=400,
        )
    body = await request.json()
    action = body.get("action")
    if action == "ack":
        r = await _platform(
            "POST", f"/api/v1/partner/orders/{order_id}/ack",
            json={"pos_order_id": f"TEMP-POS-{order_id}"},
        )
    elif action in ("preparing", "ready", "cancelled"):
        r = await _platform(
            "POST", f"/api/v1/partner/orders/{order_id}/status",
            json={"status": action},
        )
    else:
        return JSONResponse({"error": f"unknown action {action}"}, status_code=400)

    result = {
        "http": r.status_code,
        "body": (r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text),
    }
    if r.status_code == 200 and order_id in _orders and isinstance(result["body"], dict):
        _orders[order_id]["status"] = result["body"].get("status", _orders[order_id].get("status"))
    return JSONResponse(result)


@app.get("/state")
async def state() -> JSONResponse:
    """Webhook tickets for the ACTIVE store only — never another store's orders.
    Legacy tickets with no restaurant_id (pre-multi-store) are shown as-is."""
    mine = [
        t for t in _orders.values()
        if t.get("restaurant_id") in (None, _active_id)
    ]
    return JSONResponse({"orders": sorted(mine, key=lambda t: t["order_id"], reverse=True)})


# ── Multi-store: which stores this POS holds, and which one we're acting as ───
@app.get("/api/stores")
async def api_stores() -> JSONResponse:
    """Every store onboarded to this POS. Never returns any api_key — only a
    non-secret prefix so an operator can tell which credential is in play."""
    return JSONResponse({
        "active_restaurant_id": _active_id,
        "stores": [
            {
                "restaurant_id": rid,
                "name": rec.get("name"),
                "partner": rec.get("partner"),
                "pos_store_id": rec.get("pos_store_id"),
                "key_source": rec.get("source"),
                "key_prefix": (rec.get("api_key") or "")[:14] or None,
                "active": rid == _active_id,
            }
            for rid, rec in sorted(_stores.items())
        ],
    })


@app.post("/api/stores/{restaurant_id}/select")
async def api_select_store(restaurant_id: int) -> JSONResponse:
    """Switch which store the POS is acting as (its key is used for every call)."""
    global _active_id
    if restaurant_id not in _stores:
        return JSONResponse({"error": "store not onboarded here"}, status_code=404)
    _active_id = restaurant_id
    return JSONResponse({"ok": True, "active_restaurant_id": _active_id})


# ── Proxy routes so the browser never sees the API key ───────────────────────
@app.get("/api/store")
async def api_store() -> JSONResponse:
    return _passthrough(await _platform("GET", "/api/v1/partner/store"))


@app.get("/api/orders")
async def api_orders() -> JSONResponse:
    """Full order list from the platform (all statuses incl. history), so the board
    is populated even after a redeploy wiped the in-memory webhook tickets."""
    return _passthrough(
        await _platform(
            "GET", "/api/v1/partner/orders?status=all&unacked_only=false&limit=200"
        )
    )


@app.get("/api/riders")
async def api_riders() -> JSONResponse:
    """Full rider roster (every rider, not just those on a live delivery)."""
    return _passthrough(await _platform("GET", "/api/v1/partner/riders"))


@app.get("/api/customers")
async def api_customers() -> JSONResponse:
    return _passthrough(await _platform("GET", "/api/v1/partner/customers?limit=200"))


@app.get("/api/conversations")
async def api_conversations() -> JSONResponse:
    return _passthrough(await _platform("GET", "/api/v1/partner/conversations"))


@app.get("/api/conversations/{cid}/messages")
async def api_conversation_messages(cid: int) -> JSONResponse:
    return _passthrough(await _platform("GET", f"/api/v1/partner/conversations/{cid}/messages"))


@app.post("/api/conversations/{cid}/messages")
async def api_send_message(cid: int, request: Request) -> JSONResponse:
    body = await request.json()
    return _passthrough(
        await _platform("POST", f"/api/v1/partner/conversations/{cid}/messages", json=body)
    )


@app.post("/api/conversations/{cid}/takeover")
async def api_takeover(cid: int, request: Request) -> JSONResponse:
    body = await request.json()
    return _passthrough(
        await _platform("POST", f"/api/v1/partner/conversations/{cid}/takeover", json=body)
    )


@app.get("/api/menu")
async def api_menu() -> JSONResponse:
    """Read back the store's active menu from the platform so the POS can display it
    (pull path). Archived dishes excluded by the platform; sold-out rows kept so the
    POS can show them greyed."""
    return _passthrough(await _platform("GET", "/api/v1/partner/menu/items"))


@app.post("/api/menu/upload-image")
async def api_menu_upload_image(request: Request) -> JSONResponse:
    """Upload a REAL photo of a dish (parity with the ops upload button).

    Generate-image only ever produces an AI impression of a dish; a restaurant that has
    photographed its own food should be able to use that photo. Streams the multipart
    straight through, so the API key stays server-side.
    """
    form = await request.form()
    up = form.get("file")
    if not hasattr(up, "read"):
        return JSONResponse({"detail": "No file"}, status_code=422)
    data = await up.read()
    result = await _platform(
        "POST",
        "/api/v1/partner/menu/upload-image",
        files={"file": (up.filename or "dish.jpg", data,
                        up.content_type or "image/jpeg")},
        timeout=60.0,
    )
    return _passthrough(result)


@app.get("/api/menu/next-number")
async def api_menu_next_number() -> JSONResponse:
    """The next free dish number, so Add-dish can pre-fill it.

    Asked of the platform rather than worked out from /api/menu: that listing hides
    archived dishes and shows only the active menu, so max+1 over it can hand back a
    number an old dish still owns — and a dish number is how a customer orders.
    """
    return _passthrough(await _platform("GET", "/api/v1/partner/menu/next-number"))


@app.put("/api/menu/add")
async def api_menu_add(request: Request) -> JSONResponse:
    """Add a single dish (bulk-upsert of one item by pos_id)."""
    item = await request.json()
    return _passthrough(
        await _platform("PUT", "/api/v1/partner/menu/items", json={"items": [item]})
    )


@app.delete("/api/menu/{dish_id}")
async def api_menu_delete(dish_id: int) -> JSONResponse:
    """Delete a dish by platform id. The platform hard-deletes it (or archives it if it
    has order history) and removes it from Meta — the POS never touches the token."""
    return _passthrough(
        await _platform(
            "DELETE", f"/api/v1/partner/menu/items/by-id/{dish_id}", timeout=60.0
        )
    )


@app.patch("/api/menu/{dish_id}/whatsapp")
async def api_menu_whatsapp(dish_id: int, request: Request) -> JSONResponse:
    """Flip a dish's WhatsApp switch by its platform id. Sends ONLY the boolean — the
    platform never returns the Meta token/secret, so nothing sensitive is exposed."""
    body = await request.json()
    return _passthrough(
        await _platform(
            "PATCH", f"/api/v1/partner/menu/items/by-id/{dish_id}",
            json={"whatsapp_enabled": bool(body.get("whatsapp_enabled"))},
            timeout=60.0,
        )
    )


@app.post("/api/menu/describe")
async def api_menu_describe(request: Request) -> JSONResponse:
    """AI 'Suggest' a description. The AI key stays on the platform."""
    body = await request.json()
    return _passthrough(
        await _platform("POST", "/api/v1/partner/menu/describe", json=body, timeout=60.0)
    )


@app.post("/api/menu/generate-image")
async def api_menu_generate_image(request: Request) -> JSONResponse:
    """AI (GPT) dish photo. The OpenAI key stays on the platform; returns a /media URL.
    Adds an absolute ``preview_url`` (platform base + path) so the POS can show it — the
    relative ``url`` is what gets saved on the dish (the platform serves it to Meta)."""
    body = await request.json()
    r = await _platform(
        "POST", "/api/v1/partner/menu/generate-image", json=body, timeout=90.0
    )
    ct = r.headers.get("content-type", "")
    data = r.json() if ct.startswith("application/json") else {"raw": r.text}
    if r.status_code == 200 and isinstance(data, dict) and data.get("url"):
        u = data["url"]
        data["preview_url"] = u if u.startswith("http") else BASE_URL.rstrip("/") + u
    return JSONResponse(data, status_code=r.status_code)


@app.post("/api/menu/upload")
async def api_menu_upload(request: Request) -> JSONResponse:
    """AI menu digitization: forward the uploaded photo/PDF file(s) to the platform's
    extractor. Streams multipart straight through (the API key stays server-side)."""
    form = await request.form()
    files = []
    for key, val in form.multi_items():
        if hasattr(val, "read"):  # an UploadFile
            data = await val.read()
            files.append(("files", (val.filename, data, val.content_type or "application/octet-stream")))
    if not files:
        return JSONResponse({"http": 422, "body": {"detail": "No files"}}, status_code=422)
    result = await _platform(
        "POST", "/api/v1/partner/menu/upload", files=files, timeout=120.0
    )
    return _passthrough(result)


@app.get("/api/settings")
async def api_get_settings() -> JSONResponse:
    """Full operational settings for the store (WhatsApp token/secrets stripped by the
    platform)."""
    return _passthrough(await _platform("GET", "/api/v1/partner/settings"))


@app.patch("/api/settings")
async def api_patch_settings(request: Request) -> JSONResponse:
    """Update the store's operational settings. The platform drops any secret /
    Meta-connection key, so the POS can never touch the WhatsApp token or catalog."""
    body = await request.json()
    return _passthrough(await _platform("PATCH", "/api/v1/partner/settings", json=body))


@app.get("/health")
async def health() -> dict:
    active = _active_store()
    return {
        "ok": True,
        "base_url": BASE_URL,
        "api_key_set": bool(_api_key()),
        "secret_set": bool(SECRET),
        # Where onboarded stores live. "db" = persisted (survives restart);
        # "memory" = no DATABASE_URL, so a restart loses every key.
        "storage": "db" if _pool is not None else "memory",
        # How we got the ACTIVE store's key: "claim" (enterprise pull — the webhook
        # carried a token we traded for the key), "push" (legacy — key came in the
        # webhook body), or null (not onboarded yet).
        "key_source": active.get("source"),
        # Non-secret fragment of the active key, so an operator can tell WHICH
        # credential is in play and match it to key_prefix in the platform DB. The
        # key itself is never returned.
        "key_prefix": (active.get("api_key") or "")[:14] or None,
        # The store we are acting as right now.
        "store": {
            "restaurant_id": active.get("restaurant_id"),
            "name": active.get("name"),
            "partner": active.get("partner"),
            "pos_store_id": active.get("pos_store_id"),
        },
        # MULTI-STORE: every store onboarded here. One POS serves many restaurants,
        # each with its own key — see GET /api/stores to switch.
        "stores_held": len(_stores),
        "stores": [
            {
                "restaurant_id": rid,
                "name": rec.get("name"),
                "key_source": rec.get("source"),
                "key_prefix": (rec.get("api_key") or "")[:14] or None,
                "active": rid == _active_id,
            }
            for rid, rec in sorted(_stores.items())
        ],
    }


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(_PAGE)


_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>Temp POS — Ops Console</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{
  --bg:#0f172a;--surface:#1e293b;--inset:#0b1220;--border:#334155;
  --text:#e2e8f0;--muted:#94a3b8;--accent:#38bdf8;--accent-dim:#0c2733;
 }
 *{box-sizing:border-box}
 body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;height:100vh;overflow:hidden}
 /* Sidebar — mirrors the OPS dashboard NavSidebar */
 nav{width:220px;flex-shrink:0;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column}
 .logo{font-family:ui-monospace,Menlo,monospace;font-weight:700;font-size:13px;letter-spacing:.08em;height:56px;display:flex;align-items:center;padding:0 16px;border-bottom:1px solid var(--border)}
 .navitem{display:flex;align-items:center;gap:10px;font-size:13px;font-weight:500;color:var(--muted);min-height:40px;padding:8px 16px;cursor:pointer;border:0;background:none;width:100%;text-align:left;font-family:inherit}
 .navitem:hover{background:var(--inset);color:var(--text)}
 .navitem.active{background:var(--accent-dim);color:var(--accent);font-weight:600}
 .navitem .ic{font-size:16px}
 .navitem .count{margin-left:auto;font-family:ui-monospace,monospace;font-size:10px;background:#dc2626;color:#fff;border-radius:8px;padding:1px 6px}
 .navfoot{margin-top:auto;border-top:1px solid var(--border);padding:10px 16px;font-size:11px;color:var(--muted)}
 .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
 .dot.ok{background:#16a34a}.dot.bad{background:#dc2626}
 /* Main */
 main{flex:1;overflow-y:auto;display:flex;flex-direction:column}
 header{height:56px;flex-shrink:0;display:flex;align-items:center;justify-content:space-between;padding:0 22px;border-bottom:1px solid var(--border);background:var(--surface)}
 header h1{font-size:16px;margin:0}
 header .meta{font-size:12px;color:var(--muted)}
 .body{padding:20px}
 /* Orders grid */
 #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:14px}
 .card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px}
 .card.late{border-color:#ef4444}
 .row{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
 .num{font-weight:700;font-size:16px}
 .badge{font-size:11px;padding:3px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.03em}
 .b-confirmed{background:#334155;color:#cbd5e1}.b-preparing{background:#a16207;color:#fef9c3}
 .b-ready{background:#166534;color:#dcfce7}.b-assigned,.b-picked_up{background:#1d4ed8;color:#dbeafe}
 .b-delivered{background:#065f46;color:#d1fae5}.b-cancelled{background:#7f1d1d;color:#fecaca}
 .cust{font-size:13px;color:#cbd5e1;margin-bottom:6px}
 ul.items{list-style:none;margin:6px 0;padding:0;font-size:13px}
 ul.items li{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px dashed var(--border)}
 .tot{font-weight:700;margin-top:6px;font-size:14px}
 .rider{margin-top:8px;font-size:12px;color:#93c5fd}
 .btns{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
 button.act{border:0;border-radius:6px;padding:7px 10px;font-size:12px;font-weight:600;cursor:pointer;color:#fff}
 .bd{background:#2563eb}.bg{background:#16a34a}.by{background:#ca8a04}.br{background:#dc2626}
 button:disabled{opacity:.35;cursor:not-allowed}
 .empty{color:#64748b;padding:40px;text-align:center}
 /* Tables */
 table{width:100%;border-collapse:collapse;font-size:13px}
 th,td{text-align:left;padding:9px 12px;border-bottom:1px solid var(--border)}
 th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
 /* Chat */
 .chatwrap{display:flex;gap:0;height:calc(100vh - 56px);border:0}
 .convlist{width:300px;flex-shrink:0;border-right:1px solid var(--border);overflow-y:auto}
 .conv{padding:11px 14px;border-bottom:1px solid var(--border);cursor:pointer}
 .conv:hover{background:var(--inset)}
 .conv.active{background:var(--accent-dim)}
 .conv .top{display:flex;justify-content:space-between;font-size:13px;font-weight:600}
 .conv .prev{font-size:12px;color:var(--muted);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 .conv .unread{width:8px;height:8px;border-radius:50%;background:var(--accent);display:inline-block}
 .thread{flex:1;display:flex;flex-direction:column}
 .threadhead{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center}
 .msgs{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
 .bub{max-width:70%;padding:8px 12px;border-radius:12px;font-size:13px;line-height:1.35;word-wrap:break-word}
 .bub.inbound{align-self:flex-start;background:var(--surface);border:1px solid var(--border)}
 .bub.outbound{align-self:flex-end;background:#075985;color:#e0f2fe}
 .composer{display:flex;gap:8px;padding:12px 16px;border-top:1px solid var(--border)}
 .composer input{flex:1;background:var(--inset);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:9px 12px;font-size:13px}
 .composer button{border:0;border-radius:8px;background:var(--accent);color:#04222e;font-weight:700;padding:0 16px;cursor:pointer}
 .pill{font-size:11px;padding:3px 9px;border-radius:999px;cursor:pointer;border:1px solid var(--border);background:none;color:var(--muted)}
 .pill.on{background:#a16207;color:#fef9c3;border-color:#a16207}
 .kv{display:grid;grid-template-columns:200px 1fr;gap:10px 16px;font-size:13px;max-width:640px}
 .kv .k{color:var(--muted)}
 .setgrid{display:grid;grid-template-columns:210px 1fr;gap:10px 16px;align-items:center;font-size:13px;margin-bottom:6px}
 .setgrid label.k{color:var(--muted)}
 .setgrid input,.setgrid textarea{background:var(--inset);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:7px 10px;font-size:13px;width:100%;font-family:inherit}
 .setgrid textarea{font-family:ui-monospace,Menlo,monospace;resize:vertical}
 .savebtn{border:0;border-radius:8px;background:var(--accent);color:#04222e;font-weight:700;padding:9px 18px;cursor:pointer;font-family:inherit;font-size:13px}
 /* ---- Settings: mirrors the ops dashboard (vertical nav + card panel + per-tab save) ---- */
 .slayout{display:grid;grid-template-columns:220px 1fr;gap:28px;align-items:start}
 .snav{display:flex;flex-direction:column;gap:2px;position:sticky;top:0}
 .snavitem{display:flex;align-items:center;gap:11px;width:100%;text-align:left;background:none;border:1px solid transparent;border-radius:10px;padding:10px 12px;cursor:pointer;color:var(--muted);font-family:inherit;transition:background .12s,border-color .12s,color .12s}
 .snavitem:not(.on):hover{background:var(--inset);color:var(--text)}
 .snavitem.on{background:var(--surface);border-color:var(--border);color:var(--text)}
 .snavicon{display:grid;place-items:center;width:32px;height:32px;flex-shrink:0;border-radius:8px;font-size:16px;background:var(--inset)}
 .snavitem.on .snavicon{background:var(--accent-dim)}
 .snavtext{display:flex;flex-direction:column;gap:1px;min-width:0}
 .snavlabel{font-size:13px;font-weight:600;line-height:1.2}
 .snavdesc{font-size:11px;color:var(--muted);line-height:1.2}
 .spanel{min-width:0;background:var(--surface);border:1px solid var(--border);border-radius:14px;padding:4px 24px 24px}
 .sechead{padding:18px 0 14px;margin-bottom:4px;border-bottom:1px solid var(--border)}
 .sectitle{font-size:20px;font-weight:700;color:var(--text);margin:0}
 .secblurb{font-size:12.5px;color:var(--muted);margin:6px 0 0;line-height:1.5}
 .srow2{display:flex;gap:24px;padding:18px 0;border-bottom:1px solid var(--border);flex-wrap:wrap}
 .srowstack{display:flex;flex-direction:column;gap:10px;padding:18px 0;border-bottom:1px solid var(--border)}
 .scol{flex:1 1 210px;display:flex;flex-direction:column;gap:7px;min-width:0}
 .sname{font-size:14px;font-weight:500;color:var(--text)}
 .shint{font-size:12px;color:var(--muted);line-height:1.45;max-width:520px}
 .spanel input,.spanel textarea,.spanel select{background:var(--inset);border:1px solid var(--border);border-radius:8px;color:var(--text);padding:9px 11px;font-size:13px;font-family:inherit;width:100%;box-sizing:border-box}
 .spanel input:focus,.spanel select:focus{outline:none;border-color:var(--accent)}
 .grouptitle{margin:18px 0 2px;font-size:11px;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--muted)}
 .sactions{display:flex;justify-content:flex-end;padding-top:18px}
 .lock{display:inline-flex;align-items:center;gap:8px;font-size:14px;font-weight:600;font-family:ui-monospace,Menlo,monospace;color:var(--text)}
 /* fee tiers */
 .tiertable{display:flex;flex-direction:column;gap:8px;max-width:440px;padding-top:6px}
 .tierhead{display:grid;grid-template-columns:1fr 1fr 34px;gap:10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);padding:0 2px}
 .tierrow{display:grid;grid-template-columns:1fr 1fr 34px;gap:10px;align-items:center}
 .tierband{font-size:11px;font-weight:600;color:var(--muted);padding-left:2px}
 .tierx{width:34px;height:34px;border:1px solid var(--border);background:var(--inset);border-radius:8px;color:var(--muted);font-size:18px;line-height:1;cursor:pointer;padding:0}
 .tierx:hover{border-color:#b02a2a;color:#fca5a5}
 .addbtn{align-self:flex-start;margin-top:2px;background:var(--inset);border:1px dashed var(--border);border-radius:8px;color:var(--muted);font-size:12px;font-weight:600;padding:8px 14px;cursor:pointer;font-family:inherit}
 .addbtn:hover{border-color:var(--accent);color:var(--accent)}
 /* hours */
 .htoggle{display:flex;align-items:center;gap:10px;font-size:13px;font-weight:500;color:var(--text);cursor:pointer;background:var(--inset);border:1px solid var(--border);border-radius:10px;padding:11px 14px;max-width:440px}
 .htoggle input{width:16px;height:16px;accent-color:var(--accent);cursor:pointer}
 .hlist{display:flex;flex-direction:column;gap:6px;max-width:440px;padding:14px 0 4px}
 .hrow{display:grid;grid-template-columns:150px 1fr;gap:10px;align-items:center;padding:8px 10px;border:1px solid var(--border);border-radius:10px;background:var(--inset)}
 .hday{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--text);cursor:pointer}
 .hday input{width:16px;height:16px;accent-color:var(--accent)}
 .htimes{display:flex;align-items:center;gap:8px}
 .htimes input{width:auto}
 .hclosed{font-size:12px;color:var(--muted);font-style:italic}
 /* preset chips */
 .spresets{display:inline-flex;flex-wrap:wrap;background:var(--inset);border:1px solid var(--border);border-radius:10px;padding:3px;gap:2px;margin-top:6px}
 .schip{font-size:13px;font-weight:500;background:transparent;border:0;border-radius:7px;padding:6px 14px;color:var(--muted);cursor:pointer;white-space:nowrap;font-family:inherit}
 .schip:hover{color:var(--text);background:var(--surface)}
 .zonerow{display:grid;grid-template-columns:1fr .8fr .8fr .6fr 34px;gap:8px;align-items:center;margin-bottom:8px;max-width:560px}
 h3.sec{margin:24px 0 6px;font-size:14px}
 .flash{position:fixed;bottom:16px;right:16px;background:var(--surface);border:1px solid var(--border);padding:10px 14px;border-radius:8px;font-size:13px;opacity:0;transition:.3s}
 .hide{display:none!important}
</style></head><body>
<nav>
  <div class="logo">🧾 TEMP POS</div>
  <button class="navitem active" data-tab="orders"><span class="ic">📋</span>Orders<span class="count hide" id="c-orders"></span></button>
  <button class="navitem" data-tab="menu"><span class="ic">🍽️</span>Menu<span class="count hide" id="c-menu"></span></button>
  <button class="navitem" data-tab="chat"><span class="ic">💬</span>Chat<span class="count hide" id="c-chat"></span></button>
  <button class="navitem" data-tab="customers"><span class="ic">👥</span>Customers</button>
  <button class="navitem" data-tab="riders"><span class="ic">🛵</span>Riders</button>
  <button class="navitem" data-tab="settings"><span class="ic">⚙️</span>Settings</button>
  <div class="navfoot"><span class="dot" id="conn"></span><span id="connlbl">connecting…</span></div>
</nav>
<main>
  <header><h1 id="title">Orders</h1><div class="meta" id="meta"></div></header>
  <div class="body" id="view"></div>
</main>
<div class="flash" id="flash"></div>
<script>
const TITLES={orders:"Orders",menu:"Menu",chat:"Chat",customers:"Customers",riders:"Riders",settings:"Settings"};
let TAB="orders", CONVS=[], ACTIVE_CONV=null, TAKEOVER=false;
function money(v){return v==null?'':'AED '+Number(v).toFixed(2)}
function esc(s){return (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function badge(s){return '<span class="badge b-'+s+'">'+s+'</span>'}
function flash(m){const f=document.getElementById('flash');f.textContent=m;f.style.opacity=1;clearTimeout(f._t);f._t=setTimeout(()=>f.style.opacity=0,2500)}

document.querySelectorAll('.navitem').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.navitem').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');TAB=b.dataset.tab;document.getElementById('title').textContent=TITLES[TAB];
  render();
});

// ---- ORDERS ----
async function act(id,action){flash(action+'…');
  const r=await fetch('/pos/'+id+'/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
  const j=await r.json();flash(action+' → HTTP '+(j.http||r.status));loadOrders();}
function orderCard(t){
  const s=t.status||'confirmed';
  const done=['ready','assigned','picked_up','delivered','cancelled'].includes(s);
  const items=(t.items||[]).map(i=>'<li><span>'+(i.qty||1)+'× '+esc(i.name)+'</span><span>'+money(i.price)+'</span></li>').join('');
  let rider=t.rider?('<div class="rider">🛵 '+esc(t.rider.name)+' · '+esc(t.delivery_status||'')+(t.cod_collected!=null?(' · COD '+money(t.cod_collected)):'')+'</div>'):'';
  return '<div class="card'+(t.late?' late':'')+'">'
   +'<div class="row"><span class="num">#'+esc(t.order_number||t.order_id)+'</span>'+badge(s)+'</div>'
   +'<div class="cust">'+esc((t.customer&&t.customer.name)||'—')+' · '+esc((t.customer&&t.customer.phone)||'')+'</div>'
   +'<ul class="items">'+items+'</ul>'
   +'<div class="tot">Total '+money(t.total)+' · COD '+money(t.cod_due)+'</div>'+rider
   +'<div class="btns">'
     +'<button class="act bd" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\'ack\')">Accept</button>'
     +'<button class="act by" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\'preparing\')">Preparing</button>'
     +'<button class="act bg" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\'ready\')">Ready ▶ dispatch</button>'
     +'<button class="act br" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\'cancelled\')">Cancel</button>'
   +'</div></div>';
}
let LAST_ORDERS=[];
function apiToTicket(o){
  return {order_id:o.order_id,order_number:o.order_number,status:o.status,
    customer:o.customer||{},items:o.items||[],total:o.total,cod_due:o.cod_due,
    address:o.address||{},rider:null,delivery_status:null,cod_collected:null,late:false};
}
// Status precedence: a further-along / terminal status must never be masked by a
// stale one. The platform DB (backlog) is authoritative for the lifecycle status;
// webhook tickets add live rider/delivery richness. If a webhook was missed
// (delivery failed after retries), the backlog still carries the true status.
var STATUS_RANK={confirmed:1,preparing:2,ready:3,assigned:4,picked_up:5,delivered:6,cancelled:6};
function rank(s){return STATUS_RANK[s]||0;}
async function loadOrders(){
  // Live webhook tickets (rich: rider/delivery updates) overlaid on the API
  // backlog (survives redeploys). Rich fields come from the webhook; the STATUS
  // is whichever source is further along, so a cancellation/delivery always wins.
  let webhook=[],backlog=[];
  try{webhook=((await (await fetch('/state')).json()).orders)||[];setConn(true);}catch(e){setConn(false);}
  try{backlog=((await (await fetch('/api/orders')).json()).items||[]).map(apiToTicket);}catch(e){}
  const byId={};
  backlog.forEach(t=>byId[t.order_id]=t);
  webhook.forEach(w=>{
    const b=byId[w.order_id];
    if(!b){byId[w.order_id]=w;return;}
    const merged=Object.assign({},b,{
      rider:w.rider||b.rider,
      delivery_status:w.delivery_status||b.delivery_status,
      cod_collected:w.cod_collected!=null?w.cod_collected:b.cod_collected,
      late:w.late||b.late
    });
    merged.status=rank(w.status)>=rank(b.status)?(w.status||b.status):b.status;
    byId[w.order_id]=merged;
  });
  LAST_ORDERS=Object.values(byId).sort((a,b)=>b.order_id-a.order_id);
  const active=LAST_ORDERS.filter(t=>!['delivered','cancelled'].includes(t.status||'confirmed')).length;
  setCount('c-orders',active);
  if(TAB==='orders'){
    document.getElementById('meta').textContent=LAST_ORDERS.length+' order(s) · auto-refresh 3s';
    document.getElementById('view').innerHTML = LAST_ORDERS.length? '<div id="grid">'+LAST_ORDERS.map(orderCard).join('')+'</div>' : '<div class="empty">No orders yet — place a WhatsApp order.</div>';
  }
  if(TAB==='riders') renderRiders();
}

// ---- CHAT ----
async function loadConvs(){
  try{const r=await fetch('/api/conversations');const j=await r.json();CONVS=j.items||[];setConn(true);}
  catch(e){setConn(false);return;}
  setCount('c-chat',CONVS.filter(c=>c.unread).length);
  if(TAB==='chat') renderChat();
}
async function openConv(cid){ACTIVE_CONV=cid;renderChat();loadThread();}
async function loadThread(){
  if(ACTIVE_CONV==null)return;
  const r=await fetch('/api/conversations/'+ACTIVE_CONV+'/messages');const j=await r.json();
  TAKEOVER=!!j.manual_takeover;
  const box=document.getElementById('msgs');if(!box)return;
  box.innerHTML=(j.items||[]).map(m=>'<div class="bub '+m.direction+'">'+esc(m.text||('['+m.type+']'))+'</div>').join('');
  box.scrollTop=box.scrollHeight;
  const tk=document.getElementById('tk');if(tk){tk.textContent=TAKEOVER?'🙋 You have the chat':'🤖 Bot is answering';tk.className='pill'+(TAKEOVER?' on':'');}
}
async function sendMsg(){
  const inp=document.getElementById('composer-input');const text=inp.value.trim();if(!text||ACTIVE_CONV==null)return;
  inp.value='';
  await fetch('/api/conversations/'+ACTIVE_CONV+'/messages',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
  loadThread();loadConvs();
}
async function toggleTakeover(){
  if(ACTIVE_CONV==null)return;
  await fetch('/api/conversations/'+ACTIVE_CONV+'/takeover',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({active:!TAKEOVER})});
  loadThread();
}
function renderChat(){
  const list=CONVS.map(c=>'<div class="conv'+(c.id===ACTIVE_CONV?' active':'')+'" onclick="openConv('+c.id+')">'
    +'<div class="top"><span>'+esc(c.phone)+'</span>'+(c.unread?'<span class="unread"></span>':'')+'</div>'
    +'<div class="prev">'+esc(c.last_message_preview||'—')+'</div></div>').join('')
    || '<div class="empty">No conversations yet.</div>';
  let thread;
  if(ACTIVE_CONV==null){thread='<div class="thread"><div class="empty">Select a conversation.</div></div>';}
  else{
    const c=CONVS.find(x=>x.id===ACTIVE_CONV)||{};
    thread='<div class="thread"><div class="threadhead"><b>'+esc(c.phone||'')+'</b>'
      +'<button class="pill" id="tk" onclick="toggleTakeover()">…</button></div>'
      +'<div class="msgs" id="msgs"></div>'
      +'<div class="composer"><input id="composer-input" placeholder="Reply as POS…" onkeydown="if(event.key===\'Enter\')sendMsg()"><button onclick="sendMsg()">Send</button></div></div>';
  }
  document.getElementById('meta').textContent=CONVS.length+' conversation(s)';
  document.getElementById('view').innerHTML='<div class="chatwrap"><div class="convlist">'+list+'</div>'+thread+'</div>';
  if(ACTIVE_CONV!=null)loadThread();
}

// ---- MENU (read/add/upload via the partner API) ----
function menuToolbar(){
  return '<div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">'
    +'<button class="act bg" onclick="toggleAddDish()">＋ Add dish</button>'
    +'<button class="act by" onclick="document.getElementById(\'menu-file\').click()">⬆ Upload menu (photo/PDF)</button>'
    +'<input type="file" id="menu-file" class="hide" accept="image/*,application/pdf" multiple onchange="uploadMenu(this)">'
    +'</div>'
    +'<div id="add-dish-form" class="hide" style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:16px">'
      +'<div style="display:grid;grid-template-columns:90px 1fr;gap:10px;max-width:520px;align-items:center">'
      +'<label>Number</label><input id="ad-num" type="number" min="1" placeholder="auto">'
      +'<label>Name</label><input id="ad-name" placeholder="Dish name">'
      +'<label>Price (AED)</label><input id="ad-price" type="number" min="0" step="0.5" placeholder="e.g. 22">'
      +'<label>Sale price</label><input id="ad-sale" type="number" min="0" step="0.5" placeholder="optional — shown struck-through on WhatsApp">'
      +'<label>Serves</label><div>'
        +'<div id="ad-variants"></div>'
        +'<button class="act by" onclick="addVariant()">＋ Add serving size</button>'
        +'<div class="muted" style="font-size:12px;margin-top:4px">optional, e.g. 1 serve AED 22 / 4 serve AED 60</div>'
        +'</div>'
      +'<label>Category</label><input id="ad-cat" placeholder="e.g. Rice">'
      +'<label>Description</label><div style="display:flex;gap:6px"><input id="ad-desc" style="flex:1" placeholder="optional, max 3 lines">'
        +'<button class="act by" style="white-space:nowrap" onclick="suggestDesc()">✨ Suggest</button></div>'
      +'<label>Image</label><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        +'<button class="act by" onclick="document.getElementById(\'ad-imgfile\').click()">⬆ Upload photo</button>'
        +'<input type="file" id="ad-imgfile" class="hide" accept="image/png,image/jpeg,image/webp" onchange="uploadImage(this)">'
        +'<button class="act by" onclick="genImage()">🖼 Generate image</button>'
        +'<img id="ad-imgprev" style="height:44px;border-radius:6px;display:none" />'
        +'<input type="hidden" id="ad-imgurl" /></div>'
      +'<label>Available</label><input id="ad-avail" type="checkbox" checked style="justify-self:start;width:18px;height:18px">'
      +'</div>'
      +'<div style="margin-top:12px;display:flex;gap:8px"><button class="act bg" onclick="saveDish()">Save dish</button>'
      +'<button class="act bd" onclick="toggleAddDish()">Cancel</button></div>'
    +'</div>';
}
async function suggestDesc(){
  const name=document.getElementById('ad-name').value.trim();
  if(!name){flash('Type the dish name first');return;}
  flash('Thinking…');
  const cat=document.getElementById('ad-cat').value.trim();
  const r=await fetch('/api/menu/describe',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name,category:cat||null})});
  const j=await r.json();const b=j.body||j;const t=b.description||'';
  if(r.ok&&t){document.getElementById('ad-desc').value=t;flash('Suggested ✅');}
  else{flash('No suggestion: '+((b.detail)||('HTTP '+r.status)));}
}
async function genImage(){
  const name=document.getElementById('ad-name').value.trim();
  if(!name){flash('Type the dish name first');return;}
  flash('Generating image… (can take ~30s)');
  const cat=document.getElementById('ad-cat').value.trim();
  const r=await fetch('/api/menu/generate-image',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:name,category:cat||null})});
  const j=await r.json();const b=j.body||j;
  if(r.ok&&b.url){
    document.getElementById('ad-imgurl').value=b.url;
    const p=document.getElementById('ad-imgprev');p.src=b.preview_url||b.url;p.style.display='inline-block';
    flash('Image generated ✅');
  }else{flash('Image failed: '+((b.detail)||('HTTP '+r.status)));}
}
function toggleAddDish(){
  const f=document.getElementById('add-dish-form');
  f.classList.toggle('hide');
  if(!f.classList.contains('hide'))fillNextNumber();
}
// Pre-fill the dish number with the next free one. The platform decides it: it counts
// archived dishes and other menus, which /api/menu does not show — so a number worked
// out here could collide with an old dish, and a dish number is how a customer orders.
// Still editable; left blank the platform assigns one anyway.
async function fillNextNumber(){
  const el=document.getElementById('ad-num');
  if(!el||el.value)return;
  try{
    const r=await fetch('/api/menu/next-number');
    const j=await r.json();const b=j.body||j;
    if(b&&b.next_number!=null)el.value=b.next_number;
  }catch(e){/* leave blank — the platform still auto-assigns on save */}
}
// Serving sizes (1 serve / 4 serve). Rows are read straight off the DOM on save.
function addVariant(name,price){
  const wrap=document.getElementById('ad-variants');
  const row=document.createElement('div');
  row.className='ad-variant';
  row.style.cssText='display:flex;gap:6px;margin-bottom:6px';
  row.innerHTML='<input class="v-name" placeholder="e.g. 4 serve" style="flex:1" />'
    +'<input class="v-price" type="number" min="0" step="0.5" placeholder="AED" style="width:90px" />'
    +'<button class="act bd" onclick="this.parentNode.remove()">✕</button>';
  if(name)row.querySelector('.v-name').value=name;
  if(price!=null)row.querySelector('.v-price').value=price;
  wrap.appendChild(row);
}
function readVariants(){
  const out=[];
  document.querySelectorAll('#ad-variants .ad-variant').forEach(function(r){
    const n=r.querySelector('.v-name').value.trim();
    const p=parseFloat(r.querySelector('.v-price').value);
    if(n&&p>0)out.push({name:n,price:p});
  });
  return out;
}
// Upload a real photo. The platform enforces JPG/PNG/WebP + 5MB and stores it in
// Postgres, so it survives a redeploy and Meta can fetch it as the product image.
async function uploadImage(input){
  if(!input.files||!input.files.length)return;
  const fd=new FormData();fd.append('file',input.files[0]);
  input.value='';
  flash('Uploading photo…');
  try{
    const r=await fetch('/api/menu/upload-image',{method:'POST',body:fd});
    const j=await r.json();const b=j.body||j;
    if(r.ok&&b.url){
      document.getElementById('ad-imgurl').value=b.url;
      const prev=document.getElementById('ad-imgprev');
      prev.src=b.url;prev.style.display='';
      flash('Photo uploaded ✅');
    }else{flash('Upload failed: '+((b.detail)||('HTTP '+r.status)));}
  }catch(e){flash('Upload failed');}
}
async function saveDish(){
  const name=document.getElementById('ad-name').value.trim();
  const price=parseFloat(document.getElementById('ad-price').value);
  const num=document.getElementById('ad-num').value.trim();
  if(!name||!(price>0)){flash('Name and a price > 0 are required');return;}
  const item={pos_id:'pos-'+(num||Date.now()),name:name,price:price,
    is_available:document.getElementById('ad-avail').checked};
  if(num)item.dish_number=parseInt(num,10);
  const cat=document.getElementById('ad-cat').value.trim();if(cat)item.category=cat;
  const desc=document.getElementById('ad-desc').value.trim();if(desc)item.description=desc;
  if(document.getElementById('ad-imgurl').value)item.image_url=document.getElementById('ad-imgurl').value;
  const sale=parseFloat(document.getElementById('ad-sale').value);
  if(sale>0)item.sale_price=sale;
  const vars=readVariants();if(vars.length)item.variants=vars;
  flash('Saving…');
  const r=await fetch('/api/menu/add',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(item)});
  const j=await r.json();const b=j.body||j;
  if(r.ok&&(b.created||b.updated)){flash('Dish saved ✅');renderMenu();}
  else{flash('Save failed: '+JSON.stringify(b.errors||b.detail||r.status));}
}
async function uploadMenu(input){
  if(!input.files||!input.files.length)return;
  const fd=new FormData();
  for(const f of input.files)fd.append('files',f);
  input.value='';
  flash('Reading menu… (AI extraction can take up to a minute)');
  const r=await fetch('/api/menu/upload',{method:'POST',body:fd});
  const j=await r.json();
  const b=j.body||j;
  if(r.ok&&b.detail){flash(b.detail);renderMenu();}
  else{flash('Upload failed: '+((b.detail)||('HTTP '+r.status)));}
}
// Per-dish WhatsApp switch. Sends ONLY {whatsapp_enabled} — never a token/secret.
function waToggle(i){
  const on=i.whatsapp_enabled!==false;
  const bg=on?'#128C7E':'transparent';const bd=on?'#128C7E':'var(--border)';const fg=on?'#fff':'var(--muted)';
  return '<button title="Toggle whether this dish is offered on WhatsApp" '
    +'style="cursor:pointer;border:1px solid '+bd+';background:'+bg+';color:'+fg+';border-radius:14px;padding:4px 12px;font-size:12px;font-weight:600" '
    +'onclick="toggleWa('+i.id+','+on+',event)">'+(on?'🟢 On':'⚪ Off')+'</button>';
}
async function toggleWa(id,cur,ev){
  if(ev){const b=ev.target;b.disabled=true;b.textContent='…';}
  const next=!cur;
  const r=await fetch('/api/menu/'+id+'/whatsapp',{method:'PATCH',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({whatsapp_enabled:next})});
  const j=await r.json();const b=j.body||j;
  if(r.ok&&b.whatsapp_enabled===next){flash('WhatsApp '+(next?'ON':'OFF')+' for this dish ✅');renderMenu();}
  else{flash('Toggle failed: '+((b.detail)||('HTTP '+r.status)));renderMenu();}
}
async function deleteDish(id,name){
  if(!confirm('Delete "'+name+'"? It will be removed from WhatsApp too.'))return;
  flash('Deleting…');
  const r=await fetch('/api/menu/'+id,{method:'DELETE'});
  const j=await r.json();const b=j.body||j;
  if(r.ok&&b.detail){flash(b.detail);renderMenu();}
  else{flash('Delete failed: '+((b.detail)||('HTTP '+r.status)));}
}
async function renderMenu(){
  document.getElementById('view').innerHTML=menuToolbar()+'<div class="empty">Loading…</div>';
  let items=[];
  try{const r=await fetch('/api/menu');const j=await r.json();items=(j.body&&j.body.items)||j.items||[];}
  catch(e){document.getElementById('view').innerHTML=menuToolbar()+'<div class="empty">Could not load the menu.</div>';return;}
  const avail=items.filter(i=>i.is_available).length;
  document.getElementById('meta').textContent=items.length+' item(s) · '+avail+' available';
  let html=menuToolbar();
  if(!items.length){document.getElementById('view').innerHTML=html+'<div class="empty">No menu yet. Add a dish or upload your menu above.</div>';return;}
  // group by category for a readable board
  const groups={};
  items.forEach(i=>{const c=i.category||'Other';(groups[c]=groups[c]||[]).push(i);});
  const cats=Object.keys(groups).sort();
  cats.forEach(c=>{
    html+='<h2 style="margin:18px 0 8px;font-size:14px;color:var(--muted)">'+esc(c)+'</h2>';
    html+='<table><thead><tr><th style="width:60px">#</th><th>Dish</th><th style="width:100px">Price</th><th style="width:110px">Status</th><th style="width:130px">WhatsApp</th><th style="width:70px"></th></tr></thead><tbody>';
    html+=groups[c].map(i=>'<tr'+(i.is_available?'':' style="opacity:.5"')+'>'
      +'<td>'+(i.dish_number!=null?esc(i.dish_number):'—')+'</td>'
      +'<td>'+esc(i.name)+(i.description?('<div style="font-size:12px;color:var(--muted)">'+esc(i.description)+'</div>'):'')+'</td>'
      +'<td>'+(i.price!=null?money(i.price):'—')+'</td>'
      +'<td>'+(i.is_available?'<span class="badge b-confirmed">available</span>':'<span class="badge b-cancelled">sold out</span>')+'</td>'
      +'<td>'+waToggle(i)+'</td>'
      +'<td><button title="Delete dish" style="cursor:pointer;border:1px solid var(--border);background:transparent;color:#c0392b;border-radius:8px;padding:4px 10px;font-size:13px" onclick="deleteDish('+i.id+',\''+esc(i.name).replace(/'/g,"\\'")+'\')">🗑</button></td>'
      +'</tr>').join('');
    html+='</tbody></table>';
  });
  document.getElementById('view').innerHTML=html;
}

// ---- CUSTOMERS ----
async function renderCustomers(){
  document.getElementById('view').innerHTML='<div class="empty">Loading…</div>';
  const r=await fetch('/api/customers');const j=await r.json();const items=j.items||[];
  document.getElementById('meta').textContent=items.length+' customer(s)';
  document.getElementById('view').innerHTML = items.length? '<table><thead><tr><th>Name</th><th>Phone</th><th>Orders</th><th>Spend</th><th>Last order</th></tr></thead><tbody>'
    +items.map(c=>'<tr><td>'+esc(c.name||'—')+'</td><td>'+esc(c.phone)+'</td><td>'+c.total_orders+'</td><td>'+money(c.total_spend)+'</td><td>'+esc((c.last_order_at||'').slice(0,10)||'—')+'</td></tr>').join('')
    +'</tbody></table>' : '<div class="empty">No customers yet.</div>';
}

// ---- RIDERS ----
let ROSTER=[];
async function loadRiders(){
  try{ROSTER=((await (await fetch('/api/riders')).json()).items)||[];}catch(e){ROSTER=[];}
  if(TAB==='riders')renderRiders();
}
function renderRiders(){
  // Full roster, with any live delivery from the order tickets overlaid by phone.
  const live={};
  LAST_ORDERS.filter(t=>t.rider&&t.rider.phone).forEach(t=>{live[t.rider.phone]={order:t.order_number||t.order_id,status:t.delivery_status,cod:t.cod_collected,late:t.late};});
  document.getElementById('meta').textContent=ROSTER.length+' rider(s) · '+Object.keys(live).length+' on delivery';
  if(!ROSTER.length){document.getElementById('view').innerHTML='<div class="empty">No riders registered for this store.</div>';return;}
  document.getElementById('view').innerHTML='<table><thead><tr><th>Rider</th><th>Phone</th><th>Duty</th><th>Status</th><th>Current order</th><th>Deliveries</th></tr></thead><tbody>'
    +ROSTER.map(r=>{const l=live[r.phone];
      return '<tr><td>'+esc(r.name)+(l&&l.late?' ⚠️':'')+'</td><td>'+esc(r.phone)+'</td>'
        +'<td>'+(r.on_duty?'🟢 on':'⚪ off')+'</td>'
        +'<td>'+badge(l?(l.status||'on_delivery'):r.status)+'</td>'
        +'<td>'+(l?('#'+esc(l.order)+(l.cod!=null?(' · COD '+money(l.cod)):'')):'—')+'</td>'
        +'<td>'+(r.total_deliveries||0)+'</td></tr>';
    }).join('')
    +'</tbody></table>';
}

// ---- SETTINGS — fixed form, mirrors the ops dashboard (always shows every
//      field with sensible defaults; secrets/token never touched). ----
var SS=null;
var LDEF={enabled:false,earn_rate:0.05,earn_max_per_order_aed:20,credit_ttl_days:90,
  tiers:{gold:{min_orders:5,min_spend_aed:300,max_recency_days:30},
         silver:{min_orders:3,min_spend_aed:120,max_recency_days:60},
         bronze:{min_orders:2,min_spend_aed:0,max_recency_days:90}},
  tier_rewards:{gold:{discount_aed:25,every_n_orders:5},silver:{discount_aed:10,every_n_orders:6},bronze:{discount_aed:0,every_n_orders:0}},
  demotion_grace_days:30,scope_includes_catalog:true};
var SDEF={
  max_orders_per_batch:3,max_item_qty:10,
  cart_reminder_enabled:true,cart_recovery_minutes:15,cart_expiry_minutes:60,
  resale:{enabled:true,discount_type:'percent',discount_value:30,max_age_minutes:30},
  loyalty:LDEF,
  delivery_fee_tiers:[{max_km:3,fee_aed:0},{max_km:5,fee_aed:5},{max_km:10,fee_aed:10}],
  dispatch_engine:'greedy',default_prep_minutes:15,prep_dispatch_lead_min:8,
  batch_expedite_radius_km:1.5,batch_proximity_km:1.0,batch_max_detour_km:0,
  batch_hold_seconds:0,sla_buffer_per_order_minutes:10,delivery_zones:[],
  open_hours:{tz:'Asia/Dubai',days:{}}
};
var DAYS=['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
var LTIERS=[['gold','🥇 Gold'],['silver','🥈 Silver'],['bronze','🥉 Bronze']];
var DPRE={
  slaSafe:{dispatch_engine:'ortools',batch_proximity_km:1.5,batch_max_detour_km:0.5,batch_hold_seconds:120,sla_buffer_per_order_minutes:10},
  dense:{dispatch_engine:'ortools',batch_proximity_km:2.0,batch_max_detour_km:0.8,batch_hold_seconds:150,sla_buffer_per_order_minutes:10},
  suburban:{dispatch_engine:'ortools',batch_proximity_km:3.0,batch_max_detour_km:1.5,batch_hold_seconds:120,sla_buffer_per_order_minutes:10},
  conservative:{dispatch_engine:'greedy',batch_proximity_km:1.0,batch_max_detour_km:0,batch_hold_seconds:0,sla_buffer_per_order_minutes:10}
};
function isObj(v){return v!==null&&typeof v==='object'&&!Array.isArray(v)}
function deepMerge(def,val){
  if(!isObj(def))return (val===undefined?def:val);
  var out={},k;for(k in def)out[k]=def[k];
  if(isObj(val))for(k in val)out[k]=isObj(def[k])?deepMerge(def[k],val[k]):val[k];
  return out;
}
function setPath(path,val,cast){
  if(cast==='int'){val=parseInt(val,10);if(isNaN(val))return;}
  else if(cast==='num'){val=parseFloat(val);if(isNaN(val))return;}
  else if(cast==='bool')val=!!val;
  var p=path.split('.'),o=SS.s,i;
  for(i=0;i<p.length-1;i++)o=o[p[i]];
  o[p[p.length-1]]=val;
}
// input builders
function iNum(path,val,cast,step){return '<input type="number" value="'+esc(val)+'"'+(step?' step="'+step+'"':'')+' oninput="setPath(\''+path+'\',this.value,\''+cast+'\')">';}
function iChk(path,val){return '<input type="checkbox" '+(val?'checked':'')+' style="width:18px;height:18px" onchange="setPath(\''+path+'\',this.checked,\'bool\')">';}
function iSel(path,val,opts){var o='';opts.forEach(function(op){o+='<option value="'+op[0]+'"'+(String(val)===String(op[0])?' selected':'')+'>'+esc(op[1])+'</option>';});return '<select onchange="setPath(\''+path+'\',this.value)">'+o+'</select>';}

// fee tiers
function setTier(i,f,v){SS.s.delivery_fee_tiers[i][f]=parseFloat(v)||0;}
function addTier(){var t=SS.s.delivery_fee_tiers,last=t.length?t[t.length-1].max_km:0;t.push({max_km:last+1,fee_aed:0});drawSettings();}
function rmTier(i){SS.s.delivery_fee_tiers.splice(i,1);drawSettings();}
// delivery zones
function addZone(){SS.s.delivery_zones.push({name:'Zone '+(SS.s.delivery_zones.length+1),center_lat:Number(SS.lat)||25.2,center_lng:Number(SS.lng)||55.2,radius_km:2.5});drawSettings();}
function setZone(i,f,v){SS.s.delivery_zones[i][f]=(f==='name')?v:(parseFloat(v)||0);}
function rmZone(i){SS.s.delivery_zones.splice(i,1);drawSettings();}
// hours
function setNoHours(b){SS.noFixedHours=b;drawSettings();}
function toggleDay(i,b){SS.hoursArr[i].open=b;drawSettings();}
function setDay(i,f,v){SS.hoursArr[i][f]=v;}
// dispatch preset
function applyPreset(k){var p=DPRE[k],f;for(f in p)SS.s[f]=p[f];drawSettings();}

async function renderSettings(){
  document.getElementById('view').innerHTML='<div class="empty">Loading…</div>';
  var d={};try{d=await (await fetch('/api/settings')).json();}catch(e){}
  var h={};try{h=await (await fetch('/health')).json();}catch(e){}
  var s=deepMerge(SDEF,(d&&d.settings)||{});
  SS={name:d.name,lat:d.lat,lng:d.lng,phone:d.phone,rid:d.restaurant_id,health:h,s:s};
  var days=(s.open_hours&&s.open_hours.days)||{};
  SS.noFixedHours=Object.keys(days).length===0;
  SS.hoursArr=[0,1,2,3,4,5,6].map(function(i){var w=days[String(i)];return w?{open:true,from:w[0],to:w[1]}:{open:false,from:'10:00',to:'23:00'};});
  drawSettings();
}
// sub-tabs (mirror the ops dashboard settings nav)
var STAB='general';
var STABS=[
  ['general','🏪','General','Profile & location'],
  ['fees','🛵','Delivery fees','Distance pricing'],
  ['hours','🕒','Opening hours','When you’re open'],
  ['batching','📦','Batching','Order grouping'],
  ['cart','🛒','Cart recovery','Abandoned carts'],
  ['resale','⚡','Resale','Cancelled food'],
  ['loyalty','🎁','Loyalty','Tiers & rewards'],
  ['dispatch','🧭','Dispatch & Kitchen','Engine & prep timing'],
  ['connection','🔑','Connection','Platform link']
];
var STITLE={
  general:['General','Your restaurant’s name and pickup location.'],
  fees:['Delivery fees','Charge by delivery distance. Smallest row starts at 0 km; the largest tier sets your radius (max 25 km).'],
  hours:['Opening hours','Hours the bot tells customers. Times are Asia/Dubai.'],
  batching:['Batching','Limits for grouping orders under the 40-minute SLA.'],
  cart:['Cart recovery','Remind customers who left items in their cart, and auto-clear stale carts.'],
  resale:['Cancelled-order resale','Offer cooked-but-cancelled food to the next customer at a discount.'],
  loyalty:['Loyalty','Reward repeat customers with earned credit and tier-based perks.'],
  dispatch:['Dispatch & Kitchen','Routing engine and distance-driven kitchen timing.'],
  connection:['Connection','How this POS reaches the platform (read-only).']
};
function setStab(k){STAB=k;drawSettings();}
// row/field builders in the ops visual language
function field(name,ctrl,hint){return '<div class="scol"><span class="sname">'+esc(name)+'</span>'+ctrl+(hint?'<div class="shint">'+esc(hint)+'</div>':'')+'</div>';}
function row2(){return '<div class="srow2">'+Array.prototype.join.call(arguments,'')+'</div>';}
function saveBar(fn,label){return '<div class="sactions"><button class="savebtn" onclick="'+fn+'()">'+(label||'Save')+'</button></div>';}

function tabGeneral(){
  return row2(
    field('Restaurant name','<input value="'+esc(SS.name==null?'':SS.name)+'" oninput="SS.name=this.value">'),
    field('WhatsApp number','<span class="lock">🔒 '+esc(SS.phone||'—')+'</span>','WhatsApp Business number, locked.')
  )+row2(
    field('Latitude','<input type="number" step="0.0001" value="'+esc(SS.lat==null?'':SS.lat)+'" oninput="SS.lat=this.value">'),
    field('Longitude','<input type="number" step="0.0001" value="'+esc(SS.lng==null?'':SS.lng)+'" oninput="SS.lng=this.value">')
  )+saveBar('saveGeneral','Save');
}
function tabFees(){
  var s=SS.s,h='<div class="tiertable"><div class="tierhead"><span>Up to (km)</span><span>Fee (AED)</span><span></span></div>';
  s.delivery_fee_tiers.forEach(function(t,i){
    var lower=s.delivery_fee_tiers.map(function(x){return x.max_km}).filter(function(km){return km<t.max_km}).reduce(function(m,km){return Math.max(m,km)},0);
    var band=lower+'–'+t.max_km+' km · '+(t.fee_aed===0?'Free':'AED '+t.fee_aed);
    h+='<div><div class="tierrow">'+
      '<input type="number" min="1" value="'+esc(t.max_km)+'" oninput="setTier('+i+',\'max_km\',this.value)">'+
      '<input type="number" min="0" value="'+esc(t.fee_aed)+'" oninput="setTier('+i+',\'fee_aed\',this.value)">'+
      '<button class="tierx" onclick="rmTier('+i+')" title="Remove">×</button></div>'+
      '<span class="tierband">'+esc(band)+'</span></div>';
  });
  h+='<button class="addbtn" onclick="addTier()">+ Add tier</button></div>';
  return h+saveBar('saveFees','Save fee tiers');
}
function tabHours(){
  var h='<label class="htoggle"><input type="checkbox" '+(SS.noFixedHours?'checked':'')+' onchange="setNoHours(this.checked)"> No fixed hours (always available)</label>';
  if(!SS.noFixedHours){
    h+='<div class="hlist">';
    SS.hoursArr.forEach(function(dd,i){
      h+='<div class="hrow"><label class="hday"><input type="checkbox" '+(dd.open?'checked':'')+' onchange="toggleDay('+i+',this.checked)"> '+DAYS[i]+'</label>';
      if(dd.open)h+='<div class="htimes"><input type="time" value="'+esc(dd.from)+'" onchange="setDay('+i+',\'from\',this.value)"><span style="color:var(--muted)">–</span><input type="time" value="'+esc(dd.to)+'" onchange="setDay('+i+',\'to\',this.value)"></div>';
      else h+='<span class="hclosed">Closed</span>';
      h+='</div>';
    });
    h+='</div>';
  }
  return h+saveBar('saveHours','Save hours');
}
function tabBatching(){
  var s=SS.s;
  return row2(
    field('Max orders per rider',iNum('max_orders_per_batch',s.max_orders_per_batch,'int'),'Most orders one rider carries in a single trip.'),
    field('Confirm large quantity above',iNum('max_item_qty',s.max_item_qty,'int'),'Above this of one item, the bot pauses for a human to confirm.')
  )+saveBar('saveBatching','Save');
}
function tabCart(){
  var s=SS.s;
  return row2(field('Send cart reminder',iChk('cart_reminder_enabled',s.cart_reminder_enabled)))+
    row2(
      field('Remind after (minutes)',iNum('cart_recovery_minutes',s.cart_recovery_minutes,'int'),'How long a cart sits quiet before the reminder.'),
      field('Clear cart after (minutes)',iNum('cart_expiry_minutes',s.cart_expiry_minutes,'int'),'Quiet time before an abandoned cart is emptied.')
    )+saveBar('saveCart','Save');
}
function tabResale(){
  var s=SS.s;
  return row2(field('Enable resale offers',iChk('resale.enabled',s.resale.enabled)))+
    row2(
      field('Discount type',iSel('resale.discount_type',s.resale.discount_type,[['percent','Percent (%)'],['fixed','Fixed amount (AED)']])),
      field(s.resale.discount_type==='percent'?'Discount (%)':'Discount (AED)',iNum('resale.discount_value',s.resale.discount_value,'num')),
      field('Max age (minutes)',iNum('resale.max_age_minutes',s.resale.max_age_minutes,'int'),'Don’t offer food cancelled longer ago than this.')
    )+saveBar('saveResale','Save');
}
function tabLoyalty(){
  var s=SS.s;
  var h=row2(field('Enable loyalty',iChk('loyalty.enabled',s.loyalty.enabled)))+
    row2(
      field('Earn rate (fraction)',iNum('loyalty.earn_rate',s.loyalty.earn_rate,'num','0.005'),'0.05 = 5% of subtotal credited.'),
      field('Max credit per order (AED)',iNum('loyalty.earn_max_per_order_aed',s.loyalty.earn_max_per_order_aed,'num')),
      field('Credit expires (days)',iNum('loyalty.credit_ttl_days',s.loyalty.credit_ttl_days,'int'),'0 = never expires.')
    );
  LTIERS.forEach(function(tk){
    var key=tk[0];
    h+='<div class="grouptitle">'+tk[1]+'</div>'+row2(
      field('Min orders',iNum('loyalty.tiers.'+key+'.min_orders',s.loyalty.tiers[key].min_orders,'int')),
      field('Min spend (AED)',iNum('loyalty.tiers.'+key+'.min_spend_aed',s.loyalty.tiers[key].min_spend_aed,'num')),
      field('Max recency (days)',iNum('loyalty.tiers.'+key+'.max_recency_days',s.loyalty.tiers[key].max_recency_days,'int')),
      field('Reward AED',iNum('loyalty.tier_rewards.'+key+'.discount_aed',(s.loyalty.tier_rewards[key]||{}).discount_aed||0,'num')),
      field('Reward every N',iNum('loyalty.tier_rewards.'+key+'.every_n_orders',(s.loyalty.tier_rewards[key]||{}).every_n_orders||0,'int'))
    );
  });
  h+=row2(
    field('Demotion grace (days)',iNum('loyalty.demotion_grace_days',s.loyalty.demotion_grace_days,'int'),'Quiet days allowed before a tier is lost.'),
    field('Include catalog orders',iChk('loyalty.scope_includes_catalog',s.loyalty.scope_includes_catalog))
  );
  return h+saveBar('saveLoyalty','Save');
}
function tabDispatch(){
  var s=SS.s;
  var h='<div class="spresets">';
  [['slaSafe','SLA-safe launch'],['dense','Dense city'],['suburban','Suburban'],['conservative','Conservative']].forEach(function(p){
    h+='<button class="schip" onclick="applyPreset(\''+p[0]+'\')">'+p[1]+'</button>';
  });
  h+='</div>';
  h+=row2(field('Dispatch engine',iSel('dispatch_engine',s.dispatch_engine,[['greedy','Greedy — proximity batching'],['ortools','OR-Tools — SLA-first optimizer']])));
  h+='<div class="grouptitle">Batching</div>'+row2(
    field('Group orders within (km)',iNum('batch_proximity_km',s.batch_proximity_km,'num','0.1')),
    field('Wait to group (sec)',iNum('batch_hold_seconds',s.batch_hold_seconds,'int'),'0 = send each order right away.'),
    field('On-the-way detour (km)',iNum('batch_max_detour_km',s.batch_max_detour_km,'num','0.1'),'0 = simple nearby grouping.'),
    field('SLA buffer per extra stop (min)',iNum('sla_buffer_per_order_minutes',s.sla_buffer_per_order_minutes,'int'))
  );
  h+='<div class="grouptitle">Delivery zones</div><div style="padding-top:8px">';
  s.delivery_zones.forEach(function(z,i){
    h+='<div class="zonerow">'+
      '<input value="'+esc(z.name)+'" oninput="setZone('+i+',\'name\',this.value)" placeholder="Name">'+
      '<input type="number" step="0.0001" value="'+esc(z.center_lat)+'" oninput="setZone('+i+',\'center_lat\',this.value)" placeholder="lat">'+
      '<input type="number" step="0.0001" value="'+esc(z.center_lng)+'" oninput="setZone('+i+',\'center_lng\',this.value)" placeholder="lng">'+
      '<input type="number" step="0.1" value="'+esc(z.radius_km)+'" oninput="setZone('+i+',\'radius_km\',this.value)" placeholder="km">'+
      '<button class="tierx" onclick="rmZone('+i+')">×</button></div>';
  });
  h+='<button class="addbtn" onclick="addZone()">+ Add delivery zone</button></div>';
  h+='<div class="grouptitle">Kitchen</div>'+row2(
    field('Prep dispatch lead (min)',iNum('prep_dispatch_lead_min',s.prep_dispatch_lead_min,'int')),
    field('Default cook time (min)',iNum('default_prep_minutes',s.default_prep_minutes,'int')),
    field('Expedite radius (km)',iNum('batch_expedite_radius_km',s.batch_expedite_radius_km,'num','0.1'))
  );
  return h+saveBar('saveDispatch','Save');
}
function tabConnection(){
  var h=SS.health||{};
  return '<div class="kv" style="padding-top:18px">'
    +'<div class="k">Platform base URL</div><div>'+esc(h.base_url||'—')+'</div>'
    +'<div class="k">API key configured</div><div>'+(h.api_key_set?'✅ yes':'❌ no')+'</div>'
    +'<div class="k">Webhook secret set</div><div>'+(h.secret_set?'✅ yes':'❌ no (signatures NOT verified)')+'</div>'
    +'<div class="k">WhatsApp token</div><div>🔒 never shared with the POS</div>'
    +'</div>';
}
function drawSettings(){
  document.getElementById('meta').textContent='Store #'+(SS.rid||'—');
  var nav='';
  STABS.forEach(function(t){
    nav+='<button class="snavitem'+(STAB===t[0]?' on':'')+'" onclick="setStab(\''+t[0]+'\')">'+
      '<span class="snavicon">'+t[1]+'</span><span class="snavtext"><span class="snavlabel">'+t[2]+'</span><span class="snavdesc">'+t[3]+'</span></span></button>';
  });
  var body={general:tabGeneral,fees:tabFees,hours:tabHours,batching:tabBatching,cart:tabCart,resale:tabResale,loyalty:tabLoyalty,dispatch:tabDispatch,connection:tabConnection}[STAB]();
  var title=STITLE[STAB];
  var panel='<div class="spanel"><div class="sechead"><h2 class="sectitle">'+esc(title[0])+'</h2>'+(title[1]?'<p class="secblurb">'+esc(title[1])+'</p>':'')+'</div>'+body+'</div>';
  document.getElementById('view').innerHTML='<div class="slayout"><nav class="snav">'+nav+'</nav>'+panel+'</div>';
}

// per-tab save (only that tab's keys; mirrors the ops dashboard)
async function doSave(patch,label){
  flash('Saving…');
  var r,j={};
  try{r=await fetch('/api/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});}
  catch(e){flash('Save failed: network');return;}
  try{j=await r.json();}catch(e){}
  if(r.status===200){flash((label||'Settings')+' saved ✓');await renderSettings();}
  else{flash('Save failed: HTTP '+r.status+(j&&j.detail?' — '+(typeof j.detail==='string'?j.detail:JSON.stringify(j.detail)):''));}
}
function saveGeneral(){var p={};if(SS.name&&String(SS.name).trim())p.name=String(SS.name).trim();var la=parseFloat(SS.lat);if(!isNaN(la))p.lat=la;var ln=parseFloat(SS.lng);if(!isNaN(ln))p.lng=ln;doSave(p,'Profile');}
function saveFees(){doSave({delivery_fee_tiers:SS.s.delivery_fee_tiers},'Fee tiers');}
function saveHours(){var p;if(SS.noFixedHours){p={open_hours:{tz:'Asia/Dubai',days:{}}};}else{var days={},i;for(i=0;i<7;i++){var dd=SS.hoursArr[i];if(dd.open)days[i]=[dd.from,dd.to];}p={open_hours:{tz:'Asia/Dubai',days:days}};}doSave(p,'Hours');}
function saveBatching(){doSave({max_orders_per_batch:SS.s.max_orders_per_batch,max_item_qty:SS.s.max_item_qty},'Batching');}
function saveCart(){doSave({cart_reminder_enabled:SS.s.cart_reminder_enabled,cart_recovery_minutes:SS.s.cart_recovery_minutes,cart_expiry_minutes:SS.s.cart_expiry_minutes},'Cart recovery');}
function saveResale(){doSave({resale:SS.s.resale},'Resale');}
function saveLoyalty(){doSave({loyalty:SS.s.loyalty},'Loyalty');}
function saveDispatch(){doSave({dispatch_engine:SS.s.dispatch_engine,default_prep_minutes:SS.s.default_prep_minutes,prep_dispatch_lead_min:SS.s.prep_dispatch_lead_min,batch_expedite_radius_km:SS.s.batch_expedite_radius_km,batch_proximity_km:SS.s.batch_proximity_km,batch_max_detour_km:SS.s.batch_max_detour_km,batch_hold_seconds:SS.s.batch_hold_seconds,sla_buffer_per_order_minutes:SS.s.sla_buffer_per_order_minutes,delivery_zones:SS.s.delivery_zones},'Dispatch');}

// ---- shared ----
function setConn(ok){const d=document.getElementById('conn');const l=document.getElementById('connlbl');
  d.className='dot '+(ok?'ok':'bad');l.textContent=ok?'connected':'offline';}
function setCount(id,n){const el=document.getElementById(id);if(!el)return;if(n>0){el.textContent=n;el.classList.remove('hide');}else el.classList.add('hide');}
function render(){
  if(TAB==='orders')loadOrders();
  else if(TAB==='menu')renderMenu();
  else if(TAB==='chat'){renderChat();loadConvs();}
  else if(TAB==='customers')renderCustomers();
  else if(TAB==='riders'){renderRiders();loadRiders();}
  else if(TAB==='settings')renderSettings();
}
// polling loops
loadOrders();loadConvs();render();
setInterval(loadOrders,3000);
setInterval(()=>{loadConvs();if(TAB==='chat'&&ACTIVE_CONV!=null)loadThread();if(TAB==='riders')loadRiders();},3000);
</script></body></html>"""
