"""One value, one place.

A quantity rendered in two panels is a quantity that will eventually disagree
with itself. Heading did: the vessel panel read it from the ``vessel`` stream at
5 Hz and the heading panel from the ``heading`` stream at 2 Hz, so the two rows
sat next to each other reading 029° and 028°. An operator cannot resolve that,
so they stop trusting both — which is worse than either number being slightly
wrong.

These are source-level checks. They cannot prove the rule holds everywhere, but
they pin the cases that were actually wrong, so re-introducing one fails here
rather than on a beach.
"""

import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

GUI = Path(__file__).resolve().parents[2] / "asket_gui" / "src"

pytestmark = pytest.mark.skipif(
    not GUI.is_dir(), reason="the frontend is not present"
)


def source(*parts) -> str:
    return (GUI.joinpath(*parts)).read_text()


def test_heading_is_not_displayed_in_the_vessel_panel():
    """The regression this whole module exists for."""
    text = source("panels", "VesselState.jsx")
    assert "heading_deg" not in text
    assert "heading_valid" not in text
    # And the reason is written down, so the next person does not helpfully
    # add it back.
    assert "Heading is deliberately not here" in text


def test_the_heading_panel_still_has_a_heading_when_the_link_is_at_its_worst():
    """Removing the duplicate must not lose the value on the beacon profile,
    where the heading stream is not carried at all."""
    text = source("panels", "HeadingPanel.jsx")
    assert "vessel.heading_deg" in text
    # ...and says so, because a bearing with no accuracy figure is worth less
    # than one with, and the operator has to know which they are looking at.
    assert "the heading stream is not carried on this link" in text


def test_hull_attitude_and_transducer_attitude_are_labelled_apart():
    """Both are real, both are roll and pitch, and they are different sensors
    reading different numbers. Identical labels would read as a contradiction."""
    assert 'label="Roll / pitch"' in source("panels", "VesselState.jsx")
    assert 'label="Transducer pitch / roll"' in source("panels", "SonarPanel.jsx")


def test_the_status_strip_reads_the_same_streams_as_the_panels_it_summarises():
    """The strip is a summary, not a second source. If it read anything the
    owning panel does not, the two could disagree — which is the exact failure
    this file exists to prevent, reproduced in the header."""
    strip = source("components", "StatusStrip.jsx")
    for stream in ("power", "mission", "link"):
        assert f"streamPayload(state, '{stream}')" in strip

    # The fields it shows are the ones those panels show.
    assert "state_of_charge" in strip and "state_of_charge" in source("panels", "PowerPanel.jsx")
    assert "endurance_s" in strip and "endurance_s" in source("panels", "PowerPanel.jsx")
    assert "elapsed_s" in strip and "elapsed_s" in source("panels", "MissionPanel.jsx")
    assert "active_link" in strip and "active_link" in source("panels", "LinkStatus.jsx")


def test_the_status_strip_degrades_stale_values_like_everything_else():
    """A value in the header is still a live value. One that has gone stale
    while pinned above the fold would be the most persuasive lie on screen."""
    strip = source("components", "StatusStrip.jsx")
    assert "staleness" in strip and "thresholdsForRate" in strip


def test_map_layer_colours_are_declared_once():
    """The legend names the colours the layers are painted with. Two literals
    for one colour is a legend that lies after the next edit."""
    text = source("panels", "MissionMap.jsx")
    body = text[text.index("const COLOURS"):]
    # Every paint reference goes through COLOURS, so no raw hex survives in a
    # paint block below the declaration.
    paints = re.findall(r"'(?:line|fill|circle)-color':\s*([^,}]+)", body)
    assert paints, "no layer paints found — has the file been restructured?"
    for value in paints:
        assert value.strip().startswith("COLOURS."), f"raw colour in a paint: {value}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_relay_and_esc_bytes_are_decoded_into_words():
    """`Relays · · · ·` and `ESCs e1 e1` are precise and unreadable. Somebody who
    did not write the firmware cannot tell whether e1 is normal or catastrophic."""
    out = subprocess.run(
        ["node", "--input-type=module", "-e", textwrap.dedent(f"""
            const m = await import('{GUI}/lib/hull.js');
            const armed = m.hullSummary({{
                armed: true, relay_states: [true, true, false, false], esc_status: [0, 3],
            }});
            const disarmed = m.hullSummary({{
                armed: false, relay_states: [false, false], esc_status: [1, 1],
            }});
            console.log(JSON.stringify({{ armed, disarmed }}));
        """)],
        capture_output=True, text=True, check=True,
    )
    import json
    data = json.loads(out.stdout)

    armed = data["armed"]
    assert armed["relays"][0]["text"] == "closed"
    assert armed["relays"][2]["text"] == "open"
    assert armed["escs"][0]["text"] == "running"
    # An undocumented code is reported, never interpreted.
    assert armed["escs"][1]["text"] == "code 3"
    assert armed["escs"][1]["unknown"] is True
    # A thruster that is not running while armed is promoted out of the
    # collapsed section: a fault is never hidden behind a disclosure triangle.
    assert armed["alert"] == "1 of 2 thrusters not running while armed"

    # Disarmed, a stopped thruster is the correct state. Calling it a fault
    # would train the crew to ignore the line that matters.
    assert data["disarmed"]["alert"] is None


def test_nothing_is_given_a_name_nobody_has_confirmed():
    """A caption is something an operator acts on, so it has to be earned.

    This used to assert that `RELAY_LABELS` was empty, which was the right rule
    read through the wrong fact: the panel said `0/4 relays closed` and none of
    the four was named. There is **one** relay, `ESTOP_RELAY_PIN` on GPIO21,
    and its function is confirmed in `firmware/pico-node_v4/` — so naming it
    "ESC power" is reading the source, not inventing a caption.

    What is still unconfirmed stays unnamed: any further relay this hull might
    grow, and every ESC status code beyond 0. The firmware reports no ESC code
    over serial at all.
    """
    text = source("lib", "hull.js")

    # The one relay whose function is in the firmware, named.
    assert "export const RELAY_LABELS = { 0: 'ESC power' };" in text

    # Nothing hull-specific invented for the rest. `relayName()` falls back to
    # a bare index, and the ESC codes keep their PROVISIONAL marker.
    assert "`Relay ${index + 1}`" in text
    assert "PROVISIONAL (Q7)" in text
    assert "ESC_STATUS_LABELS = { 0: 'running' }" in text
    for invented in ("Bilge", "pump", "Nav light", "Main power"):
        assert invented not in text, f"hull.js invents a name: {invented}"


# -- collapsing detail ----------------------------------------------------
#
# Roughly forty-five numbers were on screen at once, and nobody monitors
# forty-five. Folding the detail away is only safe because of two rules, and
# both are worth a test: a collapsed panel shows a verdict rather than raw data,
# and anything off-nominal forces itself back open. Without the second, this is
# just a way to hide faults.

COLLAPSIBLE = {
    "PowerPanel.jsx": "power",
    "HeadingPanel.jsx": "heading",
    "SonarPanel.jsx": "sonar",
    "VesselState.jsx": "vessel",
    "LidarPanel.jsx": "obstacles",
    "DiagnosticsPanel.jsx": "preflight",
    "MissionPanel.jsx": "recording",
    "LinkStatus.jsx": "link",
}


def summary_block(panel: str) -> str:
    """The JSX passed as `summary` — what stays on screen when the panel folds.

    Sliced to the line that closes the Panel's props (a lone ``>``), rather than
    to the first ``>`` found, which lands inside the JSX itself.
    """
    text = source("panels", panel)
    start = text.index("summary={")
    end = text.index("\n    >\n", start)
    return text[start:end]


@pytest.mark.parametrize("panel,panel_id", sorted(COLLAPSIBLE.items()))
def test_every_collapsible_panel_can_force_itself_open(panel, panel_id):
    """A fault behind a disclosure triangle is a hidden fault."""
    text = source("panels", panel)
    assert f'id="{panel_id}"' in text
    assert "collapsible" in text
    assert "forceOpen={Boolean(forced)}" in text
    assert "forceReason={forced}" in text
    # The condition has to be built from the data, not hard-coded false.
    assert "const forced =" in text


@pytest.mark.parametrize("panel", sorted(COLLAPSIBLE))
def test_every_collapsible_panel_shows_something_when_folded(panel):
    """Folding a panel to nothing but its title would make the cockpit tidier
    and useless."""
    assert "summary={" in source("panels", panel)


def test_the_sonar_clock_offset_is_never_folded_away():
    """The one value whose drift ruins an entire dataset with no other visible
    symptom: the survey looks perfect on the day and is un-georeferenceable back
    home. Behind a disclosure triangle nobody would ever look at it."""
    summary = summary_block("SonarPanel.jsx")
    assert "clock_offset_ms" in summary
    assert "clock_compromised" in summary


def test_power_folds_to_a_verdict_rather_than_a_percentage():
    """'95%' does not say whether it is enough to finish. The comparison does."""
    assert "verdict" in summary_block("PowerPanel.jsx")
    assert "Enough charge to finish the planned survey" in source("panels", "PowerPanel.jsx")


def test_the_operators_preference_survives_a_reload():
    text = source("lib", "collapse.js")
    assert "localStorage" in text
    # ...and a browser with storage disabled still renders a cockpit.
    assert text.count("catch") >= 2


def test_a_forced_panel_does_not_overwrite_what_the_operator_chose():
    """When the fault clears the panel goes back to however it was left, rather
    than to wherever the fault happened to put it."""
    panel = source("components", "Panel.jsx")
    assert "const open = forceOpen || openPref;" in panel


def test_lidar_reports_beams_swept_not_just_returns():
    """`0 of 0` made open water and a blind sensor identical. They need very
    different responses."""
    assert "beams_per_revolution" in source("panels", "LidarPanel.jsx")
    assert "beams_per_revolution" in source("lib", "mock", "payloads.js")


def test_the_synthetic_basemap_says_so_once_not_on_every_tile():
    """Tile indices and coordinates repeated across every 256 px square are
    debug output, and they were the loudest thing on a map whose job is showing
    coverage."""
    tiles = source("lib", "mock", "tiles.js")
    assert "ASKET_TILE_LABELS" in tiles
    assert "SYNTHETIC" not in tiles, "the warning belongs on the map, not per tile"
    assert "Synthetic basemap — not a chart" in source("panels", "MissionMap.jsx")
