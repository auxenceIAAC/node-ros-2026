"""What a fully working vessel looks like, and that it is reachable.

Every panel in this GUI renders absence gracefully — "not sent" rather than a
zero or a fault — which is correct, and is also why a freshly started simulator
looks so much like a broken one. In MANUAL, disarmed, not recording and with no
pre-flight run, four panels have nothing to say. Nothing is wrong. It does not
look like anything.

That is not only a presentation problem. Nobody can judge whether a *degraded*
state reads correctly without knowing what the healthy one looks like, and
until this existed the healthy one had never been assembled — every default
view of this interface was a partial one.

So the baseline is a thing that exists and is tested, rather than a sequence
somebody remembers to perform.
"""

import pytest
from asket_sim.core.world import SimWorld, WorldConfig
from gui_backend.core.hub import ClientSession, Hub
from gui_backend.core.sim_source import SimSource

#: Every stream the cockpit subscribes to. A healthy vessel populates all of
#: them; the point of the baseline is that none is left to the imagination.
COCKPIT_STREAMS = (
    "vessel", "pico", "heading", "power", "sonar", "lidar",
    "mission", "diagnostics", "coverage", "track", "plan", "link",
)


@pytest.fixture
def healthy():
    source = SimSource(SimWorld(WorldConfig()), time_scale=1.0)
    hub = Hub(source, tick_hz=20.0)
    t = 1000.0
    for _ in range(600):          # half a minute under way before anything else
        t += 0.05
        hub.tick(t)
    source.bring_up_healthy()
    for _ in range(600):          # and half a minute of it working
        t += 0.05
        hub.tick(t)
    return source


def payload(source, stream):
    sample = source.snapshot(stream, "full", 0)
    assert sample is not None, f"{stream} produced nothing on a healthy vessel"
    return sample.payload


# -- the baseline itself ---------------------------------------------------


@pytest.mark.parametrize("stream", COCKPIT_STREAMS)
def test_every_cockpit_panel_has_something_to_show(healthy, stream):
    assert payload(healthy, stream)


def test_the_vessel_is_under_way(healthy):
    vessel = payload(healthy, "vessel")
    assert vessel["sog_ms"] > 0.2, "a stationary vessel is the thing this fixes"
    assert vessel["heading_valid"] is True
    assert vessel["num_sats"] is not None


def test_the_vessel_is_autonomous_and_armed(healthy):
    pico = payload(healthy, "pico")
    assert pico["mode"] == "AUTONOMOUS"
    assert pico["armed"] is True
    assert pico["rc_link_ok"] is True
    # The arming-block sentence exists for a vessel that cannot start. On a
    # healthy one it must be absent, or it is noise that trains people to
    # ignore it.
    assert pico["arming_block"] is None


def test_a_mission_is_recording(healthy):
    mission = payload(healthy, "mission")
    assert mission["state"] == "RECORDING"
    assert mission["bytes_written"] > 0, "recording, but nothing on the disk"
    assert mission["trajectory_records"] > 0


def test_the_survey_is_being_painted(healthy):
    assert payload(healthy, "coverage")["total"] > 0
    assert payload(healthy, "track")["total"] > 0
    assert len(payload(healthy, "plan")["lines"]) > 0


def test_the_sonar_is_pinging(healthy):
    sonar = payload(healthy, "sonar")
    assert sonar["connected"] is True
    assert sonar["actual_ping_rate_hz"] > 0.5
    assert sonar["points_per_ping"] > 0


def test_the_preflight_says_go(healthy):
    report = payload(healthy, "diagnostics")
    assert report["go"] is True, report["summary"]
    assert report["items"], "a GO with no checks behind it is not a GO"


def test_nothing_is_failing(healthy):
    """GO tolerates warnings; it does not tolerate a FAIL."""
    failed = [i["id"] for i in payload(healthy, "diagnostics")["items"] if i["status"] == "FAIL"]
    assert not failed, f"healthy baseline has failing checks: {failed}"


def test_the_warnings_that_remain_are_the_ones_that_should(healthy):
    """Two warnings are correct and permanent, and must not be tuned away.

    ``pico.estop_feedback`` — nothing confirms the ESC rail collapsed when the
    relay was commanded open, and it will warn until the firmware flag is set.
    ``sonar.mounting`` — nobody has measured the transducer geometry.

    A baseline that silenced them would be teaching the crew that amber means
    nothing. If this test fails because a warning disappeared, check that it
    was fixed rather than hidden.
    """
    warned = {i["id"] for i in payload(healthy, "diagnostics")["items"] if i["status"] == "WARN"}
    assert "pico.estop_feedback" in warned
    assert "sonar.mounting" in warned


def test_every_check_that_is_not_passing_says_what_to_do(healthy):
    for item in payload(healthy, "diagnostics")["items"]:
        if item["status"] in ("WARN", "FAIL", "SKIPPED"):
            assert item["remedy"], f"{item['id']} is amber with no remedy"


# -- and it is reachable the way an operator would reach it ----------------


def test_the_baseline_uses_the_real_command_path():
    """Not a back door into the world.

    A baseline assembled by setting fields directly could show a picture the
    actual controls cannot produce, which would make it worse than useless: a
    reviewer would sign off on a state the boat can never be in.
    """
    source = SimSource(SimWorld(WorldConfig()), time_scale=1.0)
    steps = source.bring_up_healthy()

    assert set(steps) == {"mode", "recording", "preflight"}
    for step, detail in steps.items():
        assert not detail.startswith("REFUSED"), f"{step}: {detail}"


def test_a_client_subscribing_to_a_healthy_vessel_gets_every_stream(healthy):
    """End to end through the hub, which is what the browser actually sees."""
    hub = Hub(healthy, tick_hz=20.0)
    session = ClientSession("browser")
    hub.add_client(session)
    hub.subscribe(session, [{"name": name} for name in COCKPIT_STREAMS])

    t = 5000.0
    seen = set()
    for _ in range(400):
        t += 0.05
        hub.tick(t)
        while not session.outbox.empty():
            message = session.outbox.get_nowait()
            if message.get("type") == "data":
                seen.add(message["stream"])

    missing = set(COCKPIT_STREAMS) - seen
    assert not missing, f"a healthy vessel sent nothing on: {sorted(missing)}"
