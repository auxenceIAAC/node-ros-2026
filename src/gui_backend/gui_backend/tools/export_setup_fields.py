"""Export the setup field inventory as JSON, for the frontend mock.

Mock mode is how this page gets reviewed, and the thing most worth reviewing
is what it says to somebody who has arrived on a beach with nothing filled in.
That sentence is generated from the inventory — which fields have no sensible
default, which are on a guess that can ruin a survey quietly — so the mock
needs the inventory, and in mock mode there is no Python.

Generated and checked in rather than transcribed, exactly as
`streamPolicy.json` is, because a hand-written copy of twenty fields and their
consequence sentences would drift on the first change and the page would then
be a review of a system that does not exist.

    python3 -m gui_backend.tools.export_setup_fields \
        --out src/asket_gui/src/lib/mock/setupFields.json

Values are *not* here. This is the inventory only: what can be answered, and
what it costs to get wrong. The answers are mock state, and the real page gets
them from `/api/setup`.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gui_backend.core.setup_api import field_dict, tier_dicts

from asket_common.setup_profile import DEPLOYMENT_STALE_AFTER_MS, FIELDS


def build() -> dict:
    return {
        "_comment": (
            "GENERATED from asket_common/setup_profile.py by "
            "gui_backend/tools/export_setup_fields.py. Do not edit by hand — "
            "test_mock_setup_fields.py fails if this drifts from the Python."
        ),
        # Through `field_dict` rather than a second rendering of FieldSpec, so
        # the mock sees the identical shape the route serves. A divergence
        # here would be a page that works in mock mode and not on the vessel.
        "fields": [field_dict(spec) for spec in FIELDS],
        "tiers": tier_dicts(),
        "stale_after_ms": DEPLOYMENT_STALE_AFTER_MS,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(build(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
