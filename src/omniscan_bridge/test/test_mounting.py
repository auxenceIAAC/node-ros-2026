"""Loading mounting.yaml.

The failure this file guards against is not a crash. It is a survey that comes
back displaced by half a metre, consistently, because the config never loaded
and nobody was told.
"""

from pathlib import Path

import pytest
from omniscan_bridge.core.geometry import SonarMounting
from omniscan_bridge.core.mounting import load_mounting

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config" / "mounting.yaml"

MEASURED = """
measured: true
measured_by: "A. Daillen"
measured_utc: "2026-03-02T09:15Z"
tilt_deg: 31.5
yaw_deg: 1.0
pitch_deg: -0.5
lever_x_m: -0.185
lever_y_m: -0.402
lever_z_m: -0.144
side: "starboard"
"""


def write(tmp_path, text, name="mounting.yaml"):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_a_measured_file_loads_its_numbers_and_its_provenance(tmp_path):
    mounting, prov = load_mounting(write(tmp_path, MEASURED))
    assert mounting.tilt_deg == 31.5
    assert mounting.lever_y_m == -0.402
    assert prov.found and prov.measured and not prov.provisional
    assert prov.measured_by == "A. Daillen"


def test_the_file_this_repository_ships_is_marked_unmeasured():
    """If this ever fails, somebody has quietly promoted the brief's guesses to
    the truth. The numbers in it have never been near the boat."""
    mounting, prov = load_mounting(REPO_CONFIG)
    assert prov.found
    assert prov.provisional, "mounting.yaml claims to be measured — was it?"
    assert mounting.tilt_deg == 35.0


def test_a_missing_file_falls_back_to_defaults_rather_than_taking_the_sonar_down(tmp_path):
    """Raising here would lose the whole sonar over a config path typo, which is
    the wrong trade at sea. The pre-flight is what makes the noise."""
    mounting, prov = load_mounting(tmp_path / "nope.yaml")
    assert mounting == SonarMounting()
    assert not prov.found
    assert prov.missing
    # Not an "error": there is no file, so no measurement is being ignored.
    assert not prov.error


def test_no_path_configured_is_reported_as_such(tmp_path):
    _, prov = load_mounting("")
    assert not prov.found
    assert prov.missing


@pytest.mark.parametrize(
    "text, expected",
    [
        ("tilt_deg: [1, 2\n", "not valid YAML"),
        ("", "file is empty"),
        ("- 35.0\n- 0.0\n", "expected a mapping"),
        ("tilt_deg: 'about 35'\n", "tilt_deg is not a number"),
        ("side: 'left'\n", "side must be"),
    ],
)
def test_every_way_the_file_can_be_wrong_is_named(tmp_path, text, expected):
    """The error string ends up in the pre-flight message, so it has to be
    something a person on a beach can act on."""
    mounting, prov = load_mounting(write(tmp_path, text))
    assert not prov.found
    assert expected in prov.error
    assert mounting == SonarMounting()


def test_a_broken_file_never_reports_itself_as_measured(tmp_path):
    """The worst outcome available: numbers ignored, check green."""
    _, prov = load_mounting(write(tmp_path, "measured: true\ntilt_deg: 'thirty'\n"))
    assert not prov.measured
    assert prov.provisional


def test_a_typo_in_a_field_name_is_surfaced_not_swallowed(tmp_path):
    """`lever_y` is not `lever_y_m`. Without this the file looks filled in and
    the default is what actually gets applied."""
    mounting, prov = load_mounting(write(
        tmp_path, MEASURED + "lever_y: -0.9\nleaver_x_m: 0.1\n"))
    assert prov.unknown_fields == ["leaver_x_m", "lever_y"]
    assert mounting.lever_y_m == -0.402


def test_a_partial_file_keeps_defaults_for_what_it_does_not_say(tmp_path):
    mounting, prov = load_mounting(write(tmp_path, "measured: true\ntilt_deg: 20.0\n"))
    assert mounting.tilt_deg == 20.0
    assert mounting.lever_z_m == SonarMounting().lever_z_m


def test_provenance_survives_the_round_trip_to_a_dict(tmp_path):
    """It travels to the pre-flight check and to the setup page, so it has to
    be JSON-able.

    No longer flat: `overlay` and `geometry` are nested, because the setup
    file's own problems have to stay distinguishable from `mounting.yaml`'s —
    one `error` string covering both would read as "config broken" and get
    ignored.
    """
    _, prov = load_mounting(write(tmp_path, MEASURED), use_overlay=False)
    d = prov.to_dict()
    assert d["measured"] is True
    assert d["unknown_fields"] == []
    assert set(d) == {
        "measured", "measured_by", "measured_utc", "notes",
        "path", "found", "missing", "error", "unknown_fields", "fingerprint",
        "overlay", "geometry",
    }
    import json
    json.dumps(d)
    assert set(d["geometry"]) == {
        "tilt_deg", "yaw_deg", "pitch_deg",
        "lever_x_m", "lever_y_m", "lever_z_m", "side",
    }
