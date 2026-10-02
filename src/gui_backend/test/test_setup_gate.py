"""Three guards on a setup change, against three different failure modes.

They are not alternatives and this file is mostly about keeping them apart.

**The automatic condition prevents the accident.** Nobody intends to change
the sonar's lever arm with the boat under way; it happens because a page was
left open and somebody leaned on it. The Jetson refuses, on state it reads
from the vessel rather than anything the browser claims — a guard in the page
is a guard a stale page does not have.

**The explicit confirmation prevents the rush**, and leaves a trace: who,
when, which fields, and what the boat was doing at the time.

**Confirmed state prevents the lie.** The page does not say "done" when the
file was written or when the command was sent, but when the node reports it is
running on the new numbers. Safety rule 4, applied to configuration.

A confirmation without the condition lets somebody confirm their way into
changing geometry mid-survey. A condition without the confirmation turns a
disarmed boat into a free-for-all with no record. Confirmed state without
either is a tick on a change nobody checked.
"""

from gui_backend.core import setup_gate
from gui_backend.core.commands import mounting_reloaded
from gui_backend.core.setup_gate import (
    BLOCKED_ARMED,
    BLOCKED_RECORDING,
    BLOCKED_UNKNOWN,
    SetupChangeRecord,
)

DISARMED = {"pico": {"armed": False, "mode": "MANUAL"}, "mission": {"state": "IDLE"}}


def state(**overrides):
    merged = {"pico": dict(DISARMED["pico"]), "mission": dict(DISARMED["mission"])}
    for key, value in overrides.items():
        merged.setdefault(key, {}).update(value)
    return merged


# -- the automatic condition -----------------------------------------------


def test_a_disarmed_idle_vessel_accepts_a_change():
    assert setup_gate.evaluate(DISARMED).allowed


def test_an_armed_vessel_refuses():
    """The accident this exists for: a page left open on a laptop and somebody
    leaning on it while the boat can move."""
    decision = setup_gate.evaluate(state(pico={"armed": True}))
    assert not decision.allowed
    assert decision.code == BLOCKED_ARMED
    assert "armed" in decision.reason


def test_the_refusal_names_the_switch_that_fixes_it():
    """Arming is RC channel 7 and nothing else. A refusal that said "disarm the
    vessel" without saying how would send somebody looking for a button that
    does not exist, which is the whole reason the arming-block sentence exists
    elsewhere in this GUI."""
    decision = setup_gate.evaluate(state(pico={"armed": True}))
    assert "channel 7" in decision.remedy


def test_a_recording_mission_refuses():
    decision = setup_gate.evaluate(state(mission={"state": "RECORDING"}))
    assert not decision.allowed
    assert decision.code == BLOCKED_RECORDING


def test_the_recording_refusal_says_what_it_would_cost():
    """Not "not allowed". Half the survey placed one way and half the other,
    with nothing in the file to say where the change fell — that is the thing
    somebody needs to know, and it is why this refusal is worth obeying."""
    remedy = setup_gate.evaluate(state(mission={"state": "RECORDING"})).remedy
    assert "half" in remedy


def test_an_unreadable_pico_refuses_rather_than_assuming_the_best():
    """"We cannot see the Pico" is not a known safe state. It is also a real
    condition — a lost serial link, an unparseable STATE line — and letting a
    change through on state nobody can read defeats the point of the gate."""
    decision = setup_gate.evaluate({"pico": {}, "mission": {"state": "IDLE"}})
    assert not decision.allowed
    assert decision.code == BLOCKED_UNKNOWN


def test_an_entirely_absent_state_refuses():
    assert not setup_gate.evaluate({}).allowed


def test_armed_outranks_recording_so_the_worse_problem_is_named_first():
    """Both are true on a recording, armed vessel. Telling somebody to stop the
    recording when the boat can also move is naming the lesser of the two."""
    decision = setup_gate.evaluate(
        state(pico={"armed": True}, mission={"state": "RECORDING"})
    )
    assert decision.code == BLOCKED_ARMED


def test_the_decision_is_pure():
    """Asked by the backend before it dispatches, and by the page to decide
    whether to offer the button at all. Two answers from one function, so the
    button and the refusal cannot disagree."""
    first = setup_gate.evaluate(DISARMED)
    for _ in range(5):
        assert setup_gate.evaluate(DISARMED) == first


def test_nothing_the_browser_sends_is_consulted():
    """A guard the page could satisfy by claiming to be satisfied is not a
    guard. Every input here comes off the vessel's own report."""
    assert not setup_gate.evaluate({
        "pico": {"armed": True},
        "mission": {"state": "IDLE"},
        "operator_says_its_fine": True,
        "allowed": True,
    }).allowed


# -- the trace -------------------------------------------------------------


def test_the_record_says_what_the_boat_was_doing():
    assert setup_gate.describe_vessel_state(DISARMED) == (
        "MANUAL, disarmed, not recording"
    )
    assert "recording" in setup_gate.describe_vessel_state(
        state(mission={"state": "RECORDING"})
    )


def test_unknown_arming_is_said_rather_than_guessed():
    described = setup_gate.describe_vessel_state({"pico": {"mode": "MANUAL"}})
    assert "arming unknown" in described


def test_a_record_carries_who_what_and_when():
    record = SetupChangeRecord(
        utc_ms=1_789_932_000_000,
        by="Auxence",
        field_ids=("vessel.sonar.mounting.tilt_deg",),
        content_hash="abc123",
        vessel_state="MANUAL, disarmed, not recording",
        note="measured with a tape, bow up",
    )
    as_dict = record.to_dict()
    assert as_dict["by"] == "Auxence"
    assert as_dict["fields"] == ["vessel.sonar.mounting.tilt_deg"]
    assert as_dict["content_hash"] == "abc123"
    assert "disarmed" in as_dict["vessel_state"]


def test_the_state_is_recorded_even_when_the_change_was_allowed():
    """A record that only appeared on refusal would make the common case
    invisible, and the common case is the one somebody wants to reconstruct
    three months later."""
    record = SetupChangeRecord(
        utc_ms=1, by="A", field_ids=(), content_hash="h",
        vessel_state=setup_gate.describe_vessel_state(DISARMED),
    )
    assert "disarmed" in record.to_dict()["vessel_state"]


# -- confirmed state -------------------------------------------------------


def test_a_reload_is_confirmed_only_by_the_geometry_the_node_reports():
    """Not by the file having been written, and not by the command having been
    sent. Those are three different things and the gap between them is where
    the failure lives."""
    confirm = mounting_reloaded("abc123def456")

    assert not confirm({})
    assert not confirm({"mounting": {}})
    assert not confirm({"mounting": {"fingerprint": "staleoldvalue"}})
    assert confirm({"mounting": {"fingerprint": "abc123def456"}})


def test_a_reload_that_landed_on_someone_elses_value_reads_as_failed():
    """Somebody else saved in between. The question is not "did something
    change" but "is the vessel on the exact geometry I just sent", and a
    field-by-field comparison with a tolerance would quietly call this
    success."""
    confirm = mounting_reloaded("mine0000")
    assert not confirm({"mounting": {"fingerprint": "theirs00"}})


def test_an_absent_fingerprint_never_confirms():
    """A node too old to report one must read as unconfirmed rather than as
    agreeing with everything — which is what an empty-equals-empty comparison
    would do."""
    assert not mounting_reloaded("")({"mounting": {"fingerprint": ""}})
    assert not mounting_reloaded("abc")({"mounting": {}})


def test_the_fingerprint_follows_the_geometry_and_not_the_paperwork():
    """Editing a comment or a notes line does not move the transducer. A
    confirmation that fired on cosmetic edits would train people to ignore
    it."""
    from omniscan_bridge.core.geometry import SonarMounting
    from omniscan_bridge.core.mounting import geometry_fingerprint

    assert geometry_fingerprint(SonarMounting(tilt_deg=35.0)) == geometry_fingerprint(
        SonarMounting(tilt_deg=35.0)
    )
    assert geometry_fingerprint(SonarMounting(tilt_deg=35.0)) != geometry_fingerprint(
        SonarMounting(tilt_deg=34.2)
    )


def test_a_float_that_differs_in_its_sixteenth_decimal_is_the_same_measurement():
    """A lever arm is quoted to the centimetre. Round-tripping through YAML
    must not produce a geometry the vessel reports as different from the one
    that was sent, or every reload would read as failed."""
    from omniscan_bridge.core.geometry import SonarMounting
    from omniscan_bridge.core.mounting import geometry_fingerprint

    assert geometry_fingerprint(
        SonarMounting(lever_x_m=-0.20)
    ) == geometry_fingerprint(SonarMounting(lever_x_m=-0.2000000000000001))


def test_every_load_path_stamps_a_fingerprint():
    """load_mounting returns from a good many places — missing file, bad YAML,
    empty, each kind of malformed. One without a fingerprint is the one
    somebody hits, and it would read as a reload that never confirms."""
    from omniscan_bridge.core.mounting import load_mounting

    for path in (None, "", "/nowhere/at/all.yaml"):
        _, provenance = load_mounting(path)
        assert provenance.fingerprint, f"no fingerprint for {path!r}"


def test_the_fingerprint_reaches_the_dict_the_preflight_reads():
    from omniscan_bridge.core.mounting import load_mounting

    _, provenance = load_mounting(None)
    assert provenance.to_dict()["fingerprint"] == provenance.fingerprint
