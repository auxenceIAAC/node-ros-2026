"""The page's copy of the gate must reach the Python's conclusions.

`lib/setup.js`'s `gateDecision` is not the guard — `setup_gate.evaluate` on the
Jetson is, off the vessel's own report, where a stale page cannot satisfy it.
What the page's copy is for is saying *why* the button is not offering itself,
before anybody presses it.

That makes drift a specific, nasty failure: a button that looks available and
always fails, or one that refuses a change the vessel would have taken. Both
teach people to stop believing the page. So the two are compared here, over
every state that reaches a different branch, including the wording — because
the remedy sentence is the actionable half and "disarm the vessel" without
"channel 7" sends somebody looking for a button that does not exist.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from gui_backend.core import setup_gate

GUI = Path(__file__).resolve().parents[2] / "asket_gui"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (GUI / "src" / "lib" / "setup.js").is_file(),
    reason="node or the frontend is not present",
)

#: Every state that reaches a different branch, plus the ones that nearly do.
#:
#: Keyed by name; each is the `pico` and `mission` payload as the vessel
#: reported it. The page holds these inside `state.streams`, the backend holds
#: them flat, so each case is given in both shapes from one definition.
CASES = {
    "disarmed_idle": ({"armed": False, "mode": "MANUAL"}, {"state": "IDLE"}),
    "armed": ({"armed": True, "mode": "AUTO"}, {"state": "IDLE"}),
    "recording": ({"armed": False, "mode": "AUTO"}, {"state": "RECORDING"}),
    "armed_and_recording": ({"armed": True, "mode": "AUTO"}, {"state": "RECORDING"}),
    "no_pico_payload": (None, {"state": "IDLE"}),
    "pico_without_armed": ({"mode": "MANUAL"}, {"state": "IDLE"}),
    "no_mission_payload": ({"armed": False, "mode": "MANUAL"}, None),
    "nothing_at_all": (None, None),
    # Lower case, because the recorder's state is a string off the wire and
    # one side upper-casing it while the other does not would be a gate that
    # silently stops noticing a recording.
    "recording_lower_case": ({"armed": False, "mode": "AUTO"}, {"state": "recording"}),
    "paused_is_not_recording": ({"armed": False, "mode": "AUTO"}, {"state": "PAUSED"}),
}


def python_decisions() -> dict:
    out = {}
    for name, (pico, mission) in CASES.items():
        state = {}
        if pico is not None:
            state["pico"] = pico
        if mission is not None:
            state["mission"] = mission
        out[name] = setup_gate.evaluate(state).to_dict()
    return out


def js_decisions() -> dict:
    store_states = {}
    for name, (pico, mission) in CASES.items():
        streams = {}
        if pico is not None:
            streams["pico"] = {"payload": pico}
        if mission is not None:
            streams["mission"] = {"payload": mission}
        store_states[name] = {"streams": streams}

    script = textwrap.dedent(
        f"""
        const {{ gateDecision }} = await import('{GUI}/src/lib/setup.js');
        const cases = {json.dumps(store_states)};
        const out = {{}};
        for (const [name, state] of Object.entries(cases)) {{
          out[name] = gateDecision(state);
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


@pytest.fixture(scope="module")
def both():
    return python_decisions(), js_decisions()


def test_the_same_states_reach_the_same_verdict(both):
    python, js = both
    for name in python:
        assert js[name]["allowed"] == python[name]["allowed"], name
        assert js[name]["code"] == python[name]["code"], name


def test_the_wording_is_identical(both):
    """The remedy is the actionable half. Two versions of it means one of them
    is the version nobody maintains."""
    python, js = both
    for name in python:
        assert js[name]["reason"] == python[name]["reason"], name
        assert js[name]["remedy"] == python[name]["remedy"], name


def test_the_cases_cover_every_branch(both):
    python, _ = both
    codes = {case["code"] for case in python.values()}
    assert codes == {"", setup_gate.BLOCKED_ARMED, setup_gate.BLOCKED_RECORDING,
                     setup_gate.BLOCKED_UNKNOWN}


def test_a_recording_in_lower_case_still_blocks(both):
    """The recorder's state is a string off the wire. One side upper-casing it
    and the other not would be a gate that quietly stops noticing."""
    python, js = both
    assert python["recording_lower_case"]["code"] == setup_gate.BLOCKED_RECORDING
    assert js["recording_lower_case"]["code"] == setup_gate.BLOCKED_RECORDING


def test_a_page_with_no_vessel_data_offers_nothing(both):
    """The browser-mock case and the just-connected case. Offering a save here
    would be the page deciding for itself that the boat is safe."""
    python, js = both
    assert not python["nothing_at_all"]["allowed"]
    assert not js["nothing_at_all"]["allowed"]
