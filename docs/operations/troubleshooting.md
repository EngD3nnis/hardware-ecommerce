# Troubleshooting

Start with the **request id**. Every response has an `X-Request-ID` header and every API error body contains `request_id`. Search the logs for it (JSON logs, field `correlation_id`). Celery tasks started by that request carry the same id, and audit events store it in `correlation_id` (Admin → Audit events, search).

| Symptom | Check |
|---|---|
| App won't start, `ImproperlyConfigured` | The message names the missing/weak setting ([deployment.md](deployment.md)) |
| `/health/ready` returns 503 | `checks` shows which dependency: `docker compose … ps`, `logs postgres` / `logs redis` |
| Customer says an old quote link shows nothing | `GET /api/v1/catalog/products/legacy/?ids=…`. Archived/draft products are hidden by design |
| "Not enough stock" but shelves have it | Admin → Stock balances: check *reserved* (held by orders) and *damaged*. Then Stock movements for that product, which tell the whole story. Correct with a Stock adjustment or a Stock count, never by editing balances |
| Reconciliation incident (inventory mismatch) | Someone or something changed balances outside the service. Compare the incident details with Stock movements, fix with a Stock count, and find the cause (raw SQL? a bug?) |
| M-Pesa payment not showing | Admin → Inbound payment events: RECEIVED (task not run: the check re-processes it within 15 min, or use *Process again*), IGNORED (no matching request/duplicate), FAILED (e.g. amount mismatch: verify in the M-Pesa portal and record manually). No event at all means the callback never arrived: check `MPESA_CALLBACK_BASE_URL`/token and Safaricom |
| Customer didn't get a WhatsApp message | Admin → Outbound messages: no row means the customer hasn't opted in (by design). FAILED: read the error, fix, *Retry* |
| Agent did something odd | Admin → Agent runs: every model call and tool call, with inputs and outputs. Disable it in `/ops/` while you investigate |
| Everything automated must stop now | `/ops/` → **DISABLE ALL AUTOMATIONS**, or set `AUTOMATION_HARD_DISABLE=1` and restart the worker |
| Spreadsheet import FAILED | The batch's *error* field says why (usually data changed since validation): *Validate again*, then approve |

Useful commands:

```sh
C="docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod"
$C logs -f web worker beat | grep <request-id>
$C exec web python manage.py shell -c "from apps.inventory.services import reconcile; print(reconcile())"
$C exec web python manage.py showmigrations | grep '\[ \]'
```
