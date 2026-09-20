"""Simulated Pico.

The Pico is the authority on what the vessel is *actually* doing. The GUI must
never display a requested mode — only a confirmed one (docs/safety.md rule 4).
This simulator therefore models the thing that makes that rule non-trivial: a
mode request takes time to take effect, can be rejected, and can be lost.

It also models the two things that are *not* ours to control: the hardware
killswitch and RC channel 8. Software can observe them. Software cannot move
them. Attempting to do so from here is a bug, and there is a test for it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

MODE_ESTOP = 0
MODE_MANUAL = 1
MODE_AUTONOMOUS = 2

MODE_NAMES = {MODE_ESTOP: "ESTOP", MODE_MANUAL: "MANUAL", MODE_AUTONOMOUS: "AUTONOMOUS"}
MODE_VALUES = {v: k for k, v in MODE_NAMES.items()}

#: The *firmware's* numbering, which is not this module's.
#: ``enum OperationMode { MODE_ESTOP = 1, MODE_MANUAL = 2, MODE_AUTONOMOUS = 3 }``
#: Used only when writing a STATE line, so the text the simulator puts on the
#: wire is the text the firmware would put there. The two scales are mapped
#: explicitly rather than by adding one: an off-by-one here would make the
#: simulator agree with a parser that is wrong about the real thing.
FIRMWARE_MODE = {MODE_ESTOP: 1, MODE_MANUAL: 2, MODE_AUTONOMOUS: 3}

#: The firmware version this simulator pretends to be. Must track
#: ``FW_VERSION`` in ``firmware/pico-node_v4/pico-node_v4.ino``; a test asserts
#: it matches what ``gui_backend`` is willing to parse, so the three cannot
#: drift apart silently.
FIRMWARE_VERSION = 4

#: Raw SBUS counts, as the firmware reports them: 11 bits clamped to this
#: range, 991 at centre. **Not** percentages.
SBUS_MIN = 172
SBUS_MID = 991
SBUS_MAX = 1811

#: The firmware's own thresholds, on that scale.
CH8_ESTOP_MAX = 700     # Ch8 below this -> MODE_ESTOP
CH8_MANUAL_MAX = 1400   # Ch8 below this -> MANUAL forced; above -> autonomy permitted
CH7_ARM_MIN = 1000      # Ch7 above this -> armed


@dataclass
class PicoConfig:
    #: How long the Pico takes to act on a mode request and report back.
    confirm_delay_s: float = 0.6
    #: Probability a mode request is simply lost, so the GUI's timeout path is
    #: exercised rather than assumed.
    request_loss_probability: float = 0.0
    #: **One.** There is one relay on this hull — ``ESTOP_RELAY_PIN`` on
    #: GPIO21, cutting ESC power — and the firmware has never had more.
    #:
    #: This was 4, a placeholder nobody revisited, and the GUI faithfully
    #: rendered whatever length of array arrived: the panel read
    #: ``0/4 relays closed``, which is a statement about the e-stop path and
    #: it was false. The real path was never wrong — ``adapters.py`` builds
    #: ``[relay_closed]`` from the single ``relay=`` field — so the lie lived
    #: only in simulation, which is exactly where this GUI gets reviewed.
    num_relays: int = 1
    num_escs: int = 2


@dataclass
class PicoSample:
    utc_ms: int
    mode: int
    armed: bool
    estop_latched: bool
    relay_states: list[bool]
    esc_status: list[int]
    rc_link_ok: bool
    rc_channel7_raw: int
    rc_channel8_raw: int
    hardware_killswitch_engaged: bool
    firmware_version: int = FIRMWARE_VERSION


class PicoSim:
    def __init__(self, config: PicoConfig | None = None, seed: int = 4) -> None:
        self.cfg = config or PicoConfig()
        self._rng = random.Random(seed)
        self.mode = MODE_MANUAL
        self.armed = False
        self.estop_latched = False
        self.rc_link_ok = True
        #: The sovereign channels, in raw SBUS counts. Only the operator's
        #: transmitter moves these; nothing in software may write them.
        #: Ch8 selects the mode (3 positions), Ch7 arms (2 positions).
        self.rc_channel8_raw = SBUS_MAX
        self.rc_channel7_raw = SBUS_MAX
        self.hardware_killswitch_engaged = False
        self.relay_states = [False] * self.cfg.num_relays
        self.esc_status = [0] * self.cfg.num_escs

        #: Firmware identity, reported on the wire so the Jetson can refuse a
        #: version it cannot parse. Settable so a test can simulate the exact
        #: failure this whole mechanism exists to catch.
        self.firmware_version = FIRMWARE_VERSION

        #: Fields the firmware reports that have no equivalent in PicoSample.
        #: They exist so state_line() is complete rather than a subset — a
        #: simulator that emits fewer fields than the real thing is a simulator
        #: that cannot catch a parser ignoring one.
        self.serial_wants_auto = False
        self.link_live = True
        self.rc_throttle_raw = SBUS_MID
        self.rc_yaw_raw = SBUS_MID
        self.sbus_frames_ok = 0
        self.sbus_frames_bad = 0
        self.sbus_frame_lost = False
        self._pending: tuple[int, float] | None = None
        self._t = 0.0

    # -- command surface --------------------------------------------------

    def request_mode(self, mode: int) -> bool:
        """Queue a mode change. Returns whether the request was *accepted*.

        Acceptance is not confirmation. Confirmation is a later ``sample()``
        reporting the new mode.
        """
        if mode not in MODE_NAMES:
            return False
        if self._rng.random() < self.cfg.request_loss_probability:
            return True  # accepted, then silently lost — the nastiest case
        self._pending = (mode, self._t + self.cfg.confirm_delay_s)
        return True

    # -- hardware-side events, not commandable from software --------------

    def set_hardware_killswitch(self, engaged: bool) -> None:
        """Operator pressed the physical killswitch. Simulation input only."""
        self.hardware_killswitch_engaged = engaged
        if engaged:
            self.mode = MODE_ESTOP
            self.estop_latched = True
            self.armed = False
            self._pending = None

    def set_rc_channel8(self, raw: int) -> None:
        """Operator moved the RC mode selector. Simulation input only.

        ``raw`` is an SBUS count (172..1811), not a percentage. The firmware
        compares against 700 and 1400 on exactly this scale.
        """
        self.rc_channel8_raw = max(SBUS_MIN, min(SBUS_MAX, int(raw)))
        if self.rc_channel8_raw < CH8_ESTOP_MAX:
            self.mode = MODE_ESTOP
            self.estop_latched = True
            self.armed = False
            self._pending = None

    def set_rc_channel7(self, raw: int) -> None:
        """Operator moved the arm switch. Simulation input only."""
        self.rc_channel7_raw = max(SBUS_MIN, min(SBUS_MAX, int(raw)))
        if self.rc_channel7_raw <= CH7_ARM_MIN:
            self.armed = False

    def set_rc_link(self, ok: bool) -> None:
        self.rc_link_ok = ok

    # -- simulation -------------------------------------------------------

    def step(self, dt: float) -> None:
        self._t += dt
        # One decoded SBUS frame every 14 ms, as a real receiver delivers them.
        # Counting rather than faking a constant matters: sbusok standing still
        # is how a dead radio looks on the wire.
        if self.rc_link_ok:
            self.sbus_frames_ok += max(1, int(dt / 0.014))
        if self._pending and self._t >= self._pending[1]:
            mode, _ = self._pending
            self._pending = None
            # Hardware wins: no software request can leave ESTOP while the
            # killswitch or the RC channel is asserting it.
            blocked = (
                self.hardware_killswitch_engaged
                or self.rc_channel8_raw < CH8_ESTOP_MAX
            )
            if blocked and mode != MODE_ESTOP:
                return
            self.mode = mode
            self.estop_latched = mode == MODE_ESTOP
            self.armed = mode != MODE_ESTOP
            self.relay_states = [self.armed] * self.cfg.num_relays
            self.esc_status = [0 if self.armed else 1] * self.cfg.num_escs

    def sample(self, utc_ms: int) -> PicoSample:
        return PicoSample(
            utc_ms=utc_ms,
            mode=self.mode,
            armed=self.armed,
            estop_latched=self.estop_latched,
            relay_states=list(self.relay_states),
            esc_status=list(self.esc_status),
            rc_link_ok=self.rc_link_ok,
            rc_channel7_raw=self.rc_channel7_raw,
            rc_channel8_raw=self.rc_channel8_raw,
            hardware_killswitch_engaged=self.hardware_killswitch_engaged,
        )

    # -- the wire ---------------------------------------------------------

    def state_line(self) -> str:
        """One ``STATE`` line, exactly as ``pico-node_v4`` would write it.

        This is the whole point of the text layer. The simulator used to hand
        ``gui_backend`` a structured message directly, which meant the format,
        the serial framing and the parser were never exercised anywhere — and
        that is precisely the layer where the firmware and the Jetson had
        silently disagreed for months. Whatever this method emits is what
        ``parse_state_line`` has to cope with, so a format change that breaks
        the parser now breaks a test.

        Field order and spelling are copied from the firmware's ``loop()``.
        """
        return (
            f"STATE ver={self.firmware_version}"
            f" mode={FIRMWARE_MODE[self.mode]}"
            f" armed={int(self.armed)}"
            f" relay={int(any(self.relay_states))}"
            f" wantauto={int(self.serial_wants_auto)}"
            f" link={int(self.link_live)}"
            f" estoplatch={int(self.estop_latched)}"
            f" thr={self.rc_throttle_raw}"
            f" yaw={self.rc_yaw_raw}"
            f" ch7={self.rc_channel7_raw}"
            f" ch8={self.rc_channel8_raw}"
            f" sbusok={self.sbus_frames_ok}"
            f" sbusbad={self.sbus_frames_bad}"
            f" sbusfs={int(not self.rc_link_ok)}"
            f" sbuslost={int(self.sbus_frame_lost)}"
        )
