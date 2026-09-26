# Deployment

One VPS running Docker Compose. Keep it simple ([target-state §1](../architecture/target-state.md)):

```
Internet → Caddy (HTTPS, static/media) → web (Gunicorn/Django) ─┬─ PostgreSQL 16
                                          worker (Celery) ───────┤
                                          beat (Celery beat) ────┴─ Redis 7
```

## First deployment

1. A VPS with Docker (2 vCPU / 4 GB is plenty to start), with DNS for your domain (e.g. `app.dewmixhardware.com`) pointing at it.
2. `git clone` the repository and `cp .env.example deploy/.env.prod`, then fill in:
   - `DJANGO_SECRET_KEY`, `JWT_SIGNING_KEY`: two different values from `python -c "import secrets; print(secrets.token_urlsafe(50))"`
   - `DJANGO_ALLOWED_HOSTS=app.dewmixhardware.com,localhost` (`localhost` is used by the container health check)
   - `DJANGO_CSRF_TRUSTED_ORIGINS=https://app.dewmixhardware.com`
   - `SITE_DOMAIN=app.dewmixhardware.com`, and `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` (a long random password)
   - `CORS_ALLOWED_ORIGINS=https://dewmixhardware.com` (the static site), `SENTRY_DSN`, `METRICS_TOKEN`
   - Later: `MPESA_*`, `WHATSAPP_*`, `ANTHROPIC_API_KEY`, `AWS_*` (object storage) as each feature goes live
3. Start: `docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod up -d --build`
4. One-time setup:
   ```sh
   C="docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod"
   $C exec web python manage.py migrate
   $C run --rm static                                   # publish static files to Caddy
   $C exec web python manage.py setup_roles
   $C exec web python manage.py createsuperuser
   $C exec web python manage.py import_legacy_catalog   # 1,160 products + photos, safe to re-run
   ```
5. Check: `https://<domain>/health/ready` returns `{"status": "ok"}`, and log in at `/admin/`.
6. Install the nightly backup cron job ([backups.md](backups.md)).

The app refuses to start with missing or weak configuration. Read the error message; it names the variable.

## Deploying an update

```sh
git pull
$C build web worker beat
$C exec web python manage.py migrate      # migrations are backwards-compatible within a release
$C up -d web worker beat
$C run --rm static
```

Take a backup first if the release contains migrations (`deploy/backup.sh`). **Rollback:** `git checkout <previous tag>` and rebuild. If a migration must be undone, restore the pre-deploy backup ([runbooks.md](runbooks.md#restore)); don't hand-edit tables.

## Things that must stay true in production

- `DJANGO_SETTINGS_MODULE` is production (the image default).
- Only Caddy is exposed (ports 80/443). PostgreSQL and Redis are on the internal network only.
- `TRUSTED_PROXY_IPS` is Caddy's fixed address (set in the compose file), so audit logs record real client IPs.
- The M-Pesa callback URL is `https://<domain>/webhooks/mpesa/<MPESA_CALLBACK_TOKEN>/`, and the WhatsApp webhook is `https://<domain>/webhooks/whatsapp/`.
