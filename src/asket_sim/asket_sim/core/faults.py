"""Injectable faults.

The point of the simulator is not to show the system working. It is to show the
system failing in the specific ways it will fail in Namibia, on the bench, in
January, where fixing it is cheap.

Every fault is named, toggleable at runtime, and optionally self-clearing after
a duration so a fault can be injected and walked away from.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Canonical fault names. Anything not in here is rejected, so a typo in a
#: launch file or a GUI request fails loudly instead of silently doing nothing.
FAULTS = {
    "link_loss": "Shore link drops entirely.",
    "link_degraded": "Shore link falls back to 4G-grade latency and bandwidth.",
    "link_alignment_lost": (
        "The shore sector is swung right off its bearing and the boat falls "
        "out of the beam over about two seconds. Deliberately a large error: a "
        "120-degree sector with 19 dB in hand shrugs off a nudge, so a small "
        "knock is not what takes this link away. Does nothing beside the ramp, "
        "where the antenna's backlobe still carries several megabits — that is "
        "correct, and `link_loss` is the fault for 'nothing arrives'."
    ),
    "link_glassy_water": (
        "The sea goes flat and the surface reflection survives, putting deep "
        "multipath nulls at fixed ranges (~106 m, 53 m, 35 m with the current "
        "antenna heights). A calm morning inshore, not a fault in the gear."
    ),
    "sonar_dropout": "Sonar stops producing pings without closing the socket.",
    "sonar_packet_loss": "Sonar packets are lost intermittently.",
    "clock_drift": "Sonar clock drifts away from the Jetson's; ruins post-fusion.",
    "heading_invalid": "Heading source goes invalid; recorded data is compromised.",
    "gnss_degraded": "Satellite count and HDOP degrade.",
    "disk_full": "Recording disk fills up.",
    "rc_link_loss": "RC link to the operator's transmitter is lost.",
    "battery_fault": "Propulsion draws far more than it should.",
    "lidar_stall": "Lidar stops rotating.",
}


@dataclass
class _Active:
    remaining_s: float | None  # None = until explicitly cleared


@dataclass
class FaultInjector:
    _active: dict[str, _Active] = field(default_factory=dict)

    def inject(self, name: str, duration_s: float | None = None) -> None:
        if name not in FAULTS:
            raise KeyError(f"unknown fault {name!r}; known: {sorted(FAULTS)}")
        self._active[name] = _Active(duration_s)

    def clear(self, name: str) -> None:
        self._active.pop(name, None)

    def clear_all(self) -> None:
        self._active.clear()

    def active(self, name: str) -> bool:
        return name in self._active

    def names(self) -> list[str]:
        return sorted(self._active)

    def step(self, dt: float) -> None:
        for name in list(self._active):
            entry = self._active[name]
            if entry.remaining_s is None:
                continue
            entry.remaining_s -= dt
            if entry.remaining_s <= 0.0:
                del self._active[name]
