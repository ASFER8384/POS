# Temp POS

A standalone **fake POS partner** for testing the WhatsApp restaurant platform's
partner integration — deployed as its own Render web service, exactly like a
real POS would run.

What it does:
1. **Receives** the platform's outbound webhooks at `POST /hooks/whatsapp`,
   verifies the HMAC signature, and remembers each event.
2. **Acts as the POS kitchen** — on `order.created` it calls the platform API
   (`X-API-Key`) to ack the order, mark it `preparing`, then `ready` (which
   fires dispatch on the platform).

Open `GET /` in a browser to see the last events it received.

## Deploy to Render

1. Push this folder to a new GitHub repo.
2. Render → **New → Web Service** → connect the repo. It auto-detects
   `render.yaml`, or set manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn app:app --host 0.0.0.0 --port $PORT`
   - Plan: Free
3. Set env vars in the Render dashboard:
   - `POS_BASE_URL` = `https://restaurant-whatsapp-service.onrender.com`
   - `POS_API_KEY` = the API key minted for the store under test
   - `POS_WEBHOOK_SECRET` = the same secret set in the store's partner config
   - `POS_AUTO_ADVANCE` = `true`
4. Deploy. Your public URL is `https://temp-pos-XXXX.onrender.com`.

## Wire it to the platform

On the platform, for the restaurant you'll order to, set:
- `partner_enabled` = `true`
- `partner_webhook_url` = `https://temp-pos-XXXX.onrender.com/hooks/whatsapp`
- `partner_webhook_secret` = same as `POS_WEBHOOK_SECRET` above
- and mint the API key you put in `POS_API_KEY`

## Test

Place a **WhatsApp order** on that restaurant. Then open the temp POS `GET /`:
you should see `order.created` (auto-advanced to ready), then
`order.rider_assigned`, `order.picked_up`, `order.delivered` as the order
progresses — each with `hmac_ok: true`.

## Run locally (optional)

```bash
pip install -r requirements.txt
POS_BASE_URL=http://127.0.0.1:8000 POS_API_KEY=... POS_WEBHOOK_SECRET=... \
  uvicorn app:app --host 0.0.0.0 --port 8799
```
