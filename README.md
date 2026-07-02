# Temp POS — Ops Console

A standalone **fake POS partner** for testing the WhatsApp restaurant platform's
partner integration — deployed as its own Render web service, exactly like a
real POS (e.g. Cratis) would run. It mirrors the platform's **OPS dashboard**
layout: a left sidebar with tabs.

## Tabs

| Tab | What it shows | Platform APIs used |
|-----|---------------|--------------------|
| **Orders** | Live kitchen tickets from `order.*` webhooks; Accept / Preparing / Ready ▶ dispatch / Cancel | `order.*` webhook → `POST /partner/orders/{id}/ack`, `POST /partner/orders/{id}/status` |
| **Chat** | Every WhatsApp thread; reply as the POS agent (auto-takeover) or hand back to the bot | `GET /partner/conversations`, `GET/POST /partner/conversations/{id}/messages`, `POST /partner/conversations/{id}/takeover` |
| **Customers** | The store's customer book | `GET /partner/customers` |
| **Riders** | Who's carrying what right now (derived from live tickets) | from `order.rider_assigned` / `picked_up` / `delivered` |
| **Settings** | Store identity, integration flags, this POS's config | `GET /partner/store` + `/health` |

The API key lives **only on this server**; the browser talks to local `/api/*`
proxy routes, so the key is never exposed to the page.

## The two directions (how it talks to the platform)

- **Platform → POS**: outbound webhooks arrive at `POST /hooks/whatsapp`,
  HMAC-signed with `X-Partner-Signature` (verified with `POS_WEBHOOK_SECRET`).
- **POS → Platform**: every action calls the platform API with
  `X-API-Key: <POS_API_KEY>` over HTTPS.

## What a real POS MUST implement (mandatory)

1. **Verify the HMAC signature** on every webhook (`X-Partner-Signature`).
2. **Ack `order.created`** (`POST /partner/orders/{id}/ack`).
3. **Push kitchen status** back (preparing → ready → cancelled).
4. **Reconcile COD** (show `cod_due` vs `cod_collected`).

Everything else is **value-add**: chat takeover, rider view, incremental
customer sync.

## Deploy to Render

1. Push this folder to a GitHub repo.
2. Render → **New → Web Service** → connect the repo (auto-detects `render.yaml`),
   or set manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - Plan: Free
3. Set env vars:
   - `POS_BASE_URL` = `https://restaurant-whatsapp-service.onrender.com`
   - `POS_API_KEY` = the API key minted for the store under test
   - `POS_WEBHOOK_SECRET` = the same secret set in the store's partner config
4. Deploy. Your public URL is `https://temp-pos-XXXX.onrender.com`.

## Wire it to the platform

For the restaurant you'll order to, set (Settings → integration, or it's
auto-provisioned on Meta connect):
- `partner_enabled` = `true`
- `partner_webhook_url` = `https://temp-pos-XXXX.onrender.com/hooks/whatsapp`
- `partner_webhook_secret` = same as `POS_WEBHOOK_SECRET` above
- and mint the API key you put in `POS_API_KEY`

## Test

Place a **WhatsApp order** on that restaurant, then open the POS `GET /`:
- **Orders** shows the ticket; click through Accept → Preparing → Ready
  (fires dispatch), and watch `rider_assigned` → `picked_up` → `delivered`.
- **Chat** shows the customer's thread — reply and watch it arrive on WhatsApp;
  the takeover pill stops the bot from answering.

## Run locally

```bash
pip install -r requirements.txt
POS_BASE_URL=http://127.0.0.1:8000 POS_API_KEY=... POS_WEBHOOK_SECRET=... \
  uvicorn app:app --host 0.0.0.0 --port 8799
```
