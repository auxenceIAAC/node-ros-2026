"""Saving the setup, under all three guards.

The guards are in `setup_gate.py` and tested there. This is about the handler
that uses them, and about the line the user drew: the page does not say "done"
because a file was written. So the save command settles with what *actually*
happened, and what the vessel is not yet using comes back as pending effects,
in the words the Python chose.
"""

import pytest
from asket_common.setup_profile import FIELDS, MEASURED, ON_RELOAD
from asket_sim.core.world import SimWorld, WorldConfig
from gui_backend.core import setup_store
from gui_backend.core.hub import Hub
from gui_backend.core.sim_source import SimSource

TILT = "vessel.sonar.mounting.tilt_deg"


@pytest.fixture
def hub(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_store, "DEFAULT_PATH", tmp_path / "asket_setup.yaml")
    return Hub(SimSource(SimWorld(WorldConfig())))


def save(hub, fields, **extra):
    args = {"fields": fields, "by": "Auxence", "confirmed": True}
    args.update(extra)
    return hub.issue_command("save_setup", args)


def test_a_disarmed_vessel_takes_a_change(hub, tmp_path):
    result = save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    assert result["status"] == "confirmed", result["detail"]
    assert (tmp_path / "asket_setup.yaml").is_file()
    assert setup_store.load().get(TILT) == 34.2


def test_an_armed_vessel_is_refused_and_told_which_switch(hub, monkeypatch, tmp_path):
    """The gate reads the vessel's own report, not the request. A page left
    open on a laptop is the accident this exists for, so there is nothing the
    browser can send that gets round it."""
    monkeypatch.setattr(
        hub.source, "state",
        lambda: {"pico": {"armed": True, "mode": "AUTO"}, "mission": {"state": "IDLE"}},
    )
    result = save(hub, {TILT: {"value": "34.2"}})
    assert result["status"] == "failed"
    assert "armed" in result["detail"]
    assert "channel 7" in result["detail"]
    assert not (tmp_path / "asket_setup.yaml").exists()


def test_a_recording_mission_is_refused(hub, monkeypatch, tmp_path):
    monkeypatch.setattr(
        hub.source, "state",
        lambda: {"pico": {"armed": False, "mode": "AUTO"}, "mission": {"state": "RECORDING"}},
    )
    result = save(hub, {TILT: {"value": "34.2"}})
    assert result["status"] == "failed"
    assert "half" in result["detail"]
    assert not (tmp_path / "asket_setup.yaml").exists()


def test_an_unconfirmed_request_writes_nothing(hub, tmp_path):
    """The second guard, and the one a satisfied condition does not cover. A
    disarmed boat with no confirmation step would be a free-for-all."""
    result = hub.issue_command("save_setup", {"fields": {TILT: {"value": "34.2"}}})
    assert result["status"] == "failed"
    assert "not confirmed" in result["detail"]
    assert not (tmp_path / "asket_setup.yaml").exists()


def test_an_empty_change_is_refused_rather_than_written(hub, tmp_path):
    assert save(hub, {})["status"] == "failed"
    assert not (tmp_path / "asket_setup.yaml").exists()


# -- values -----------------------------------------------------------------


def test_a_string_from_a_browser_input_becomes_a_number(hub):
    """Every value off an HTML input is a string. A setup file whose sonar tilt
    is `"35"` would load, pass every check, and reach the sonar bridge as
    something that is not a number."""
    save(hub, {TILT: {"value": "34.2"}})
    assert isinstance(setup_store.load().get(TILT), float)


def test_a_value_that_is_not_a_number_is_refused_by_name(hub, tmp_path):
    """Refused rather than coerced to zero. A tilt that silently became 0.0 is
    the precise failure this page is about."""
    result = save(hub, {TILT: {"value": "about 35ish"}})
    assert result["status"] == "failed"
    assert "Sonar downward tilt" in result["detail"]
    assert not (tmp_path / "asket_setup.yaml").exists()


def test_a_choice_outside_the_offered_set_is_refused(hub):
    result = save(hub, {"vessel.sonar.side": {"value": "upwards"}})
    assert result["status"] == "failed"
    assert "port" in result["detail"] and "starboard" in result["detail"]


def test_an_unknown_field_is_refused(hub):
    result = save(hub, {"vessel.sonar.mounting.tilt_dge": {"value": 34.2}})
    assert result["status"] == "failed"
    assert "tilt_dge" in result["detail"]


def test_one_bad_value_writes_none_of_them(hub, tmp_path):
    """A tape-measure session changes seven numbers at once. Four saved and
    three rejected is a state no file describes and nobody could debug."""
    result = save(hub, {
        TILT: {"value": "34.2"},
        "vessel.sonar.mounting.yaw_deg": {"value": "sideways"},
    })
    assert result["status"] == "failed"
    assert not (tmp_path / "asket_setup.yaml").exists()


# -- saving is not applying -------------------------------------------------


def test_a_reload_field_is_reported_as_saved_but_not_in_use(hub):
    """The line the whole exercise turns on. The file is written; the sonar
    bridge is still using the old numbers, and the page must say so rather than
    say "done"."""
    result = save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    assert result["status"] == "confirmed"
    effects = result["pending_effects"]
    assert [e["takes_effect"] for e in effects] == [ON_RELOAD]
    assert effects[0]["applied_by"] == "omniscan_bridge"
    assert effects[0]["can_be_applied_from_here"] is True
    assert "still using the old" in result["detail"]


def test_seven_mounting_numbers_produce_one_sentence_not_seven(hub):
    """Seven identical "reload the sonar bridge" lines is seven chances to read
    past the one that matters."""
    mounting = [s.id for s in FIELDS if ".mounting." in s.id]
    assert len(mounting) >= 6
    result = save(hub, {fid: {"value": "1.0", "provenance": MEASURED} for fid in mounting})
    assert len(result["pending_effects"]) == 1
    assert result["pending_effects"][0]["field_ids"] == sorted(mounting)


def test_a_live_field_says_it_is_in_use(hub):
    live = next(s for s in FIELDS if s.takes_effect == "immediately" and not s.choices)
    result = save(hub, {live.id: {"value": "1.0"}})
    assert result["status"] == "confirmed"
    assert result["pending_effects"] == []
    assert "in use" in result["detail"]


def test_what_changed_comes_back_so_the_page_need_not_guess(hub):
    result = save(hub, {TILT: {"value": "34.2"}})
    assert result["changed"] == [TILT]
    assert result["content_hash"] == setup_store.load().content_hash()


# -- not restamping dates ---------------------------------------------------


def test_saving_an_unchanged_value_writes_nothing_and_keeps_the_date(hub):
    """Opening the page and pressing save must not reset the staleness dates.
    Those dates are the only thing that says "this station position is from a
    different beach"."""
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    first = setup_store.load().entry(TILT).set_utc_ms

    result = save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    assert result["status"] == "confirmed"
    assert "already" in result["detail"]
    assert setup_store.load().entry(TILT).set_utc_ms == first


def test_a_changed_provenance_is_a_change(hub):
    """"Typed in 35" and "measured 35" are the same number and different
    claims, and the second is the whole reason the provenance exists."""
    save(hub, {TILT: {"value": "35.0", "provenance": "entered"}})
    result = save(hub, {TILT: {"value": "35.0", "provenance": MEASURED}})
    assert result["changed"] == [TILT]
    assert setup_store.load().entry(TILT).provenance == MEASURED


# -- the trace --------------------------------------------------------------


def test_a_change_leaves_a_record_of_who_what_and_what_the_boat_was_doing(hub, tmp_path):
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}}, note="tape measure")
    records = setup_store.read_changes()
    assert len(records) == 1
    assert records[0]["by"] == "Auxence"
    assert records[0]["fields"] == [TILT]
    assert "disarmed" in records[0]["vessel_state"]
    assert records[0]["note"] == "tape measure"
    assert records[0]["content_hash"] == setup_store.load().content_hash()


def test_the_log_is_append_only(hub):
    save(hub, {TILT: {"value": "34.2"}})
    save(hub, {TILT: {"value": "33.1"}})
    records = setup_store.read_changes()
    assert len(records) == 2
    assert records[0]["utc_ms"] <= records[1]["utc_ms"]


def test_one_unreadable_line_does_not_cost_the_rest(hub):
    """The point of an append-only log is that one bad record does not take the
    history with it."""
    save(hub, {TILT: {"value": "34.2"}})
    log = setup_store.change_log_path()
    log.write_text("{not json\n" + log.read_text())
    assert len(setup_store.read_changes()) == 1


def test_a_refused_change_leaves_no_record_of_having_happened(hub, monkeypatch):
    monkeypatch.setattr(
        hub.source, "state",
        lambda: {"pico": {"armed": True, "mode": "AUTO"}, "mission": {"state": "IDLE"}},
    )
    save(hub, {TILT: {"value": "34.2"}})
    assert setup_store.read_changes() == []


# -- the existing file ------------------------------------------------------


def test_a_file_that_will_not_parse_is_not_overwritten(hub, tmp_path):
    """It holds answers somebody entered. Saving on top of it would discard
    them silently, which is the one direction this must never fail."""
    broken = "fields: [this is not a mapping\n"
    (tmp_path / "asket_setup.yaml").write_text(broken)
    result = save(hub, {TILT: {"value": "34.2"}})
    assert result["status"] == "failed"
    assert "will not parse" in result["detail"]
    assert (tmp_path / "asket_setup.yaml").read_text() == broken


def test_a_save_keeps_the_answers_it_did_not_touch(hub):
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    save(hub, {"vessel.sonar.side": {"value": "port"}})
    profile = setup_store.load()
    assert profile.get(TILT) == 34.2
    assert profile.get("vessel.sonar.side") == "port"


# -- the command lifecycle --------------------------------------------------


def test_no_save_is_ever_left_pending(hub, monkeypatch):
    """`begin` leaves a command pending and every path must end it. One that
    did not would time out with "no confirmation from the vessel", which would
    be a lie about where the fault was."""
    cases = [
        {"fields": {TILT: {"value": "34.2"}}, "confirmed": True},
        {"fields": {TILT: {"value": "nonsense"}}, "confirmed": True},
        {"fields": {}, "confirmed": True},
        {"fields": {TILT: {"value": "34.2"}}},
        {"fields": {"nope.at.all": {"value": 1}}, "confirmed": True},
    ]
    for args in cases:
        hub.issue_command("save_setup", args)
    assert hub.commands.pending == []
