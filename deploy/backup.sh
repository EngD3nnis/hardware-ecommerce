#!/bin/sh
# Nightly PostgreSQL backup (run from cron on the host). Keeps BACKUP_KEEP_DAYS days locally;
# copy the directory off-site too (see docs/operations/backups.md), a backup on the same disk is not a backup.
set -eu
COMPOSE="docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.prod"
DIR="${BACKUP_DIR:-/var/backups/dewmix}"
KEEP="${BACKUP_KEEP_DAYS:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DIR"
$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner' > "$DIR/dewmix-$STAMP.dump"
test -s "$DIR/dewmix-$STAMP.dump"
find "$DIR" -name 'dewmix-*.dump' -mtime "+$KEEP" -delete
echo "backup ok: $DIR/dewmix-$STAMP.dump ($(du -h "$DIR/dewmix-$STAMP.dump" | cut -f1))"
