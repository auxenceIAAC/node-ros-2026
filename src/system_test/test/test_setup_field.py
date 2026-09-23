"""A check that is waiting on a value says which value.

The fault this exists for is two weeks old and still running. ``sonar.mounting``
returns the same amber warning on every pre-flight, naming
``omniscan_bridge/config/mounting.yaml``, and nothing has happened — the file
lives behind an SSH session nobody in the club has, so the warning reads as
background noise rather than as a task. It is not that the message is unclear.
It is that the remedy names something the reader cannot open.

``setup_field`` is the structural half of the fix: the check names the *field*
it is waiting on rather than the path it happens to live at today. That lets
the panel separate "nobody has told us this yet" from "this is broken" — two
ambers that need completely different responses and until now looked
identical — and, once the setup page exists, lets it link straight there.

The narrowness is the point. A check that reports a fault must NOT carry a
field, or the category stops meaning anything and we are back to one
undifferentiated column of amber.
"""

import pytest
from system_test.core.checks import (
    FAIL,
    PASS,
    SKIPPED,
    WARN,
    CheckResult,
    Thresholds,
    run_checks,
)
from system_test.core.checks import MOUNTING_FIELD

THRESHOLDS = Thresholds()


def mounting_result(**mounting) -> CheckResult:
    """Run the pre-flight and pull out the one check under test."""
    results = {r.id: r for r in run_checks({"mounting": mounting}, THRESHOLDS).items}
    return results["sonar.mounting"]


# -- the field travels -----------------------------------------------------


def test_an_unmeasured_hull_names_the_field():
    """The case that has been amber for two weeks."""
    result = mounting_result(found=True, measured=False, path="mounting.yaml")
    assert result.status == WARN
    assert result.setup_field == MOUNTING_FIELD


def test_a_missing_file_names_the_same_field():
    """Two different messages, one thing the operator has to do. The field is
    what they have to do; the message is only how we noticed."""
    result = mounting_result(found=False, missing=True)
    assert result.status == WARN
    assert result.setup_field == MOUNTING_FIELD


def test_the_field_is_tier_first_and_matches_the_design():
    """``vessel.`` rather than ``deployment.`` — measured once on a given hull
    and kept, which is also why this check stays amber rather than becoming a
    FAIL. It is a thing to do properly once, not a thing that goes stale."""
    assert MOUNTING_FIELD == "vessel.sonar.mounting"
    assert MOUNTING_FIELD.startswith("vessel.")


# -- and only where it should ----------------------------------------------


def test_a_file_that_will_not_load_is_a_fault_not_a_missing_value():
    """The worst of the mounting states: somebody may well have measured the
    vessel and the numbers are being silently ignored. Sending that to a form
    would be sending it to the wrong place — the value exists, something is
    broken."""
    result = mounting_result(found=True, error="could not parse line 12")
    assert result.status == FAIL
    assert result.setup_field == ""


def test_a_typo_is_a_fault_not_a_missing_value():
    result = mounting_result(found=True, measured=True, unknown_fields=["tilt_dgrees"])
    assert result.status == WARN
    assert result.setup_field == ""


def test_a_measured_hull_asks_for_nothing():
    result = mounting_result(
        found=True, measured=True, measured_by="Auxence", measured_utc="2026-09-20",
    )
    assert result.status == PASS
    assert result.setup_field == ""


def test_no_other_check_claims_to_be_waiting_on_a_value():
    """The guard that keeps the category meaningful.

    Every check reporting a fault must leave this empty. If a second check
    starts carrying a field, that should be a decision somebody made on
    purpose — and this test is where they will have to make it.
    """
    state = {"mounting": {"found": True, "measured": False}}
    carrying = {
        r.id for r in run_checks(state, THRESHOLDS).items if r.setup_field
    }
    assert carrying == {"sonar.mounting"}, (
        f"unexpected checks are naming a setup field: {sorted(carrying - {'sonar.mounting'})}"
    )


def test_the_default_is_empty():
    """A check written without thinking about this must not accidentally join
    the category."""
    assert CheckResult("x", "X", PASS, "fine").setup_field == ""


# -- it reaches the panel --------------------------------------------------


def test_the_field_survives_serialisation():
    result = mounting_result(found=True, measured=False)
    assert result.to_dict()["setup_field"] == MOUNTING_FIELD


def test_every_item_carries_the_key_even_when_empty():
    """Present and empty, not absent. The panel filters on it, and a key that
    sometimes exists is a key that will one day be read as undefined on the
    one item where it mattered."""
    for result in run_checks({"mounting": {"found": True, "measured": True}}, THRESHOLDS).items:
        assert "setup_field" in result.to_dict()


def test_the_ros_adapter_carries_it_too():
    """One panel, one dialect. If the ROS path dropped this field, the
    category would work in simulation and be empty on the vessel — which is
    the divergence this project keeps meeting."""
    from types import SimpleNamespace

    from gui_backend.core import adapters

    item = SimpleNamespace(
        id="sonar.mounting", name="Sonar mounting geometry", status=1,
        message="PROVISIONAL", remedy="Measure it", measured_value=float("nan"),
        units="", active=False, setup_field=MOUNTING_FIELD,
    )
    report = SimpleNamespace(
        go=True, summary="GO", items=[item], duration_s=0.1,
        run_utc_ms=1789932000000,
    )
    adapted = adapters.preflight_from_ros(report)
    assert adapted["items"][0]["setup_field"] == MOUNTING_FIELD


def test_an_older_vessel_without_the_field_does_not_take_the_panel_down():
    """A Jetson running a system_test built before this field existed sends a
    message without it. Reading it directly would raise, and the Pre-flight
    panel would go blank — which is exactly the class of failure this GUI has
    been bitten by twice, and is far worse than losing one field.
    """
    from types import SimpleNamespace

    from gui_backend.core import adapters

    item = SimpleNamespace(
        id="gnss.fix", name="GNSS fix", status=0, message="ok", remedy="",
        measured_value=9.0, units="", active=False,   # no setup_field at all
    )
    report = SimpleNamespace(
        go=True, summary="GO", items=[item], duration_s=0.1,
        run_utc_ms=1789932000000,
    )
    adapted = adapters.preflight_from_ros(report)
    assert adapted["items"][0]["setup_field"] == ""


def test_the_message_definition_has_the_field():
    """The .msg and the dataclass have to agree or the value silently stops at
    the ROS boundary — present in simulation, absent on the boat."""
    from pathlib import Path

    msg = (
        Path(__file__).resolve().parents[2]
        / "asket_interfaces" / "msg" / "SystemTestItem.msg"
    )
    if not msg.is_file():
        pytest.skip("asket_interfaces not present")
    assert "setup_field" in msg.read_text()


# -- and the distinction it exists to draw ---------------------------------


def test_a_missing_value_and_a_fault_are_distinguishable():
    """The whole point, stated as an assertion.

    Before this, both were WARN with a remedy and nothing separated them. An
    operator with twenty minutes of daylight needs to know which of the two
    ambers in front of them is a job they can finish and which is something
    that needs a repair.
    """
    waiting = mounting_result(found=True, measured=False)
    broken = mounting_result(found=True, error="bad YAML")

    assert waiting.status == WARN and broken.status == FAIL
    assert bool(waiting.setup_field) != bool(broken.setup_field)


def test_skipped_checks_do_not_pretend_to_be_waiting_on_a_form():
    """SKIPPED means "I could not tell" — a node that is not running, not a
    value nobody typed. Sending that to a form would send somebody to fill in
    a field that is already filled in."""
    result = {r.id: r for r in run_checks({}, THRESHOLDS).items}["sonar.mounting"]
    assert result.status == SKIPPED
    assert result.setup_field == ""
