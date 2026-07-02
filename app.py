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
async function loadOrders(){
  const r=await fetch('/state');const j=await r.json();LAST_ORDERS=j.orders||[];
  setConn(true);
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
function renderRiders(){
  const riders=LAST_ORDERS.filter(t=>t.rider).map(t=>({name:t.rider.name,phone:t.rider.phone,order:t.order_number||t.order_id,status:t.delivery_status,cod:t.cod_collected,late:t.late}));
  document.getElementById('meta').textContent=riders.length+' active delivery(ies)';
  document.getElementById('view').innerHTML = riders.length? '<table><thead><tr><th>Rider</th><th>Phone</th><th>Order</th><th>Status</th><th>COD collected</th></tr></thead><tbody>'
    +riders.map(r=>'<tr><td>'+esc(r.name)+(r.late?' ⚠️':'')+'</td><td>'+esc(r.phone||'')+'</td><td>#'+esc(r.order)+'</td><td>'+badge(r.status||'assigned')+'</td><td>'+(r.cod!=null?money(r.cod):'—')+'</td></tr>').join('')
    +'</tbody></table>' : '<div class="empty">No riders on delivery right now.</div>';
}

// ---- SETTINGS ----
async function renderSettings(){
  document.getElementById('view').innerHTML='<div class="empty">Loading…</div>';
  let store={};try{store=await (await fetch('/api/store')).json();}catch(e){}
  let h={};try{h=await (await fetch('/health')).json();}catch(e){}
  document.getElementById('meta').textContent='';
  document.getElementById('view').innerHTML='<div class="kv">'
    +'<div class="k">Store</div><div>'+esc(store.name||'—')+'</div>'
    +'<div class="k">WhatsApp number</div><div>'+esc(store.phone||'—')+'</div>'
    +'<div class="k">POS store id</div><div>'+esc(store.pos_store_id||'—')+'</div>'
    +'<div class="k">Partner enabled</div><div>'+(store.partner_enabled?'✅ yes':'❌ no')+'</div>'
    +'<div class="k">Order push mode</div><div>'+esc(store.pos_order_push_mode||'—')+'</div>'
    +'<div class="k">Platform base URL</div><div>'+esc(h.base_url||'—')+'</div>'
    +'<div class="k">API key configured</div><div>'+(h.api_key_set?'✅ yes':'❌ no')+'</div>'
    +'<div class="k">Webhook secret set</div><div>'+(h.secret_set?'✅ yes':'❌ no (signatures NOT verified)')+'</div>'
    +'</div>';
}

// ---- shared ----
function setConn(ok){const d=document.getElementById('conn');const l=document.getElementById('connlbl');
  d.className='dot '+(ok?'ok':'bad');l.textContent=ok?'connected':'offline';}
function setCount(id,n){const el=document.getElementById(id);if(!el)return;if(n>0){el.textContent=n;el.classList.remove('hide');}else el.classList.add('hide');}
function render(){
  if(TAB==='orders')loadOrders();
  else if(TAB==='chat'){renderChat();loadConvs();}
  else if(TAB==='customers')renderCustomers();
  else if(TAB==='riders'){renderRiders();}
  else if(TAB==='settings')renderSettings();
}
// polling loops
loadOrders();loadConvs();render();
setInterval(loadOrders,3000);
setInterval(()=>{loadConvs();if(TAB==='chat'&&ACTIVE_CONV!=null)loadThread();},3000);
</script></body></html>"""
