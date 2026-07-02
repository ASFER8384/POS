"""Temp POS — a standalone fake POS partner for testing the WhatsApp platform.

Deploy this as its own Render web service (or run locally). It behaves like a
real POS partner:

  1. RECEIVES the platform's outbound webhooks at POST /hooks/whatsapp,
     verifies the HMAC signature, and remembers each event.
  2. ACTS as the POS kitchen — when an ``order.created`` arrives it calls the
     platform API (X-API-Key) to ack the order, then mark it preparing, then
     ready (which fires dispatch on the platform side).

Open GET / in a browser to see the last events it received.

Env vars:
  POS_BASE_URL        platform API base (default the live Render deployment)
  POS_API_KEY         X-API-Key for the store under test (needed to auto-advance)
  POS_WEBHOOK_SECRET  shared HMAC secret configured in the store's partner config
  POS_AUTO_ADVANCE    "true" (default) = auto ack+preparing+ready on order.created
  PORT                injected by Render; the server binds to it
"""
from __future__ import annotations

import hashlib
import hmac
import os
from collections import deque

import httpx
from fastapi import FastAPI, Request, Response

BASE_URL = os.environ.get(
    "POS_BASE_URL", "https://restaurant-whatsapp-service.onrender.com"
).rstrip("/")
API_KEY = os.environ.get("POS_API_KEY", "")
SECRET = os.environ.get("POS_WEBHOOK_SECRET", "")
AUTO_ADVANCE = os.environ.get("POS_AUTO_ADVANCE", "true").lower() == "true"

app = FastAPI(title="Temp POS", version="1.0")

# In-memory ring buffer of the most recent events (visible at GET /).
_events: deque[dict] = deque(maxlen=50)


def _verify(raw: bytes, header: str | None) -> bool:
    if not SECRET:
        return True  # not verifying — no secret configured
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def _api_post(client: httpx.AsyncClient, path: str, body: dict) -> str:
    if not API_KEY:
        return "skipped (no POS_API_KEY)"
    try:
        r = await client.post(
            f"{BASE_URL}{path}", headers={"X-API-Key": API_KEY}, json=body, timeout=30.0
        )
        return f"HTTP {r.status_code} {r.text[:150]}"
    except Exception as exc:  # noqa: BLE001
        return f"error: {exc}"


async def _drive_kitchen(order_id: int) -> list[str]:
    """Behave like the POS kitchen: ack -> preparing -> ready."""
    out: list[str] = []
    async with httpx.AsyncClient() as client:
        out.append(
            "ack: "
            + await _api_post(
                client,
                f"/api/v1/partner/orders/{order_id}/ack",
                {"pos_order_id": f"TEMP-POS-{order_id}"},
            )
        )
        out.append(
            "preparing: "
            + await _api_post(
                client,
                f"/api/v1/partner/orders/{order_id}/status",
                {"status": "preparing"},
            )
        )
        out.append(
            "ready: "
            + await _api_post(
                client,
                f"/api/v1/partner/orders/{order_id}/status",
                {"status": "ready"},
            )
        )
    return out


@app.get("/")
async def home() -> dict:
    return {
        "service": "temp-pos",
        "platform": BASE_URL,
        "api_key": "set" if API_KEY else "NOT SET (cannot auto-advance)",
        "hmac": "verifying" if SECRET else "NOT verifying (no secret)",
        "auto_advance": AUTO_ADVANCE,
        "recent_events": list(_events),
    }


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/hooks/whatsapp")
async def receive(request: Request) -> Response:
    raw = await request.body()
    sig = request.headers.get("X-Partner-Signature")
    event = request.headers.get("X-Partner-Event", "?")
    ok = _verify(raw, sig)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    data = body.get("data", {}) if isinstance(body, dict) else {}

    record = {
        "event": event,
        "idempotency_key": body.get("idempotency_key") if isinstance(body, dict) else None,
        "hmac_ok": ok,
        "data": data,
        "actions": [],
    }

    if ok and event == "order.created" and AUTO_ADVANCE and data.get("order_id") is not None:
        record["actions"] = await _drive_kitchen(int(data["order_id"]))

    _events.appendleft(record)
    print(f"received {event} hmac_ok={ok} idem={record['idempotency_key']}")
    return Response(status_code=200 if ok else 401, content="ok" if ok else "bad signature")
