"""Real data source: ROS 2 topics.

The counterpart to :class:`~gui_backend.core.sim_source.SimSource`, satisfying
the same interface so that everything above it — the hub, the payloads, the
alarms, the command lifecycle, the frontend — is identical in both modes.

It holds the latest message from each configured topic and builds payloads on
demand, rather than converting on every callback. Most messages arrive far
faster than any client is subscribed at, and converting a 10 Hz IMU message
into JSON forty times a second to send it twice would be work done for nothing.

``rclpy`` is imported lazily so this module can be *imported* without ROS
present, which keeps the adapters importable in tests.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from asket_common.geo import LocalOrigin
from asket_common.link_budget import (
    Antenna,
    Propagation,
    RadioConfig,
    ShoreStation,
    VesselRadio,
)

from asket_common.heading import (
    SOURCE_EKF,
    SOURCE_NONE,
    evaluate_heading,
)

from . import adapters, payloads
from .commands import (
    CMD_CUT_PROPULSION,
    CMD_RUN_SYSTEM_TEST,
    CMD_SET_MODE,
    CMD_SET_PING_PARAMETERS,
    CMD_START_MISSION,
    CMD_STOP_MISSION,
)
from .source import CommandOutcome, Sample
from .streams import DETAIL_FULL

MODE_NAMES = {0: "ESTOP", 1: "MANUAL", 2: "AUTONOMOUS"}


@dataclass
class LatestMessage:
    message: object | None = None
    received_monotonic: float = 0.0

    @property
    def age_s(self) -> float:
        if self.received_monotonic == 0.0:
            return float("inf")
        return time.monotonic() - self.received_monotonic


def _radio_from_config(config: dict):
    """Build the link budget from ``topics.yaml``, or return nothing.

    Nothing is the honest answer when the station has not been entered. The
    tripod moves between missions and can be turned by hand mid-mission, so
    there is no surveyed position to fall back on, and a default one would
    produce a confident bearing for a station that is somewhere else. Every
    geometry field then goes out as ``null`` and the panel says the station is
    not configured, which is true and is actionable.

    This is the interim. These values belong on the mission setup page beside
    the sonar mounting geometry — see ``docs/open_questions.md`` Q11.
    """
    station_cfg = (config.get("shore_station") or {})
    if not station_cfg.get("configured"):
        return None, None

    origin = LocalOrigin(
        lat_deg=float(station_cfg["lat"]), lon_deg=float(station_cfg["lon"])
    )
    antenna_cfg = station_cfg.get("antenna") or {}
    station = ShoreStation(
        east_m=0.0,
        north_m=0.0,
        height_m=float(station_cfg.get("height_m", 2.9)),
        boresight_deg=float(station_cfg.get("boresight_deg", 0.0)),
        antenna=Antenna(
            gain_dbi=float(antenna_cfg.get("gain_dbi", 15.0)),
            azimuth_beamwidth_deg=float(antenna_cfg.get("azimuth_beamwidth_deg", 120.0)),
            elevation_beamwidth_deg=float(
                antenna_cfg.get("elevation_beamwidth_deg", 10.0)
            ),
        ),
    )
    vessel_cfg = station_cfg.get("vessel_radio") or {}
    radio = RadioConfig(
        station=station,
        vessel=VesselRadio(height_m=float(vessel_cfg.get("height_m", 1.0))),
        propagation=Propagation(
            freq_mhz=float(station_cfg.get("freq_mhz", 5500.0)),
            wave_height_m=float(station_cfg.get("wave_height_m", 0.5)),
            channel_width_mhz=float(station_cfg.get("channel_width_mhz", 40.0)),
        ),
    )
    return radio, origin


class RosSource:
    """Reads real topics. Constructed with an already-created ``rclpy`` node."""

    def __init__(self, node, config: dict) -> None:
        self.node = node
        self.config = config
        self.latest: dict[str, LatestMessage] = {}
        self._track: list[tuple[float, float]] = []
        self._coverage: list[dict] = []
        self._last_track_utc = 0
        self._publishers: dict[str, object] = {}
        self._service_clients: dict[str, object] = {}
        self._radio, self._station_origin = _radio_from_config(config)
        self._link = adapters.link_from_measurements("wifi", 1.0, 0.0, 800_000.0)

        self._subscribe_all()
        self._prepare_commands()

    # -- wiring -----------------------------------------------------------

    def _subscribe_all(self) -> None:
        from rclpy.qos import QoSProfile, QoSReliabilityPolicy
        from rosidl_runtime_py.utilities import get_message

        sensor_qos = QoSProfile(depth=5, reliability=QoSReliabilityPolicy.BEST_EFFORT)

        for name, spec in (self.config.get("sources") or {}).items():
            self.latest[name] = LatestMessage()
            try:
                msg_type = get_message(spec["type"])
            except (ImportError, AttributeError, ValueError) as exc:
                # A missing optional type is a degraded GUI, not a dead one.
                level = self.node.get_logger().warn if spec.get("optional") else \
                    self.node.get_logger().error
                level(f"cannot resolve {spec['type']} for {name}: {exc}")
                continue

            self.node.create_subscription(
                msg_type,
                spec["topic"],
                self._make_callback(name),
                sensor_qos,
            )

    def _make_callback(self, name: str):
        def callback(msg) -> None:
            self.latest[name] = LatestMessage(msg, time.monotonic())

        return callback

    def _prepare_commands(self) -> None:
        from rosidl_runtime_py.utilities import get_message, get_service

        for name, spec in (self.config.get("commands") or {}).items():
            try:
                if "topic" in spec:
                    self._publishers[name] = self.node.create_publisher(
                        get_message(spec["type"]), spec["topic"], 10
                    )
                else:
                    self._service_clients[name] = self.node.create_client(
                        get_service(spec["type"]), spec["service"]
                    )
            except (ImportError, AttributeError, ValueError) as exc:
                self.node.get_logger().error(f"cannot prepare command {name}: {exc}")

    # -- clock ------------------------------------------------------------

    def step(self, now_s: float) -> None:
        """Nothing to advance: ROS callbacks fill the buffers themselves."""
        self._accumulate_map_layers()

    def now_utc_ms(self) -> int:
        return int(time.time() * 1000)

    # -- assembly ---------------------------------------------------------

    def _msg(self, name: str):
        entry = self.latest.get(name)
        return entry.message if entry else None

    def _vessel_record(self):
        fix = self._msg("gnss_fix")
        if fix is None:
            return None

        odom = self._msg("odom_filtered")
        # Heading is invalid if its topic has gone quiet, even though the last
        # value is still in memory. A frozen heading that still renders is
        # exactly the failure this whole design is built to prevent.
        odom_age = self.latest.get("odom_filtered", LatestMessage()).age_s
        heading_valid = odom is not None and odom_age < 2.0

        return adapters.vessel_from_odometry(
            fix,
            odom,
            self._msg("imu"),
            extra={
                "heading_source": SOURCE_EKF if heading_valid else SOURCE_NONE,
                "heading_valid": heading_valid,
                "distance_travelled_m": self._track_distance_m(),
            },
        )

    def _heading_estimate(self):
        vessel = self._vessel_record()
        if vessel is None:
            return evaluate_heading(None, SOURCE_NONE, 0.0, 0.0, source_valid=False)
        return evaluate_heading(
            heading_deg=vessel.heading_deg,
            source=vessel.heading_source,
            cog_deg=vessel.cog_deg,
            sog_ms=vessel.sog_ms,
            reported_accuracy_deg=vessel.heading_accuracy_deg,
            source_valid=vessel.heading_valid,
        )

    def _track_distance_m(self) -> float:
        from asket_common.geo import LocalOrigin

        if len(self._track) < 2:
            return 0.0
        origin = LocalOrigin(*self._track[0])
        total = 0.0
        previous = origin.to_enu(*self._track[0])
        for lat, lon in self._track[1:]:
            current = origin.to_enu(lat, lon)
            total += math.hypot(current[0] - previous[0], current[1] - previous[1])
            previous = current
        return total

    def _accumulate_map_layers(self) -> None:
        vessel = self._vessel_record()
        if vessel is None:
            return
        utc = vessel.utc_ms
        if utc - self._last_track_utc < 1000:
            return
        self._last_track_utc = utc
        self._track.append((round(vessel.lat, 7), round(vessel.lon, 7)))
        del self._track[: max(0, len(self._track) - 4000)]

    def set_link_measurement(self, active_link: str, quality: float, rtt_ms: float,
                             capacity: float) -> None:
        """Fed by the node from whatever link telemetry it has.

        The measurements are kept as measurements. What gets added is the
        geometry: where the boat is against where the tripod is, and what the
        link budget says that range should be giving. Same module, same
        arithmetic as the simulator — the boat's position is the only input
        that differs, and here it comes from the GNSS fix instead of from a
        simulated vessel.
        """
        self._link = adapters.link_from_measurements(
            active_link, quality, rtt_ms, capacity,
            radio=self._radio, **self._vessel_enu(),
        )

    def _vessel_enu(self) -> dict:
        """The boat in the station's local ENU frame, or nothing.

        Nothing rather than a zero: the origin is where the tripod stands, so
        a missing fix defaulting to (0, 0) would put the boat on top of it and
        report a perfect link. Absence has to stay absence all the way down.
        """
        if self._radio is None:
            return {}
        vessel = self._vessel_record()
        if vessel is None:
            return {}
        east_m, north_m = self._station_origin.to_enu(vessel.lat, vessel.lon)
        return {"east_m": east_m, "north_m": north_m}

    # -- the DataSource interface -----------------------------------------

    def snapshot(
        self, stream: str, detail: str = DETAIL_FULL, cursor: int = 0
    ) -> Sample | None:
        if stream == "vessel":
            record = self._vessel_record()
            return Sample(stream, record.utc_ms, payloads.vessel_payload(record, detail)) \
                if record else None

        if stream == "heading":
            record = self._vessel_record()
            if record is None:
                return None
            return Sample(
                stream, record.utc_ms,
                payloads.heading_payload(self._heading_estimate(), detail),
            )

        if stream == "pico":
            msg = self._msg("pico_status")
            if msg is None:
                return None
            # A std_msgs/String carries no timestamp, so the record is stamped
            # with when this process received it. See adapters.pico_from_ros.
            record = adapters.pico_from_ros(msg, self.now_utc_ms())
            return Sample(stream, record.utc_ms, payloads.pico_payload(record, detail))

        if stream == "power":
            msg = self._msg("battery")
            if msg is None:
                return None
            vessel_cfg = self.config.get("vessel") or {}
            record = adapters.battery_from_ros(
                msg,
                capacity_wh=vessel_cfg.get("battery_capacity_wh", 1200.0),
                hotel_load_w=vessel_cfg.get("hotel_load_w", 85.0),
            )
            vessel = self._vessel_record()
            return Sample(
                stream, record.utc_ms,
                payloads.power_payload(
                    record, detail, speed_ms=vessel.sog_ms if vessel else None
                ),
            )

        if stream == "sonar":
            msg = self._msg("sonar_status")
            if msg is None:
                return None
            record = adapters.sonar_from_ros(msg)
            return Sample(stream, self.now_utc_ms(), payloads.sonar_payload(record, detail))

        if stream == "lidar":
            return self._lidar_sample(detail)

        if stream == "link":
            return Sample(
                stream, self.now_utc_ms(),
                payloads.link_payload(
                    self._link, profile="", profile_manual=False, clients=0,
                    rate_bytes_per_s=0.0, detail=detail,
                ),
            )

        if stream == "track":
            step = {"full": 1, "reduced": 3, "minimal": 10}[detail]
            return Sample(
                stream, self.now_utc_ms(),
                payloads.track_payload(self._track, cursor, step),
                cursor=len(self._track),
            )

        if stream == "diagnostics":
            # The Pre-flight panel's only source of data.
            #
            # This branch did not exist, so `snapshot("diagnostics")` returned
            # None on every tick and the panel read "No pre-flight has run yet
            # in this session" forever — including right after a boot pre-flight
            # that had run, logged its verdict, and published this very message.
            msg = self._msg("preflight_report")
            if msg is None:
                return None
            report = adapters.preflight_from_ros(msg)
            return Sample(stream, report["run_utc_ms"] or self.now_utc_ms(), report)

        if stream == "coverage":
            step = {"full": 1, "reduced": 3, "minimal": 10}[detail]
            side = (self.config.get("survey") or {}).get("sonar_side", "starboard")
            return Sample(
                stream, self.now_utc_ms(),
                payloads.coverage_payload(self._coverage, cursor, side, step),
                cursor=len(self._coverage),
            )

        return None

    def state(self) -> dict:
        vessel = self._vessel_record()
        pico_msg = self._msg("pico_status")
        sonar = self.snapshot("sonar", DETAIL_FULL)
        power = self.snapshot("power", DETAIL_FULL)
        return {
            "utc_ms": self.now_utc_ms(),
            "vessel": payloads.vessel_payload(vessel, DETAIL_FULL) if vessel else {},
            "heading": payloads.heading_payload(self._heading_estimate(), DETAIL_FULL),
            "pico": payloads.pico_payload(adapters.pico_from_ros(pico_msg), DETAIL_FULL)
            if pico_msg
            else {},
            "power": power.payload if power else {},
            "sonar": sonar.payload if sonar else {},
            "mission": {"state": "IDLE"},
            "link_sample": self._link,
        }

    def alarm_state(self) -> dict:
        state = self.state()
        pico_age = self.latest.get("pico_status", LatestMessage()).age_s
        return {
            # Age of the freshest thing the vessel sends us: if that has gone
            # quiet, everything on screen is stale regardless of the browser's
            # own connection being fine.
            "link_age_s": min(
                pico_age, self.latest.get("gnss_fix", LatestMessage()).age_s
            ),
            "state_of_charge": state["power"].get("state_of_charge"),
            "can_finish_survey": state["power"].get("can_finish_survey"),
            "recording": state["mission"].get("state") == "RECORDING",
            "recording_error": (
                state["mission"].get("error_message")
                if state["mission"].get("state") == "ERROR"
                else None
            ),
            "disk_free_bytes": None,
            "sonar_expected": self._msg("sonar_status") is not None,
            "sonar_connected": state["sonar"].get("connected"),
            "sonar_seconds_since_data": state["sonar"].get("seconds_since_data"),
            "clock_offset_ms": state["sonar"].get("clock_offset_ms"),
            "heading_valid": state["heading"].get("valid"),
            "heading_divergence_suspicious": state["heading"].get("divergence_suspicious"),
            "heading_divergence_deg": state["heading"].get("divergence_deg"),
            "roll_deg": state["vessel"].get("roll_deg"),
            "geofence_distance_m": None,
            "rc_link_ok": state["pico"].get("rc_link_ok"),
            # Whether the GUI is reading the Pico's status or guessing at it.
            **self._pico_state_facts(),
        }

    def _pico_state_facts(self) -> dict:
        """What the pre-flight needs to judge the STATE parser.

        Absent when nothing has arrived, so the check reports SKIPPED rather
        than passing a vessel nobody has heard from.
        """
        msg = self._msg("pico_status")
        if msg is None:
            return {}
        record = adapters.pico_from_ros(msg, self.now_utc_ms())
        return {
            "pico_state_format_verified": getattr(record, "state_format_verified", True),
            "pico_state_parsed": getattr(record, "state_parsed", True),
            "pico_state_line": getattr(record, "state_line", ""),
            "pico_state_unknown_keys": list(getattr(record, "state_unknown_keys", [])),
        }

    def send_command(self, name: str, args: dict) -> CommandOutcome:
        if name in (CMD_SET_MODE, CMD_CUT_PROPULSION):
            return self._send_mode(name, args)

        if name == CMD_SET_PING_PARAMETERS:
            return self._call_service(
                "set_ping_parameters",
                lambda req: (
                    setattr(req, "range_m", float(args.get("range_m", 30.0))),
                    setattr(req, "gain", int(args.get("gain", 4))),
                    setattr(req, "ping_rate_hz", float(args.get("ping_rate_hz", 5.0))),
                ),
            )

        if name == CMD_START_MISSION:
            return self._call_service(
                "start_mission",
                lambda req: (
                    setattr(req, "name", str(args.get("name", "mission"))),
                    setattr(req, "record_rosbag", bool(args.get("record_rosbag", False))),
                ),
            )

        if name == CMD_STOP_MISSION:
            return self._call_service("stop_mission", lambda req: None)

        if name == CMD_RUN_SYSTEM_TEST:
            # The service and its client already existed — `topics.yaml` names
            # it and `_prepare_commands()` builds the client — and this branch
            # was simply missing, so the button fell through to "not wired up"
            # on every press.
            #
            # `include_active` is hard false and the consent token stays empty.
            # Active tests turn motors; they need a consent token and somebody
            # standing next to the boat, and this panel says in as many words
            # that it does not offer them. Passing the flag through from the
            # client would make that promise a client-side one.
            return self._call_service(
                "run_system_test",
                lambda req: (
                    setattr(req, "only", [str(s) for s in (args.get("only") or [])]),
                    setattr(req, "include_active", False),
                    setattr(req, "consent_token", ""),
                ),
            )

        return CommandOutcome(False, f"command {name!r} is not wired up")

    def _lidar_sample(self, detail: str):
        """Obstacle returns, from the perception stack's own output.

        The Obstacles panel's raw/filtered toggle is backed by two real topics:
        ``/lidar_driver/scan_raw`` and ``/obstacles/lidar``, which filters
        beyond 10 m. Both are sent so the operator can tell a sensor fault from
        a filter one — which is the entire reason the toggle exists.

        The filtered set is taken from perception rather than re-derived here.
        Filtering twice, in two places, would eventually produce two different
        answers about where the obstacles are, and the navigation stack's answer
        is the one that matters.
        """
        scan = self._msg("lidar_scan")
        if scan is None:
            return None
        record = adapters.lidar_from_ros(scan)
        decimation = {"full": 1, "reduced": 4, "minimal": 12}[detail]
        payload = payloads.lidar_payload(record, detail, decimation)

        cloud = self._msg("obstacles_lidar")
        if cloud is not None:
            filtered = adapters.obstacles_from_pointcloud(cloud)
            payload["filtered"] = filtered[::decimation]
            payload["nearest_range_m"] = min((p[1] for p in filtered), default=None)
            payload["nearest_bearing_deg"] = next(
                (p[0] for p in filtered if p[1] == payload["nearest_range_m"]), None
            )
        return Sample("lidar", record.utc_ms, payload)

    def _send_mode(self, name: str, args: dict) -> CommandOutcome:
        """Publish a mode request in the Pico's own vocabulary.

        ``pico_bridge`` accepts exactly two words, ``AUTO`` and ``MANUAL``, and
        logs a warning for anything else. So the GUI translates rather than
        hoping: ``AUTONOMOUS`` goes out as ``AUTO``.

        **Cut propulsion has nothing to send.** There is no software ESTOP in
        the firmware bridge. The configured interim is a drop to ``MANUAL``,
        which removes software authority over the thrusters — the Pico drives
        them only when armed *and* in AUTONOMOUS — without stopping the boat.
        The button says so on screen. If ``estop.mode`` is anything else the
        command is refused rather than guessed at: a Cut propulsion button that
        silently did nothing would be far worse than one that reports it cannot.
        """
        publisher = self._publishers.get("mode_request")
        if publisher is None:
            return CommandOutcome(False, "no mode_request publisher configured")
        from std_msgs.msg import String

        spec = (self.config.get("commands") or {}).get("mode_request") or {}
        accepts = [w.upper() for w in spec.get("accepts", ["AUTO", "MANUAL"])]

        if name == CMD_CUT_PROPULSION:
            estop = (self.config.get("estop") or {})
            if estop.get("mode") != "mode_request_manual":
                return CommandOutcome(
                    False,
                    "no propulsion-cut path is configured. The hardware killswitch "
                    "and RC channel 8 are unaffected and still work.",
                )
            word = "MANUAL"
            detail = "dropped to MANUAL — the RC pilot has control"
        else:
            requested = str(args.get("mode", "")).upper()
            word = {"AUTONOMOUS": "AUTO", "AUTO": "AUTO", "MANUAL": "MANUAL"}.get(requested)
            if word is None:
                return CommandOutcome(False, f"unknown mode {requested!r}")
            detail = "sent to the Pico"

        if word not in accepts:
            return CommandOutcome(False, f"pico_bridge does not accept {word!r}")

        publisher.publish(String(data=word))
        return CommandOutcome(True, detail)

    def _call_service(self, key: str, fill) -> CommandOutcome:
        """Send a service request without waiting for it.

        Blocking here would stall the hub and therefore every client. The
        response does not decide anything anyway: confirmation comes from
        observing the vessel's own status, never from an acknowledgement that a
        request was received.
        """
        client = self._service_clients.get(key)
        if client is None:
            return CommandOutcome(False, f"no client configured for {key}")
        if not client.service_is_ready():
            return CommandOutcome(False, f"{key} service is not available")
        request = client.srv_type.Request()
        fill(request)
        client.call_async(request)
        return CommandOutcome(True, "sent")

    def describe(self) -> dict:
        estop = (self.config.get("estop") or {})
        return {
            "mode": "ros",
            "topics": {
                name: spec.get("topic") or spec.get("service")
                for name, spec in (self.config.get("sources") or {}).items()
            },
            # What the propulsion-cut button can actually do here. The frontend
            # labels the button from this rather than assuming a capability the
            # vessel may not have: on this stack there is no software ESTOP, and
            # a button claiming otherwise is the worst thing on the screen.
            "estop": {
                "available": bool(estop.get("mode")),
                "label": estop.get("label") or "Cut propulsion",
                "effect": estop.get("effect") or "",
            },
        }
