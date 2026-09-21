"""The mock's payloads must have the same shape as the real backend's.

Mock mode is how the GUI gets reviewed. If it sends a field the vessel never
sends, a panel comes to depend on it and works perfectly right up until the
first time it is pointed at a boat. If it omits one, a real bug goes unnoticed
because the mock never reproduces it.

So the JavaScript payload builders are run under Node, and their key sets are
compared with the Python ones, per stream and per detail level. The test skips
if Node is not installed — it is a cross-language check, not a build dependency.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from asket_sim.core.world import SimWorld, WorldConfig
from gui_backend.core import payloads
from gui_backend.core.sim_source import SimSource

GUI = Path(__file__).resolve().parents[2] / "asket_gui"
DETAILS = ["full", "reduced", "minimal"]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (GUI / "src" / "lib" / "mock").is_dir(),
    reason="node or the frontend mock is not present",
)


def mock_payload_keys() -> dict:
    """Run the JS payload builders under Node and return their key sets."""
    script = textwrap.dedent(
        f"""
        const base = '{GUI}/src/lib/mock';
        const {{ MockWorld }} = await import(base + '/world.js');
        const P = await import(base + '/payloads.js');

        const world = new MockWorld();
        // Run it far enough that the vessel is moving: several payloads only
        // carry their optional fields once there is a speed to divide by.
        for (let i = 0; i < 900; i += 1) world.step(0.1);

        const context = {{ profile: 'full', manual: false, rateBytesPerS: 1000 }};
        const out = {{}};
        for (const detail of ['full', 'reduced', 'minimal']) {{
          out[detail] = {{
            vessel: Object.keys(P.vesselPayload(world, detail)),
            heading: Object.keys(P.headingPayload(world, detail)),
            pico: Object.keys(P.picoPayload(world, detail)),
            power: Object.keys(P.powerPayload(world, detail)),
            sonar: Object.keys(P.sonarPayload(world, detail)),
            lidar: Object.keys(P.lidarPayload(world, detail)),
            link: Object.keys(P.linkPayload(world, detail, context)),
          }};
        }}
        console.log(JSON.stringify(out));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")
    return json.loads(result.stdout)


def python_payload_keys() -> dict:
    source = SimSource(SimWorld(WorldConfig()), time_scale=10.0)
    t = 0.0
    for _ in range(300):
        t += 0.1
        source.step(t)

    snapshot = source.world.snapshot()
    estimate = source._heading_estimate()
    sonar = source.snapshot("sonar", "full").payload

    class Health:
        def __init__(self, data):
            self.__dict__.update(data)
            self.clock_ok = data["clock_ok"]
            self.clock_compromised = data["clock_compromised"]
            self.ping_rate_ok = data["ping_rate_ok"]

    out = {}
    for detail in DETAILS:
        entry = {
            "vessel": list(payloads.vessel_payload(snapshot.vessel, detail)),
            "heading": list(payloads.heading_payload(estimate, detail)),
            "pico": list(payloads.pico_payload(snapshot.pico, detail)),
            "power": list(
                payloads.power_payload(
                    snapshot.battery, detail,
                    survey_remaining_m=source._survey_remaining_m(),
                    speed_ms=max(0.5, snapshot.vessel.sog_ms),
                )
            ),
            "sonar": list(payloads.sonar_payload(Health(sonar), detail)),
            "lidar": list(payloads.lidar_payload(snapshot.lidar, detail, 1)),
            "link": list(
                payloads.link_payload(
                    snapshot.link, profile="full", profile_manual=False,
                    clients=1, rate_bytes_per_s=1000.0, detail=detail,
                )
            ),
        }
        out[detail] = entry
    return out


@pytest.mark.parametrize("detail", DETAILS)
def test_mock_payload_keys_match_the_backend(detail):
    mock = mock_payload_keys()[detail]
    real = python_payload_keys()[detail]

    for stream in sorted(real):
        assert set(mock[stream]) == set(real[stream]), (
            f"{stream} at detail '{detail}' differs.\n"
            f"  only in mock: {sorted(set(mock[stream]) - set(real[stream]))}\n"
            f"  only in real: {sorted(set(real[stream]) - set(mock[stream]))}"
        )


def test_detail_levels_actually_shrink_the_mock_payload():
    """`minimal` must not be `full` with a smaller number in front of it."""
    keys = mock_payload_keys()
    for stream in ("vessel", "pico", "power"):
        full = set(keys["full"][stream])
        minimal = set(keys["minimal"][stream])
        assert minimal < full, f"{stream} does not shrink at minimal detail"


def test_the_arming_block_wording_matches_across_languages():
    """The one sentence in this payload that is prose rather than a number.

    ``arming_block`` is computed in Python by
    ``asket_common.mode_arbitration.arming_block_reason()`` and mirrored in the
    mock so that mock mode shows a reviewer the wording the boat will actually
    send. Two copies of a sentence is a drift risk like any other, so the four
    strings are compared here rather than trusted.
    """
    from asket_common import mode_arbitration as ma

    script = textwrap.dedent(
        f"""
        const P = await import('{GUI}/src/lib/mock/payloads.js');
        console.log(JSON.stringify(P.ARM_BLOCK_STRINGS));
        """
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    ).stdout
    js = json.loads(out)

    assert js["rc_low"] == ma.ARM_BLOCKED_RC_LOW
    assert js["latched"] == ma.ARM_BLOCKED_LATCHED
    assert js["unknown"] == ma.ARM_BLOCKED_UNKNOWN
    assert js["contradictory"] == ma.ARM_BLOCKED_CONTRADICTORY


def test_the_mock_agrees_with_python_on_who_is_blocked():
    """Not just the wording — the decision. A mock that says "blocked" where
    the backend says "fine" teaches an operator the wrong reflex."""
    from asket_common.mode_arbitration import arming_block_reason

    cases = [
        (True, True, False), (True, False, True), (False, False, False),
        (False, True, True), (False, True, False), (None, False, True),
        (None, None, None), (False, None, None),
    ]
    script = textwrap.dedent(
        f"""
        const P = await import('{GUI}/src/lib/mock/payloads.js');
        const cases = {json.dumps(cases)};
        console.log(JSON.stringify(cases.map(
          ([a, r, e]) => P.armingBlockReason(a, r, e)
        )));
        """
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    ).stdout
    js = json.loads(out)

    for (armed, rc_high, latched), got in zip(cases, js):
        want = arming_block_reason(armed, rc_arm_high=rc_high, estop_latched=latched)
        assert got == want, f"armed={armed} rc={rc_high} latched={latched}"


def test_the_mock_and_the_simulator_agree_there_is_one_relay():
    """Both ends of the `0/4 relays closed` bug.

    The panel rendered whatever length of array arrived, and both simulated
    sources sent four. The real path never did — `adapters.py` builds
    `[relay_closed]` from the single `relay=` field — so this was a lie that
    existed only where the GUI gets reviewed, about the one component whose
    job is cutting propulsion.

    Pinned at both ends, because correcting one and not the other would leave
    mock mode and sim mode disagreeing about the e-stop path.
    """
    from asket_sim.core.pico import PicoConfig

    assert PicoConfig().num_relays == 1

    script = textwrap.dedent(
        f"""
        const base = '{GUI}/src/lib/mock';
        const {{ MockWorld }} = await import(base + '/world.js');
        const P = await import(base + '/payloads.js');
        const world = new MockWorld();
        for (let i = 0; i < 100; i += 1) world.step(0.1);
        const pico = P.picoPayload(world, 'full');
        const hull = await import(base + '/../hull.js');
        console.log(JSON.stringify({{
          relays: pico.relay_states.length,
          summary: hull.hullSummary(pico).summary,
          name: hull.decodeRelays(pico.relay_states)[0].name,
          described: Boolean(hull.decodeRelays(pico.relay_states)[0].description),
        }}));
        """
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True,
    ).stdout
    got = json.loads(out)

    assert got["relays"] == 1
    # Named, because unlike the hull wiring its function is confirmed in
    # firmware source. An unnamed safety component is one nobody checks.
    assert got["name"] == "ESC power"
    assert got["described"], "the one relay should say what it does"
    # And the summary says what it is rather than counting to one.
    assert "relays closed" not in got["summary"], got["summary"]
    assert "ESC power" in got["summary"], got["summary"]


def test_the_mock_can_reach_the_same_healthy_baseline_as_the_backend():
    """The dev panel's "Everything working" button, driven under Node.

    Two baselines that drifted apart would be worse than one: a reviewer would
    sign off in mock mode on a picture the backend never produces. So the mock
    runs the same three commands, and is asserted to arrive at the same place —
    autonomous, armed, recording, GO, nothing failing.

    `SimSource.bring_up_healthy()` is the backend half, covered by
    test_healthy_baseline.py.
    """
    script = textwrap.dedent(
        f"""
        const base = '{GUI}/src/lib/mock';
        const {{ MockWorld }} = await import(base + '/world.js');
        const {{ MockTransport }} = await import(base + '/mockTransport.js');
        const P = await import(base + '/payloads.js');

        const world = new MockWorld();
        const transport = new MockTransport(world, {{ timeScale: 1 }});

        // The transport opens on the next turn of the event loop and starts
        // its own ticker. Wait for that, then stop it: this test drives the
        // world itself so it does not depend on wall-clock timing.
        await new Promise((resolve) => setTimeout(resolve, 20));
        clearInterval(transport.timer);

        for (let i = 0; i < 600; i += 1) world.step(0.05);

        // Exactly what MockControls' "Everything working" button issues.
        for (const [name, args] of [
          ['set_mode', {{ mode: 'AUTONOMOUS' }}],
          ['start_mission', {{ name: 'demo' }}],
          ['run_system_test', {{}}],
        ]) {{
          transport.send(JSON.stringify({{ type: 'command', id: name, name, args }}));
          for (let i = 0; i < 40; i += 1) world.step(0.05);
        }}

        const pico = P.picoPayload(world, 'full');
        const report = transport.lastReport;
        console.log(JSON.stringify({{
          mode: pico.mode,
          armed: pico.armed,
          arming_block: pico.arming_block,
          mission: world.missionState,
          go: report ? report.go : null,
          checks: report ? report.items.length : 0,
          failing: report
            ? report.items.filter((i) => i.status === 'FAIL').map((i) => i.id)
            : null,
        }}));
        transport.close();
        """
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout
    got = json.loads(out)

    assert got["mode"] == "AUTONOMOUS", got
    assert got["armed"] is True, got
    assert got["arming_block"] is None, got
    assert got["mission"] == "RECORDING", got
    assert got["go"] is True, got
    assert got["checks"] > 0, "a GO with no checks behind it is not a GO"
    assert got["failing"] == [], got["failing"]
