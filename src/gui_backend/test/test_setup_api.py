"""The setup page's payload.

Mostly about one property: the page holds no second copy of the field
inventory. Labels, units, consequences, choices and the tier split all travel
with the values, so a field added in `asket_common` appears on the page and a
field removed stops appearing, with no JavaScript changing. The failure this
avoids is the one `mounting.yaml` already demonstrated — the thing on screen
and the thing in use drifting apart with nobody able to see it.
"""

import time

import pytest
from asket_common.setup_profile import (
    DEPLOYMENT_STALE_AFTER_MS,
    ENTERED,
    FIELD_BY_ID,
    FIELDS,
    MEASURED,
    TIER_DEPLOYMENT,
    TIER_VESSEL,
    SetupProfile,
    default_profile,
)
from fastapi.testclient import TestClient
from gui_backend.core import setup_api

NOW = 1_789_932_000_000


def test_every_field_is_sent():
    """Not a subset chosen here. A field the page never hears about is a field
    nobody can answer, and it would still be in the pre-flight's warnings."""
    doc = setup_api.payload(default_profile(), NOW)
    assert [f["id"] for f in doc["fields"]] == [spec.id for spec in FIELDS]


def test_the_order_is_the_order_the_inventory_declares():
    """`FIELDS` is ordered roughly by what blocks what, and that ordering is a
    judgement worth keeping. Sorting it in the page would discard it."""
    doc = setup_api.payload(default_profile(), NOW)
    assert [f["id"] for f in doc["fields"]] == [spec.id for spec in FIELDS]


def test_a_field_carries_why_it_matters_and_not_just_its_name():
    """"Sonar lever arm, forward of GNSS" tells somebody what to type and
    nothing about whether it is worth fetching a tape measure. The consequence
    sentence is the reason a value gets measured rather than guessed, so it
    travels with the field."""
    doc = setup_api.payload(default_profile(), NOW)
    tilt = next(f for f in doc["fields"] if f["id"] == "vessel.sonar.mounting.tilt_deg")
    assert tilt["consequence"]
    assert tilt["silently_corrupts"] is True
    assert tilt["units"] == "deg"
    assert tilt["label"] == FIELD_BY_ID[tilt["id"]].label


def test_choices_travel_with_the_field_that_offers_them():
    """A select whose options were written in the frontend is a select that can
    offer a value the vessel does not accept."""
    doc = setup_api.payload(default_profile(), NOW)
    side = next(f for f in doc["fields"] if f["id"] == "vessel.sonar.side")
    assert side["choices"] == ["port", "starboard"]
    assert side["supplied"] == "by_choice"


def test_both_tiers_are_described_in_one_place():
    doc = setup_api.payload(default_profile(), NOW)
    assert [t["id"] for t in doc["tiers"]] == [TIER_VESSEL, TIER_DEPLOYMENT]
    for tier in doc["tiers"]:
        assert tier["label"] and tier["blurb"]


def test_every_field_belongs_to_a_tier_the_page_was_told_about():
    """A field in a third tier would render into nothing at all."""
    doc = setup_api.payload(default_profile(), NOW)
    known = {t["id"] for t in doc["tiers"]}
    assert {f["tier"] for f in doc["fields"]} <= known


def test_what_the_vessel_is_using_is_sent_separately_from_what_was_answered():
    """A field's value and whether anybody ever confirmed that value are two
    questions, and the page shows both. One merged dict loses the second."""
    doc = setup_api.payload(default_profile(), NOW)
    assert doc["values"] == {}
    assert doc["effective"]["vessel.sonar.mounting.tilt_deg"] == 35.0


def test_an_answer_appears_with_its_provenance_and_its_date():
    profile = default_profile().set(
        "vessel.sonar.mounting.tilt_deg", 34.2,
        provenance=MEASURED, utc_ms=NOW - 1000, by="Auxence",
        note="tape measure, bow up",
    )
    doc = setup_api.payload(profile, NOW)
    entry = doc["values"]["vessel.sonar.mounting.tilt_deg"]
    assert entry["value"] == 34.2
    assert entry["provenance"] == MEASURED
    assert entry["set_by"] == "Auxence"
    assert entry["note"] == "tape measure, bow up"
    assert doc["effective"]["vessel.sonar.mounting.tilt_deg"] == 34.2


def test_the_headline_is_the_one_the_profile_computes():
    """Not a sentence assembled in the page. The ranking of "nothing at all"
    over "a guess that ruins a survey quietly" is a judgement with
    consequences, and it has to be the same judgement everywhere it is said."""
    profile = default_profile()
    assert setup_api.payload(profile, NOW)["headline"] == profile.headline()


def test_a_fresh_jetson_leads_with_what_has_no_default():
    doc = setup_api.payload(default_profile(), NOW)
    assert "no sensible default" in doc["headline"]
    first = doc["outstanding"][0]
    assert first["blocking"] is True


def test_outstanding_is_ranked_and_not_alphabetical():
    """An undifferentiated list is what the all-amber pre-flight already
    produces, to no effect."""
    doc = setup_api.payload(default_profile(), NOW)
    blocking = [o["blocking"] for o in doc["outstanding"]]
    assert blocking == sorted(blocking, reverse=True)


def test_an_answered_field_leaves_the_outstanding_list():
    spec = next(s for s in FIELDS if not s.has_default)
    profile = default_profile().set(spec.id, 1.0, provenance=ENTERED, utc_ms=NOW)
    doc = setup_api.payload(profile, NOW)
    assert spec.id not in {o["id"] for o in doc["outstanding"]}


# -- staleness -------------------------------------------------------------


def _deployment_field():
    return next(s for s in FIELDS if s.tier == TIER_DEPLOYMENT)


def test_a_fresh_deployment_answer_is_not_stale():
    spec = _deployment_field()
    profile = default_profile().set(spec.id, 1.0, provenance=ENTERED, utc_ms=NOW)
    assert setup_api.payload(profile, NOW)["stale"] == []


def test_a_deployment_answer_from_last_week_is_stale_with_its_date():
    spec = _deployment_field()
    set_at = NOW - 7 * DEPLOYMENT_STALE_AFTER_MS
    profile = default_profile().set(spec.id, 1.0, provenance=ENTERED, utc_ms=set_at)
    stale = setup_api.payload(profile, NOW)["stale"]
    assert [s["id"] for s in stale] == [spec.id]
    # The date, not just the fact. "Set on the 14th" is what lets somebody
    # decide; "stale" on its own is a thing to click past.
    assert stale[0]["set_utc_ms"] == set_at
    assert stale[0]["age_ms"] == NOW - set_at


def test_a_vessel_answer_never_goes_stale_however_old():
    """The point of the tiers. Mounting geometry measured in July is still true
    in September; a station position from July is a different beach."""
    spec = next(s for s in FIELDS if s.tier == TIER_VESSEL)
    profile = default_profile().set(
        spec.id, 1.0, provenance=MEASURED, utc_ms=NOW - 400 * DEPLOYMENT_STALE_AFTER_MS
    )
    assert setup_api.payload(profile, NOW)["stale"] == []


def test_the_threshold_is_sent_so_the_page_can_say_what_old_means():
    doc = setup_api.payload(default_profile(), NOW)
    assert doc["stale_after_ms"] == DEPLOYMENT_STALE_AFTER_MS


# -- the file ---------------------------------------------------------------


def test_an_unrecognised_field_id_is_reported_rather_than_dropped():
    """Almost always a typo in a value somebody did measure. Dropping it
    silently means the measurement never reaches the vessel and nobody finds
    out — which is exactly what `mounting.yaml`'s check exists to prevent."""
    profile = SetupProfile.from_dict(
        {"fields": {"vessel.sonar.mounting.tilt_dge": {"value": 34.2, "provenance": "measured"}}}
    )
    doc = setup_api.payload(profile, NOW)
    assert doc["unknown_fields"] == ["vessel.sonar.mounting.tilt_dge"]


def test_the_file_state_travels_with_the_document():
    doc = setup_api.payload(default_profile(), NOW, file_state={"path": "/x", "missing": True})
    assert doc["file"]["missing"] is True


def test_the_content_hash_is_the_profile_s_own():
    profile = default_profile().set("vessel.sonar.side", "port", provenance=ENTERED, utc_ms=NOW)
    assert setup_api.payload(profile, NOW)["content_hash"] == profile.content_hash()


def test_the_payload_is_json():
    """It is served as JSON, so a value that is not serialisable is a 500 on a
    beach rather than a type error here."""
    import json

    json.dumps(setup_api.payload(default_profile(), NOW))


# -- the route --------------------------------------------------------------


@pytest.fixture
def client(monkeypatch, tmp_path):
    from asket_sim.core.world import SimWorld, WorldConfig
    from gui_backend.core import setup_store
    from gui_backend.core.hub import Hub
    from gui_backend.core.server import create_app
    from gui_backend.core.sim_source import SimSource

    monkeypatch.setattr(setup_store, "DEFAULT_PATH", tmp_path / "asket_setup.yaml")
    hub = Hub(SimSource(SimWorld(WorldConfig())))
    with TestClient(create_app(hub, tiles_path=None)) as c:
        yield c


def test_the_route_serves_the_inventory_with_no_file_present(client):
    """A fresh Jetson. The page must be readable before anything has been
    saved, because that is the state it exists to get somebody out of."""
    doc = client.get("/api/setup").json()
    assert doc["values"] == {}
    assert len(doc["fields"]) == len(FIELDS)
    assert doc["file"]["missing"] is True
    assert "no sensible default" in doc["headline"]


def test_the_route_is_outside_the_subscription_machinery(client):
    """No profile header, no negotiation, no stream. Setup is needed most when
    the link is worst, and a page a `minimal` profile could negotiate away
    would vanish exactly when somebody is working out why it is minimal."""
    assert client.get("/api/setup").status_code == 200


def test_a_broken_file_is_reported_rather_than_raised(client, monkeypatch, tmp_path):
    """"There is no file" and "there is a file and it will not parse" send
    somebody to two different places. A 500 says neither, and the inventory
    behind it is still worth reading while the file is being fixed."""
    from gui_backend.core import setup_store

    (tmp_path / "asket_setup.yaml").write_text("fields: [this is not a mapping\n")
    response = client.get("/api/setup")
    assert response.status_code == 200
    doc = response.json()
    assert doc["file"].get("error")
    assert len(doc["fields"]) == len(FIELDS)
    assert setup_store.DEFAULT_PATH == tmp_path / "asket_setup.yaml"


def test_a_saved_answer_is_served_back(client, tmp_path):
    from gui_backend.core import setup_store

    profile = default_profile().set(
        "vessel.sonar.mounting.tilt_deg", 34.2,
        provenance=MEASURED, utc_ms=int(time.time() * 1000), by="Auxence",
    )
    setup_store.save(
        profile, path=tmp_path / "asket_setup.yaml",
        utc_ms=int(time.time() * 1000), by="Auxence",
    )

    doc = client.get("/api/setup").json()
    assert doc["values"]["vessel.sonar.mounting.tilt_deg"]["value"] == 34.2
    assert doc["effective"]["vessel.sonar.mounting.tilt_deg"] == 34.2
