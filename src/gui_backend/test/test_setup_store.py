"""The setup file: reading it, writing it, and the three ways it can be absent.

Loading has to distinguish states that look alike from a distance and send
somebody to completely different places:

* **No file.** A vessel nobody has told anything. Expected, legitimate, and
  the state the setup page exists to get somebody out of.
* **A file that will not load.** Somebody may well have measured the vessel
  and be having their numbers silently ignored in favour of defaults. This is
  the worst of the three and it is a fault, not an absence.
* **A file with nothing answered in it.** Present, valid, and empty — which
  reads as "the page was opened and nothing was filled in".

The existing ``sonar.mounting`` check already draws these three distinctions
for one file. This is the same reasoning applied to all of them.
"""

import pytest
import yaml
from asket_common.setup_profile import (
    CAPTURED,
    ENTERED,
    MEASURED,
    SetupProfile,
    default_profile,
)
from gui_backend.core import setup_store
from gui_backend.core.setup_store import SetupFileError

NOW = 1_789_932_000_000
MOUNTING = "vessel.sonar.mounting.tilt_deg"
STATION_LAT = "deployment.station.lat"


@pytest.fixture
def path(tmp_path):
    return tmp_path / "asket_setup.yaml"


def measured_profile() -> SetupProfile:
    return (
        default_profile()
        .set(MOUNTING, 34.2, MEASURED, utc_ms=NOW, by="Auxence", note="tape, bow up")
        .set(STATION_LAT, -22.9576, CAPTURED, utc_ms=NOW, note="8 sats, HDOP 1.1")
    )


# -- the round trip --------------------------------------------------------


def test_what_was_written_is_what_comes_back(path):
    setup_store.save(measured_profile(), path, utc_ms=NOW, by="Auxence")
    loaded = setup_store.load(path)

    assert loaded.get(MOUNTING) == 34.2
    assert loaded.entry(MOUNTING).provenance == MEASURED
    assert loaded.entry(MOUNTING).set_by == "Auxence"
    assert loaded.entry(STATION_LAT).note == "8 sats, HDOP 1.1"
    assert loaded.written_by == "Auxence"
    assert loaded.written_utc_ms == NOW


def test_the_file_is_readable_by_a_human(path):
    """It will be read over somebody's shoulder on a beach, and hand-editing is
    not forbidden. A header that explains the provenance words is worth the
    bytes."""
    setup_store.save(measured_profile(), path, utc_ms=NOW)
    text = path.read_text()

    assert text.startswith("#")
    for word in ("default", "entered", "captured", "measured"):
        assert word in text
    assert yaml.safe_load(text)["fields"][MOUNTING]["value"] == 34.2


def test_the_hash_is_written_alongside(path):
    """So the pre-flight can say "this is the setup that was pushed", which
    matters most in the case the page exists to fix: a value that looks saved
    while a node is still running the old one."""
    written = setup_store.save(measured_profile(), path, utc_ms=NOW)
    document = yaml.safe_load(path.read_text())
    assert document["content_hash"] == written.content_hash()


def test_saving_does_not_modify_the_profile_it_was_given(path):
    profile = measured_profile()
    written = setup_store.save(profile, path, utc_ms=NOW, by="Auxence")
    assert profile.written_utc_ms is None
    assert written.written_utc_ms == NOW


# -- the three absences ----------------------------------------------------


def test_no_file_is_a_vessel_nobody_has_told_anything(path):
    """Not an error. A fresh Jetson on a beach is exactly the case the page
    exists for, and raising here would make the page unreachable at the one
    moment it is most needed."""
    profile = setup_store.load(path)
    assert not profile.answered(MOUNTING)
    assert profile.outstanding()


def test_a_file_that_will_not_parse_raises_rather_than_silently_resetting(path):
    """The worst of the three states: somebody may have measured the vessel
    and be running on defaults without knowing. Returning an empty profile
    would hide exactly that."""
    path.write_text("fields: {oh dear: [\n")
    with pytest.raises(SetupFileError):
        setup_store.load(path)


def test_a_file_that_is_not_a_mapping_raises(path):
    """A YAML document that parses into a list is valid YAML and not a setup
    file. Reading it as one would find no fields and look like an empty
    vessel."""
    path.write_text("- one\n- two\n")
    with pytest.raises(SetupFileError) as excinfo:
        setup_store.load(path)
    assert "list" in str(excinfo.value)


def test_an_empty_file_is_valid_and_answers_nothing(path):
    path.write_text("")
    profile = setup_store.load(path)
    assert len(profile.outstanding()) > 0
    assert profile.unknown_fields == ()


# -- unrecognised entries --------------------------------------------------


def test_a_misspelled_field_is_kept_and_reported(path):
    """Almost always a typo in a value somebody DID measure. Dropping it
    silently means the measurement never reaches the instrument and nobody
    finds out — which is precisely what mounting.yaml's own check exists to
    catch."""
    path.write_text(yaml.safe_dump({
        "fields": {
            MOUNTING: {"value": 34.2, "provenance": MEASURED},
            "vessel.sonar.mounting.tilt_dgrees": {"value": 34.2, "provenance": MEASURED},
        },
    }))
    profile = setup_store.load(path)
    assert profile.unknown_fields == ("vessel.sonar.mounting.tilt_dgrees",)
    assert profile.get(MOUNTING) == 34.2


def test_describe_reports_the_typo_without_raising(path):
    path.write_text(yaml.safe_dump({
        "fields": {"vessel.nonsense": {"value": 1, "provenance": ENTERED}},
    }))
    assert setup_store.describe(path)["unknown_fields"] == ["vessel.nonsense"]


# -- what the pre-flight will read -----------------------------------------


def test_describe_distinguishes_the_three_states(path):
    """Shaped like the mounting state the existing checks already consume, so
    a check can say which of three problems it has rather than 'no setup'."""
    missing = setup_store.describe(path)
    assert missing["found"] is False and missing["missing"] is True

    path.write_text("fields: {oh dear: [\n")
    broken = setup_store.describe(path)
    assert broken["found"] is True and "error" in broken

    setup_store.save(measured_profile(), path, utc_ms=NOW, by="Auxence")
    good = setup_store.describe(path)
    assert good["found"] is True
    assert "error" not in good
    assert good["written_by"] == "Auxence"
    assert good["content_hash"]


def test_describe_lists_what_is_outstanding(path):
    setup_store.save(measured_profile(), path, utc_ms=NOW)
    outstanding = setup_store.describe(path)["outstanding"]
    assert MOUNTING not in outstanding
    assert STATION_LAT not in outstanding
    assert "deployment.station.boresight_deg" in outstanding


def test_describe_never_raises_whatever_is_in_the_file(path):
    """It is read by a pre-flight check, and a check that raises takes the
    whole report down — which loses the other seventeen checks to say nothing
    useful about this one."""
    for content in ("", "[]", "fields: {oh dear: [\n", "fields: null", "%%%"):
        path.write_text(content)
        assert "path" in setup_store.describe(path)


# -- writing safely --------------------------------------------------------


def test_the_write_is_atomic(path):
    """The one thing worse than no setup file is half of one: a file that
    parses, holds some of the answers, and looks entirely fine. Written to a
    neighbour and renamed, so a yanked cable leaves the old file intact."""
    setup_store.save(measured_profile(), path, utc_ms=NOW)
    leftovers = list(path.parent.glob("*.tmp"))
    assert leftovers == [], f"temporary files left behind: {leftovers}"


def test_saving_creates_the_directory(tmp_path):
    nested = tmp_path / "does" / "not" / "exist" / "asket_setup.yaml"
    setup_store.save(default_profile(), nested, utc_ms=NOW)
    assert nested.is_file()


def test_a_second_save_replaces_the_first(path):
    setup_store.save(measured_profile(), path, utc_ms=NOW, by="Auxence")
    later = setup_store.load(path).set(MOUNTING, 33.9, MEASURED, utc_ms=NOW + 1000)
    setup_store.save(later, path, utc_ms=NOW + 1000, by="Tom")

    reloaded = setup_store.load(path)
    assert reloaded.get(MOUNTING) == 33.9
    assert reloaded.written_by == "Tom"


# -- where it lives --------------------------------------------------------


def test_the_default_path_is_runtime_state_not_source(monkeypatch):
    """Under ~/.ros rather than in the tree: a colcon build must never be able
    to overwrite what somebody measured."""
    assert "src" not in setup_store.DEFAULT_PATH.parts
    assert setup_store.DEFAULT_PATH.name.endswith(".yaml")


def test_the_path_can_be_overridden_by_the_environment(tmp_path, monkeypatch):
    """So a test, a container, or somebody running two vessels off one laptop
    can point at a different file without editing anything.

    Re-imported rather than asserted about, because the override is read at
    import time and a test that only checked the source text would pass
    against a module that had stopped honouring it.
    """
    import importlib

    target = tmp_path / "elsewhere.yaml"
    monkeypatch.setenv("ASKET_SETUP_FILE", str(target))
    reloaded = importlib.reload(setup_store)
    try:
        assert reloaded.DEFAULT_PATH == target
        reloaded.save(default_profile().set(
            MOUNTING, 34.2, MEASURED, utc_ms=NOW,
        ), utc_ms=NOW)
        assert target.is_file()
        assert reloaded.load().get(MOUNTING) == 34.2
    finally:
        monkeypatch.delenv("ASKET_SETUP_FILE")
        importlib.reload(setup_store)
