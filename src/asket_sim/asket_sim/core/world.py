"""The simulated world.

One object owning every simulated source, stepped by a single clock. Consumers
(the ROS publisher node, and ``gui_backend`` when it runs in sim) read from here
and never touch the individual simulators, so there is exactly one place where
faults are applied and one definition of "now".

The clock is explicit rather than ``time.time()``: tests step it deterministically,
the ROS node steps it from a wall-clock timer, and a future mission replayer can
step it from a recording.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

from asket_common.geo import LocalOrigin
from asket_common.survey import SIDE_STARBOARD, SurveyPlan

from .battery import BatteryConfig, BatterySample, BatterySim
from .faults import FaultInjector
from .lidar import LidarConfig, LidarScan, LidarSim, Obstacle
from .link import (
    ALIGNMENT_LOST_DEG,
    GLASSY_WAVE_HEIGHT_M,
    LINK_4G,
    LINK_NONE,
    LinkConfig,
    LinkSample,
    LinkSim,
)
from .pico import PicoConfig, PicoSample, PicoSim
from .sonar import PingSet, SeabedConfig, SonarSim, SonarSimConfig
from .vessel import VesselConfig, VesselSample, VesselSim


@dataclass
class WorldConfig:
    #: Walvis Bay, Namibia — a placeholder origin so the map has somewhere
    #: plausible to open. PROVISIONAL, see docs/open_questions.md Q1.
    origin_lat: float = -22.9576
    origin_lon: float = 14.5053

    survey_heading_deg: float = 20.0
    survey_line_length_m: float = 300.0
    survey_line_spacing_m: float = 20.0
    survey_num_lines: int = 6
    sonar_side: str = SIDE_STARBOARD

    #: Simulated disk, so "disk full during recording" is reachable.
    disk_total_bytes: int = 512 * 1024**3
    disk_used_bytes: int = 40 * 1024**3

    seed: int = 1

    vessel: VesselConfig = field(default_factory=VesselConfig)
    lidar: LidarConfig = field(default_factory=LidarConfig)
    sonar: SonarSimConfig = field(default_factory=SonarSimConfig)
    seabed: SeabedConfig = field(default_factory=SeabedConfig)
    battery: BatteryConfig = field(default_factory=BatteryConfig)
    pico: PicoConfig = field(default_factory=PicoConfig)
    link: LinkConfig = field(default_factory=LinkConfig)

    #: Static obstacles, in local ENU metres.
    #:
    #: Placed near the survey lines but never on them. The simulator does not
    #: do obstacle avoidance — that is the navigation stack's job, and faking
    #: it here would test nothing — so an obstacle sitting on a planned line
    #: would have the vessel drive straight through it. A demo in which the
    #: boat passes through a moored vessel is a demo nobody believes.
    #:
    #: Clearances are measured against the track the vessel ACTUALLY drives,
    #: not the planned one. The controller is lookahead-based and works in a
    #: cross-current, so it runs a couple of metres wide of the line and up to
    #: about four metres wide through a turn. An obstacle placed to clear the
    #: planned track by three metres is therefore an obstacle the boat drives
    #: through. These clear the driven track by 6 m, 6 m and 12 m.
    #: ``test_obstacles_are_never_driven_through`` holds that true.
    obstacles: list[Obstacle] = field(
        default_factory=lambda: [
            Obstacle(54.4, 122.1, 1.2, "channel buoy"),
            Obstacle(74.0, 239.0, 3.5, "rock"),
            Obstacle(132.0, 26.5, 6.0, "moored vessel"),
        ]
    )


@dataclass
class WorldSnapshot:
    """Everything the world can currently be asked about, at one instant."""

    utc_ms: int
    sim_time_s: float
    vessel: VesselSample
    pico: PicoSample
    battery: BatterySample
    link: LinkSample
    lidar: LidarScan | None
    ping: PingSet | None
    sonar_pitch_deg: float
    sonar_roll_deg: float
    disk_free_bytes: int
    disk_total_bytes: int
    active_faults: list[str]
    seabed_depth_m: float


class SimWorld:
    def __init__(self, config: WorldConfig | None = None, start_utc_ms: int | None = None):
        self.cfg = config or WorldConfig()
        cfg = self.cfg

        self.origin = LocalOrigin(cfg.origin_lat, cfg.origin_lon)
        self.plan = SurveyPlan(
            origin=self.origin,
            heading_deg=cfg.survey_heading_deg,
            line_length_m=cfg.survey_line_length_m,
            line_spacing_m=cfg.survey_line_spacing_m,
            num_lines=cfg.survey_num_lines,
            sonar_side=cfg.sonar_side,
        )

        self.faults = FaultInjector()
        self.vessel = VesselSim(self.plan, cfg.vessel, seed=cfg.seed)
        self.lidar = LidarSim(cfg.obstacles, cfg.lidar, seed=cfg.seed + 1)
        self.sonar = SonarSim(cfg.seabed, cfg.sonar, seed=cfg.seed + 2)
        self.battery = BatterySim(cfg.battery)
        self.pico = PicoSim(cfg.pico, seed=cfg.seed + 3)
        self.link = LinkSim(cfg.link, seed=cfg.seed + 4)

        self.t = 0.0
        self.start_utc_ms = start_utc_ms if start_utc_ms is not None else int(time.time() * 1000)
        self.disk_used_bytes = cfg.disk_used_bytes

        self._next_ping_t = 0.0
        self._next_lidar_t = 0.0
        self._last_ping: PingSet | None = None
        self._last_lidar: LidarScan | None = None
        # Pings produced since a consumer last drained them. Bounded: a
        # consumer that stops draining must not grow this without limit.
        # Stepping the world in large chunks produces several pings at once, so
        # a consumer that only ever saw the latest would measure a ping rate the
        # sonar is not actually running at.
        self._ping_queue: deque[PingSet] = deque(maxlen=256)
        self._clock_drift_ms = 0.0

    # -- clock ------------------------------------------------------------

    @property
    def utc_ms(self) -> int:
        return self.start_utc_ms + int(self.t * 1000.0)

    # -- faults -----------------------------------------------------------

    def inject_fault(self, name: str, duration_s: float | None = None) -> None:
        self.faults.inject(name, duration_s)

    def clear_fault(self, name: str) -> None:
        self.faults.clear(name)

    def _apply_faults(self, dt: float) -> None:
        f = self.faults

        self.vessel.set_heading_valid(not f.active("heading_invalid"))

        if f.active("gnss_degraded"):
            self.vessel.cfg.num_sats = 4
            self.vessel.cfg.hdop = 4.5
            self.vessel.cfg.fix_type = 2
        else:
            self.vessel.cfg.num_sats = 14
            self.vessel.cfg.hdop = 0.8
            self.vessel.cfg.fix_type = 3

        # A dropout stops the sonar; it does not un-send the host's command.
        self.sonar.pinging = self.sonar.ping_enabled_by_command and not f.active(
            "sonar_dropout"
        )
        if f.active("sonar_packet_loss") and self.sonar.drop_next == 0:
            self.sonar.drop_next = 1

        if f.active("clock_drift"):
            # 40 ms per second: fast enough to see in a demo, slow enough that a
            # threshold-based warning is a real test rather than a step change.
            self._clock_drift_ms += 40.0 * dt
        self.sonar.cfg.clock_offset_ms = int(self._clock_drift_ms)

        self.pico.set_rc_link(not f.active("rc_link_loss"))
        self.battery.set_load_multiplier(2.5 if f.active("battery_fault") else 1.0)

        if f.active("disk_full"):
            self.disk_used_bytes = self.cfg.disk_total_bytes - 8 * 1024**2

        # The shore antenna and the weather. Both are set every tick rather
        # than on the fault's edges: a fault that is injected, cleared and
        # injected again must not leave the tripod pointing somewhere nobody
        # asked for.
        self.link.set_alignment_error(
            ALIGNMENT_LOST_DEG if f.active("link_alignment_lost") else 0.0
        )
        self.link.set_sea_state(GLASSY_WAVE_HEIGHT_M
                                if f.active("link_glassy_water") else None)

    def _forced_link(self) -> str | None:
        if self.faults.active("link_loss"):
            return LINK_NONE
        if self.faults.active("link_degraded"):
            return LINK_4G
        return None

    # -- stepping ---------------------------------------------------------

    def step(self, dt: float) -> None:
        self.t += dt
        self.faults.step(dt)
        self._apply_faults(dt)

        self.vessel.step(dt)
        self.pico.step(dt)
        self.link.step(dt)

        # Propulsion only draws power when the Pico says the vessel is armed —
        # the same authority the GUI displays.
        throttle = self.vessel.throttle if self.pico.armed else 0.0
        self.battery.step(dt, throttle)

        v = self.vessel
        if not self.faults.active("lidar_stall") and self.t >= self._next_lidar_t:
            self._next_lidar_t = self.t + 1.0 / max(0.1, self.cfg.lidar.rotation_hz)
            self._last_lidar = self.lidar.scan(
                self.utc_ms, v.east, v.north, v.true_heading,
                roll_deg=v.sample(self.utc_ms).roll_deg,
            )

        if self.t >= self._next_ping_t:
            self._next_ping_t = self.t + 1.0 / max(0.1, self.cfg.sonar.ping_rate_hz)
            sample = v.sample(self.utc_ms)
            side_sign = 1.0 if self.cfg.sonar_side == SIDE_STARBOARD else -1.0
            ping = self.sonar.ping(
                self.utc_ms, v.east, v.north, v.true_heading,
                roll_deg=sample.roll_deg, side_sign=side_sign,
            )
            if ping is not None:
                self._last_ping = ping
                self._ping_queue.append(ping)

    # -- observation ------------------------------------------------------

    def snapshot(self) -> WorldSnapshot:
        utc = self.utc_ms
        vessel = self.vessel.sample(utc)
        pitch, roll = self.sonar.attitude(vessel.roll_deg, vessel.pitch_deg)
        return WorldSnapshot(
            utc_ms=utc,
            sim_time_s=self.t,
            vessel=vessel,
            pico=self.pico.sample(utc),
            battery=self.battery.sample(utc),
            link=self.link.sample(
                utc, vessel.east_m, vessel.north_m, self._forced_link(),
                heading_deg=vessel.heading_deg,
            ),
            lidar=self._last_lidar,
            ping=self._last_ping,
            sonar_pitch_deg=pitch,
            sonar_roll_deg=roll,
            disk_free_bytes=max(0, self.cfg.disk_total_bytes - self.disk_used_bytes),
            disk_total_bytes=self.cfg.disk_total_bytes,
            active_faults=self.faults.names(),
            seabed_depth_m=self.cfg.seabed.depth_at(vessel.east_m, vessel.north_m),
        )

    def take_ping(self) -> PingSet | None:
        """Consume the most recent ping and discard any older ones.

        For consumers stepping in increments smaller than the ping period, which
        is every consumer that wants one ping at a time.
        """
        self._ping_queue.clear()
        ping, self._last_ping = self._last_ping, None
        return ping

    def take_pings(self) -> list[PingSet]:
        """Consume every ping produced since the last call.

        For consumers stepping in chunks larger than the ping period: seeing
        only the newest would under-report the ping rate by the ratio of the two.
        """
        pings = list(self._ping_queue)
        self._ping_queue.clear()
        if pings:
            self._last_ping = None
        return pings

    def take_lidar(self) -> LidarScan | None:
        scan, self._last_lidar = self._last_lidar, None
        return scan
