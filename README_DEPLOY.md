# ✨ V2RAY SHOP BOT

Production-oriented Telegram V2Ray shop bot for Render Web Service.

## Files
- `v2ray_shop_bot_production.py` — single-file application
- `requirements.txt` — pinned Python dependencies
- `render.yaml` — Render Blueprint
- `.env.example` — environment variable template

## Deploy
1. Put these files in the root of your GitHub repository.
2. In Render, create the service from the repository / Blueprint.
3. Set `BOT_TOKEN`, `ADMIN_IDS`, `WEBHOOK_SECRET`, `API_KEY`, `CARD_NUMBER`, and `CARD_OWNER`.
4. Keep `DATABASE_PATH=/var/data/v2ray_shop.db`.
5. Deploy and check `/health`.
6. Open the bot in Telegram and run `/start`.
7. As admin, add real configs with `/addconfig PRODUCT_ID`.
8. Do not enable `SEED_DEMO` in production.

## Important
This build uses SQLite on a Render persistent disk and is intentionally configured for one web-service instance. If you later need multiple instances/high traffic, migrate the database to PostgreSQL rather than sharing SQLite between instances.

The bot does not connect to a V2Ray/Xray control panel automatically; it delivers inventory configs that the admin has uploaded. For true panel/API provisioning, a separate panel integration is required.
