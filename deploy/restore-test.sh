#!/bin/sh
# Prove a backup restores: load it into a throwaway PostgreSQL container, then run the app's
# integrity checks against it. Usage: deploy/restore-test.sh /var/backups/dewmix/dewmix-XXXX.dump [image]
set -eu
DUMP="$1"
IMAGE="${2:-dewmix-prod-web}"
NAME="dewmix-restore-test-$$"
docker network create "$NAME" >/dev/null
trap 'docker rm -f "$NAME-db" >/dev/null 2>&1; docker network rm "$NAME" >/dev/null 2>&1' EXIT
docker run -d --name "$NAME-db" --network "$NAME" -e POSTGRES_PASSWORD=restore -e POSTGRES_DB=restore postgres:16-alpine >/dev/null
until docker exec "$NAME-db" pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
sleep 2
docker exec -i "$NAME-db" pg_restore -U postgres -d restore --no-owner --exit-on-error < "$DUMP"
docker run --rm --network "$NAME" \
  -e DJANGO_SETTINGS_MODULE=config.settings.test \
  -e DATABASE_URL="postgres://postgres:restore@$NAME-db:5432/restore" \
  "$IMAGE" python manage.py shell -c "
from django.db.migrations.executor import MigrationExecutor
from django.db import connection
from apps.catalog.models import Product
from apps.sales.models import Order
from apps.inventory.services import reconcile
pending = MigrationExecutor(connection).migration_plan(MigrationExecutor(connection).loader.graph.leaf_nodes())
print('products', Product.objects.count(), 'orders', Order.objects.count())
print('unapplied migrations', len(pending))
mismatches = reconcile()
print('inventory ledger mismatches', len(mismatches))
assert not pending and not mismatches, 'RESTORE TEST FAILED'
print('RESTORE TEST PASSED')
"
