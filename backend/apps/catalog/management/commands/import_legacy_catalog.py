import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalog.legacy import import_legacy_catalog
from apps.core.actors import Actor


class Command(BaseCommand):
    help = (
        "Import the static website catalogue (dewmix_source/) into the database. "
        "Idempotent and non-destructive: safe to re-run."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--source",
            default=str(Path(settings.BASE_DIR).parent / "dewmix_source"),
            help="Directory containing index.html and imgs/ (default: repository dewmix_source/).",
        )
        parser.add_argument("--dry-run", action="store_true", help="Report what would happen, change nothing.")
        parser.add_argument("--skip-images", action="store_true")
        parser.add_argument("--json", action="store_true", help="Print the full report as JSON.")

    def handle(self, *args, **options):
        source = Path(options["source"])
        if not (source / "index.html").is_file():
            raise CommandError(f"{source / 'index.html'} not found.")
        report = import_legacy_catalog(
            source,
            Actor.system("import_legacy_catalog"),
            dry_run=options["dry_run"],
            with_images=not options["skip_images"],
        )
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2, default=str))
            return
        prefix = "DRY RUN (nothing saved): " if report.dry_run else ""
        lines = [
            f"{prefix}{report.rows} products in source",
            f"  created {report.products_created}, unchanged {report.products_unchanged}, "
            f"differs from DB (left as is) {len(report.products_conflicting)}, rejected {len(report.rejected)}",
            f"  categories created {report.categories_created}",
            f"  images: {report.images_found} files, {report.images_stored} stored, "
            f"{report.images_attached} attached, {len(report.images_rejected)} rejected, "
            f"{len(report.images_missing)} products without image",
            f"  review items created {report.review_items_created}, change proposals {report.proposals_created}",
        ]
        for legacy_id, reason in report.rejected[:20]:
            lines.append(f"  rejected legacy id {legacy_id}: {reason}")
        for name, reason in report.images_rejected[:20]:
            lines.append(f"  rejected image {name}: {reason}")
        if report.products_conflicting:
            lines.append(f"  differs: legacy ids {report.products_conflicting[:30]}")
        self.stdout.write("\n".join(lines))
