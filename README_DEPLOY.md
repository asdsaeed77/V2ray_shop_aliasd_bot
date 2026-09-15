# ✨ V2RAY SHOP BOT — PostgreSQL Production

Production Telegram V2Ray shop bot for Render Web Service using **PostgreSQL** for persistent data.

## Render settings

- Root Directory: blank if these files are at repository root
- Build Command: `pip install -r requirements.txt`
- Start Command: `python v2ray_shop_bot.py`

## Required environment variables

- `BOT_TOKEN`
- `ADMIN_IDS`
- `DATABASE_URL` — PostgreSQL connection string from Render PostgreSQL
- `WEBHOOK_SECRET`
- `API_KEY`

Optional payment settings:
- `CARD_NUMBER`
- `CARD_OWNER`

`WEBHOOK_BASE_URL` can normally be left blank because the bot falls back to Render's external URL.

## PostgreSQL setup on Render

1. Create a PostgreSQL database in Render.
2. Copy its **Internal Database URL** into the web service's `DATABASE_URL` environment variable.
3. Remove the old `DATABASE_PATH` variable if it exists.
4. Redeploy the web service.
5. Open `/health` and `/ready` to verify the service and database.

The bot creates its PostgreSQL tables automatically on first startup. Do not set `SEED_DEMO=1` in production.

## Persistence

User accounts, balances, orders, inventory, coupons, tickets, favorites, audit logs and settings are stored in PostgreSQL and survive web-service redeploys/restarts. Database backups should also be configured on the PostgreSQL service.

The admin backup button creates a JSON data snapshot instead of an SQLite file.

## Important

The bot does not automatically provision users on a V2Ray/Xray panel. Admin-uploaded configs are delivered from the bot inventory. A panel/API integration would be a separate feature.
