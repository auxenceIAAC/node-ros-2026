"""The checked-in setup inventory must be the Python one.

Twenty fields, each with a consequence sentence somebody wrote carefully, and
a tier that decides whether the value goes stale. Transcribed by hand into
JavaScript, that drifts on the first change, and the page then reviews a
system that does not exist — which is worse than no mock, because it is
reviewed and believed.

So the JSON is generated. This test is what makes that true.
"""

import json
from pathlib import Path

import pytest
from asket_common.setup_profile import FIELDS
from gui_backend.tools.export_setup_fields import build

INVENTORY_PATH = (
    Path(__file__).resolve().parents[2]
    / "asket_gui" / "src" / "lib" / "mock" / "setupFields.json"
)


def test_the_checked_in_inventory_matches_the_python():
    if not INVENTORY_PATH.is_file():
        pytest.skip(f"{INVENTORY_PATH} not present")
    on_disk = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
    assert on_disk == build(), (
        "src/asket_gui/src/lib/mock/setupFields.json is out of date. Regenerate it:\n"
        "  python3 -m gui_backend.tools.export_setup_fields "
        "--out src/asket_gui/src/lib/mock/setupFields.json"
    )


def test_the_export_is_the_inventory_and_not_the_answers():
    """Values are mock state; the inventory is not. A set of answers baked
    into the frontend would be a page that looks filled in on a vessel nobody
    has told anything."""
    exported = build()
    assert "values" not in exported
    assert "effective" not in exported
    assert len(exported["fields"]) == len(FIELDS)


def test_every_exported_field_carries_what_the_page_needs_to_rank_it():
    for spec in build()["fields"]:
        assert spec["label"], spec["id"]
        assert spec["consequence"], spec["id"]
        assert isinstance(spec["has_default"], bool), spec["id"]
        assert isinstance(spec["silently_corrupts"], bool), spec["id"]
        assert spec["tier"] in {t["id"] for t in build()["tiers"]}, spec["id"]


def test_the_shape_is_the_route_s_shape():
    """Through `field_dict`, not a second rendering of FieldSpec. A divergence
    here is a page that works in mock mode and not on the vessel."""
    from gui_backend.core import setup_api
    from asket_common.setup_profile import default_profile

    served = setup_api.payload(default_profile(), 0)
    assert build()["fields"] == served["fields"]
    assert build()["tiers"] == served["tiers"]
