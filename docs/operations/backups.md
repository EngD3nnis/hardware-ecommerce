# Backups & recovery

**A backup that has never been restored is not a proven backup.** The restore test below is part of the routine.

## What to back up

| Data | How | Frequency |
|---|---|---|
| PostgreSQL (all business data) | `deploy/backup.sh`: `pg_dump` custom format, keeps 14 days locally | Nightly (cron) + before each deploy with migrations |
| Off-site copy | Sync `/var/backups/dewmix` to object storage (e.g. `rclone sync /var/backups/dewmix r2:dewmix-backups`) | Nightly, after the dump |
| Product images (`media` volume, or the bucket) | Object storage: enable bucket versioning. Local volume: `docker run --rm -v dewmix-prod_media:/m -v /var/backups/dewmix:/b alpine tar czf /b/media-$(date +%F).tgz -C /m .` | Weekly |
| Configuration | `deploy/.env.prod` in a password manager (it holds the secrets) | On change |

Redis holds only queues, caches and rate-limit counters. It needs no backup: queued work is re-derivable, and the payment/message outboxes live in PostgreSQL.

Example cron (host):

```
30 2 * * * cd /opt/dewmix && deploy/backup.sh >> /var/log/dewmix-backup.log 2>&1 && rclone sync /var/backups/dewmix r2:dewmix-backups
```

## Monthly restore test

```sh
deploy/restore-test.sh /var/backups/dewmix/<latest>.dump dewmix-prod-web
```

It restores into a throwaway PostgreSQL container, then checks that the data is readable, no migrations are missing, and the inventory ledger reconciles. It must print `RESTORE TEST PASSED`. Record the date and result (e.g. in an internal task).

This script was run against a real dump during development (1,160 products restored, ledger clean).

## Disaster recovery (server lost)

1. New VPS with Docker, clone the repository, restore `deploy/.env.prod` from the password manager.
2. `up -d postgres redis`, then load the latest off-site dump:
   `docker compose … exec -T postgres pg_restore -U $POSTGRES_USER -d $POSTGRES_DB --clean --if-exists --no-owner < dewmix-XXXX.dump`
3. Restore media (bucket: nothing to do; volume: untar into the `media` volume).
4. `up -d`, `run --rm static`, point DNS at the new server, and check `/health/ready`.
5. **Payments:** compare M-Pesa statements since the backup time with the Payments admin, and record any missing payments manually. Anything recorded after the last backup is lost otherwise.
