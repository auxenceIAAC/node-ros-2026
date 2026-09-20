"""Mode arbitration between the RC transmitter and the Jetson's request.

**The RC transmitter is sovereign.** Channel 8 decides what the vessel is
*allowed* to do; software decides, within that, what it *does*. Software can
never make the vessel more permissive than the switch in the operator's hand.
This module is the one place in Python that knows that rule, and it is a pure
function of its inputs so the whole decision table can be checked without a
boat.

This is the Python mirror of ``update_state()`` in
``firmware/pico-node_v4/pico-node_v4.ino``. The firmware is the authority — it
is what actually drives the relay — but the two implement the same table, and
``test_firmware_arbitration_matches.py`` compiles the real sketch and drives
**every** combination through both, so a change on one side that is not
mirrored on the other fails a test rather than surprising somebody on the
water.

The three zones of channel 8
----------------------------

Unlike a simple "most restrictive of two modes" rule, v4's top zone grants
*permission*, not autonomy::

    ch8 <  MODE_LOW_MAX   ->  ESTOP.      Hardware stop. Nothing overrides it.
    ch8 <  MODE_MID_MAX   ->  MANUAL.     Forced. Software cannot take autonomy here.
    ch8 >= MODE_MID_MAX   ->  AUTONOMY PERMITTED, but the vessel stays MANUAL
                              until the Jetson asks (``MODE AUTO``) *and* the
                              serial heartbeat is fresh.

So channel 8 high on its own does not start a mission. That is deliberate: the
switch says "you may", the Jetson says "I am", and the heartbeat says "I still
am". Losing any one of the three drops the vessel back to MANUAL, which is why
:func:`arbitrate` reports which one it was — see :func:`autonomy_block_reason`.

A receiver-declared failsafe (``sbus_failsafe``) forces ESTOP from any zone, and
is applied last so that no branch above it can bypass it.

There is no software ESTOP verb, and that is on purpose
-------------------------------------------------------

``MODE_ESTOP`` exists in the firmware and works; what does not exist is a serial
path into it. Adding one is a change to the safety chain, and it is made on the
bench with the boat out of the water, as its own step — not folded into an
integration. Nothing in this module should grow one on the way past.

Arming is a separate authority
------------------------------

This module decides the *mode*. It does not decide whether the propellers may
turn — that is channel 7, and it is not commandable from software at all. See
:func:`arming_block_reason` at the bottom of this file for why that is worth
saying out loud in the interface.
"""

from __future__ import annotations

from dataclasses import dataclass

MODE_ESTOP = "ESTOP"
MODE_MANUAL = "MANUAL"
MODE_AUTONOMOUS = "AUTONOMOUS"

#: Least to most permissive. Used only to assert that arbitration never hands
#: back something more permissive than the zone allows.
MODE_RANK = {MODE_ESTOP: 0, MODE_MANUAL: 1, MODE_AUTONOMOUS: 2}

#: Firmware's ``OperationMode`` enum, as it appears in the ``STATE`` line's
#: ``mode=`` field.
#: ``enum OperationMode { MODE_ESTOP = 1, MODE_MANUAL = 2, MODE_AUTONOMOUS = 3 }``
#:
#: Note this is **not** the numbering in ``PicoStatus.msg`` (0/1/2). The two are
#: bridged by name, never by adding one. See ``pico_state.py``.
FIRMWARE_MODE_NUMBERS = {1: MODE_ESTOP, 2: MODE_MANUAL, 3: MODE_AUTONOMOUS}

# -- thresholds, mirrored from the sketch -----------------------------------
#
# Every one of these exists in Python only because it cannot be read off the
# wire, which makes every one a drift risk. The firmware cross-check test
# asserts each against the sketch.

#: Channel 8 below this is the hardware ESTOP zone.
MODE_LOW_MAX = 700
#: Channel 8 below this (and above :data:`MODE_LOW_MAX`) forces MANUAL.
MODE_MID_MAX = 1400
#: Channel 7 above this is "armed" as far as the transmitter is concerned.
ARM_THRESHOLD = 1000

#: Seconds the serial heartbeat may go stale before autonomy is withdrawn.
#: Mirrors ``HEARTBEAT_TIMEOUT_MS`` in the firmware.
HEARTBEAT_TIMEOUT_S = 0.6

#: After closing the relay, the firmware holds both thrusters at neutral for
#: this long while the ESCs boot. Mirrors ``ESC_ARM_DELAY_MS``.
#:
#: Mirrored so that software does not read a vessel correctly waiting out its
#: arm window as a vessel ignoring its throttle.
ESC_ARM_DELAY_MS = 2000

#: Mirrors ``ESTOP_FEEDBACK_ENABLED`` in the firmware, which is **0**.
#:
#: At 0 the firmware's ``check_power_feedback()`` compiles to nothing, so
#: **nothing confirms that ESC power actually dropped when the relay was
#: commanded open.** Commanding a relay and observing the rail collapse are two
#: different claims, and only the first is being made.
#:
#: The pre-flight ``pico.estop_feedback`` check WARNs on every run while this
#: is ``False``. Set it to ``True`` in the same change that sets the firmware
#: flag to 1 — the cross-check test fails if the two disagree.
#:
#: This lives here rather than in ``pico_state`` because it is a statement
#: about firmware *behaviour*, not about the wire format, and because
#: ``system_test`` needs it and must not import ``gui_backend``.
ESTOP_FEEDBACK_ENABLED = False


# -- why autonomy is not engaged --------------------------------------------

REASON_RC_ESTOP = "rc_estop"
REASON_RC_MANUAL = "rc_manual"
REASON_SBUS_FAILSAFE = "sbus_failsafe"
REASON_NOT_REQUESTED = "not_requested"
REASON_LINK_STALE = "link_stale"

#: Operator-facing wording. The firmware ships codes, not English sentences — a
#: Pico has no business putting prose on a serial link at 4 Hz — so the wording
#: lives here and the GUI and the logs say the same thing about the same cause.
REASON_TEXT = {
    REASON_RC_ESTOP: (
        "RC channel 8 is in the E-stop zone, so the vessel is stopped in hardware"
    ),
    REASON_RC_MANUAL: (
        "RC channel 8 is in the manual zone, which does not permit autonomy — "
        "move the transmitter switch up first"
    ),
    REASON_SBUS_FAILSAFE: (
        "the receiver is reporting failsafe, which forces E-stop from any switch "
        "position"
    ),
    REASON_NOT_REQUESTED: (
        "autonomy is permitted by the transmitter but has not been requested"
    ),
    REASON_LINK_STALE: (
        "the serial heartbeat has gone stale, so autonomy was withdrawn — the "
        "transmitter still permits it"
    ),
}


def reason_text(code: str) -> str:
    """An operator-facing sentence for a reason code.

    An unknown code is quoted rather than dropped: a cause we cannot name is
    still a cause, and hiding it would leave the interface looking merely slow.
    """
    return REASON_TEXT.get(code, f"autonomy not engaged ({code})")


@dataclass(frozen=True)
class Arbitration:
    """What arbitration decided, and why.

    :param mode: the mode the vessel should actually be in.
    :param armed: whether the propellers may turn.
    :param estop_latched: the latch state *after* this evaluation. The firmware
        clears it when the operator disarms or selects ESTOP, so it is an
        output as well as an input.
    :param autonomy_permitted: channel 8 is in the top zone. Permission, not a
        grant — see the module docstring.
    :param autonomy_blocked_by: reason code when the vessel is not AUTONOMOUS,
        or ``""`` when it is. This is the field the GUI reads to say *why* a
        mission is not running.
    """

    mode: str
    armed: bool
    estop_latched: bool
    autonomy_permitted: bool = False
    autonomy_blocked_by: str = ""


def rc_zone(mode_ch: int) -> str:
    """The mode channel 8 alone selects, before software has any say.

    The top zone reads as MANUAL because that is what the vessel does there
    until the Jetson asks for more. :attr:`Arbitration.autonomy_permitted`
    carries the "you may" separately.
    """
    if mode_ch < MODE_LOW_MAX:
        return MODE_ESTOP
    return MODE_MANUAL


def arbitrate(
    mode_ch: int,
    arm_ch: int,
    *,
    serial_wants_auto: bool = False,
    link_live: bool = False,
    sbus_failsafe: bool = False,
    estop_latched: bool = False,
) -> Arbitration:
    """Decide mode and arming from the RC channels and the Jetson's request.

    Pure: no clock, no I/O, no globals. Mirrors ``update_state()`` statement for
    statement, deliberately including the order of the last three lines — the
    failsafe override, the latch clear and the latch application are all
    order-dependent and rearranging them changes what the boat does.

    :param mode_ch: raw SBUS count on channel 8 (172..1811).
    :param arm_ch: raw SBUS count on channel 7 (172..1811).
    :param serial_wants_auto: the Jetson has sent ``MODE AUTO`` and not
        ``MODE MANUAL`` since.
    :param link_live: the serial heartbeat is fresher than
        :data:`HEARTBEAT_TIMEOUT_S`.
    :param sbus_failsafe: the receiver declared failsafe in the frame footer.
    :param estop_latched: the latch state going in.
    """
    permitted = mode_ch >= MODE_MID_MAX and not sbus_failsafe

    # --- Resolve the channel 8 zone ---
    if mode_ch < MODE_LOW_MAX:
        mode = MODE_ESTOP
        blocked = REASON_RC_ESTOP
    elif mode_ch < MODE_MID_MAX:
        mode = MODE_MANUAL
        blocked = REASON_RC_MANUAL
    else:
        if serial_wants_auto and link_live:
            mode = MODE_AUTONOMOUS
            blocked = ""
        else:
            mode = MODE_MANUAL
            blocked = REASON_NOT_REQUESTED if not serial_wants_auto else REASON_LINK_STALE

    # The receiver's own failsafe overrides everything, and is applied last so
    # that no branch above can bypass it.
    if sbus_failsafe:
        mode = MODE_ESTOP
        blocked = REASON_SBUS_FAILSAFE

    arm_high = arm_ch > ARM_THRESHOLD
    want_armed = arm_high and mode != MODE_ESTOP

    # The operator disarming, or selecting ESTOP, is what clears the latch.
    if not arm_high or mode == MODE_ESTOP:
        estop_latched = False
    if estop_latched:
        want_armed = False

    return Arbitration(
        mode=mode,
        armed=want_armed,
        estop_latched=estop_latched,
        autonomy_permitted=permitted,
        autonomy_blocked_by=blocked,
    )


def autonomy_block_reason(arb: Arbitration) -> str | None:
    """One sentence for why the vessel is not autonomous, or ``None`` if it is.

    Exists so the GUI never has to reconstruct the cause from three separate
    fields and get it subtly wrong.
    """
    if not arb.autonomy_blocked_by:
        return None
    return reason_text(arb.autonomy_blocked_by)


# -- arming: a separate authority, and a separate explanation ---------------
#
# Arbitration decides the MODE. It does not decide whether the propellers may
# turn — that is channel 7, and it is deliberately not commandable from
# software. Somebody is physically present to launch this boat, and flipping
# that switch is their consent that propellers may turn. A mode arrives over a
# radio link from a laptop; consent does not. **The RC authorises; the GUI
# drives.**
#
# The consequence is a state that is very easy to misread: a mission can be
# requested, permitted, confirmed in the STATE line — and the boat moves
# nothing, because the arm switch is down. An operator who cannot see why is
# left guessing whether the boat ignored them or the switch did. So the reason
# is computed here, once, and shown everywhere it matters.

#: Shown when channel 7 is down. The wording is deliberately about the switch,
#: not about the request: the request worked.
ARM_BLOCKED_RC_LOW = "RC channel 7 disarmed, propulsion cannot start"
ARM_BLOCKED_LATCHED = (
    "e-stop latched, propulsion cannot start until the arm switch is cycled"
)
ARM_BLOCKED_UNKNOWN = "the vessel reports disarmed, so propulsion cannot start"
ARM_BLOCKED_CONTRADICTORY = (
    "the vessel reports disarmed although channel 7 is up — propulsion cannot "
    "start, and the two disagree"
)


def arming_block_reason(
    armed: bool | None,
    rc_arm_high: bool | None = None,
    estop_latched: bool | None = None,
) -> str | None:
    """Why propulsion cannot start, or ``None`` if nothing is blocking it.

    Pure, and deliberately conservative about what it claims:

    * ``armed`` unknown returns ``None``. Not knowing is not evidence of a
      block, and inventing one would put a red line under a healthy vessel.
    * ``armed`` true returns ``None`` even if the other inputs look odd — the
      vessel says the propellers may turn, and it is the authority on that.
    * A disarmed vessel whose cause we cannot identify still says so, rather
      than staying silent because the diagnosis is incomplete.
    """
    if armed is not True:
        if armed is None:
            return None
        if rc_arm_high is False:
            return ARM_BLOCKED_RC_LOW
        if estop_latched is True:
            return ARM_BLOCKED_LATCHED
        if rc_arm_high is True:
            return ARM_BLOCKED_CONTRADICTORY
        return ARM_BLOCKED_UNKNOWN
    return None
