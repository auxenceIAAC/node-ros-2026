"""The firmware's arbitration and the Python mirror must agree, cell for cell.

The Pico cannot be run here, but the sketch compiles against the stub Arduino
core in ``firmware/test/``. So ``firmware/test/arbitration_table.cpp`` drives
the **real** ``update_state()`` through every combination of channel 8, channel
7, the autonomy request, the heartbeat, the receiver failsafe and the e-stop
latch, and prints the decision. This file drives
:mod:`asket_common.mode_arbitration` through the same combinations and compares.

That is not a substitute for bench validation, and it proves nothing about
SBUS timing, relays or the USB stack. What it does prove is the one thing that
has bitten this project twice: **two ends of a contract disagreeing with
nothing noticing.** A change to the firmware table that is not mirrored in
Python, or the reverse, fails here rather than on the water.

Why compile the whole sketch instead of lifting the function out of it
----------------------------------------------------------------------

v3 had a pure ``arbitrate_mode()`` that could be extracted and compiled on its
own. v4 does not: ``update_state()`` reads globals and writes its answer
through ``apply_transition()``. Refactoring it into a pure function would be a
firmware edit, and firmware edits belong on the bench with the boat out of the
water — not folded into an integration. Compiling the sketch and setting its
globals tests the code that actually ships, which is the better bargain anyway.

If ``g++`` or ``make`` is unavailable the tests skip rather than passing
quietly — a check that silently does nothing is worse than no check.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from asket_common.mode_arbitration import (
    ARM_THRESHOLD,
    ESC_ARM_DELAY_MS,
    ESTOP_FEEDBACK_ENABLED,
    FIRMWARE_MODE_NUMBERS,
    HEARTBEAT_TIMEOUT_S,
    MODE_AUTONOMOUS,
    MODE_ESTOP,
    MODE_LOW_MAX,
    MODE_MANUAL,
    MODE_MID_MAX,
    MODE_RANK,
    arbitrate,
)

ROOT = Path(__file__).resolve().parents[3]
FIRMWARE = ROOT / "firmware" / "pico-node_v4" / "pico-node_v4.ino"
TEST_DIR = ROOT / "firmware" / "test"

pytestmark = pytest.mark.skipif(
    shutil.which("g++") is None or shutil.which("make") is None,
    reason="no C++ toolchain to check the firmware against",
)


def _sketch() -> str:
    assert FIRMWARE.is_file(), f"firmware sketch not found at {FIRMWARE}"
    return FIRMWARE.read_text()


def _constant(pattern: str, what: str) -> int:
    match = re.search(pattern, _sketch())
    assert match, f"{what} not found in the sketch"
    return int(match.group(1))


@pytest.fixture(scope="module")
def firmware_rows():
    """The compiled firmware's decision table, keyed by its inputs."""
    subprocess.run(
        ["make", "-C", str(TEST_DIR), "table"], check=True, capture_output=True
    )
    out = subprocess.run(
        [str(TEST_DIR / "fwtable")], check=True, capture_output=True, text=True
    ).stdout

    rows = {}
    for line in out.strip().splitlines():
        ch8, ch7, wants, link, fs, latch_in, mode, armed, latch_out = (
            int(x) for x in line.split()
        )
        rows[(ch8, ch7, bool(wants), bool(link), bool(fs), bool(latch_in))] = (
            FIRMWARE_MODE_NUMBERS[mode],
            bool(armed),
            bool(latch_out),
        )
    return rows


def test_the_table_covers_the_whole_input_space(firmware_rows):
    # 7 channel-8 values (zone interiors and both boundaries) x 4 channel-7
    # values x 2^4 booleans.
    assert len(firmware_rows) == 7 * 4 * 2 * 2 * 2 * 2 == 448


def test_firmware_agrees_with_python_on_every_cell(firmware_rows):
    """The whole point of this file."""
    mismatches = []
    for key, got in sorted(firmware_rows.items()):
        ch8, ch7, wants, link, fs, latch_in = key
        expected = arbitrate(
            ch8,
            ch7,
            serial_wants_auto=wants,
            link_live=link,
            sbus_failsafe=fs,
            estop_latched=latch_in,
        )
        want = (expected.mode, expected.armed, expected.estop_latched)
        if got != want:
            mismatches.append(
                f"  ch8={ch8} ch7={ch7} wantauto={wants} link={link} "
                f"failsafe={fs} latch={latch_in}: firmware={got} python={want}"
            )
    assert not mismatches, (
        f"firmware and Python arbitration disagree on {len(mismatches)} of "
        f"{len(firmware_rows)} cells:\n" + "\n".join(mismatches[:20])
    )


# -- structural rules, read straight off the compiled firmware --------------
#
# These deliberately do not consult the Python mirror. If both sides drifted
# together the test above would still pass; these would not.


def test_the_transmitter_is_sovereign(firmware_rows):
    """Software can never make the vessel more permissive than channel 8 allows.

    The rule the whole safety argument rests on. Autonomy is reachable only
    from the top zone, and never while the receiver declares failsafe.
    """
    for (ch8, _ch7, _w, _l, fs, _latch), (mode, _armed, _out) in firmware_rows.items():
        if mode == MODE_AUTONOMOUS:
            assert ch8 >= MODE_MID_MAX, f"autonomy granted at ch8={ch8}"
            assert not fs, "autonomy granted while the receiver declared failsafe"
        if ch8 < MODE_LOW_MAX:
            assert mode == MODE_ESTOP, f"ch8={ch8} is the E-stop zone"


def test_the_top_zone_permits_autonomy_rather_than_granting_it(firmware_rows):
    """Channel 8 high on its own does not start a mission.

    The switch says "you may"; the Jetson says "I am"; the heartbeat says "I
    still am". This is the distinction v4 draws that v3 did not, and it is the
    one most likely to be lost in a future refactor.
    """
    for (ch8, _ch7, wants, link, fs, _latch), (mode, _a, _o) in firmware_rows.items():
        if ch8 >= MODE_MID_MAX and not fs:
            expected = MODE_AUTONOMOUS if (wants and link) else MODE_MANUAL
            assert mode == expected, (
                f"ch8={ch8} wantauto={wants} link={link} gave {mode}"
            )


def test_a_receiver_failsafe_forces_estop_from_every_position(firmware_rows):
    for (_ch8, _ch7, _w, _l, fs, _latch), (mode, armed, _o) in firmware_rows.items():
        if fs:
            assert mode == MODE_ESTOP
            assert armed is False


def test_estop_is_never_armed(firmware_rows):
    """Whatever else is true, the E-stop mode does not turn propellers."""
    for _key, (mode, armed, _out) in firmware_rows.items():
        if mode == MODE_ESTOP:
            assert armed is False


def test_a_held_latch_keeps_the_vessel_disarmed(firmware_rows):
    """And clears only when the operator disarms or selects ESTOP.

    This is the mechanism behind "why will it not arm", and the one an operator
    meets most often.
    """
    for (ch8, ch7, _w, _l, _fs, latch_in), (mode, armed, latch_out) in (
        firmware_rows.items()
    ):
        arm_high = ch7 > ARM_THRESHOLD
        if latch_in and arm_high and mode != MODE_ESTOP:
            assert latch_out is True, "the latch cleared without operator action"
            assert armed is False, "a latched e-stop armed the vessel"
        if latch_in and (not arm_high or mode == MODE_ESTOP):
            assert latch_out is False, "the latch survived an operator reset"


def test_arming_needs_channel_seven(firmware_rows):
    for (_ch8, ch7, _w, _l, _fs, _latch), (_mode, armed, _o) in firmware_rows.items():
        if armed:
            assert ch7 > ARM_THRESHOLD


def test_no_decision_is_more_permissive_than_its_zone(firmware_rows):
    for (ch8, _ch7, _w, _l, _fs, _latch), (mode, _a, _o) in firmware_rows.items():
        ceiling = MODE_ESTOP if ch8 < MODE_LOW_MAX else MODE_AUTONOMOUS
        assert MODE_RANK[mode] <= MODE_RANK[ceiling]


# -- constants mirrored out of the firmware ---------------------------------
#
# Each of these exists in Python only because it cannot be read off the wire,
# which makes every one a drift risk. Where a value is spelled in two Python
# places as well as the sketch, all three are checked — a two-way check would
# pass while the third quietly disagreed.


def test_the_channel_thresholds_match_the_firmware():
    assert _constant(r"const int MODE_LOW_MAX\s*=\s*(\d+);", "MODE_LOW_MAX") == (
        MODE_LOW_MAX
    )
    assert _constant(r"const int MODE_MID_MAX\s*=\s*(\d+);", "MODE_MID_MAX") == (
        MODE_MID_MAX
    )
    assert _constant(r"const int ARM_THRESHOLD\s*=\s*(\d+);", "ARM_THRESHOLD") == (
        ARM_THRESHOLD
    )


def test_the_gui_thresholds_match_the_arbitration_thresholds():
    """pico_state derives the RC-selected mode for the panel; mode_arbitration
    decides it. The two spell the same firmware constants differently, so they
    are checked against each other as well as against the sketch."""
    from gui_backend.core import pico_state

    assert pico_state.CH8_ESTOP_MAX == MODE_LOW_MAX
    assert pico_state.CH7_ARM_MIN == ARM_THRESHOLD


def test_the_heartbeat_timeout_matches_the_firmware():
    ms = _constant(
        r"const unsigned long HEARTBEAT_TIMEOUT_MS = (\d+);", "HEARTBEAT_TIMEOUT_MS"
    )
    assert ms / 1000.0 == HEARTBEAT_TIMEOUT_S


def test_the_sbus_range_matches_the_firmware():
    from gui_backend.core import pico_state

    for name, expected in (
        ("SBUS_MIN", pico_state.SBUS_MIN),
        ("SBUS_MID", pico_state.SBUS_MID),
        ("SBUS_MAX", pico_state.SBUS_MAX),
    ):
        assert _constant(rf"const uint16_t {name}\s*=\s*(\d+);", name) == expected, name


def test_the_protocol_version_matches_the_firmware():
    """v4's whole point. If these drift, a parser reads a layout it does not
    know and the pre-flight's version check is the thing that catches it — so
    the check itself must be pinned to the sketch."""
    from gui_backend.core.pico_state import PROTOCOL_VERSION

    assert _constant(r"#define FW_VERSION (\d+)", "FW_VERSION") == PROTOCOL_VERSION


def test_the_esc_arm_delay_matches_the_firmware():
    """How long the firmware holds neutral after closing the relay.

    Unpinned, this is how the next drift starts: software would read a vessel
    correctly waiting out its arm window as a vessel ignoring its throttle.
    """
    assert _constant(
        r"const unsigned long ESC_ARM_DELAY_MS = (\d+);", "ESC_ARM_DELAY_MS"
    ) == ESC_ARM_DELAY_MS


def test_the_estop_feedback_flag_matches_the_firmware():
    """The flag the pre-flight warns about, pinned to the thing it describes.

    If somebody enables the feedback in firmware and not here, the pre-flight
    keeps warning about a problem that no longer exists — and a crew that learns
    to ignore one amber row learns to ignore the next. If the reverse, the GUI
    stops warning about a real one, which is worse.
    """
    assert bool(
        _constant(r"#define ESTOP_FEEDBACK_ENABLED (\d+)", "ESTOP_FEEDBACK_ENABLED")
    ) is ESTOP_FEEDBACK_ENABLED


def test_the_failsafe_timeouts_are_both_500ms():
    """Documented as 600 ms in several places for a long time. They are 500."""
    for name in ("SBUS_FAILSAFE_TIMEOUT_MS", "SERIAL_FAILSAFE_TIMEOUT_MS"):
        assert _constant(rf"const unsigned long {name} = (\d+);", name) == 500, name


def test_the_firmware_still_has_no_serial_estop_verb():
    """Deliberately absent, and this test is here so its return is a decision
    rather than an accident.

    ``MODE_ESTOP`` exists in the firmware and works; a *serial path into it*
    does not. Adding one is a safety-chain change for the bench, with the boat
    out of the water, as its own step. If somebody adds the verb, this test
    fails and they have to come and delete it on purpose.
    """
    source = _sketch()
    handler = source[source.index("// --- Mode verbs"):]
    handler = handler[: handler.index("\n}\n")]
    assert "MODE AUTO" in handler
    assert "MODE MANUAL" in handler
    assert "ESTOP" not in handler, (
        "a serial ESTOP verb has appeared in the firmware's mode verbs — that "
        "is a safety-chain change and belongs on the bench, not in a merge"
    )


def test_the_bench_override_flag_has_not_come_back():
    """``BENCH_NO_RC_OVERRIDE`` at 1 removed the hardware E-stop's authority,
    and nothing distinguished the two binaries once flashed. v4 dropped it; this
    keeps it dropped."""
    assert "#define BENCH_NO_RC_OVERRIDE" not in _sketch()


def test_mode_numbering_is_bridged_by_name_not_by_offset():
    """The firmware counts 1/2/3 and PicoStatus.msg counts 0/1/2. Adding one is
    the obvious shortcut and it silently breaks the day either side renumbers."""
    assert FIRMWARE_MODE_NUMBERS == {
        1: MODE_ESTOP,
        2: MODE_MANUAL,
        3: MODE_AUTONOMOUS,
    }
    source = _sketch()
    assert "enum OperationMode { MODE_ESTOP = 1, MODE_MANUAL = 2, MODE_AUTONOMOUS = 3 };" in (
        source
    )


def test_the_firmware_prints_every_key_the_parser_expects():
    """The other half of the v3/v4 disaster, and the half still unguarded.

    ``ver=`` protects against the firmware changing shape *wholesale* — a
    parser that does not know the version rejects the line. It does not protect
    against a single field being renamed inside a version: the parser matches
    whole keys, so a renamed field becomes ``None`` and the panel shows "not
    sent" for a vessel that is sending perfectly well.

    That is quieter than the original bug and the same species of it: a dead
    field and a quiet vessel are indistinguishable on a screen built to render
    absence gracefully. So every key the parser expects is asserted against the
    sketch's own ``STATE`` print. If you rename one, bump ``FW_VERSION`` — and
    this test will remind you.
    """
    from gui_backend.core.pico_state import _FIELDS

    source = _sketch()
    start = source.index('Serial.print(F("STATE ver="))')
    end = source.index("last_status = now;", start)
    printed = source[start:end]

    missing = [key for key in _FIELDS if f'{key}=' not in printed and key != "ver"]
    assert not missing, (
        f"the firmware's STATE line no longer prints: {missing}. "
        "Either restore the field or bump FW_VERSION and update the parser."
    )
