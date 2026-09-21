"""Simulated data source.

Drives the whole backend from ``asket_sim`` in-process, with no ROS. This is
what runs when a developer opens the GUI on a laptop, and it is also what the
backend's own tests use.

It is not a stub. Everything the real source produces, this produces: the same
stream names, the same payload shapes, the same command surface. The one thing
it adds is a command surface for **fault injection**, so the degraded cases can
be reached from the GUI itself rather than only from a test.
"""

from __future__ import annotations

import math
import time

from asket_common.heading import (
    SOURCE_GNSS_COMPASS,
    SOURCE_MAGNETOMETER,
    evaluate_heading,
)
from asket_common.mode_arbitration import ESTOP_FEEDBACK_ENABLED
from asket_common.survey import swath_half_width_m
from asket_sim.core.faults import FAULTS
from asket_sim.core.pico import MODE_NAMES, MODE_VALUES
from asket_sim.core.raw_stream import encode_sim_ping
from asket_sim.core.vessel import HEADING_SOURCE_GNSS
from asket_sim.core.world import SimWorld, WorldConfig
from mission_recorder.core.export import (
    NO_FAST_PATH_MESSAGE,
    delete_mission,
    detect_destinations,
    export_mission,
)
from mission_recorder.core.mission import (
    MissionRecorder,
    RecorderConfig,
    list_missions,
)
from omniscan_bridge.core.mounting import load_mounting
from omniscan_bridge.core.status import SonarHealthTracker
from system_test.core.checks import run_checks
from system_test.core.history import PreflightHistory

from . import payloads
from .commands import (
    CMD_CLEAR_FAULT,
    CMD_CUT_PROPULSION,
    CMD_DELETE_MISSION,
    CMD_EXPORT_MISSION,
    CMD_INJECT_FAULT,
    CMD_RUN_SYSTEM_TEST,
    CMD_SET_MODE,
    CMD_SET_PING_PARAMETERS,
    CMD_START_MISSION,
    CMD_STOP_MISSION,
)
from .source import CommandOutcome, Sample
from .streams import DETAIL_FULL


def _default_mounting_path():
    """``src/omniscan_bridge/config/mounting.yaml``, found from this file.

    Sim mode runs from a source checkout, where the packaged share directory
    does not exist. Returning None when it is not there is deliberate: the
    pre-flight then reports "no mounting file", which is the truth.
    """
    from pathlib import Path

    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "omniscan_bridge" / "config" / "mounting.yaml"
        if candidate.is_file():
            return candidate
    return None


class SimSource:
    """A :class:`~gui_backend.core.source.DataSource` backed by ``SimWorld``."""

    def __init__(
        self,
        world: SimWorld | None = None,
        time_scale: float = 1.0,
        max_step_s: float = 0.2,
        missions_root=None,
        mounting_path=None,
    ) -> None:
        self.world = world or SimWorld(WorldConfig())
        self.time_scale = time_scale
        #: Cap on a single step. If the process is descheduled for a second we
        #: simulate a second of survey, not a second of frozen vessel — but we
        #: do it in bounded chunks so the physics stays sane.
        self.max_step_s = max_step_s

        self._last_wall_s: float | None = None
        self._sonar_health = SonarHealthTracker()
        self._sonar_health.on_parameters_commanded(
            self.world.cfg.sonar.range_setting_m,
            self.world.cfg.sonar.gain,
            self.world.cfg.sonar.ping_rate_hz,
        )
        self._pings_seen = 0
        self._track: list[tuple[float, float]] = []
        self._coverage: list[dict] = []
        self._last_track_utc = 0
        self._last_trajectory_utc = 0
        self._last_diagnostics_utc = 0
        self._last_mode: int | None = None

        # A real recorder writing real files, backed by the simulated disk so
        # that "disk full during recording" is reachable from the GUI. Running
        # the actual writer rather than a stub is the point: the mission files
        # a simulated run produces are the ones a real run produces.
        import tempfile
        from pathlib import Path

        root = Path(missions_root) if missions_root else Path(
            tempfile.mkdtemp(prefix="asket-sim-missions-")
        )
        self.recorder = MissionRecorder(
            RecorderConfig(missions_root=root, min_free_bytes=1024**3),
            disk_usage=self._simulated_disk_usage,
        )
        self.missions_root = root
        self.preflight = PreflightHistory(root / "system_test.jsonl")
        self.last_report = None

        # The real mounting.yaml, not a simulated one. Rehearsing the pre-flight
        # is the point of sim mode, and the mounting check is the one item on it
        # that a simulated vessel can answer truthfully — the file either says
        # somebody measured the boat or it does not.
        self._mounting, self._mounting_provenance = load_mounting(
            mounting_path if mounting_path is not None else _default_mounting_path()
        )

    # -- clock ------------------------------------------------------------

    def _simulated_disk_usage(self, _path):
        """The recorder writes to a real directory but sees the simulated disk,
        so the disk_full fault reaches the code that has to handle it."""
        snapshot = self.world.snapshot()
        return type(
            "Usage", (),
            {
                "free": snapshot.disk_free_bytes,
                "total": snapshot.disk_total_bytes,
                "used": snapshot.disk_total_bytes - snapshot.disk_free_bytes,
            },
        )()

    def step(self, now_s: float | None = None) -> None:
        now_s = now_s if now_s is not None else time.monotonic()
        if self._last_wall_s is None:
            self._last_wall_s = now_s
            return

        elapsed = (now_s - self._last_wall_s) * self.time_scale
        self._last_wall_s = now_s

        remaining = min(elapsed, 5.0)   # never try to catch up more than 5 s
        while remaining > 1e-4:
            dt = min(self.max_step_s, remaining)
            self.world.step(dt)
            remaining -= dt

        self._consume_sonar()
        self._accumulate_map_layers()
        self._record(now_s)

    def now_utc_ms(self) -> int:
        return self.world.utc_ms

    # -- derived state ----------------------------------------------------

    def _consume_sonar(self) -> None:
        """Feed the sonar health tracker as though the bridge were running.

        In the full sim stack the real bridge does this over a real socket; the
        backend's own sim mode short-circuits it so the sonar panel is alive on
        a laptop with nothing else running.
        """
        pings = self.world.take_pings()
        if not pings:
            return

        if self.recorder.recording:
            # The same Ping Protocol frames the real bridge would hand the
            # recorder. Writing the real bytes rather than a placeholder is what
            # makes a simulated mission directory a genuine rehearsal of the
            # post-mission fusion path: raw stream plus trajectory, paired on
            # utc_ms, exactly as SonarView will have to do it.
            for ping in pings:
                self.recorder.write_sonar(encode_sim_ping(ping))
        period = 1.0 / max(0.1, self.world.cfg.sonar.ping_rate_hz)
        # Space the batch across the interval it was actually produced in, so
        # the measured rate is the rate the sonar ran at rather than the rate
        # this loop happened to drain it.
        first_t = self.world.t - period * (len(pings) - 1)
        for i, ping in enumerate(pings):
            self._pings_seen += 1
            valid = sum(1 for p in ping.points if p.pt_type != 0)
            self._sonar_health.on_point_set(
                utc_msec=ping.utc_ms,
                num_points=len(ping.points),
                num_valid=valid,
                speed_of_sound=ping.speed_of_sound_ms,
                # Measured against SIMULATED time, not the wall clock. With
                # time_scale > 1 the wall clock would report a ping rate the
                # vessel is not achieving, which is the opposite of the point.
                now_monotonic=first_t + i * period,
                now_utc_ms=ping.utc_ms - self.world.cfg.sonar.clock_offset_ms,
            )
        snap = self.world.snapshot()
        self._sonar_health.on_attitude(snap.sonar_pitch_deg, snap.sonar_roll_deg)

    def _accumulate_map_layers(self) -> None:
        """Track history and the coverage ribbon.

        Coverage is accumulated as a ribbon of near/far edge pairs rather than a
        filled polygon: a gap in the data then shows as a gap in the ribbon,
        which is precisely what an operator with one single-sided sonar needs to
        see (brief, section 7.1).
        """
        snap = self.world.snapshot()
        v = snap.vessel
        if snap.utc_ms - self._last_track_utc < 1000:
            return
        self._last_track_utc = snap.utc_ms

        self._track.append((round(v.lat, 7), round(v.lon, 7)))
        del self._track[: max(0, len(self._track) - 4000)]

        # Only paint coverage where the sonar is actually ensonifying: on a
        # survey leg, pinging, and with a valid heading. Painting during a turn
        # or a heading dropout would claim coverage we do not have.
        painting = (
            v.on_survey
            and v.heading_valid
            and self.world.sonar.pinging
            and not self.world.faults.active("sonar_dropout")
        )
        if not painting:
            self._coverage.append({"gap": True})
            del self._coverage[: max(0, len(self._coverage) - 4000)]
            return

        half = swath_half_width_m(
            self.world.cfg.sonar.range_setting_m,
            self.world.cfg.sonar.mounting_tilt_deg,
            snap.seabed_depth_m,
        )
        nadir = snap.seabed_depth_m * math.tan(
            max(0.0, math.radians(self.world.cfg.sonar.mounting_tilt_deg - 30.0))
        )
        self._coverage.append(
            {
                "lat": v.lat,
                "lon": v.lon,
                "heading_deg": v.heading_deg,
                "half_width_m": half,
                "inner_gap_m": nadir,
            }
        )
        del self._coverage[: max(0, len(self._coverage) - 4000)]

    def _record(self, now_s: float) -> None:
        """Feed the recorder, at the rates the mission file layout expects."""
        snap = self.world.snapshot()
        utc = snap.utc_ms

        # Mode changes are events whether or not anything is recording, but only
        # a running recorder has anywhere to put them.
        if self._last_mode is not None and snap.pico.mode != self._last_mode:
            self.recorder.write_event(
                utc, "mode_changed",
                {"from": MODE_NAMES.get(self._last_mode), "to": MODE_NAMES.get(snap.pico.mode)},
            )
        self._last_mode = snap.pico.mode

        if self.recorder.recording:
            # Trajectory at ~10 Hz, per the brief.
            if utc - self._last_trajectory_utc >= 100:
                self._write_trajectory(utc)

            if utc - self._last_diagnostics_utc >= 1000:
                self._last_diagnostics_utc = utc
                sonar = self.snapshot("sonar", DETAIL_FULL)
                self.recorder.write_diagnostics({
                    "utc_ms": utc,
                    # Logged every second, so if the clock does drift the damage
                    # is at least visible in the recording rather than invisible
                    # in the data.
                    "clock_offset_ms": (sonar.payload if sonar else {}).get("clock_offset_ms"),
                    "sonar_connected": (sonar.payload if sonar else {}).get("connected"),
                    "battery_soc": snap.battery.state_of_charge,
                    "link": snap.link.active_link,
                    "link_quality": round(snap.link.quality, 3),
                    "active_faults": snap.active_faults,
                })

        self.recorder.tick(now_s, utc)

    def _write_trajectory(self, utc: int) -> None:
        """One trajectory record.

        Called on the 10 Hz tick, and also once at mission start and once at
        stop. Those two extra records are not redundant: post-mission fusion
        interpolates the trajectory at each ping's timestamp, and without them
        the first and last pings fall outside the trajectory and cannot be
        placed at all. The trajectory must bracket the sonar stream.
        """
        self._last_trajectory_utc = utc
        heading = self._heading_estimate()
        v = self.world.snapshot().vessel
        self.recorder.write_trajectory({
            "utc_ms": utc,
            "lat": v.lat, "lon": v.lon, "alt": v.alt,
            "heading_deg": heading.heading_deg,
            "heading_source": heading.source,
            "heading_valid": heading.valid,
            "cog_deg": v.cog_deg, "sog_ms": v.sog_ms,
            "roll_deg": v.roll_deg, "pitch_deg": v.pitch_deg,
            "gnss_fix_type": v.gnss_fix_type, "num_sats": v.num_sats, "hdop": v.hdop,
        })

    def _heading_estimate(self):
        v = self.world.snapshot().vessel
        source = (
            SOURCE_GNSS_COMPASS
            if self.world.cfg.vessel.heading_source == HEADING_SOURCE_GNSS
            else SOURCE_MAGNETOMETER
        )
        return evaluate_heading(
            heading_deg=v.heading_deg,
            source=source,
            cog_deg=v.cog_deg,
            sog_ms=v.sog_ms,
            reported_accuracy_deg=v.heading_accuracy_deg,
            source_valid=v.heading_valid,
        )

    def _survey_remaining_m(self) -> float:
        plan = self.world.plan
        done = self.world.vessel.distance_travelled
        return max(0.0, plan.total_track_distance_m() - done)

    # -- the DataSource interface -----------------------------------------

    def snapshot(
        self, stream: str, detail: str = DETAIL_FULL, cursor: int = 0
    ) -> Sample | None:
        snap = self.world.snapshot()
        utc = snap.utc_ms

        if stream == "vessel":
            return Sample(stream, utc, payloads.vessel_payload(snap.vessel, detail))
        if stream == "heading":
            return Sample(stream, utc, payloads.heading_payload(self._heading_estimate(), detail))
        if stream == "pico":
            return Sample(stream, utc, payloads.pico_payload(snap.pico, detail))
        if stream == "power":
            return Sample(
                stream,
                utc,
                payloads.power_payload(
                    snap.battery, detail,
                    survey_remaining_m=self._survey_remaining_m(),
                    speed_ms=snap.vessel.sog_ms,
                ),
            )
        if stream == "sonar":
            health = self._sonar_health.health(
                connected=self.world.sonar.pinging
                and not self.world.faults.active("sonar_dropout"),
                packet_loss_ratio=0.0,
                packets_parsed=self._pings_seen,
                checksum_errors=0,
                bytes_discarded=0,
                seconds_since_data=0.0
                if not self.world.faults.active("sonar_dropout")
                else 30.0,
                now_monotonic=self.world.t,
            )
            return Sample(stream, utc, payloads.sonar_payload(health, detail))
        if stream == "lidar":
            if snap.lidar is None:
                return None
            decimation = {"full": 1, "reduced": 4, "minimal": 12}[detail]
            return Sample(
                stream, snap.lidar.utc_ms,
                payloads.lidar_payload(snap.lidar, detail, decimation),
            )
        if stream == "link":
            # The bearer-side facts only. Profile, client count and the rate
            # actually being pushed are the hub's to know, and it merges them
            # in — the source has no idea how many browsers are attached.
            return Sample(
                stream, utc,
                payloads.link_payload(
                    snap.link, profile="", profile_manual=False, clients=0,
                    rate_bytes_per_s=0.0, detail=detail,
                ),
            )
        if stream == "mission":
            return Sample(stream, utc, self._mission_payload())
        if stream == "diagnostics":
            if self.last_report is None:
                return None
            return Sample(
                stream, self.last_report.run_utc_ms,
                {
                    **self.last_report.to_dict(),
                    "history": self.preflight.summary(),
                },
            )
        if stream == "plan":
            return Sample(stream, utc, payloads.plan_payload(self.world.plan))
        if stream == "track":
            step = {"full": 1, "reduced": 3, "minimal": 10}[detail]
            return Sample(
                stream, utc,
                payloads.track_payload(self._track, cursor, step),
                cursor=len(self._track),
            )
        if stream == "coverage":
            step = {"full": 1, "reduced": 3, "minimal": 10}[detail]
            return Sample(
                stream, utc,
                payloads.coverage_payload(
                    self._coverage, cursor, self.world.cfg.sonar_side, step
                ),
                cursor=len(self._coverage),
            )
        return None

    def _mission_payload(self) -> dict:
        status = self.recorder.status
        return {
            "state": status.state,
            "name": status.name,
            "mission_dir": status.mission_dir,
            "elapsed_s": round(status.elapsed_s, 1),
            "bytes_written": status.bytes_written,
            "disk_free_bytes": status.disk_free_bytes,
            "disk_total_bytes": status.disk_total_bytes,
            "estimated_remaining_s": (
                None
                if status.estimated_remaining_s == float("inf")
                else round(status.estimated_remaining_s)
            ),
            "trajectory_records": status.trajectory_records,
            "error_message": status.error_message,
            "missions": [m.to_dict() for m in list_missions(self.missions_root)],
            "destinations": [d.to_dict() for d in self._destinations()],
            "no_fast_path_message": NO_FAST_PATH_MESSAGE,
        }

    def _destinations(self):
        return detect_destinations(exclude_roots=[str(self.missions_root)])

    def _preflight_state(self) -> dict:
        """The flattened snapshot the pre-flight checks read."""
        snap = self.world.snapshot()
        heading = self._heading_estimate()
        sonar = self.snapshot("sonar", DETAIL_FULL)
        sonar_payload = sonar.payload if sonar else {}
        return {
            "pico_age_s": 0.0,
            "rc_link_ok": snap.pico.rc_link_ok,
            "rc_channel8_raw": snap.pico.rc_channel8_raw,
            "rc_channel7_raw": snap.pico.rc_channel7_raw,
            "pico_firmware_version": snap.pico.firmware_version,
            # A firmware compile flag, not a wire field: the Pico cannot tell
            # us whether its own feedback was compiled in. Mirrored in
            # pico_state and pinned to the sketch by the cross-check test.
            "pico_estop_feedback_enabled": ESTOP_FEEDBACK_ENABLED,
            "num_sats": snap.vessel.num_sats,
            "gnss_fix_type": snap.vessel.gnss_fix_type,
            "hdop": snap.vessel.hdop,
            "heading_valid": heading.valid,
            "heading_source": heading.source,
            "heading_accuracy_deg": heading.accuracy_deg,
            "heading_divergence_deg": heading.divergence_deg,
            "heading_divergence_meaningful": heading.divergence_meaningful,
            "roll_deg": snap.vessel.roll_deg,
            "pitch_deg": snap.vessel.pitch_deg,
            "lidar_rotation_hz": snap.lidar.rotation_hz if snap.lidar else None,
            "lidar_points_per_revolution": (
                snap.lidar.points_per_revolution if snap.lidar else None
            ),
            "sonar_connected": sonar_payload.get("connected"),
            "sonar_discovered": True,
            "sonar_ping_rate_hz": sonar_payload.get("actual_ping_rate_hz"),
            "sonar_points_per_ping": sonar_payload.get("points_per_ping"),
            "clock_offset_ms": sonar_payload.get("clock_offset_ms"),
            "disk_free_bytes": snap.disk_free_bytes,
            # Not measured in sim: reported as unknown rather than invented,
            # which is what makes it a SKIPPED check rather than a false PASS.
            "disk_write_mbps": None,
            "state_of_charge": snap.battery.state_of_charge,
            "battery_voltage": snap.battery.voltage,
            # So the disk check can say which of the two actually binds.
            "battery_endurance_s": (
                None if math.isinf(snap.battery.endurance_s) else snap.battery.endurance_s
            ),
            "link_rtt_ms": snap.link.rtt_ms,
            "link_active": snap.link.active_link,
            "expected_nodes": [],
            "present_nodes": [],
            "mounting": self._mounting_provenance.to_dict(),
        }

    def bring_up_healthy(self, mission_name: str = "demo") -> dict:
        """Put the simulated vessel into a plausibly working state, at once.

        Every panel on this GUI renders absence gracefully, which is correct and
        is also why a freshly started simulator looks so much like a broken one:
        the vessel is in MANUAL and disarmed, nothing is recording, and no
        pre-flight has run, so three panels read "not sent" and a fourth says no
        report exists. Nothing is wrong. It just does not look like anything.

        That matters more than convenience. Nobody can judge whether a
        *degraded* state reads correctly without knowing what the healthy one
        looks like, and until now the healthy one had never been assembled —
        every default view of this interface has been a partial one.

        So this is the baseline: autonomous and armed, recording, surveying,
        pre-flight run. It uses the ordinary command path rather than reaching
        into the world, so what it produces is what an operator pressing the
        same three buttons would get, and it cannot drift into being a picture
        the real controls cannot reach.

        Returns what happened, so a caller can print it rather than assume it.
        """
        steps: dict[str, str] = {}

        mode = self.send_command(CMD_SET_MODE, {"mode": "AUTONOMOUS"})
        steps["mode"] = mode.detail if mode.accepted else f"REFUSED: {mode.detail}"

        recording = self.send_command(CMD_START_MISSION, {"name": mission_name})
        steps["recording"] = (
            recording.detail if recording.accepted else f"REFUSED: {recording.detail}"
        )

        report = self.run_preflight()
        steps["preflight"] = report.summary
        return steps

    def run_preflight(self, only=None):
        report = run_checks(
            self._preflight_state(), only=only, run_utc_ms=self.world.utc_ms
        )
        self.last_report = report
        self.preflight.append(report)
        return report

    def state(self) -> dict:
        """Full-detail everything, for alarms and command confirmation."""
        snap = self.world.snapshot()
        heading = self._heading_estimate()
        power = payloads.power_payload(
            snap.battery, DETAIL_FULL,
            survey_remaining_m=self._survey_remaining_m(),
            speed_ms=snap.vessel.sog_ms,
        )
        sonar_sample = self.snapshot("sonar", DETAIL_FULL)
        return {
            "utc_ms": snap.utc_ms,
            "vessel": payloads.vessel_payload(snap.vessel, DETAIL_FULL),
            "heading": payloads.heading_payload(heading, DETAIL_FULL),
            "pico": payloads.pico_payload(snap.pico, DETAIL_FULL),
            "power": power,
            "sonar": sonar_sample.payload if sonar_sample else {},
            "mission": self._mission_payload(),
            "link_sample": snap.link,
            "disk_free_bytes": snap.disk_free_bytes,
            "disk_total_bytes": snap.disk_total_bytes,
            "active_faults": snap.active_faults,
        }

    def alarm_state(self) -> dict:
        """The flattened view :mod:`gui_backend.core.alarms` expects."""
        state = self.state()
        return {
            # In sim the source is in-process, so vessel data is never stale.
            # The link between the browser and the backend is a separate thing
            # and the client reports on that itself.
            "link_age_s": 0.0,
            "state_of_charge": state["power"].get("state_of_charge"),
            "can_finish_survey": state["power"].get("can_finish_survey"),
            "recording": state["mission"].get("state") == "RECORDING",
            "recording_error": (
                state["mission"].get("error_message")
                if state["mission"].get("state") == "ERROR"
                else None
            ),
            "disk_free_bytes": state["disk_free_bytes"],
            "sonar_expected": True,
            "sonar_connected": state["sonar"].get("connected"),
            "sonar_seconds_since_data": state["sonar"].get("seconds_since_data"),
            "clock_offset_ms": state["sonar"].get("clock_offset_ms"),
            "heading_valid": state["heading"].get("valid"),
            "heading_divergence_suspicious": state["heading"].get("divergence_suspicious"),
            "heading_divergence_deg": state["heading"].get("divergence_deg"),
            "roll_deg": state["vessel"].get("roll_deg"),
            "geofence_distance_m": None,
            "rc_link_ok": state["pico"].get("rc_link_ok"),
        }

    def send_command(self, name: str, args: dict) -> CommandOutcome:
        if name == CMD_SET_MODE:
            mode = MODE_VALUES.get(str(args.get("mode", "")).upper())
            if mode is None:
                return CommandOutcome(False, f"unknown mode {args.get('mode')!r}")
            return CommandOutcome(self.world.pico.request_mode(mode), "sent to the Pico")

        if name == CMD_CUT_PROPULSION:
            return CommandOutcome(self.world.pico.request_mode(0), "sent to the Pico")

        if name == CMD_SET_PING_PARAMETERS:
            cfg = self.world.cfg.sonar
            cfg.range_setting_m = float(args.get("range_m", cfg.range_setting_m))
            cfg.gain = int(args.get("gain", cfg.gain))
            cfg.ping_rate_hz = max(0.1, float(args.get("ping_rate_hz", cfg.ping_rate_hz)))
            self._sonar_health.on_parameters_commanded(
                cfg.range_setting_m, cfg.gain, cfg.ping_rate_hz
            )
            return CommandOutcome(True, "sent to the sonar")

        if name == CMD_START_MISSION:
            status = self.recorder.start(
                str(args.get("name", "mission")),
                self.world.utc_ms,
                config_snapshot={
                    "survey": {
                        "heading_deg": self.world.cfg.survey_heading_deg,
                        "line_length_m": self.world.cfg.survey_line_length_m,
                        "line_spacing_m": self.world.cfg.survey_line_spacing_m,
                        "num_lines": self.world.cfg.survey_num_lines,
                        "sonar_side": self.world.cfg.sonar_side,
                    },
                    "sonar": {
                        "range_m": self.world.cfg.sonar.range_setting_m,
                        "gain": self.world.cfg.sonar.gain,
                        "ping_rate_hz": self.world.cfg.sonar.ping_rate_hz,
                        "mounting_tilt_deg": self.world.cfg.sonar.mounting_tilt_deg,
                    },
                    "simulated": True,
                },
                record_rosbag=bool(args.get("record_rosbag", False)),
            )
            if status.state != "RECORDING":
                return CommandOutcome(False, status.error_message or "could not start")
            # Before any sonar byte is written, so the trajectory brackets the
            # stream and every ping can be interpolated.
            self._write_trajectory(self.world.utc_ms)
            return CommandOutcome(True, f"recording to {status.mission_dir}")

        if name == CMD_STOP_MISSION:
            if not self.recorder.recording:
                return CommandOutcome(False, "no mission is recording")
            self._write_trajectory(self.world.utc_ms)
            status = self.recorder.stop(self.world.utc_ms)
            return CommandOutcome(True, f"stopped, wrote {status.bytes_written} bytes")

        if name == CMD_EXPORT_MISSION:
            from pathlib import Path

            mission = Path(str(args.get("path", "")))
            result = export_mission(
                mission,
                str(args.get("destination", "")),
                recording=self.recorder.recording,
                allowed_destinations=self._destinations(),
            )
            return CommandOutcome(result.success, result.message)

        if name == CMD_DELETE_MISSION:
            if not args.get("confirm"):
                return CommandOutcome(False, "delete requires an explicit confirmation")
            ok, message = delete_mission(
                str(args.get("path", "")),
                recording_dir=self.recorder.status.mission_dir
                if self.recorder.recording
                else None,
            )
            return CommandOutcome(ok, message)

        if name == CMD_RUN_SYSTEM_TEST:
            report = self.run_preflight(only=args.get("only") or None)
            return CommandOutcome(True, report.summary)

        if name == CMD_INJECT_FAULT:
            try:
                self.world.inject_fault(
                    str(args.get("fault", "")),
                    float(args["duration_s"]) if args.get("duration_s") else None,
                )
            except (KeyError, ValueError) as exc:
                return CommandOutcome(False, str(exc))
            return CommandOutcome(True, f"injected {args.get('fault')}")

        if name == CMD_CLEAR_FAULT:
            fault = str(args.get("fault", ""))
            if fault in ("*", "all", ""):
                self.world.faults.clear_all()
            else:
                self.world.clear_fault(fault)
            return CommandOutcome(True, "cleared")

        return CommandOutcome(False, f"command {name!r} is not available in sim")

    def describe(self) -> dict:
        cfg = self.world.cfg
        return {
            "mode": "sim",
            "origin": {"lat": cfg.origin_lat, "lon": cfg.origin_lon},
            "sonar_side": cfg.sonar_side,
            "available_faults": sorted(FAULTS),
            # The simulator does model a soft latch, so here the button means
            # what it says. Against the real stack it does not — see ros_source.
            "estop": {
                "available": True,
                "label": "Cut propulsion",
                "effect": "Latches the simulated Pico into ESTOP.",
            },
        }
