"""Every button the GUI offers must reach something on the real vessel.

The fault this file exists for: "Run pre-flight" did nothing on the Jetson.
Not an error, not a spinner that resolved — nothing. `run_system_test` was
never dispatched by ``RosSource``, so it fell through to "not wired up" and
failed in the same millisecond it was issued, and the Pre-flight panel had no
line to render a failed command in.

``SimSource`` handled it, and so did the browser mock, so every place anybody
reviews the GUI was fine. That is the divergence this project keeps meeting.

No ROS here. ``RosSource.send_command`` and ``_call_service`` touch nothing but
``self._service_clients``, so the dispatch is exercised with stand-ins, the way
the adapters are.
"""

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from gui_backend.core import adapters
from gui_backend.core.ros_source import RosSource

GUI_SRC = Path(__file__).resolve().parents[2] / "asket_gui" / "src"


class _FakeRequest(SimpleNamespace):
    pass


class _FakeClient:
    """Stands in for an rclpy service client."""

    def __init__(self, ready: bool = True) -> None:
        self._ready = ready
        self.sent: list[_FakeRequest] = []
        self.srv_type = SimpleNamespace(Request=_FakeRequest)

    def service_is_ready(self) -> bool:
        return self._ready

    def call_async(self, request):
        self.sent.append(request)
        return SimpleNamespace()


def _source(**clients) -> RosSource:
    """A RosSource with nothing but its command clients.

    ``__init__`` subscribes to topics and therefore needs rclpy; the dispatch
    under test does not.
    """
    source = object.__new__(RosSource)
    source._service_clients = clients
    source._publishers = {}
    source.config = {}
    return source


# -- the fault ------------------------------------------------------------


def test_run_preflight_reaches_the_service():
    client = _FakeClient()
    outcome = _source(run_system_test=client).send_command("run_system_test", {})

    assert outcome.accepted, (
        f"the pre-flight button is still not wired up: {outcome.detail}"
    )
    assert len(client.sent) == 1


def test_the_gui_never_asks_for_an_active_test():
    """Active tests turn motors. They need a consent token and somebody beside
    the boat, and this panel says in as many words that it does not offer them.

    Hard-coded on the server, not passed through from the client, so the
    promise does not depend on what a browser sends.
    """
    client = _FakeClient()
    _source(run_system_test=client).send_command(
        "run_system_test", {"include_active": True, "consent_token": "pretty please"}
    )

    request = client.sent[0]
    assert request.include_active is False
    assert request.consent_token == ""


def test_a_subset_of_checks_can_be_requested():
    client = _FakeClient()
    _source(run_system_test=client).send_command(
        "run_system_test", {"only": ["gnss.fix", "pico.link"]}
    )
    assert client.sent[0].only == ["gnss.fix", "pico.link"]


# -- and when it cannot, it says so ---------------------------------------


def test_an_absent_service_is_reported_not_swallowed():
    """The node is not running, or has not advertised yet. That is a different
    problem from a refusal and needs different words."""
    outcome = _source(run_system_test=_FakeClient(ready=False)).send_command(
        "run_system_test", {}
    )
    assert not outcome.accepted
    assert "not available" in outcome.detail
    assert "run_system_test" in outcome.detail


def test_an_unconfigured_service_is_reported_not_swallowed():
    outcome = _source().send_command("run_system_test", {})
    assert not outcome.accepted
    assert outcome.detail, "a refusal with no reason is the bug, not the fix"


def test_an_unknown_command_names_itself():
    outcome = _source().send_command("make_the_tea", {})
    assert not outcome.accepted
    assert "make_the_tea" in outcome.detail


# -- the class of bug ------------------------------------------------------

#: Commands the GUI can issue that ``RosSource`` does **not** dispatch.
#:
#: Not an oversight list — a record. Each needs more than a service call to be
#: useful, and wiring the call alone would produce a button that succeeds while
#: the panel behind it stays empty:
#:
#: * ``export_mission`` and ``delete_mission`` act on missions the operator
#:   picks from the mission list, and the ``mission`` stream is not produced by
#:   ``RosSource`` either — ``/mission/state`` carries the recorder's state but
#:   not the mission listing, the export destinations or the disk totals the
#:   panel needs. The services exist (``~/export_mission``, ``~/delete_mission``,
#:   ``~/list_missions``); the stream is the missing half.
#:
#: If you wire one, delete it from here and the test below will hold you to it.
KNOWN_NOT_WIRED = {"export_mission", "delete_mission"}


def _commands_the_gui_can_send() -> set[str]:
    pattern = re.compile(r"connection\.command\(\s*'([a-z_]+)'")
    found: set[str] = set()
    for path in GUI_SRC.rglob("*.jsx"):
        found |= set(pattern.findall(path.read_text()))
    for path in GUI_SRC.rglob("*.js"):
        if "/mock/" in str(path):
            continue
        found |= set(pattern.findall(path.read_text()))
    return found


def test_the_frontend_has_commands_to_audit():
    """Guard the guard: a regex that silently matches nothing would make every
    assertion below vacuously true."""
    assert len(_commands_the_gui_can_send()) >= 6


@pytest.mark.parametrize("command", sorted(_commands_the_gui_can_send()))
def test_every_gui_command_is_either_wired_or_knowingly_not(command):
    """A button that reaches nothing must be a decision somebody wrote down.

    ``RosSource`` refuses an unwired command with "is not wired up", which the
    panel now shows — but the operator finding out on a beach is too late, and
    this is the check that finds it here instead.
    """
    outcome = _source().send_command(command, {})
    unwired = not outcome.accepted and "is not wired up" in outcome.detail

    if command in KNOWN_NOT_WIRED:
        assert unwired, (
            f"{command!r} is wired up now — delete it from KNOWN_NOT_WIRED so "
            "the list keeps meaning something"
        )
    else:
        assert not unwired, (
            f"{command!r} is a button in the GUI that reaches nothing on the "
            "vessel. Dispatch it in RosSource.send_command, or add it to "
            "KNOWN_NOT_WIRED with the reason."
        )


# -- the report the panel reads -------------------------------------------


def _report_msg(**overrides):
    """An ``asket_interfaces/SystemTestReport``, as system_test_node sends it."""
    item = SimpleNamespace(
        id="gnss.fix", name="GNSS fix", status=2,
        message="4 satellites, 6 required", remedy="wait, or move clear",
        measured_value=4.0, units="", active=False,
    )
    base = dict(
        go=False, summary="NO-GO — 4 satellites, 6 required",
        items=[item], duration_s=0.42, run_utc_ms=1789932000000,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_the_report_adapter_matches_what_sim_sends():
    """One panel, one dialect.

    The Pre-flight panel is a single component reading one stream. If the ROS
    adapter and ``Report.to_dict()`` disagree about a key, the panel works in
    sim and is subtly wrong in the field — which is the whole reason this
    adapter is checked against the real thing rather than against a fixture.
    """
    from system_test.core.checks import CheckResult, Report

    reference = Report(
        go=False, summary="NO-GO", run_utc_ms=1, duration_s=0.1,
        items=[CheckResult("gnss.fix", "GNSS fix", "FAIL", "msg", "remedy")],
    ).to_dict()

    adapted = adapters.preflight_from_ros(_report_msg())

    assert set(adapted) - {"history"} == set(reference)
    assert set(adapted["items"][0]) == set(reference["items"][0])


def test_the_report_adapter_reads_the_status_enum_by_name():
    """SystemTestItem counts 0..3 and the panel reads words. An off-by-one here
    would show a FAIL as a WARN, which is the wrong direction to be wrong in."""
    for value, expected in ((0, "PASS"), (1, "WARN"), (2, "FAIL"), (3, "SKIPPED")):
        item = _report_msg().items[0]
        item.status = value
        assert adapters.preflight_from_ros(_report_msg(items=[item]))["items"][0][
            "status"
        ] == expected


def test_an_absent_measurement_is_null_not_nan():
    """JSON has no NaN. Serialising one produces output some parsers accept and
    others reject, and null is what it meant anyway."""
    item = _report_msg().items[0]
    item.measured_value = float("nan")
    adapted = adapters.preflight_from_ros(_report_msg(items=[item]))
    assert adapted["items"][0]["measured_value"] is None


def test_history_is_absent_rather_than_claimed_empty():
    """The drift history lives in system_test_node and is not on the wire. An
    empty summary would assert "nothing is degrading", which this side cannot
    know."""
    assert adapters.preflight_from_ros(_report_msg())["history"] is None


# -- and the result reaching the panel ------------------------------------
#
# The other half of the same fault. `RosSource.snapshot("diagnostics")` had no
# branch at all, so it returned None on every tick and the panel read "No
# pre-flight has run yet in this session" permanently — including straight
# after a boot pre-flight that had run, logged NO-GO, and published the report
# this branch now reads.


def _source_with_report(msg):
    from gui_backend.core.ros_source import LatestMessage

    source = _source()
    source.latest = {"preflight_report": LatestMessage(msg, 0.0)}
    return source


def test_the_preflight_report_reaches_the_diagnostics_stream():
    sample = _source_with_report(_report_msg()).snapshot("diagnostics")

    assert sample is not None, (
        "the Pre-flight panel's only source of data is still empty on the "
        "real vessel"
    )
    assert sample.stream == "diagnostics"
    assert sample.payload["go"] is False
    assert sample.payload["items"][0]["id"] == "gnss.fix"
    assert sample.payload["items"][0]["remedy"], "a red word is not an instruction"


def test_the_sample_is_stamped_when_the_report_ran():
    """Not when we serialised it. The panel renders age from this, and a report
    from twenty minutes ago must not read as current."""
    sample = _source_with_report(_report_msg(run_utc_ms=1789932000000)).snapshot(
        "diagnostics"
    )
    assert sample.source_utc_ms == 1789932000000


def test_no_report_yet_is_absent_rather_than_invented():
    source = _source()
    source.latest = {}
    assert source.snapshot("diagnostics") is None
