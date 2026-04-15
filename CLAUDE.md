# Dinify Backend — Claude Code Context

## Project Overview
Dinify is a QR-code-based digital ordering and restaurant management platform
built for Uganda and mobile-money-first markets. Django/DRF backend on AWS EC2
with PostgreSQL on AWS RDS.

## Tech Stack
- Django / Django REST Framework
- PostgreSQL on AWS RDS (primary database)
- MongoDB Atlas (action logs and archiving only — currently unreachable from EC2)
- Apache / mod_wsgi on AWS EC2 (35.177.46.58)
- Repo: mugak1/dinify_backend_handover

## Current Implementation Status
- Menu module (all phases 1a–1d): ✅ Complete
- Dashboard endpoint: ✅ Exists at `api/v1/reports/restaurant/dashboard/`
  Do NOT create a new dashboard endpoint — it already exists
- Tables module: 🔄 In progress — backend endpoints for reservations,
  waitlist, and table_actions already exist
- Login 500 regression: ⚠️ Outstanding — Apache error logs needed

## Deployment Rules — CRITICAL
- Merging a PR to main automatically triggers GitHub Actions to pull code,
  run migrations, and restart Apache
- NEVER suggest manual `git pull`, `migrate`, or Apache restart — the
  pipeline handles everything
- Each feature must be on its own branch → PR → merge
- Never stack work on unmerged branches

## URL Structure
- `api/v1/restaurant-setup/` → RestaurantSetupEndpoint (catch-all) +
  dedicated endpoints for: preset-tags, upsell-config, reservations,
  waitlist, table-actions/<action>/
- `api/v1/reports/restaurant/<report_name>/` → RestaurantReportsEndpoint
- `api/v1/orders/` → v1 orders (urls.py)
- `api/v2/orders/` → v2 orders (v2_urls.py) — separate file, don't confuse
- `api/v1/kds/` → KDS endpoints (urls_kds.py) — separate file

## Endpoint Pattern — CRITICAL
New resource types get their own dedicated endpoint file in
`restaurants_app/endpoints/`, NOT added to the RestaurantSetupEndpoint
catch-all. Examples already following this pattern:
- `reservations.py`, `waitlist.py`, `table_actions.py`, `preset_tags.py`,
  `upsell_config.py`
Always register new endpoint files in `restaurants_app/urls.py` ABOVE
the catch-all `<str:config_detail>/` route.

## The Secretary Pattern — CRITICAL
- The `Secretary` class uses `EDIT_INFORMATION`
  (dinify_backend/configss/edit_information.py) to control which fields
  are editable via PUT requests
- Any new field that needs to be editable via PUT MUST be added to the
  appropriate list in EDIT_INFORMATION
- If omitted, Secretary silently ignores the field and returns
  "no changes detected"
- Current sections: `restaurants`, `restaurant_employee`, `menu_section`,
  `menu_item`, `table` (20+ fields), plus `EI_DINING_AREA` for dining areas
- Check this file before adding any editable field — it may already be there

## Monetary Fields — CRITICAL
- ALL monetary/financial fields must use `DecimalField`, never `FloatField`
- Never use `Decimal(float)` conversions or `int()` truncation in
  payment or financial logic

## MongoDB — Rules
- MongoDB Atlas is currently unreachable from EC2
- Affects only action logs and archiving — not core functionality
- `archive_record` and `save_action_log` have try/except wrappers — do not remove
- Do not add any new hard dependencies on MongoDB
- Exception: `mark_as_read` on notifications MUST remain synchronous
  because the endpoint needs its return value

## SMS / Yo Uganda
- Yo Uganda SMS gateway has DNS resolution issues on the production server
- All SMS dispatch must use `threading.Thread(daemon=True)` — never
  called synchronously (30-second timeout will block requests)

## Development Config
- `ENV=dev` must always be retained — hardcodes OTP to `1234` and skips
  SMS sending, essential for local development
- Never delete or disable this config

## Import Path Convention
- The correct config directory is `dinify_backend/configss/` (double-s)
- Referenced in 20+ imports — never rename or restructure this directory

## Key Model Defaults (not bugs)
- `first_time_menu_approval_decision` defaults to `'approve'`
- `first_time_menu_approval` defaults to `True`
- Do not revert these

## Key Serializer Notes
- `SerializerPublicGetMenuItem` includes `section` and `in_stock` —
  added deliberately for the diner menu. Do not remove them

## Existing Management Commands
- `optimize_images` in `restaurants_app/management/commands/` — resizes
  uploaded MenuItem images to 800px max. Do not recreate it

## Database
- `CONN_MAX_AGE: 600` for persistent DB connections — do not remove
- All migrations must be generated and included in PRs when models change

## Verification
Before raising any PR:
1. Confirm migrations are generated for any model changes
2. Confirm new editable fields are added to EDIT_INFORMATION
3. Confirm no synchronous SMS calls introduced (except where return value needed)
4. Confirm no new hard MongoDB dependencies
5. Confirm monetary fields use DecimalField
6. If adding a new endpoint, confirm it's registered in urls.py above the catch-all
