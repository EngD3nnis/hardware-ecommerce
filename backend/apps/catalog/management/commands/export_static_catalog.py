from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.catalog.static_site import export_catalog


class Command(BaseCommand):
    help = "Regenerate the CATALOG data in dewmix_source/index.html and products.html from the database."

    def add_arguments(self, parser):
        parser.add_argument("--site-dir", default=str(Path(settings.BASE_DIR).parent / "dewmix_source"))
        parser.add_argument("--write", action="store_true", help="Actually modify the files (default: report only).")

    def handle(self, *args, **options):
        site = Path(options["site_dir"])
        if not (site / "index.html").is_file():
            raise CommandError(f"{site / 'index.html'} not found")
        report = export_catalog(site, write=options["write"])
        verb = "Updated" if options["write"] else "Would update"
        self.stdout.write(f"{report.products} products. {verb}: {report.files_changed or 'nothing (already in sync)'}")
        if report.without_website_id:
            self.stdout.write(
                f"{len(report.without_website_id)} active products have no website id/photo and are not on the "
                f"static site: {report.without_website_id[:20]}"
            )
