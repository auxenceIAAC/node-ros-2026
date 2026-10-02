"""The setup page's file, laid over the package's own mounting config.

This is the piece that was missing: the page could write a value and the node
would carry on using `mounting.yaml`, so the fingerprint never moved and a
reload could never confirm. It is also the piece with the most dangerous
failure available — an overlay that silently reverted a measurement would
destroy exactly the numbers the page exists to collect — so most of this file
is about what must *not* happen.
"""

import pytest
import yaml
from asket_common.setup_profile import (
    CAPTURED,
    ENTERED,
    FIELD_BY_ID,
    MEASURED,
    default_profile,
)
from omniscan_bridge.core.geometry import SonarMounting
from omniscan_bridge.core.mounting import (
    SETUP_FIELD_FOR_GEOMETRY,
    SETUP_FIELD_FOR_SIDE,
    geometry_fingerprint,
    load_mounting,
)

NOW = 1_789_932_000_000
TILT = "vessel.sonar.mounting.tilt_deg"


@pytest.fixture
def base(tmp_path):
    """A `mounting.yaml` as the package ships one: unmeasured defaults."""
    path = tmp_path / "mounting.yaml"
    path.write_text(yaml.safe_dump({
        "tilt_deg": 35.0, "yaw_deg": 0.0, "pitch_deg": 0.0,
        "lever_x_m": -0.20, "lever_y_m": -0.35, "lever_z_m": -0.15,
        "side": "starboard", "measured": False,
    }))
    return path


def write_setup(tmp_path, profile):
    path = tmp_path / "asket_setup.yaml"
    path.write_text(yaml.safe_dump(profile.to_dict()))
    return path


# -- the field map ---------------------------------------------------------


def test_every_mapped_setup_field_exists():
    """The map is written out rather than derived from a string prefix, so a
    field renamed in `asket_common` has to fail here. Derived, it would quietly
    stop applying — which looks exactly like a reload that did not work."""
    for field_id in SETUP_FIELD_FOR_GEOMETRY.values():
        assert field_id in FIELD_BY_ID, field_id
    assert SETUP_FIELD_FOR_SIDE in FIELD_BY_ID


def test_every_geometry_field_is_mapped():
    """A geometry value with no setup field is one the page cannot fix, which
    would send somebody back to SSH for that one number."""
    assert set(SETUP_FIELD_FOR_GEOMETRY) == {
        "tilt_deg", "yaw_deg", "pitch_deg", "lever_x_m", "lever_y_m", "lever_z_m",
    }


# -- the ordinary cases ----------------------------------------------------


def test_no_setup_file_leaves_the_base_config_alone(base, tmp_path):
    """The state of every vessel nobody has set up from the page. Not a fault,
    and it must not report as one."""
    mounting, prov = load_mounting(base, tmp_path / "nothing.yaml")
    assert mounting.tilt_deg == 35.0
    assert prov.overlay.missing is True
    assert prov.overlay.error == ""
    assert prov.overlay.applied == []


def test_an_answered_value_overrides_the_base_file(base, tmp_path):
    profile = default_profile().set(TILT, 34.2, provenance=MEASURED, utc_ms=NOW)
    mounting, prov = load_mounting(base, write_setup(tmp_path, profile))

    assert mounting.tilt_deg == 34.2
    assert prov.overlay.applied == ["tilt_deg"]
    # Untouched fields still come from the base file.
    assert mounting.lever_x_m == -0.20


def test_an_unanswered_field_does_not_override_anything(base, tmp_path):
    """The failure that would matter most. Every field carries a shipped
    default inside the profile, so taking values without checking provenance
    would mean that saving *anything* on the page silently reverted every
    measurement in `mounting.yaml` — the page would appear to work while
    destroying the numbers it exists to collect."""
    base.write_text(yaml.safe_dump({
        "tilt_deg": 31.5, "lever_x_m": -0.42, "side": "port", "measured": True,
        "measured_by": "Auxence",
    }))
    # One unrelated field answered; everything else on its default.
    profile = default_profile().set(
        "vessel.sonar.mounting.yaw_deg", 1.5, provenance=MEASURED, utc_ms=NOW
    )
    mounting, prov = load_mounting(base, write_setup(tmp_path, profile))

    assert mounting.tilt_deg == 31.5, "the measured tilt was reverted to a default"
    assert mounting.lever_x_m == -0.42
    assert mounting.side == "port"
    assert mounting.yaw_deg == 1.5
    assert prov.overlay.applied == ["yaw_deg"]


def test_a_setup_file_that_answers_nothing_changes_nothing(base, tmp_path):
    mounting, prov = load_mounting(base, write_setup(tmp_path, default_profile()))
    assert mounting == SonarMounting(
        tilt_deg=35.0, yaw_deg=0.0, pitch_deg=0.0,
        lever_x_m=-0.20, lever_y_m=-0.35, lever_z_m=-0.15, side="starboard",
    )
    # Found, but with nothing applied. Distinct from there being no file,
    # because "the overlay is there and said nothing about the sonar" is a
    # different thing to report.
    assert prov.overlay.found is True
    assert prov.overlay.applied == []


def test_the_side_can_be_set_from_the_page(base, tmp_path):
    profile = default_profile().set(
        SETUP_FIELD_FOR_SIDE, "port", provenance=ENTERED, utc_ms=NOW
    )
    mounting, prov = load_mounting(base, write_setup(tmp_path, profile))
    assert mounting.side == "port"
    assert "side" in prov.overlay.applied


def test_the_whole_geometry_can_come_from_the_page(base, tmp_path):
    """A vessel with no `mounting.yaml` at all, set up entirely from the page.
    This is what a club member with no SSH access actually has."""
    profile = default_profile()
    for name, field_id in SETUP_FIELD_FOR_GEOMETRY.items():
        profile = profile.set(field_id, 1.25, provenance=MEASURED, utc_ms=NOW, by="A")
    mounting, prov = load_mounting(None, write_setup(tmp_path, profile))

    assert mounting.tilt_deg == 1.25
    assert len(prov.overlay.applied) == 6
    assert prov.measured is True


# -- the fingerprint, which is what makes a reload confirmable -------------


def test_the_fingerprint_moves_when_the_overlay_changes_the_geometry(base, tmp_path):
    """The whole point. Before this, the page could write a value and the node
    would report the same digest forever, so `mounting_reloaded` could never
    confirm and the save path stopped at "saved"."""
    _, before = load_mounting(base, tmp_path / "nothing.yaml")

    profile = default_profile().set(TILT, 34.2, provenance=MEASURED, utc_ms=NOW)
    after_mounting, after = load_mounting(base, write_setup(tmp_path, profile))

    assert after.fingerprint != before.fingerprint
    # And it is the digest of what is actually in use, not of either file.
    assert after.fingerprint == geometry_fingerprint(after_mounting)


def test_the_fingerprint_is_the_one_the_page_can_predict(base, tmp_path):
    """The page knows the geometry it wrote, so it can compute the digest it
    expects and wait for the node to report that exact one. A digest of
    anything else would confirm a change that did not happen."""
    profile = default_profile()
    values = {"tilt_deg": 34.2, "yaw_deg": 1.0, "pitch_deg": -0.5,
              "lever_x_m": -0.21, "lever_y_m": -0.36, "lever_z_m": -0.16}
    for name, value in values.items():
        profile = profile.set(
            SETUP_FIELD_FOR_GEOMETRY[name], value, provenance=MEASURED, utc_ms=NOW
        )
    _, prov = load_mounting(base, write_setup(tmp_path, profile))

    assert prov.fingerprint == geometry_fingerprint(SonarMounting(**values))


def test_a_cosmetic_edit_does_not_move_the_fingerprint(base, tmp_path):
    """Changing a note does not move the transducer. A confirmation that fired
    on that would train people to ignore it."""
    first = default_profile().set(
        TILT, 34.2, provenance=MEASURED, utc_ms=NOW, by="A", note="tape"
    )
    second = default_profile().set(
        TILT, 34.2, provenance=MEASURED, utc_ms=NOW + 9999, by="B", note="laser"
    )
    _, a = load_mounting(base, write_setup(tmp_path, first))
    _, b = load_mounting(base, write_setup(tmp_path, second))
    assert a.fingerprint == b.fingerprint


# -- measured, which is now earned per field -------------------------------


def test_one_measured_field_does_not_make_the_geometry_measured(base, tmp_path):
    """A tilt measured on the page with the lever arm still on an unchecked
    default is not a measured geometry, and the pre-flight's WARN is right to
    stay up."""
    profile = default_profile().set(TILT, 34.2, provenance=MEASURED, utc_ms=NOW)
    _, prov = load_mounting(base, write_setup(tmp_path, profile))
    assert prov.measured is False


def test_all_six_measured_clears_the_provisional_flag(base, tmp_path):
    """And this is the thing that gets `measured: false` cleared without
    anybody opening an SSH session — which it has not been for two weeks."""
    profile = default_profile()
    for field_id in SETUP_FIELD_FOR_GEOMETRY.values():
        profile = profile.set(
            field_id, 1.0, provenance=MEASURED, utc_ms=NOW, by="Auxence", note="tape"
        )
    _, prov = load_mounting(base, write_setup(tmp_path, profile))
    assert prov.measured is True
    assert prov.provisional is False
    assert prov.measured_by == "Auxence"
    assert prov.notes == "tape"


def test_entered_is_not_measured(base, tmp_path):
    """"Typed in 35" and "measured 35" are the same number and different
    claims. Only the second clears the warning, which is the whole reason the
    two provenances exist."""
    profile = default_profile()
    for field_id in SETUP_FIELD_FOR_GEOMETRY.values():
        profile = profile.set(field_id, 1.0, provenance=ENTERED, utc_ms=NOW)
    mounting, prov = load_mounting(base, write_setup(tmp_path, profile))
    assert mounting.tilt_deg == 1.0, "the value should still apply"
    assert prov.measured is False, "but it must not count as measured"


def test_a_measured_base_file_survives_an_unrelated_overlay(base, tmp_path):
    """Somebody measured the hull properly months ago and wrote it into
    `mounting.yaml`. Setting the sonar's side on the page must not demote
    that."""
    base.write_text(yaml.safe_dump({
        "tilt_deg": 31.5, "measured": True, "measured_by": "Auxence",
    }))
    profile = default_profile().set(
        SETUP_FIELD_FOR_SIDE, "port", provenance=ENTERED, utc_ms=NOW
    )
    _, prov = load_mounting(base, write_setup(tmp_path, profile))
    assert prov.measured is True


def test_a_captured_value_is_not_a_measurement(base, tmp_path):
    """Nothing captures a lever arm, but the rule has to hold for whatever
    provenance arrives rather than for the two somebody thought of."""
    profile = default_profile()
    for field_id in SETUP_FIELD_FOR_GEOMETRY.values():
        profile = profile.set(field_id, 1.0, provenance=CAPTURED, utc_ms=NOW)
    _, prov = load_mounting(base, write_setup(tmp_path, profile))
    assert prov.measured is False


# -- when the overlay is broken -------------------------------------------


def test_an_unparseable_overlay_is_reported_and_not_applied(base, tmp_path):
    path = tmp_path / "asket_setup.yaml"
    path.write_text("fields: [this is not a mapping\n")
    mounting, prov = load_mounting(base, path)

    assert prov.overlay.error
    assert prov.overlay.applied == []
    assert mounting.tilt_deg == 35.0, "the base config must still apply"


def test_an_overlay_that_is_not_a_mapping_is_reported(base, tmp_path):
    path = tmp_path / "asket_setup.yaml"
    path.write_text("- just\n- a list\n")
    _, prov = load_mounting(base, path)
    assert "mapping" in prov.overlay.error


def test_an_empty_overlay_is_reported_rather_than_treated_as_absent(base, tmp_path):
    path = tmp_path / "asket_setup.yaml"
    path.write_text("")
    _, prov = load_mounting(base, path)
    assert prov.overlay.error == "file is empty"
    assert prov.overlay.missing is False


def test_a_value_that_is_not_a_number_is_refused_not_coerced(base, tmp_path):
    """A lever arm that silently became 0.0 because of a stray character is the
    exact failure the provenance apparatus exists to prevent, and it would look
    like a successful reload."""
    path = write_setup(tmp_path, default_profile())
    raw = yaml.safe_load(path.read_text())
    raw["fields"] = {TILT: {"value": "about 35", "provenance": "measured"}}
    path.write_text(yaml.safe_dump(raw))

    mounting, prov = load_mounting(base, path)
    assert "not a number" in prov.overlay.error
    assert mounting.tilt_deg == 35.0


def test_a_bad_side_is_refused(base, tmp_path):
    path = write_setup(tmp_path, default_profile())
    raw = yaml.safe_load(path.read_text())
    raw["fields"] = {SETUP_FIELD_FOR_SIDE: {"value": "upwards", "provenance": "entered"}}
    path.write_text(yaml.safe_dump(raw))

    mounting, prov = load_mounting(base, path)
    assert "port" in prov.overlay.error
    assert mounting.side == "starboard"


def test_one_bad_value_applies_none_of_them(base, tmp_path):
    """All or nothing, as the save path is. Half a tape-measure session
    applied is a geometry no file describes."""
    path = write_setup(tmp_path, default_profile())
    path.write_text(yaml.safe_dump({"fields": {
        "vessel.sonar.mounting.yaw_deg": {"value": 1.5, "provenance": "measured"},
        TILT: {"value": "nonsense", "provenance": "measured"},
    }}))
    mounting, prov = load_mounting(base, path)
    assert prov.overlay.error
    assert mounting.yaw_deg == 0.0, "a good value was applied beside a bad one"


def test_a_broken_overlay_never_raises(base, tmp_path):
    """A config typo must not take the sonar bridge down at sea."""
    for content in ("", "- a\n- b\n", "{{{", "fields: 3\n", "fields:\n  x: 1\n"):
        path = tmp_path / "asket_setup.yaml"
        path.write_text(content)
        mounting, prov = load_mounting(base, path)
        assert isinstance(mounting, SonarMounting)
        assert prov.fingerprint


# -- turning it off --------------------------------------------------------


def test_the_overlay_can_be_read_out_of(base, tmp_path, monkeypatch):
    """`use_overlay=False` reads the package default alone, for a bench run
    where somebody wants to know what `mounting.yaml` says on its own."""
    monkeypatch.setenv("ASKET_SETUP_FILE", str(
        write_setup(tmp_path, default_profile().set(
            TILT, 34.2, provenance=MEASURED, utc_ms=NOW))
    ))
    with_overlay, _ = load_mounting(base)
    without, prov = load_mounting(base, use_overlay=False)

    assert with_overlay.tilt_deg == 34.2
    assert without.tilt_deg == 35.0
    assert prov.overlay.path == ""


def test_the_default_location_is_used_when_none_is_given(base, tmp_path, monkeypatch):
    """The parameter's default is empty rather than a path somebody has to
    remember, so that "measure it on the page, press reload, the sonar moves"
    works with no launch file edited."""
    setup = write_setup(tmp_path, default_profile().set(
        TILT, 33.3, provenance=MEASURED, utc_ms=NOW))
    monkeypatch.setenv("ASKET_SETUP_FILE", str(setup))

    mounting, prov = load_mounting(base)
    assert mounting.tilt_deg == 33.3
    assert prov.overlay.path == str(setup)


# -- what the pre-flight reads --------------------------------------------


def test_the_overlay_reaches_the_dict_the_preflight_reads(base, tmp_path):
    profile = default_profile().set(TILT, 34.2, provenance=MEASURED, utc_ms=NOW)
    _, prov = load_mounting(base, write_setup(tmp_path, profile))
    as_dict = prov.to_dict()
    assert as_dict["overlay"]["applied"] == ["tilt_deg"]
    assert as_dict["overlay"]["error"] == ""
    assert as_dict["fingerprint"] == prov.fingerprint
