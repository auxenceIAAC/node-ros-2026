"""When the vessel will accept a change to its setup, and when it will not.

Two protections against two different failure modes, and they are not
alternatives.

**The automatic condition prevents the accident.** Nobody should be able to
change the sonar's lever arm with the boat under way, and nobody intends to —
it happens because a page was left open on a laptop and somebody leaned on it,
or because a tab from this morning is still showing yesterday's form. So the
Jetson refuses, and it refuses on state it reads from the vessel rather than
on anything the browser claims. A guard in the page is a guard a stale page
does not have.

**The explicit confirmation prevents the rush.** A condition that is satisfied
is not a decision that was made. The second step is a human saying yes to this
specific change, and it leaves a trace: who, when, which fields, and what the
vessel was doing at the time.

Both, because either alone leaves a hole. A confirmation without the condition
lets somebody confirm their way into changing geometry mid-survey. A condition
without the confirmation turns a disarmed boat into a free-for-all, with no
record of who changed what.

## What is actually checked, and why not "cable only"

The original instinct was to accept setup changes only over a cable while the
boat is alongside. That is the right intent and the wrong mechanism: a cable
and the WiFi link are both IP to the Jetson, so enforcing it means inspecting
which interface a request arrived on, which breaks the moment somebody uses a
USB-Ethernet adapter, a different port, or a laptop bridging both. A check
that can be wrong permissively *and* obstructively is worse than none.

What is checkable is the thing that actually matters:

* the Pico reports **disarmed**, and
* the recorder reports **not recording**.

Those come off the same authority the rest of this GUI already trusts — the
Pico's own report, never a local echo — and they cannot be bypassed by opening
the page from a different machine. Cable-only remains a sensible operating
convention; it is simply not the mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Why a change was refused. Separate codes because they send somebody to
#: different places, and because a single "not allowed" would be the kind of
#: refusal that teaches people to stop reading refusals.
BLOCKED_ARMED = "armed"
BLOCKED_RECORDING = "recording"
BLOCKED_UNKNOWN = "unknown_state"


@dataclass(frozen=True)
class GateDecision:
    """Whether the vessel will take a setup change, and what to say if not."""

    allowed: bool
    code: str = ""
    #: What the operator is told, in the vessel's terms rather than the
    #: software's. "The Pico reports armed" is actionable; "precondition
    #: failed" is not.
    reason: str = ""
    #: What to do about it, when there is something to do.
    remedy: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "remedy": self.remedy,
        }


ALLOWED = GateDecision(allowed=True)


def evaluate(state: dict) -> GateDecision:
    """Decide from observed vessel state. Pure, so it is the same decision
    wherever it is asked.

    ``state`` is the backend's own view — the ``pico`` and ``mission`` payloads
    as the vessel reported them. Nothing here reads anything the browser sent.
    """
    pico = state.get("pico") or {}
    mission = state.get("mission") or {}

    armed = pico.get("armed")
    recording = (mission.get("state") or "").upper() == "RECORDING"

    if armed is None:
        # Not "probably fine". The whole point of the gate is that the vessel
        # is in a known safe state, and "we cannot see the Pico" is not one.
        # It is also a real condition — a lost serial link or an unparseable
        # STATE line — and refusing says so rather than letting a change
        # through on a state nobody can read.
        return GateDecision(
            allowed=False,
            code=BLOCKED_UNKNOWN,
            reason="The vessel is not reporting whether it is armed.",
            remedy=(
                "Setup cannot be changed while the Pico's state is unknown. "
                "Check the Pico link on the Vessel panel."
            ),
        )

    if armed:
        return GateDecision(
            allowed=False,
            code=BLOCKED_ARMED,
            reason="The Pico reports the vessel is armed.",
            remedy=(
                "Disarm with RC channel 7 and try again. Setup is not "
                "changed on a vessel that can move."
            ),
        )

    if recording:
        return GateDecision(
            allowed=False,
            code=BLOCKED_RECORDING,
            reason="A mission is recording.",
            remedy=(
                "Stop the recording first. Changing geometry part way through "
                "a mission would leave half the survey placed one way and half "
                "the other, with nothing in the file to say where the change "
                "fell."
            ),
        )

    return ALLOWED


@dataclass(frozen=True)
class SetupChangeRecord:
    """What was changed, by whom, and what the vessel was doing at the time.

    Written into the mission directory rather than kept in the GUI, so the
    trace survives the browser being closed and travels with the data it
    affects. Somebody opening a survey in three months can see that the lever
    arm was changed forty minutes before it started, and by whom.
    """

    utc_ms: int
    by: str
    field_ids: tuple[str, ...]
    #: The setup's digest after the change, so a record can be tied to the
    #: file it produced.
    content_hash: str
    #: What the gate saw. Recorded even when it allowed the change: "the boat
    #: was disarmed and not recording" is the useful half of the trace, and a
    #: record that only appears on refusal would make the common case
    #: invisible.
    vessel_state: str
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "utc_ms": self.utc_ms,
            "by": self.by,
            "fields": list(self.field_ids),
            "content_hash": self.content_hash,
            "vessel_state": self.vessel_state,
            "note": self.note,
        }


def describe_vessel_state(state: dict) -> str:
    """One line for the record: what the boat was doing when somebody changed
    its setup."""
    pico = state.get("pico") or {}
    mission = state.get("mission") or {}
    armed = pico.get("armed")
    mode = pico.get("mode") or "unknown mode"
    recording = (mission.get("state") or "").upper() == "RECORDING"

    armed_word = (
        "arming unknown" if armed is None else ("armed" if armed else "disarmed")
    )
    return (
        f"{mode}, {armed_word}, "
        f"{'recording' if recording else 'not recording'}"
    )
