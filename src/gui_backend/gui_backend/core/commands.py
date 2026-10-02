"""Command lifecycle.

This module exists to enforce two safety rules that are easy to state and easy
to violate by accident:

**Rule 4 — displayed state is always confirmed state.** A command never updates
what the GUI shows. Only the *status stream*, reporting what the Pico says the
vessel is actually doing, does that.

**Rule 5 — mode commands require two-step confirmation and Pico-confirmed
feedback.** The second step happens in the browser; the confirmation happens
here, by watching the status stream until it agrees, or timing out and saying so.

So a command has three outcomes and only three:

* ``pending``   — sent, waiting for the Pico
* ``confirmed`` — the Pico now reports the requested state
* ``failed``    — it did not, within the timeout, and the operator is told

There is no fourth outcome where the UI optimistically shows what was asked for.
That is the failure mode this module exists to prevent: an operator reading
"AUTONOMOUS" off a screen while the boat is still in MANUAL.

The soft ESTOP is named ``cut_propulsion`` here and labelled "Cut propulsion" in
the UI. Never "Emergency stop" — the hardware killswitch and RC channel 8 are
the emergency stop, and no operator should ever come to rely on a
software latch as one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Callable

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_FAILED = "failed"

CMD_SET_MODE = "set_mode"
CMD_CUT_PROPULSION = "cut_propulsion"
CMD_SET_PING_PARAMETERS = "set_ping_parameters"
CMD_START_MISSION = "start_mission"
CMD_STOP_MISSION = "stop_mission"
CMD_EXPORT_MISSION = "export_mission"
CMD_DELETE_MISSION = "delete_mission"
CMD_LIST_MISSIONS = "list_missions"
CMD_RUN_SYSTEM_TEST = "run_system_test"
CMD_SET_PROFILE = "set_profile"
CMD_INJECT_FAULT = "inject_fault"
CMD_CLEAR_FAULT = "clear_fault"
#: Save the setup, and make the vessel pick it up.
#:
#: Two commands rather than one. Saving is a file write and is reversible;
#: applying is the vessel changing how it places every sounding, needs the
#: boat in a known state, and needs confirming afterwards. Collapsing them
#: would make the dangerous half invisible inside the safe half.
CMD_SAVE_SETUP = "save_setup"
CMD_APPLY_SETUP = "apply_setup"

#: Commands that change what the vessel is doing. These get the two-step
#: confirmation in the UI and the confirmation watch here.
MODE_COMMANDS = {CMD_SET_MODE, CMD_CUT_PROPULSION}

#: Default time to wait for the Pico. Generous: a confirmation that arrives at
#: 2.9 s is still a confirmation, and a spurious "failed" that is really a slow
#: link teaches an operator to ignore the failure message.
DEFAULT_TIMEOUT_S = 3.0


@dataclass
class Command:
    id: str
    name: str
    args: dict
    issued_utc_ms: int
    status: str = STATUS_PENDING
    detail: str = ""
    #: Returns True once the vessel state shows the command took effect.
    confirm: Callable[[dict], bool] | None = field(default=None, repr=False)
    timeout_s: float = DEFAULT_TIMEOUT_S
    resolved_utc_ms: int | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "args": self.args,
            "status": self.status,
            "detail": self.detail,
            "issued_utc_ms": self.issued_utc_ms,
            "resolved_utc_ms": self.resolved_utc_ms,
        }


class CommandManager:
    """Tracks in-flight commands and confirms them against observed state."""

    def __init__(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.timeout_s = timeout_s
        self._pending: dict[str, Command] = {}
        #: Resolved commands, newest last. Bounded; this is a UI aid, not a log.
        self.history: list[Command] = []
        self._history_limit = 50

    def issue(
        self,
        name: str,
        args: dict,
        now_utc_ms: int,
        confirm: Callable[[dict], bool] | None = None,
        timeout_s: float | None = None,
        command_id: str | None = None,
    ) -> Command:
        """Register a command as sent. Returns it in the ``pending`` state.

        Note what this does *not* do: it does not touch any displayed value.
        """
        cmd = Command(
            id=command_id or uuid.uuid4().hex[:12],
            name=name,
            args=dict(args),
            issued_utc_ms=now_utc_ms,
            confirm=confirm,
            timeout_s=timeout_s if timeout_s is not None else self.timeout_s,
        )
        if confirm is None:
            # Commands with nothing to observe (a profile change, a fault
            # injection) are settled immediately — but by an explicit decision
            # here, not by defaulting to optimism.
            cmd.status = STATUS_CONFIRMED
            cmd.detail = "applied"
            cmd.resolved_utc_ms = now_utc_ms
            self._archive(cmd)
        else:
            self._pending[cmd.id] = cmd
        return cmd

    def fail(self, cmd: Command, detail: str, now_utc_ms: int) -> Command:
        """Mark a command failed at the point of sending."""
        self._pending.pop(cmd.id, None)
        cmd.status = STATUS_FAILED
        cmd.detail = detail
        cmd.resolved_utc_ms = now_utc_ms
        self._archive(cmd)
        return cmd

    def update(self, observed_state: dict, now_utc_ms: int) -> list[Command]:
        """Resolve pending commands against observed state.

        Called every time fresh vessel state arrives. Returns the commands that
        changed status, so the client can be told.
        """
        changed: list[Command] = []
        for cmd_id in list(self._pending):
            cmd = self._pending[cmd_id]

            if cmd.confirm and cmd.confirm(observed_state):
                del self._pending[cmd_id]
                cmd.status = STATUS_CONFIRMED
                cmd.detail = "confirmed by the vessel"
                cmd.resolved_utc_ms = now_utc_ms
                self._archive(cmd)
                changed.append(cmd)
                continue

            elapsed_s = (now_utc_ms - cmd.issued_utc_ms) / 1000.0
            if elapsed_s > cmd.timeout_s:
                del self._pending[cmd_id]
                cmd.status = STATUS_FAILED
                cmd.detail = (
                    f"no confirmation from the vessel after {cmd.timeout_s:g} s — "
                    "the vessel has NOT changed state"
                )
                cmd.resolved_utc_ms = now_utc_ms
                self._archive(cmd)
                changed.append(cmd)
        return changed

    @property
    def pending(self) -> list[Command]:
        return list(self._pending.values())

    def _archive(self, cmd: Command) -> None:
        self.history.append(cmd)
        del self.history[: max(0, len(self.history) - self._history_limit)]


# -- confirmation predicates ---------------------------------------------
#
# Each returns True when the observed vessel state shows the command took
# effect. They read the `pico` payload, which is the Pico's own report — never
# a local echo of what was requested.


def mode_confirmed(mode_name: str):
    def check(state: dict) -> bool:
        return (state.get("pico") or {}).get("mode") == mode_name

    return check


def propulsion_cut_confirmed(state: dict) -> bool:
    pico = state.get("pico") or {}
    return pico.get("mode") == "ESTOP" and pico.get("estop_latched") is True


def recording_confirmed(should_be_recording: bool):
    def check(state: dict) -> bool:
        mission = state.get("mission") or {}
        is_recording = mission.get("state") == "RECORDING"
        return is_recording == should_be_recording

    return check


def mounting_reloaded(fingerprint: str):
    """The sonar bridge is now placing soundings with these numbers.

    Safety rule 4 applied to configuration. The page does not say "done" when
    the file has been written, and not when the reload command was sent — only
    when the node *reports* the geometry it is using and that geometry is the
    one somebody just measured.

    The three are genuinely different and the gap between them is where the
    failure lives: a file can be written while a node carries on with the old
    lever arm, and a reload can be refused — because the file will not parse —
    while the page shows a tick. That is the same class of problem as a
    pre-flight warning naming a YAML nobody can open: it looks finished, and
    it is not.

    Compared by fingerprint rather than by field, because the question is not
    "did something change" but "is the vessel on the exact geometry I just
    sent". A reload that landed on a *different* new value — somebody else
    saved in between — must read as failed, and a field-by-field comparison
    with a tolerance would quietly call it success.
    """

    def check(state: dict) -> bool:
        mounting = (state.get("mounting") or {})
        return bool(fingerprint) and mounting.get("fingerprint") == fingerprint

    return check


def ping_parameters_confirmed(range_m: float, gain: int, rate_hz: float, tolerance=0.01):
    """The sonar is the authority on its own settings, exactly as the Pico is on
    the vessel's mode. A commanded value that never took effect must show as
    failed, not as applied."""

    def check(state: dict) -> bool:
        sonar = state.get("sonar") or {}
        return (
            abs((sonar.get("range_setting_m") or -1) - range_m) < tolerance
            and sonar.get("gain_setting") == gain
            and abs((sonar.get("commanded_ping_rate_hz") or -1) - rate_hz) < tolerance
        )

    return check
