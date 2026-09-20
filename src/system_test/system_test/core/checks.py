"""Passive pre-flight checks.

The purpose, stated plainly: **when something is wrong on a Namibian beach,
identify the faulty link in thirty seconds rather than an hour.**

Everything here follows from that.

* **Plain language, with a remedy.** Not ``GPS: ERROR`` but *"4 satellites, 6
  required — wait, or move the vessel clear of the building"*. A team member who
  did not write the code must be able to act on the message without asking
  anyone.
* **Every check names the link it exercises.** "Something is wrong with the
  sonar" is not useful; "the sonar is on the network but not answering" and
  "nothing is on the network at that address" send you to different cables.
* **Unknown is not pass.** A check with no data reports ``SKIPPED`` and says
  what is missing. A green tick that means "I could not tell" is worse than a
  red one.

Checks are pure functions over a snapshot dictionary, so the whole pre-flight
can be run against any state — a live vessel, a recorded mission, or a test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"
SKIPPED = "SKIPPED"

#: Ordering for the overall verdict: any FAIL is NO-GO.
_SEVERITY = {PASS: 0, SKIPPED: 1, WARN: 2, FAIL: 3}


@dataclass
class CheckResult:
    id: str
    name: str
    status: str
    message: str
    remedy: str = ""
    measured_value: float = float("nan")
    units: str = ""
    #: True when the check exercised hardware rather than merely observing it.
    active: bool = False

    def to_dict(self) -> dict:
        value = self.measured_value
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "remedy": self.remedy,
            "measured_value": None if isinstance(value, float) and math.isnan(value) else value,
            "units": self.units,
            "active": self.active,
        }


@dataclass
class Thresholds:
    """PROVISIONAL where marked — see docs/open_questions.md."""

    min_satellites: int = 6
    max_hdop: float = 2.5
    min_battery_soc: float = 0.4
    min_battery_voltage: float = 22.0
    max_heading_accuracy_deg: float = 12.0
    max_heading_divergence_deg: float = 15.0
    max_roll_at_rest_deg: float = 8.0
    min_lidar_rotation_hz: float = 8.0
    min_lidar_points: int = 100
    min_sonar_ping_rate_hz: float = 1.0
    min_sonar_points: int = 32
    max_clock_offset_ms: int = 250
    min_disk_free_bytes: int = 20 * 1024**3
    min_disk_write_mbps: float = 20.0
    max_link_rtt_ms: float = 400.0
    max_pico_age_s: float = 2.0
    max_rc_age_s: float = 2.0


CheckFn = Callable[[dict, Thresholds], CheckResult]


@dataclass
class Registered:
    id: str
    name: str
    fn: CheckFn
    #: A critical check that cannot run makes the verdict NO-GO.
    #:
    #: "Unknown is not pass" has to apply to the verdict and not just to the
    #: individual item. A vessel where nothing at all reports in would otherwise
    #: come out as "GO - 14 checks could not run", which is the most dangerous
    #: sentence this panel could produce.
    critical: bool = False


#: Populated by the decorator below, in declaration order — which is also the
#: order they are displayed, from the vessel outwards.
CHECKS: list[Registered] = []


def check(check_id: str, name: str, critical: bool = False):
    def register(fn: CheckFn) -> CheckFn:
        CHECKS.append(Registered(check_id, name, fn, critical))
        return fn

    return register


def _missing(check_id: str, name: str, what: str) -> CheckResult:
    """Unknown is reported as unknown. A green tick meaning 'I could not tell'
    is worse than a red one."""
    return CheckResult(
        check_id, name, SKIPPED,
        f"No data: {what}",
        "Check that the node publishing it is running (ros2 node list).",
    )


# -- the vessel itself ----------------------------------------------------


@check("pico.link", "Pico link", critical=True)
def _pico_link(state: dict, t: Thresholds) -> CheckResult:
    age = state.get("pico_age_s")
    if age is None:
        return _missing("pico.link", "Pico link", "nothing has been heard from the Pico")
    if age > t.max_pico_age_s:
        return CheckResult(
            "pico.link", "Pico link", FAIL,
            f"Last heartbeat {age:.1f} s ago",
            "Check the USB cable to the Pico and that pico_bridge is running. "
            "Inside Podman the container needs the resolved ttyACM device, not /dev/pico.",
            age, "s",
        )
    return CheckResult("pico.link", "Pico link", PASS, f"Heartbeat {age:.1f} s ago", "", age, "s")


#: The firmware protocol this stack is built to read. Kept in step with
#: ``gui_backend.core.pico_state.PROTOCOL_VERSION`` and with ``FW_VERSION`` in
#: ``firmware/pico-node_v4/``; a test asserts all three agree.
EXPECTED_FIRMWARE_VERSION = 4


@check("pico.firmware_version", "Pico firmware version", critical=True)
def _firmware_version(state: dict, t: Thresholds) -> CheckResult:
    """Is the Pico running the firmware this stack knows how to read?

    This check exists because the answer was once "no" for months, and nothing
    said so. The Jetson was written against one firmware, the Pico was flashed
    with another, the status format differed, every line was silently dropped,
    and the downlink kept working — so the boat looked fine and reported
    nothing.

    It is **critical** and it **fails** rather than warning. A version this
    stack does not know is a firmware whose field meanings are unknown, and a
    vessel panel built from misread fields is worse than an empty one: it looks
    like knowledge. Nothing downstream can be trusted, so nothing downstream
    gets the benefit of the doubt.
    """
    reported = state.get("pico_firmware_version")
    if reported is None:
        return _missing("pico.firmware_version", "Pico firmware version",
                        "the Pico has not reported a firmware version")

    if int(reported) != EXPECTED_FIRMWARE_VERSION:
        return CheckResult(
            "pico.firmware_version", "Pico firmware version", FAIL,
            f"The Pico is running firmware v{int(reported)}; this software "
            f"reads v{EXPECTED_FIRMWARE_VERSION}",
            "Flash firmware/pico-node_v4/ to the Pico, or check out the "
            "Jetson software that matches what is flashed. Do not fly on a "
            "mismatch: the fields will be read against the wrong layout and "
            "the vessel panel will be confidently wrong.",
            int(reported),
        )

    return CheckResult(
        "pico.firmware_version", "Pico firmware version", PASS,
        f"v{int(reported)}, matching this software", "", int(reported),
    )


@check("pico.state_format", "Pico status format")
def _state_format(state: dict, t: Thresholds) -> CheckResult:
    """Is the GUI reading the Pico's status, or guessing at it?

    ``/pico/status`` is a ``std_msgs/String``: ``pico_bridge`` republishes raw
    firmware ``STATE`` lines verbatim, without parsing them. The GUI parses that
    text.

    The format is now transcribed field by field from
    ``firmware/pico-node_v4/pico-node_v4.ino`` rather than guessed — but
    reading a firmware's source is not the same as reading its output, and no
    line has yet been captured off real hardware. A parser reading the wrong
    format does not look broken; it looks like a vessel that is not reporting
    much. So the pre-flight keeps saying so until somebody looks.
    """
    reported = state.get("pico_state_format_verified")
    if reported is None:
        return _missing("pico.state_format", "Pico status format",
                        "no Pico status received")

    if not reported:
        return CheckResult(
            "pico.state_format", "Pico status format", WARN,
            "Unverified: the STATE line format has never been checked against "
            "real firmware output",
            "Run `ros2 topic echo /pico/status --field data` on the Jetson, "
            "confirm the fields against gui_backend/core/pico_state.py, and set "
            "FORMAT_VERIFIED there. Until then treat the vessel panel as "
            "indicative, not confirmed.",
        )

    if state.get("pico_state_parsed") is False:
        line = str(state.get("pico_state_line") or "")[:60]
        return CheckResult(
            "pico.state_format", "Pico status format", FAIL,
            f"The Pico is sending something this GUI cannot read: {line!r}",
            "The firmware format has changed, or the parser is wrong. Nothing "
            "in the vessel panel can be trusted until it is fixed.",
        )

    unknown = state.get("pico_state_unknown_keys") or []
    if unknown:
        return CheckResult(
            "pico.state_format", "Pico status format", WARN,
            f"Unrecognised fields in the STATE line: {', '.join(unknown[:4])}",
            "The firmware is reporting something this GUI ignores. Add it to "
            "gui_backend/core/pico_state.py if it matters.",
        )

    return CheckResult(
        "pico.state_format", "Pico status format", PASS,
        "Verified against real firmware output", "",
    )


@check("pico.estop_feedback", "E-stop power feedback")
def _estop_feedback(state: dict, t: Thresholds) -> CheckResult:
    """Does anything actually verify that ESC power dropped when commanded?

    No. ``ESTOP_FEEDBACK_ENABLED`` is 0 in the firmware — the GPIO20 divider
    trace is cut for bench testing — so ``check_power_feedback()`` compiles to
    nothing and the firmware never confirms the relay did what it was told.

    **Commanding the relay open and observing the rail collapse are two
    different claims, and only the first is being made.** A green vessel panel
    is not evidence of the second.

    This WARNs on every run until the feedback is enabled, for the same reason
    the mounting-geometry check does: an assumption nobody is reminded of
    quietly becomes a belief. It is amber rather than red because the vessel is
    not unsafe to operate — the hardware killswitch and RC channel 8 both cut
    propulsion independently — it is unverified, which is a different thing and
    is worth saying in different words.
    """
    enabled = state.get("pico_estop_feedback_enabled")
    if enabled is None:
        return _missing("pico.estop_feedback", "E-stop power feedback",
                        "not reported by this build")

    if not enabled:
        return CheckResult(
            "pico.estop_feedback", "E-stop power feedback", WARN,
            "Not verified: nothing confirms the ESC rail actually collapsed "
            "when the relay was commanded open",
            "ESTOP_FEEDBACK_ENABLED is 0 in firmware/pico-node_v4 and the "
            "GPIO20 divider trace is cut for bench testing. Set it to 1, and "
            "ESTOP_FEEDBACK_ENABLED in gui_backend/core/pico_state.py to True "
            "in the same change, once the hardware is ready. Until then the "
            "hardware killswitch and RC channel 8 are the only propulsion cuts "
            "with any confirmation behind them.",
        )

    return CheckResult(
        "pico.estop_feedback", "E-stop power feedback", PASS,
        "Enabled: the firmware confirms the rail collapsed", "",
    )


#: Channel 8 below this forces MODE_ESTOP in the firmware (``MODE_LOW_MAX``).
#: A **raw SBUS count**, not a percentage: the scale is 172..1811 with 991 at
#: centre. The previous threshold here was ``< 25``, applied to a field named
#: ``rc_channel8_raw_pct`` that in fact carried a raw count — so it never fired,
#: including at the bottom of the travel, which is the one position it exists
#: to catch.
CH8_ESTOP_MAX = 700


@check("rc.link", "RC link and killswitch")
def _rc(state: dict, t: Thresholds) -> CheckResult:
    ok = state.get("rc_link_ok")
    channel8 = state.get("rc_channel8_raw")
    if ok is None:
        return _missing("rc.link", "RC link and killswitch", "no RC status from the Pico")
    if not ok:
        return CheckResult(
            "rc.link", "RC link and killswitch", FAIL,
            "No signal from the transmitter",
            "Turn the transmitter on and check its aerial. Command authority "
            "requires an operator within RC range — the killswitch does not "
            "work beyond it.",
        )
    if channel8 is None:
        return CheckResult(
            "rc.link", "RC link and killswitch", WARN,
            "Transmitter linked, but channel 8 is not being read back",
            "The mode channel cannot be confirmed. Check the Pico's channel 8 "
            "wiring before launching.",
        )
    if channel8 < CH8_ESTOP_MAX:
        return CheckResult(
            "rc.link", "RC link and killswitch", WARN,
            f"Channel 8 is in the ESTOP zone ({channel8})",
            "Propulsion is cut in hardware. Move the channel 8 switch out of "
            "the bottom position when ready.",
            channel8,
        )
    return CheckResult(
        "rc.link", "RC link and killswitch", PASS,
        f"Linked, channel 8 reads {channel8}",
        "", channel8,
    )


# -- navigation -----------------------------------------------------------


@check("gnss.fix", "GNSS fix", critical=True)
def _gnss(state: dict, t: Thresholds) -> CheckResult:
    sats = state.get("num_sats")
    fix_type = state.get("gnss_fix_type")
    hdop = state.get("hdop")
    if sats is None or fix_type is None:
        return _missing("gnss.fix", "GNSS fix", "no position messages")

    if fix_type < 3:
        return CheckResult(
            "gnss.fix", "GNSS fix", FAIL,
            f"No 3D fix — {sats} satellites",
            "Move the vessel into clear sky and wait. Cold starts take a few "
            "minutes; if it stays here, check the antenna cable.",
            sats, "satellites",
        )
    if sats < t.min_satellites:
        return CheckResult(
            "gnss.fix", "GNSS fix", FAIL,
            f"{sats} satellites, {t.min_satellites} required",
            "Wait, or move the vessel clear of buildings and vehicles.",
            sats, "satellites",
        )
    if hdop is not None and hdop > t.max_hdop:
        return CheckResult(
            "gnss.fix", "GNSS fix", WARN,
            f"{sats} satellites but HDOP is {hdop:.1f}",
            "The satellites in view are poorly spread. Position error will be "
            "larger than usual; wait a few minutes for the constellation to move.",
            hdop, "HDOP",
        )
    return CheckResult(
        "gnss.fix", "GNSS fix", PASS,
        f"3D fix, {sats} satellites, HDOP {hdop:.1f}" if hdop is not None
        else f"3D fix, {sats} satellites",
        "", sats, "satellites",
    )


@check("heading.valid", "Heading", critical=True)
def _heading(state: dict, t: Thresholds) -> CheckResult:
    valid = state.get("heading_valid")
    source = state.get("heading_source")
    accuracy = state.get("heading_accuracy_deg")
    if valid is None:
        return _missing("heading.valid", "Heading", "no heading messages")
    if not valid:
        return CheckResult(
            "heading.valid", "Heading", FAIL,
            "Heading is invalid",
            "Sonar data recorded without a heading cannot be georeferenced. "
            "Check the compass calibration, or the GNSS compass baseline.",
        )
    if accuracy is not None and not math.isnan(accuracy) and accuracy > t.max_heading_accuracy_deg:
        error_at_50m = 50.0 * math.tan(math.radians(accuracy))
        return CheckResult(
            "heading.valid", "Heading", WARN,
            f"{source} heading, ±{accuracy:.1f}° — about {error_at_50m:.1f} m of "
            "seabed error at 50 m range",
            "Acceptable for navigation, poor for survey. Re-calibrate the "
            "magnetometer away from metal, or fit the GNSS compass.",
            accuracy, "deg",
        )
    return CheckResult(
        "heading.valid", "Heading", PASS,
        f"{source} heading, ±{accuracy:.1f}°" if accuracy is not None and not math.isnan(accuracy)
        else f"{source} heading",
        "", accuracy if accuracy is not None else float("nan"), "deg",
    )


@check("heading.divergence", "Heading vs course")
def _divergence(state: dict, t: Thresholds) -> CheckResult:
    divergence = state.get("heading_divergence_deg")
    meaningful = state.get("heading_divergence_meaningful")
    if divergence is None:
        return _missing("heading.divergence", "Heading vs course", "no heading or velocity")
    if not meaningful:
        return CheckResult(
            "heading.divergence", "Heading vs course", SKIPPED,
            "Too slow to compare",
            "Run the vessel in a straight line at survey speed and re-check. "
            "This is the most useful heading diagnostic there is.",
        )
    if abs(divergence) > t.max_heading_divergence_deg:
        return CheckResult(
            "heading.divergence", "Heading vs course", WARN,
            f"Heading and course differ by {divergence:.0f}°",
            "On calm water in a straight line they should agree. Either a "
            "strong cross-current or a bad heading.",
            divergence, "deg",
        )
    return CheckResult(
        "heading.divergence", "Heading vs course", PASS,
        f"Agree within {abs(divergence):.0f}°", "", divergence, "deg",
    )


@check("imu.rest", "IMU at rest")
def _imu(state: dict, t: Thresholds) -> CheckResult:
    roll = state.get("roll_deg")
    pitch = state.get("pitch_deg")
    if roll is None or pitch is None:
        return _missing("imu.rest", "IMU at rest", "no IMU messages")
    worst = max(abs(roll), abs(pitch))
    if worst > t.max_roll_at_rest_deg:
        return CheckResult(
            "imu.rest", "IMU at rest", WARN,
            f"Reading {roll:.1f}° roll, {pitch:.1f}° pitch",
            "If the vessel is level, the IMU needs levelling or the mounting "
            "has shifted. Every sonar point is corrected using this.",
            worst, "deg",
        )
    return CheckResult(
        "imu.rest", "IMU at rest", PASS,
        f"{roll:.1f}° roll, {pitch:.1f}° pitch", "", worst, "deg",
    )


# -- sensors --------------------------------------------------------------


@check("lidar.spin", "Lidar")
def _lidar(state: dict, t: Thresholds) -> CheckResult:
    rotation = state.get("lidar_rotation_hz")
    points = state.get("lidar_points_per_revolution")
    if rotation is None:
        return _missing("lidar.spin", "Lidar", "no scans")
    if rotation < t.min_lidar_rotation_hz:
        return CheckResult(
            "lidar.spin", "Lidar", FAIL,
            f"Spinning at {rotation:.1f} Hz, expected at least {t.min_lidar_rotation_hz:.0f}",
            "Check the lidar's USB cable and that nothing is fouling the rotor.",
            rotation, "Hz",
        )
    if points is not None and points < t.min_lidar_points:
        return CheckResult(
            "lidar.spin", "Lidar", WARN,
            f"Spinning, but only {points} returns per revolution",
            "Normal over open water. On land it suggests a dirty or blocked window.",
            points, "points",
        )
    return CheckResult(
        "lidar.spin", "Lidar", PASS,
        f"{rotation:.1f} Hz, {points} returns per revolution" if points is not None
        else f"{rotation:.1f} Hz",
        "", rotation, "Hz",
    )


@check("sonar.link", "Sonar", critical=True)
def _sonar(state: dict, t: Thresholds) -> CheckResult:
    connected = state.get("sonar_connected")
    discovered = state.get("sonar_discovered")
    if connected is None:
        return _missing("sonar.link", "Sonar", "omniscan_bridge is not publishing status")
    if not connected:
        # These two send you to different cables, so they are different messages.
        if discovered:
            return CheckResult(
                "sonar.link", "Sonar", FAIL,
                "The sonar is on the network but not sending data",
                "It answered discovery, so the cable and power are fine. "
                "Power-cycle the sonar, and check the configured port (62312).",
            )
        return CheckResult(
            "sonar.link", "Sonar", FAIL,
            "Nothing answering at the sonar's address",
            "Check the sonar's Ethernet cable and its power. Then check the "
            "host address in omniscan.yaml matches the sonar.",
        )

    rate = state.get("sonar_ping_rate_hz")
    points = state.get("sonar_points_per_ping")
    if rate is not None and rate < t.min_sonar_ping_rate_hz:
        return CheckResult(
            "sonar.link", "Sonar", WARN,
            f"Connected but pinging at only {rate:.1f} Hz",
            "A long range setting forces a low ping rate. Reduce the range if "
            "the water is shallower than it is set for.",
            rate, "Hz",
        )
    if points is not None and points < t.min_sonar_points:
        return CheckResult(
            "sonar.link", "Sonar", WARN,
            f"Only {points} points per ping",
            "The seabed may be out of range, or the gain too low.",
            points, "points",
        )
    return CheckResult(
        "sonar.link", "Sonar", PASS,
        f"{rate:.1f} Hz, {points} points per ping" if rate is not None else "Connected",
        "", rate if rate is not None else float("nan"), "Hz",
    )


@check("sonar.clock", "Sonar clock (NTP)", critical=True)
def _clock(state: dict, t: Thresholds) -> CheckResult:
    """The check most likely to save a whole dataset.

    Post-mission fusion pairs the sonar stream and the trajectory on the
    sonar's own timestamp. If it drifts, the survey is not degraded — it is
    worthless, and nobody finds out until the data is opened back home.
    """
    offset = state.get("clock_offset_ms")
    if offset is None:
        return _missing("sonar.clock", "Sonar clock (NTP)", "no sonar status")
    if abs(offset) > t.max_clock_offset_ms:
        return CheckResult(
            "sonar.clock", "Sonar clock (NTP)", FAIL,
            f"The sonar's clock is {offset} ms from the Jetson's",
            "Everything recorded will be un-georeferenceable. Check the "
            "Jetson's GPS-disciplined NTP server is running. The sonar is "
            "pointed at it on the device, not from here — Cerulean removed "
            "the packet that used to set it. See docs/SETUP.md section 1.",
            offset, "ms",
        )
    return CheckResult(
        "sonar.clock", "Sonar clock (NTP)", PASS,
        f"Within {abs(offset)} ms of the Jetson", "", offset, "ms",
    )


@check("sonar.mounting", "Sonar mounting geometry")
def _mounting(state: dict, t: Thresholds) -> CheckResult:
    """Has anybody actually measured where the transducer is?

    The lever arm from the GNSS antenna to the transducer is a systematic
    offset. It does not average out and it does not look like noise: the whole
    survey is displaced, consistently, and the result looks entirely plausible
    until somebody overlays a second one.

    Nothing here can measure it. This check only asks whether a human has, and
    keeps asking — in amber, on every pre-flight — until one has said so in
    ``mounting.yaml`` (docs/open_questions.md Q2).
    """
    m = state.get("mounting")
    if m is None:
        return _missing("sonar.mounting", "Sonar mounting geometry",
                        "mounting configuration not reported")

    path = m.get("path") or "mounting.yaml"

    if m.get("error"):
        # The worst of the three states: somebody may well have measured the
        # vessel, and the numbers are being silently ignored in favour of
        # defaults. That is a fault, not an omission.
        return CheckResult(
            "sonar.mounting", "Sonar mounting geometry", FAIL,
            f"{path} could not be used ({m['error']}) — running on defaults",
            "Any measurements in that file are being ignored. Fix the file "
            "and restart omniscan_bridge; do not survey until it loads.",
        )

    if m.get("missing") or not m.get("found"):
        return CheckResult(
            "sonar.mounting", "Sonar mounting geometry", WARN,
            "No mounting file — the sonar's position is a hard-coded guess",
            "Measure the tilt and the lever arm from the GNSS antenna to the "
            f"transducer, write them into {path}, and set measured: true.",
        )

    unknown = m.get("unknown_fields") or []
    if unknown:
        # Almost always a typo in a value somebody did measure.
        return CheckResult(
            "sonar.mounting", "Sonar mounting geometry", WARN,
            f"{path} has unrecognised entries: {', '.join(unknown)}",
            "Those lines are being ignored. If one of them is a measurement, "
            "it is not reaching the sonar — check the spelling.",
        )

    if not m.get("measured"):
        return CheckResult(
            "sonar.mounting", "Sonar mounting geometry", WARN,
            f"Mounting geometry is PROVISIONAL — {path} says nobody measured it",
            "Measure the tilt and the lever arm from the GNSS antenna to the "
            "transducer, to the centimetre, then set measured: true. Until "
            "then every sounding carries the same unknown offset.",
        )

    who = m.get("measured_by") or "unrecorded"
    when = m.get("measured_utc") or "date unrecorded"
    return CheckResult(
        "sonar.mounting", "Sonar mounting geometry", PASS,
        f"Measured by {who}, {when}", "",
    )


# -- resources ------------------------------------------------------------


@check("disk.space", "Disk space", critical=True)
def _disk(state: dict, t: Thresholds) -> CheckResult:
    """Hours of *recording*, which is not hours of survey.

    A 500 GB drive reports well over a hundred hours here, and reading that as
    "we can go out for a hundred hours" is the wrong conclusion: the battery
    stops the vessel first, by a wide margin (docs/open_questions.md Q5). The
    message says which of the two binds, so the number cannot be misread.
    """
    free = state.get("disk_free_bytes")
    if free is None:
        return _missing("disk.space", "Disk space", "cannot read the missions disk")
    hours = free / (3.5 * 1024**3)          # 2-5 GB per hour; 3.5 is the middle
    if free < t.min_disk_free_bytes:
        return CheckResult(
            "disk.space", "Disk space", FAIL,
            f"{free / 1024**3:.0f} GB free — about {hours:.1f} hours of recording",
            "Export and delete an old mission before launching.",
            free / 1024**3, "GB",
        )

    endurance_h = state.get("battery_endurance_s")
    endurance_h = endurance_h / 3600.0 if endurance_h else None
    if endurance_h and endurance_h < hours:
        tail = f"— {hours:.0f} hours of recording, but the battery gives {endurance_h:.1f}"
    else:
        tail = f"— about {hours:.0f} hours of recording"
    return CheckResult(
        "disk.space", "Disk space", PASS,
        f"{free / 1024**3:.0f} GB free {tail}",
        "", free / 1024**3, "GB",
    )


@check("disk.speed", "Disk write speed")
def _disk_speed(state: dict, t: Thresholds) -> CheckResult:
    mbps = state.get("disk_write_mbps")
    if mbps is None:
        return _missing("disk.speed", "Disk write speed", "not measured")
    if mbps < t.min_disk_write_mbps:
        return CheckResult(
            "disk.speed", "Disk write speed", FAIL,
            f"Measured {mbps:.0f} MB/s, need {t.min_disk_write_mbps:.0f}",
            "The sonar produces several GB per hour. A slow card will drop "
            "data mid-survey. Use the SSD, not the eMMC or an SD card.",
            mbps, "MB/s",
        )
    return CheckResult(
        "disk.speed", "Disk write speed", PASS, f"{mbps:.0f} MB/s", "", mbps, "MB/s"
    )


@check("battery.charge", "Battery", critical=True)
def _battery(state: dict, t: Thresholds) -> CheckResult:
    soc = state.get("state_of_charge")
    voltage = state.get("battery_voltage")
    if soc is None and voltage is None:
        return _missing("battery.charge", "Battery", "no battery messages")
    if voltage is not None and voltage < t.min_battery_voltage:
        return CheckResult(
            "battery.charge", "Battery", FAIL,
            f"{voltage:.1f} V — below the safe minimum",
            "Do not launch. Charge or swap the pack.",
            voltage, "V",
        )
    if soc is not None and soc < t.min_battery_soc:
        return CheckResult(
            "battery.charge", "Battery", WARN,
            f"{soc * 100:.0f}% charge",
            "Enough to launch, probably not enough to finish a survey. "
            "Check the power panel's endurance estimate against the plan.",
            soc * 100, "%",
        )
    return CheckResult(
        "battery.charge", "Battery", PASS,
        f"{soc * 100:.0f}%, {voltage:.1f} V" if soc is not None and voltage is not None
        else "Present",
        "", (soc or 0) * 100, "%",
    )


@check("link.quality", "Shore link")
def _link(state: dict, t: Thresholds) -> CheckResult:
    rtt = state.get("link_rtt_ms")
    bearer = state.get("link_active")
    if rtt is None:
        return _missing("link.quality", "Shore link", "no client connected")
    if rtt > t.max_link_rtt_ms:
        return CheckResult(
            "link.quality", "Shore link", WARN,
            f"{bearer} link, {rtt:.0f} ms round trip",
            "The GUI will fall back to a reduced data set. Usable for "
            "supervision; move closer for anything more.",
            rtt, "ms",
        )
    return CheckResult(
        "link.quality", "Shore link", PASS,
        f"{bearer} link, {rtt:.0f} ms round trip", "", rtt, "ms",
    )


@check("ros.nodes", "ROS 2 nodes")
def _nodes(state: dict, t: Thresholds) -> CheckResult:
    expected = state.get("expected_nodes")
    present = state.get("present_nodes")
    if expected is None or present is None:
        return _missing("ros.nodes", "ROS 2 nodes", "node list unavailable")
    missing = [n for n in expected if n not in present]
    if missing:
        return CheckResult(
            "ros.nodes", "ROS 2 nodes", FAIL,
            f"Not running: {', '.join(missing)}",
            "Check the container logs (podman logs). A node that died at boot "
            "usually leaves the reason in the first ten lines.",
            len(missing), "nodes",
        )
    return CheckResult(
        "ros.nodes", "ROS 2 nodes", PASS,
        f"All {len(expected)} expected nodes running", "", len(expected), "nodes",
    )


# -- running the lot ------------------------------------------------------


@dataclass
class Report:
    go: bool
    summary: str
    items: list[CheckResult] = field(default_factory=list)
    run_utc_ms: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return {
            "go": self.go,
            "summary": self.summary,
            "run_utc_ms": self.run_utc_ms,
            "duration_s": self.duration_s,
            "items": [item.to_dict() for item in self.items],
        }

    def worst(self) -> str:
        return max((i.status for i in self.items), key=lambda s: _SEVERITY[s], default=PASS)


def run_checks(
    state: dict,
    thresholds: Thresholds | None = None,
    only: list[str] | None = None,
    run_utc_ms: int = 0,
) -> Report:
    """Run every passive check and produce a GO / NO-GO verdict.

    A ``FAIL`` anywhere is NO-GO. ``WARN`` and ``SKIPPED`` do not block launch —
    a check that could not run must not ground the vessel, but it must be
    visible, which is why it is listed rather than omitted.
    """
    thresholds = thresholds or Thresholds()
    selected = [c for c in CHECKS if only is None or c.id in only]
    items = [c.fn(state, thresholds) for c in selected]
    critical_ids = {c.id for c in selected if c.critical}

    failures = [i for i in items if i.status == FAIL]
    warnings = [i for i in items if i.status == WARN]
    skipped = [i for i in items if i.status == SKIPPED]
    blocked = [i for i in skipped if i.id in critical_ids]

    go = not failures and not blocked

    if failures:
        summary = f"NO-GO — {failures[0].message.rstrip('.')}"
        if len(failures) > 1:
            summary += f" (and {len(failures) - 1} more)"
    elif blocked:
        names = ", ".join(i.name for i in blocked[:3])
        summary = f"NO-GO — cannot confirm: {names}"
        if len(blocked) > 3:
            summary += f" (and {len(blocked) - 3} more)"
    elif warnings:
        summary = f"GO with {len(warnings)} warning(s) — {warnings[0].message.rstrip('.')}"
    elif skipped:
        summary = f"GO — {len(skipped)} non-critical check(s) could not run"
    else:
        summary = "GO — everything checks out"

    return Report(go=go, summary=summary, items=items, run_utc_ms=run_utc_ms)
