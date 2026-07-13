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

app = FastAPI(title="Temp POS", version="3.0")

# order_id -> ticket dict. In-memory (resets on spin-down; fine for testing).
_orders: dict[int, dict] = {}


def _verify(raw: bytes, header: str | None) -> bool:
    if not SECRET:
        return True
    if not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def _platform(method: str, path: str, **kw) -> httpx.Response:
    """Call the platform API as the POS (X-API-Key)."""
    headers = {"X-API-Key": API_KEY}
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as c:
        return await c.request(method, path, headers=headers, **kw)


def _passthrough(r: httpx.Response) -> JSONResponse:
    ct = r.headers.get("content-type", "")
    body = r.json() if ct.startswith("application/json") else {"raw": r.text}
    return JSONResponse(body, status_code=r.status_code)


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


# ── Order actions (us -> platform) ───────────────────────────────────────────
@app.post("/pos/{order_id}/action")
async def pos_action(order_id: int, request: Request) -> JSONResponse:
    if not API_KEY:
        return JSONResponse({"error": "POS_API_KEY not set"}, status_code=400)
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
    return JSONResponse({"orders": sorted(_orders.values(), key=lambda t: t["order_id"], reverse=True)})


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
    return {"ok": True, "base_url": BASE_URL, "api_key_set": bool(API_KEY), "secret_set": bool(SECRET)}


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
 h3.sec{margin:24px 0 6px;font-size:14px;border-top:1px solid var(--border);padding-top:16px}
 h3.sec:first-of-type{border-top:0;padding-top:0}
 h4.grp{margin:16px 0 8px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
 .secblurb{color:var(--muted);font-size:12px;margin:0 0 12px}
 .shint{color:var(--muted);font-size:11px;margin-top:3px;max-width:520px}
 .setgrid select{background:var(--inset);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:7px 10px;font-size:13px;width:100%;font-family:inherit}
 .tierrow{display:grid;grid-template-columns:1fr 1fr 90px 32px;gap:8px;align-items:center;margin-bottom:8px;max-width:520px}
 .tierrow .band{color:var(--muted);font-size:11px}
 .dayrow{display:grid;grid-template-columns:150px 1fr;gap:10px;align-items:center;margin-bottom:6px;max-width:520px}
 .dayrow .times{display:flex;gap:8px;align-items:center}
 .dayrow input[type=time]{background:var(--inset);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:6px 8px;font-family:inherit}
 .miniadd{background:none;border:1px dashed var(--border);color:var(--accent);border-radius:6px;padding:7px 12px;cursor:pointer;font-size:12px;font-family:inherit;margin-top:4px}
 .minix{background:#7f1d1d;color:#fecaca;border:0;border-radius:6px;cursor:pointer;font-weight:700}
 .chips{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0 12px}
 .chip{font-size:12px;padding:6px 12px;border-radius:999px;border:1px solid var(--border);background:none;color:var(--text);cursor:pointer;font-family:inherit}
 .chip:hover{border-color:var(--accent);color:var(--accent)}
 .flash{position:fixed;bottom:16px;right:16px;background:var(--surface);border:1px solid var(--border);padding:10px 14px;border-radius:8px;font-size:13px;opacity:0;transition:.3s}
 .hide{display:none!important}
</style></head><body>
<nav>
  <div class="logo">🧾 TEMP POS</div>
  <button class="navitem active" data-tab="orders"><span class="ic">📋</span>Orders<span class="count hide" id="c-orders"></span></button>
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
const TITLES={orders:"Orders",chat:"Chat",customers:"Customers",riders:"Riders",settings:"Settings"};
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
async function loadOrders(){
  // Live webhook tickets (rich: rider/delivery updates) overlaid on the API
  // backlog (survives redeploys). Webhook version wins when both exist.
  let webhook=[],backlog=[];
  try{webhook=((await (await fetch('/state')).json()).orders)||[];setConn(true);}catch(e){setConn(false);}
  try{backlog=((await (await fetch('/api/orders')).json()).items||[]).map(apiToTicket);}catch(e){}
  const byId={};
  backlog.forEach(t=>byId[t.order_id]=t);
  webhook.forEach(t=>byId[t.order_id]=t);
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
function grid(rows){return '<div class="setgrid">'+rows+'</div>';}
function fRow(label,input,hint){return '<label class="k">'+esc(label)+'</label><div>'+input+(hint?'<div class="shint">'+esc(hint)+'</div>':'')+'</div>';}
function sec(title,blurb,body){return '<h3 class="sec">'+esc(title)+'</h3>'+(blurb?'<p class="secblurb">'+esc(blurb)+'</p>':'')+body;}

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
function drawSettings(){
  var h=SS.health||{},s=SS.s;
  document.getElementById('meta').textContent='Store #'+(SS.rid||'—');
  var html='<div style="max-width:820px">';

  // General
  html+=sec('General','Your restaurant’s name and pickup location.',
    grid(
      fRow('Restaurant name','<input value="'+esc(SS.name==null?'':SS.name)+'" oninput="SS.name=this.value">')+
      fRow('WhatsApp number','<span>'+esc(SS.phone||'—')+' <span style="color:var(--muted)">(read-only)</span></span>','🔒 WhatsApp Business number, locked.')+
      fRow('Latitude','<input type="number" step="0.0001" value="'+esc(SS.lat==null?'':SS.lat)+'" oninput="SS.lat=this.value">')+
      fRow('Longitude','<input type="number" step="0.0001" value="'+esc(SS.lng==null?'':SS.lng)+'" oninput="SS.lng=this.value">')
    ));

  // Delivery fees
  var tiers='<div class="tierrow" style="color:var(--muted);font-size:11px;text-transform:uppercase"><span>Up to (km)</span><span>Fee (AED)</span><span>Band</span><span></span></div>';
  s.delivery_fee_tiers.forEach(function(t,i){
    var lower=s.delivery_fee_tiers.map(function(x){return x.max_km}).filter(function(km){return km<t.max_km}).reduce(function(m,km){return Math.max(m,km)},0);
    var band=lower+'–'+t.max_km+'km · '+(t.fee_aed===0?'Free':'AED '+t.fee_aed);
    tiers+='<div class="tierrow">'+
      '<input type="number" min="1" value="'+esc(t.max_km)+'" oninput="setTier('+i+',\'max_km\',this.value)">'+
      '<input type="number" min="0" value="'+esc(t.fee_aed)+'" oninput="setTier('+i+',\'fee_aed\',this.value)">'+
      '<span class="band">'+esc(band)+'</span>'+
      '<button class="minix" onclick="rmTier('+i+')" title="Remove">×</button></div>';
  });
  tiers+='<button class="miniadd" onclick="addTier()">+ Add tier</button>';
  html+=sec('Delivery fees','Charge by delivery distance. Smallest row starts at 0 km; the largest tier sets your radius (max 25 km).',tiers);

  // Opening hours
  var hrs='<label class="k" style="display:flex;gap:8px;align-items:center;cursor:pointer"><input type="checkbox" '+(SS.noFixedHours?'checked':'')+' onchange="setNoHours(this.checked)" style="width:18px;height:18px"> No fixed hours (always available)</label>';
  if(!SS.noFixedHours){
    hrs+='<div style="margin-top:12px">';
    SS.hoursArr.forEach(function(dd,i){
      hrs+='<div class="dayrow"><label style="display:flex;gap:8px;align-items:center;cursor:pointer"><input type="checkbox" '+(dd.open?'checked':'')+' onchange="toggleDay('+i+',this.checked)" style="width:16px;height:16px"> '+DAYS[i]+'</label>';
      if(dd.open)hrs+='<div class="times"><input type="time" value="'+esc(dd.from)+'" onchange="setDay('+i+',\'from\',this.value)"> – <input type="time" value="'+esc(dd.to)+'" onchange="setDay('+i+',\'to\',this.value)"></div>';
      else hrs+='<span style="color:var(--muted)">Closed</span>';
      hrs+='</div>';
    });
    hrs+='</div>';
  }
  html+=sec('Opening hours','Hours the bot tells customers. Times are Asia/Dubai.',hrs);

  // Batching
  html+=sec('Batching','Limits for grouping orders under the 40-minute SLA.',
    grid(
      fRow('Max orders per rider',iNum('max_orders_per_batch',s.max_orders_per_batch,'int'),'Most orders one rider carries in a single trip.')+
      fRow('Confirm large quantity above',iNum('max_item_qty',s.max_item_qty,'int'),'Above this of one item, the bot pauses for a human to confirm.')
    ));

  // Cart recovery
  html+=sec('Cart recovery','Remind customers who left items in their cart, and auto-clear stale carts.',
    grid(
      fRow('Send cart reminder',iChk('cart_reminder_enabled',s.cart_reminder_enabled))+
      fRow('Remind after (minutes)',iNum('cart_recovery_minutes',s.cart_recovery_minutes,'int'),'How long a cart sits quiet before the reminder.')+
      fRow('Clear cart after (minutes)',iNum('cart_expiry_minutes',s.cart_expiry_minutes,'int'),'Quiet time before an abandoned cart is emptied.')
    ));

  // Resale
  html+=sec('Cancelled-order resale','Offer cooked-but-cancelled food to the next customer at a discount.',
    grid(
      fRow('Enable resale offers',iChk('resale.enabled',s.resale.enabled))+
      fRow('Discount type',iSel('resale.discount_type',s.resale.discount_type,[['percent','Percent (%)'],['fixed','Fixed amount (AED)']]))+
      fRow(s.resale.discount_type==='percent'?'Discount (%)':'Discount (AED)',iNum('resale.discount_value',s.resale.discount_value,'num'))+
      fRow('Max age (minutes)',iNum('resale.max_age_minutes',s.resale.max_age_minutes,'int'),'Don’t offer food cancelled longer ago than this.')
    ));

  // Loyalty
  var loy=grid(
    fRow('Enable loyalty',iChk('loyalty.enabled',s.loyalty.enabled))+
    fRow('Earn rate (%)',iNum('loyalty.earn_rate',s.loyalty.earn_rate,'num','0.005'),'Fraction of subtotal credited (e.g. 0.05 = 5%).')+
    fRow('Max credit per order (AED)',iNum('loyalty.earn_max_per_order_aed',s.loyalty.earn_max_per_order_aed,'num'))+
    fRow('Credit expires (days)',iNum('loyalty.credit_ttl_days',s.loyalty.credit_ttl_days,'int'),'0 = never expires.')
  );
  LTIERS.forEach(function(tk){
    var key=tk[0];
    loy+='<h4 class="grp">'+tk[1]+'</h4>'+grid(
      fRow('Min orders',iNum('loyalty.tiers.'+key+'.min_orders',s.loyalty.tiers[key].min_orders,'int'))+
      fRow('Min spend (AED)',iNum('loyalty.tiers.'+key+'.min_spend_aed',s.loyalty.tiers[key].min_spend_aed,'num'))+
      fRow('Max recency (days)',iNum('loyalty.tiers.'+key+'.max_recency_days',s.loyalty.tiers[key].max_recency_days,'int'))+
      fRow('Reward AED',iNum('loyalty.tier_rewards.'+key+'.discount_aed',(s.loyalty.tier_rewards[key]||{}).discount_aed||0,'num'))+
      fRow('Reward every N orders',iNum('loyalty.tier_rewards.'+key+'.every_n_orders',(s.loyalty.tier_rewards[key]||{}).every_n_orders||0,'int'))
    );
  });
  loy+=grid(
    fRow('Demotion grace (days)',iNum('loyalty.demotion_grace_days',s.loyalty.demotion_grace_days,'int'),'Quiet days allowed before a tier is lost.')+
    fRow('Include catalog orders',iChk('loyalty.scope_includes_catalog',s.loyalty.scope_includes_catalog))
  );
  html+=sec('Loyalty','Reward repeat customers with earned credit and tier-based perks.',loy);

  // Dispatch & Kitchen
  var disp='<div class="chips">';
  [['slaSafe','SLA-safe launch'],['dense','Dense city'],['suburban','Suburban'],['conservative','Conservative']].forEach(function(p){
    disp+='<button class="chip" onclick="applyPreset(\''+p[0]+'\')">'+p[1]+'</button>';
  });
  disp+='</div>';
  disp+=grid(fRow('Dispatch engine',iSel('dispatch_engine',s.dispatch_engine,[['greedy','Greedy — proximity batching'],['ortools','OR-Tools — SLA-first optimizer']])));
  disp+='<h4 class="grp">Batching</h4>'+grid(
    fRow('Group orders within (km)',iNum('batch_proximity_km',s.batch_proximity_km,'num','0.1'))+
    fRow('Wait to group (sec)',iNum('batch_hold_seconds',s.batch_hold_seconds,'int'),'0 = send each order right away.')+
    fRow('On-the-way detour (km)',iNum('batch_max_detour_km',s.batch_max_detour_km,'num','0.1'),'0 = simple nearby grouping.')+
    fRow('SLA buffer per extra stop (min)',iNum('sla_buffer_per_order_minutes',s.sla_buffer_per_order_minutes,'int'))
  );
  disp+='<h4 class="grp">Delivery zones</h4>';
  s.delivery_zones.forEach(function(z,i){
    disp+='<div class="setgrid" style="grid-template-columns:1fr 1fr 1fr 90px 60px">'+
      '<input value="'+esc(z.name)+'" oninput="setZone('+i+',\'name\',this.value)" placeholder="Name">'+
      '<input type="number" step="0.0001" value="'+esc(z.center_lat)+'" oninput="setZone('+i+',\'center_lat\',this.value)" placeholder="lat">'+
      '<input type="number" step="0.0001" value="'+esc(z.center_lng)+'" oninput="setZone('+i+',\'center_lng\',this.value)" placeholder="lng">'+
      '<input type="number" step="0.1" value="'+esc(z.radius_km)+'" oninput="setZone('+i+',\'radius_km\',this.value)" placeholder="km">'+
      '<button class="minix" onclick="rmZone('+i+')">×</button></div>';
  });
  disp+='<button class="miniadd" onclick="addZone()">+ Add delivery zone</button>';
  disp+='<h4 class="grp">Kitchen</h4>'+grid(
    fRow('Prep dispatch lead (min)',iNum('prep_dispatch_lead_min',s.prep_dispatch_lead_min,'int'))+
    fRow('Default cook time (min)',iNum('default_prep_minutes',s.default_prep_minutes,'int'))+
    fRow('Expedite radius (km)',iNum('batch_expedite_radius_km',s.batch_expedite_radius_km,'num','0.1'))
  );
  html+=sec('Dispatch & Kitchen','Routing engine and distance-driven kitchen timing.',disp);

  html+='<div style="margin:22px 0"><button class="savebtn" onclick="saveSettings()">Save settings</button></div>';

  // Connection (read-only)
  html+=sec('Connection (read-only)','',
    '<div class="kv">'
    +'<div class="k">Platform base URL</div><div>'+esc(h.base_url||'—')+'</div>'
    +'<div class="k">API key configured</div><div>'+(h.api_key_set?'✅ yes':'❌ no')+'</div>'
    +'<div class="k">Webhook secret set</div><div>'+(h.secret_set?'✅ yes':'❌ no (signatures NOT verified)')+'</div>'
    +'<div class="k">WhatsApp token</div><div>🔒 never shared with the POS</div>'
    +'</div>');
  html+='</div>';
  document.getElementById('view').innerHTML=html;
}
async function saveSettings(){
  var patch={},k;for(k in SS.s)patch[k]=SS.s[k];
  if(SS.name&&String(SS.name).trim())patch.name=String(SS.name).trim();
  var la=parseFloat(SS.lat);if(!isNaN(la))patch.lat=la;
  var ln=parseFloat(SS.lng);if(!isNaN(ln))patch.lng=ln;
  if(SS.noFixedHours){patch.open_hours={tz:'Asia/Dubai',days:{}};}
  else{var days={},i;for(i=0;i<7;i++){var dd=SS.hoursArr[i];if(dd.open)days[i]=[dd.from,dd.to];}patch.open_hours={tz:'Asia/Dubai',days:days};}
  flash('Saving…');
  var r=await fetch('/api/settings',{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)});
  var j={};try{j=await r.json();}catch(e){}
  if(r.status===200){flash('Settings saved ✓');renderSettings();}
  else{flash('Save failed: HTTP '+r.status+(j&&j.detail?' — '+j.detail:''));}
}

// ---- shared ----
function setConn(ok){const d=document.getElementById('conn');const l=document.getElementById('connlbl');
  d.className='dot '+(ok?'ok':'bad');l.textContent=ok?'connected':'offline';}
function setCount(id,n){const el=document.getElementById(id);if(!el)return;if(n>0){el.textContent=n;el.classList.remove('hide');}else el.classList.add('hide');}
function render(){
  if(TAB==='orders')loadOrders();
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
