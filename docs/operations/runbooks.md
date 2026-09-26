# Runbooks

Short procedures for recurring and exceptional tasks. Every one of them has a manual path that works with all automations switched off.

## Daily (shop)
1. Admin → **Quotations** (status Draft): price the "on request" lines, then *Mark as sent*. Use **Open WhatsApp** to send the quotation from your phone if the WhatsApp API isn't live.
2. Admin → **Inbound messages** (not handled): reply to anything the sales agent escalated.
3. `/ops/`: approvals waiting, open incidents, open tasks.

## First stock take (go-live)
1. Admin → Stock counts → Add: location Kenol shop, tick **Include all active products** (optionally attach a `sku,counted` CSV).
2. Fill in counted quantities (leave uncounted lines empty), then *Complete count*. This needs the complete-count permission.
3. Set reorder points on fast movers (Admin → Stock balances).

## Receive a delivery
Purchase orders → the order → **Receive goods** → enter quantities → *Record receipt*. Stock and last cost update, and double-submitting records nothing twice.

## Take a payment manually (cash / paybill / bank)
Admin → Payments → Add: choose the order, method and amount, and the M-Pesa/bank reference (required, and it must be unique).

## Refund / cancel a paid order
1. Send the money back (M-Pesa reversal or cash).
2. Admin → Refunds → Add, with the reference and reason. This needs *record refunds*.
3. Orders → *Cancel order*. Reserved stock is released. If goods were already dispatched, book them back with a Stock adjustment *Customer return*.

## Approve an agent's request
`/ops/` → the request → review the proposed values → Approval requests → *Approve and execute* or *Reject*. The action runs as you.

## Turn an agent on
Set `ANTHROPIC_API_KEY` → `/ops/` → *Enable* one agent with a small daily budget → watch Admin → Agent runs for a few days.

## Emergency: stop all automation
`/ops/` → **DISABLE ALL AUTOMATIONS** (takes effect within ~10 s). If the admin is unreachable, set `AUTOMATION_HARD_DISABLE=1` in `.env.prod` and `up -d worker beat web`. The shop keeps working normally.

## Rotate a secret
Change it in `deploy/.env.prod`, then `up -d web worker beat`:
- `JWT_SIGNING_KEY`: API clients log in again.
- `DJANGO_SECRET_KEY`: staff log in again. Use `SECRET_KEY_FALLBACKS` to phase it in.
- `MPESA_CALLBACK_TOKEN`: update the callback URL at Safaricom at the same time.

## Restore
[backups.md](backups.md#disaster-recovery-server-lost). To roll back a bad release with a migration: stop web/worker/beat, restore the pre-deploy dump with `pg_restore --clean`, check out the previous release, and start again.

## Update the static website from the database
`python manage.py export_static_catalog` (reports), then `--write`, then upload `index.html` and `products.html` to the host as before.
