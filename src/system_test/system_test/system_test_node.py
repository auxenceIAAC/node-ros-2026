"""ROS 2 node: built-in test.

Aggregates ``diagnostic_msgs/DiagnosticArray`` from every node — each one
publishes its own health continuously — and adds the checks that need a view
across the whole system: is the sonar's clock disciplined, is there enough
disk, does the heading agree with the course.

Runs **automatically at boot**, so a result is waiting as soon as a client
connects. Nobody is aboard the vessel; an operator on the beach should not have
to ask for the first answer.

Active tests are not here. The motor test gate lives in
``system_test.core.motor_test`` and is deliberately not wired to a service:
wiring it belongs to the day a human is standing next to the boat.
"""

from __future__ import annotations

import math
import os
import tempfile
import time

import rclpy
from asket_interfaces.msg import (
    HeadingStatus,
    LinkStatus,
    MissionState,
    PicoStatus,
    SonarStatus,
    SystemTestItem,
    SystemTestReport,
)
from asket_interfaces.srv import RunSystemTest
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from rclpy.node import Node
from rclpy.qos import QoSDurabilityPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import BatteryState, Imu, LaserScan, NavSatFix

from asket_common.mode_arbitration import ESTOP_FEEDBACK_ENABLED
from system_test.core.checks import FAIL, PASS, SKIPPED, WARN, Thresholds, run_checks
from system_test.core.history import PreflightHistory

SENSOR_QOS = QoSProfile(depth=5, reliability=QoSReliabilityPolicy.BEST_EFFORT)
#: Latched, so a client connecting later still gets the boot-time verdict.
LATCHED_QOS = QoSProfile(depth=1, durability=QoSDurabilityPolicy.TRANSIENT_LOCAL)

_STATUS_VALUES = {
    PASS: SystemTestItem.PASS,
    WARN: SystemTestItem.WARN,
    FAIL: SystemTestItem.FAIL,
    SKIPPED: SystemTestItem.SKIPPED,
}


def _endurance_s(battery) -> float | None:
    """Seconds left at the present draw, or None.

    ``BatteryState`` carries current as a negative number when discharging, and
    publishes NaN for anything the BMS does not report. Both have to be ruled
    out before dividing, because the answer is quoted at the operator by the
    disk check and a fabricated one there is worse than a missing one.
    """
    if battery is None:
        return None
    charge, current = float(battery.charge), float(battery.current)
    if math.isnan(charge) or math.isnan(current):
        return None
    draw = abs(current)
    if draw < 0.1 or charge <= 0.0:
        return None
    return charge / draw * 3600.0


class SystemTestNode(Node):
    def __init__(self) -> None:
        super().__init__("system_test")

        self.declare_parameter("history_path", "/data/system_test.jsonl")
        self.declare_parameter("run_at_boot", True)
        self.declare_parameter("boot_delay_s", 15.0)
        self.declare_parameter("periodic_interval_s", 0.0)
        self.declare_parameter("disk_path", "/data")
        self.declare_parameter("measure_disk_speed", True)
        self.declare_parameter(
            "expected_nodes",
            ["omniscan_bridge", "mission_recorder", "gui_backend"],
        )

        self.history = PreflightHistory(self.get_parameter("history_path").value)
        self.thresholds = Thresholds()

        self._fix: NavSatFix | None = None
        self._imu: Imu | None = None
        self._battery: BatteryState | None = None
        self._scan: LaserScan | None = None
        self._heading: HeadingStatus | None = None
        self._sonar: SonarStatus | None = None
        self._pico: PicoStatus | None = None
        self._pico_at: float = 0.0
        self._link: LinkStatus | None = None
        self._mission: MissionState | None = None
        self._disk_write_mbps: float | None = None

        self.create_subscription(NavSatFix, "/mavros/global_position/global",
                                 self._set("_fix"), SENSOR_QOS)
        self.create_subscription(Imu, "/mavros/imu/data", self._set("_imu"), SENSOR_QOS)
        self.create_subscription(BatteryState, "/mavros/battery",
                                 self._set("_battery"), SENSOR_QOS)
        self.create_subscription(LaserScan, "/scan", self._set("_scan"), SENSOR_QOS)
        self.create_subscription(HeadingStatus, "/asket/heading", self._set("_heading"), 10)
        self.create_subscription(SonarStatus, "/sonar/status", self._set("_sonar"), 10)
        self.create_subscription(LinkStatus, "/gui/link_status", self._set("_link"), 10)
        self.create_subscription(MissionState, "/mission/state", self._set("_mission"), 10)
        self.create_subscription(PicoStatus, "/pico/status", self._on_pico, 10)
        self.create_subscription(DiagnosticArray, "/diagnostics", self._on_diagnostics, 10)

        self.pub_report = self.create_publisher(SystemTestReport, "/system_test/report",
                                                LATCHED_QOS)
        self.create_service(RunSystemTest, "~/run", self._on_run)

        self._node_diagnostics: dict[str, DiagnosticStatus] = {}

        if self.get_parameter("run_at_boot").value:
            # Delayed: everything else needs a moment to come up, and a
            # pre-flight run against a half-started system would report a
            # dozen faults that do not exist.
            delay = float(self.get_parameter("boot_delay_s").value)
            self._boot_timer = self.create_timer(delay, self._run_at_boot)

        interval = float(self.get_parameter("periodic_interval_s").value)
        if interval > 0:
            self.create_timer(interval, lambda: self.run(None))

        self.get_logger().info("built-in test ready")

    # -- inputs -----------------------------------------------------------

    def _set(self, attribute: str):
        def callback(msg) -> None:
            setattr(self, attribute, msg)

        return callback

    def _on_pico(self, msg: PicoStatus) -> None:
        self._pico = msg
        self._pico_at = time.monotonic()

    def _on_diagnostics(self, msg: DiagnosticArray) -> None:
        for status in msg.status:
            self._node_diagnostics[status.name] = status

    # -- state assembly ---------------------------------------------------

    def _measure_disk_speed(self) -> float | None:
        """Write a few megabytes and time it.

        The sonar produces several GB per hour; a slow card drops data
        mid-survey, and it does so silently. Measuring beats assuming.
        """
        if not self.get_parameter("measure_disk_speed").value:
            return None
        path = self.get_parameter("disk_path").value
        payload = os.urandom(4 * 1024 * 1024)
        try:
            with tempfile.NamedTemporaryFile(dir=path, delete=True) as handle:
                start = time.monotonic()
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                elapsed = time.monotonic() - start
        except OSError as exc:
            self.get_logger().warn(f"could not measure disk speed: {exc}")
            return None
        return (len(payload) / 1024**2) / elapsed if elapsed > 0 else None

    def _state(self) -> dict:
        import shutil

        fix, imu, battery, scan = self._fix, self._imu, self._battery, self._scan
        heading, sonar, pico, link = self._heading, self._sonar, self._pico, self._link

        roll = pitch = None
        if imu is not None:
            q = imu.orientation
            roll = math.degrees(math.atan2(
                2 * (q.w * q.x + q.y * q.z), 1 - 2 * (q.x * q.x + q.y * q.y)))
            pitch = math.degrees(math.asin(
                max(-1.0, min(1.0, 2 * (q.w * q.y - q.z * q.x)))))

        lidar_points = None
        if scan is not None:
            lidar_points = sum(
                1 for r in scan.ranges if not math.isinf(r) and not math.isnan(r)
            )

        try:
            disk_free = shutil.disk_usage(self.get_parameter("disk_path").value).free
        except OSError:
            disk_free = None

        expected = list(self.get_parameter("expected_nodes").value)
        present = [n for n in expected if any(n in name for name in self._node_diagnostics)]

        return {
            "pico_age_s": (time.monotonic() - self._pico_at) if self._pico_at else None,
            "rc_link_ok": bool(pico.rc_link_ok) if pico else None,
            "rc_channel8_raw": int(pico.rc_channel8_raw) if pico else None,
            "rc_channel7_raw": int(pico.rc_channel7_raw) if pico else None,
            "pico_firmware_version": (int(pico.firmware_version) or None) if pico else None,
            # A firmware compile flag, not a wire field: the Pico cannot
            # report whether its own feedback was compiled in. Mirrored in
            # asket_common and pinned to the sketch by the cross-check test.
            "pico_estop_feedback_enabled": ESTOP_FEEDBACK_ENABLED,
            "num_sats": None,
            "gnss_fix_type": (int(fix.status.status) + 3) if fix else None,
            "hdop": (math.sqrt(fix.position_covariance[0]) / 2.5)
            if fix is not None and fix.position_covariance[0] > 0 else None,
            "heading_valid": bool(heading.valid) if heading else None,
            "heading_source": heading.source if heading else None,
            "heading_accuracy_deg": float(heading.accuracy_deg) if heading else None,
            "heading_divergence_deg": float(heading.divergence_deg) if heading else None,
            "heading_divergence_meaningful": bool(heading.divergence_meaningful)
            if heading else None,
            "roll_deg": roll,
            "pitch_deg": pitch,
            "lidar_rotation_hz": (1.0 / scan.scan_time)
            if scan is not None and scan.scan_time > 0 else None,
            "lidar_points_per_revolution": lidar_points,
            "sonar_connected": bool(sonar.connected) if sonar else None,
            "sonar_discovered": None,
            "sonar_ping_rate_hz": float(sonar.actual_ping_rate_hz) if sonar else None,
            "sonar_points_per_ping": int(sonar.points_per_ping) if sonar else None,
            "clock_offset_ms": int(sonar.clock_offset_ms) if sonar else None,
            "disk_free_bytes": disk_free,
            "disk_write_mbps": self._disk_write_mbps,
            "state_of_charge": float(battery.percentage) if battery else None,
            "battery_voltage": float(battery.voltage) if battery else None,
            # Charge remaining over present draw. Reported only when both are
            # real numbers: an invented endurance is worse than none, because
            # the disk check would quote it (docs/open_questions.md Q5).
            "battery_endurance_s": _endurance_s(battery),
            "link_rtt_ms": float(link.rtt_ms) if link else None,
            "link_active": link.active_link if link else None,
            "expected_nodes": expected,
            "present_nodes": present,
            "mounting": self._mounting(),
        }

    def _mounting(self) -> dict | None:
        """The mounting geometry ``omniscan_bridge`` is actually applying.

        Taken from the bridge's own diagnostics rather than by re-reading
        ``mounting.yaml``: somebody who measures the vessel and edits the file
        has not changed the geometry until the bridge is restarted, and a check
        that went green in between would be worse than no check at all.
        """
        for name, status in self._node_diagnostics.items():
            if "omniscan" not in name:
                continue
            kv = {v.key: v.value for v in status.values}
            if "mounting_path" not in kv:
                continue
            unknown = [f for f in kv.get("mounting_unknown_fields", "").split(",") if f]
            return {
                "path": kv.get("mounting_path", ""),
                "found": kv.get("mounting_found") == "True",
                "missing": kv.get("mounting_missing") == "True",
                "measured": kv.get("mounting_measured") == "True",
                "measured_by": kv.get("mounting_measured_by", ""),
                "measured_utc": kv.get("mounting_measured_utc", ""),
                "error": kv.get("mounting_error", ""),
                "unknown_fields": unknown,
            }
        return None

    # -- running ----------------------------------------------------------

    def _run_at_boot(self) -> None:
        self._boot_timer.cancel()
        report = self.run(None)
        level = self.get_logger().info if report.go else self.get_logger().error
        level(f"boot pre-flight: {report.summary}")

    def run(self, only: list[str] | None):
        started = time.monotonic()
        self._disk_write_mbps = self._measure_disk_speed()

        report = run_checks(
            self._state(), self.thresholds, only=only,
            run_utc_ms=self.get_clock().now().nanoseconds // 1_000_000,
        )
        report.duration_s = time.monotonic() - started
        self.history.append(report)
        self.pub_report.publish(self._to_msg(report))
        return report

    def _to_msg(self, report) -> SystemTestReport:
        msg = SystemTestReport()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.go = report.go
        msg.summary = report.summary
        msg.duration_s = float(report.duration_s)
        msg.run_utc_ms = int(report.run_utc_ms)
        for result in report.items:
            item = SystemTestItem()
            item.id = result.id
            item.name = result.name
            item.status = _STATUS_VALUES[result.status]
            item.message = result.message
            item.remedy = result.remedy
            item.measured_value = (
                0.0 if isinstance(result.measured_value, float)
                and math.isnan(result.measured_value)
                else float(result.measured_value)
            )
            item.units = result.units
            item.active = result.active
            msg.items.append(item)
        return msg

    def _on_run(self, request, response):
        if request.include_active:
            # Safety rule 6. Refused here rather than gated further down, so
            # there is no path through this service that can reach a thruster.
            response.accepted = False
            response.message = (
                "Active tests are not available through this service. A motor "
                "test needs an operator standing next to the vessel, an "
                "explicit acknowledgement that it is out of the water or "
                "securely moored, and a deliberate action at the boat."
            )
            return response

        report = self.run(list(request.only) or None)
        response.accepted = True
        response.message = report.summary
        return response


def main(args=None) -> None:
    rclpy.init(args=args)
    node = SystemTestNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()
