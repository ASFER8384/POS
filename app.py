"""Temp POS — a standalone VISUAL fake POS partner (Cratis-style kitchen display).

Deploy as its own Render web service. It behaves like a real POS partner:

  1. RECEIVES the platform's outbound webhooks at POST /hooks/whatsapp, verifies
     the HMAC signature, and shows each order as a live TICKET on a web page.
  2. A human clicks the ticket buttons (Accept / Preparing / Ready / Cancel) —
     each calls the platform API (X-API-Key). Clicking READY fires dispatch on
     the platform, and the rider updates (assigned / picked_up / delivered) flow
     back onto the same ticket.

Open GET / in a browser: that's the POS screen.

Env vars:
  POS_BASE_URL        platform API base (default the live Render deployment)
  POS_API_KEY         X-API-Key for the store under test
  POS_WEBHOOK_SECRET  shared HMAC secret configured in the store's partner config
  PORT                injected by Render
"""
from __future__ import annotations

import hashlib
import hmac
import os

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

BASE_URL = os.environ.get(
    "POS_BASE_URL", "https://restaurant-whatsapp-service.onrender.com"
).rstrip("/")
API_KEY = os.environ.get("POS_API_KEY", "")
SECRET = os.environ.get("POS_WEBHOOK_SECRET", "")

app = FastAPI(title="Temp POS", version="2.0")

# order_id -> ticket dict. In-memory (resets on spin-down; fine for testing).
_orders: dict[int, dict] = {}


def _verify(raw: bytes, header: str | None) -> bool:
    if not SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


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
    print(f"received {event} hmac_ok={ok}")
    if not ok:
        return Response(status_code=401, content="bad signature")

    oid = data.get("order_id")
    if oid is not None:
        oid = int(oid)
        t = _orders.setdefault(oid, {"order_id": oid, "events": []})
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
    return Response(status_code=200, content="ok")


@app.post("/pos/{order_id}/action")
async def pos_action(order_id: int, request: Request) -> JSONResponse:
    """Human clicked a ticket button -> call the platform API as the POS."""
    if not API_KEY:
        return JSONResponse({"error": "POS_API_KEY not set"}, status_code=400)
    body = await request.json()
    action = body.get("action")
    headers = {"X-API-Key": API_KEY}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as c:
        if action == "ack":
            r = await c.post(
                f"/api/v1/partner/orders/{order_id}/ack",
                headers=headers,
                json={"pos_order_id": f"TEMP-POS-{order_id}"},
            )
        elif action in ("preparing", "ready", "cancelled"):
            r = await c.post(
                f"/api/v1/partner/orders/{order_id}/status",
                headers=headers,
                json={"status": action},
            )
        else:
            return JSONResponse({"error": f"unknown action {action}"}, status_code=400)

    result = {"http": r.status_code, "body": (r.json() if r.headers.get("content-type","").startswith("application/json") else r.text)}
    # Optimistically reflect the new status on the ticket.
    if r.status_code == 200 and order_id in _orders and isinstance(result["body"], dict):
        _orders[order_id]["status"] = result["body"].get("status", _orders[order_id].get("status"))
    return JSONResponse(result)


@app.get("/state")
async def state() -> JSONResponse:
    # newest first
    return JSONResponse({"orders": sorted(_orders.values(), key=lambda t: t["order_id"], reverse=True)})


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Temp POS — Kitchen Display</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0}
 header{background:#1e293b;padding:14px 20px;display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #334155}
 header h1{font-size:18px;margin:0}
 header .meta{font-size:12px;color:#94a3b8}
 #grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px;padding:18px}
 .card{background:#1e293b;border:1px solid #334155;border-radius:10px;padding:14px;box-shadow:0 2px 6px rgba(0,0,0,.3)}
 .card.late{border-color:#ef4444}
 .row{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
 .num{font-weight:700;font-size:16px}
 .badge{font-size:11px;padding:3px 8px;border-radius:999px;text-transform:uppercase;letter-spacing:.03em}
 .b-confirmed{background:#334155;color:#cbd5e1}
 .b-preparing{background:#a16207;color:#fef9c3}
 .b-ready{background:#166534;color:#dcfce7}
 .b-assigned,.b-picked_up{background:#1d4ed8;color:#dbeafe}
 .b-delivered{background:#065f46;color:#d1fae5}
 .b-cancelled{background:#7f1d1d;color:#fecaca}
 .cust{font-size:13px;color:#cbd5e1;margin-bottom:6px}
 ul.items{list-style:none;margin:6px 0;padding:0;font-size:13px}
 ul.items li{display:flex;justify-content:space-between;padding:2px 0;border-bottom:1px dashed #334155}
 .tot{font-weight:700;margin-top:6px;font-size:14px}
 .rider{margin-top:8px;font-size:12px;color:#93c5fd}
 .btns{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px}
 button{border:0;border-radius:6px;padding:7px 10px;font-size:12px;font-weight:600;cursor:pointer;color:#fff}
 .bd{background:#2563eb}.bg{background:#16a34a}.by{background:#ca8a04}.br{background:#dc2626}
 button:disabled{opacity:.35;cursor:not-allowed}
 .empty{color:#64748b;padding:40px;text-align:center;grid-column:1/-1}
 .flash{position:fixed;bottom:16px;right:16px;background:#334155;padding:10px 14px;border-radius:8px;font-size:13px;opacity:0;transition:.3s}
</style></head><body>
<header><h1>🧾 Temp POS — Kitchen Display</h1><div class="meta" id="meta"></div></header>
<div id="grid"></div>
<div class="flash" id="flash"></div>
<script>
const LATE='';
function badge(s){return '<span class="badge b-'+s+'">'+s+'</span>'}
function money(v){return v==null?'':'AED '+Number(v).toFixed(2)}
async function act(id,action){
  flash(action+'…');
  const r=await fetch('/pos/'+id+'/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
  const j=await r.json();
  flash(action+' → HTTP '+(j.http||r.status));
  load();
}
function flash(m){const f=document.getElementById('flash');f.textContent=m;f.style.opacity=1;clearTimeout(f._t);f._t=setTimeout(()=>f.style.opacity=0,2500)}
function card(t){
  const s=t.status||'confirmed';
  const done=['ready','assigned','picked_up','delivered','cancelled'].includes(s);
  const items=(t.items||[]).map(i=>'<li><span>'+(i.qty||1)+'× '+(i.name||'')+'</span><span>'+money(i.price)+'</span></li>').join('');
  let rider='';
  if(t.rider){rider='<div class="rider">🛵 '+(t.rider.name||'')+' · '+(t.delivery_status||'')+(t.cod_collected!=null?(' · COD '+money(t.cod_collected)):'')+'</div>'}
  return '<div class="card'+(t.late?' late':'')+'">'
   +'<div class="row"><span class="num">#'+(t.order_number||t.order_id)+'</span>'+badge(s)+'</div>'
   +'<div class="cust">'+((t.customer&&t.customer.name)||'—')+' · '+((t.customer&&t.customer.phone)||'')+'</div>'
   +'<ul class="items">'+items+'</ul>'
   +'<div class="tot">Total '+money(t.total)+' · COD '+money(t.cod_due)+'</div>'
   +rider
   +'<div class="btns">'
     +'<button class="bd" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\\'ack\\')">Accept</button>'
     +'<button class="by" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\\'preparing\\')">Preparing</button>'
     +'<button class="bg" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\\'ready\\')">Ready ▶ dispatch</button>'
     +'<button class="br" '+(done?'disabled':'')+' onclick="act('+t.order_id+',\\'cancelled\\')">Cancel</button>'
   +'</div></div>';
}
async function load(){
  const r=await fetch('/state');const j=await r.json();
  const g=document.getElementById('grid');
  document.getElementById('meta').textContent=(j.orders.length)+' order(s) · auto-refresh 3s';
  g.innerHTML = j.orders.length? j.orders.map(card).join('') : '<div class="empty">No orders yet — place a WhatsApp order.</div>';
}
load();setInterval(load,3000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def home() -> HTMLResponse:
    return HTMLResponse(_PAGE)
