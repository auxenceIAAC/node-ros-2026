"""Setup page to sonar geometry, with nothing stubbed in between.

Everything up to now stopped one step short. The page could write a file and
say honestly that the sonar bridge was still using the old numbers; there was
no way to make it use the new ones, because `mounting.yaml` was read on its own
and the overlay was ignored. The fingerprint never moved, so a confirmation
could never arrive, so `apply_setup` was left unwired rather than shipped as a
command that always failed.

This is that path, end to end, through the real `load_mounting` and the real
command lifecycle:

    save  ->  file on disk, and the vessel honestly still on the old geometry
    apply ->  the node re-reads, the digest moves
    tick  ->  the hub sees the digest it predicted and confirms

The last step is the one that matters. The page says "applied" when the vessel
reports the numbers it was sent — not when the file was written, and not when
the service call returned.
"""

import pytest
import yaml
from asket_common.setup_profile import MEASURED
from asket_sim.core.world import SimWorld, WorldConfig
from gui_backend.core import setup_store
from gui_backend.core.hub import Hub
from gui_backend.core.sim_source import SimSource
from omniscan_bridge.core.mounting import SETUP_FIELD_FOR_GEOMETRY

TILT = "vessel.sonar.mounting.tilt_deg"


@pytest.fixture
def hub(monkeypatch, tmp_path):
    """A hub whose setup file and mounting file are both in a temp directory.

    `ASKET_SETUP_FILE` as well as the patched `DEFAULT_PATH`: the backend reads
    the module attribute, and `omniscan_bridge` resolves the shared default
    itself — on purpose, so it need not import this package. Both have to point
    at the same file here or the test would be rehearsing the bug.
    """
    setup_file = tmp_path / "asket_setup.yaml"
    monkeypatch.setattr(setup_store, "DEFAULT_PATH", setup_file)
    monkeypatch.setenv("ASKET_SETUP_FILE", str(setup_file))

    mounting = tmp_path / "mounting.yaml"
    mounting.write_text(yaml.safe_dump({
        "tilt_deg": 35.0, "yaw_deg": 0.0, "pitch_deg": 0.0,
        "lever_x_m": -0.20, "lever_y_m": -0.35, "lever_z_m": -0.15,
        "side": "starboard", "measured": False,
    }))
    return Hub(SimSource(SimWorld(WorldConfig()), mounting_path=mounting))


def save(hub, fields):
    return hub.issue_command("save_setup", {
        "fields": fields, "by": "Auxence", "confirmed": True,
    })


def reported(hub) -> dict:
    return hub.source.state().get("mounting") or {}


def test_the_whole_path(hub):
    before = reported(hub)["fingerprint"]
    assert reported(hub)["geometry"]["tilt_deg"] == 35.0

    # 1. Save. The file is written and the vessel is still on the old numbers,
    #    and the detail says exactly that rather than "done".
    saved = save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    assert saved["status"] == "confirmed"
    assert "still using the old" in saved["detail"]
    assert reported(hub)["fingerprint"] == before, "a file write is not a reload"

    # 2. Apply. Dispatched, and NOT confirmed by the call returning.
    applied = hub.issue_command("apply_setup", {})
    assert applied["status"] == "pending"
    expected = applied["expected_fingerprint"]
    assert expected != before

    # 3. The node has re-read, so the digest it reports has moved to the one
    #    the hub predicted, and the next tick resolves the command.
    assert reported(hub)["fingerprint"] == expected
    assert reported(hub)["geometry"]["tilt_deg"] == 34.2

    resolved = hub.commands.update(hub.source.state(), hub.source.now_utc_ms())
    assert [c.status for c in resolved] == ["confirmed"]
    assert hub.commands.pending == []


def test_the_predicted_digest_is_of_the_merge_not_of_either_file(hub):
    """The setup file is an overlay, so what the sonar ends up on is the
    package default with the answered fields laid over it. A digest of the
    overlay alone would never match anything the node reports."""
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    applied = hub.issue_command("apply_setup", {})

    geometry = reported(hub)["geometry"]
    assert geometry["tilt_deg"] == 34.2, "from the overlay"
    assert geometry["lever_x_m"] == -0.20, "from mounting.yaml"
    assert reported(hub)["fingerprint"] == applied["expected_fingerprint"]


def test_applying_with_nothing_to_apply_says_so_rather_than_hanging(hub):
    applied = hub.issue_command("apply_setup", {})
    assert applied["status"] == "confirmed"
    assert "already running on these numbers" in applied["detail"]
    assert hub.commands.pending == []


def test_an_armed_vessel_refuses_to_apply(hub, monkeypatch):
    """Re-reading geometry part way through a survey places the second half of
    it differently from the first. Same hazard as a save, same gate."""
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    monkeypatch.setattr(
        hub.source, "state",
        lambda: {"pico": {"armed": True, "mode": "AUTO"}, "mission": {"state": "IDLE"}},
    )
    applied = hub.issue_command("apply_setup", {})
    assert applied["status"] == "failed"
    assert "channel 7" in applied["detail"]


def test_a_recording_mission_refuses_to_apply(hub, monkeypatch):
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    monkeypatch.setattr(
        hub.source, "state",
        lambda: {"pico": {"armed": False}, "mission": {"state": "RECORDING"}},
    )
    assert hub.issue_command("apply_setup", {})["status"] == "failed"


def test_a_vessel_not_reporting_its_geometry_is_not_a_vessel_that_refused(hub, monkeypatch):
    """Dispatching here could only ever time out with "no confirmation from the
    vessel", which reads as the vessel having rejected the change. It did not;
    we cannot tell, and the refusal says which."""
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    monkeypatch.setattr(
        hub.source, "state",
        lambda: {"pico": {"armed": False}, "mission": {"state": "IDLE"}},
    )
    applied = hub.issue_command("apply_setup", {})
    assert applied["status"] == "failed"
    assert "not reporting its mounting geometry" in applied["detail"]
    assert "Nothing was sent" in applied["detail"]


def test_an_unparseable_setup_file_is_refused_at_apply(hub, tmp_path):
    (tmp_path / "asket_setup.yaml").write_text("fields: [not a mapping\n")
    applied = hub.issue_command("apply_setup", {})
    assert applied["status"] == "failed"
    assert "will not parse" in applied["detail"]


def test_a_wrong_digest_never_confirms(hub):
    """The predicate is an equality on the exact geometry sent, not "something
    changed". Somebody else saving in between must read as unconfirmed rather
    than as success."""
    save(hub, {TILT: {"value": "34.2", "provenance": MEASURED}})
    hub.issue_command("apply_setup", {}, command_id="mine")

    # A second save lands before the first reload is observed, so the vessel
    # ends up on different numbers from the ones that command was issued for.
    save(hub, {TILT: {"value": "31.0", "provenance": MEASURED}})
    hub.source.send_command("apply_setup", {})

    resolved = hub.commands.update(hub.source.state(), hub.source.now_utc_ms())
    assert [c.status for c in resolved] != ["confirmed"]


def test_all_six_measured_on_the_page_clears_the_preflight_warning(hub):
    """The thing this whole page was built for. `measured: false` has sat in
    `mounting.yaml` for two weeks because clearing it needed an SSH session."""
    assert reported(hub)["measured"] is False

    save(hub, {
        field_id: {"value": "1.0", "provenance": MEASURED}
        for field_id in SETUP_FIELD_FOR_GEOMETRY.values()
    })
    hub.issue_command("apply_setup", {})

    assert reported(hub)["measured"] is True
    report = hub.source.run_preflight(only=["sonar.mounting"])
    item = next(i for i in report.items if i.id == "sonar.mounting")
    assert item.status == "PASS", item.message
    assert "Auxence" in item.message
