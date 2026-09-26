import json
import shutil
from pathlib import Path

import pytest
from django.conf import settings

from apps.catalog import services
from apps.catalog.static_site import export_catalog
from apps.catalog.tests.factories import CategoryFactory, ProductFactory

pytestmark = pytest.mark.django_db
SITE = Path(settings.BASE_DIR).parent / "dewmix_source"


@pytest.fixture
def site(tmp_path):
    for name in ("index.html", "products.html"):
        shutil.copy(SITE / name, tmp_path / name)
    return tmp_path


def read_catalog(path, marker):
    html = path.read_text(encoding="utf-8")
    data, _ = json.JSONDecoder().raw_decode(html, html.find(marker) + len(marker))
    return data


def test_export_rewrites_both_files_from_database(site, system_actor):
    taps = CategoryFactory(name="Taps & Faucets")
    basin = CategoryFactory(name="Basin Taps", parent=taps)
    p = ProductFactory(sku="DWX-0001", legacy_id=1, name="Basin Tap <Chrome>", category=basin)
    ProductFactory(sku="NEW-1", name="No website id")
    report = export_catalog(site, write=True)
    assert report.products == 1 and report.without_website_id == ["NEW-1"]
    for name, marker in (("index.html", "const CATALOG="), ("products.html", "var CATALOG=")):
        assert read_catalog(site / name, marker) == [
            {"id": 1, "sku": "DWX-0001", "cat": "Taps & Faucets", "sub": "Basin Taps", "name": "Basin Tap <Chrome>"}
        ]
    # the rest of the page is untouched
    assert (
        (site / "index.html")
        .read_text(encoding="utf-8")
        .endswith((SITE / "index.html").read_text(encoding="utf-8")[-500:])
    )
    services.update_product(p, {"name": "Basin Tap Chrome"}, system_actor)
    assert export_catalog(site, write=False).files_changed == ["index.html", "products.html"]
    export_catalog(site, write=True)
    assert export_catalog(site, write=False).files_changed == []
