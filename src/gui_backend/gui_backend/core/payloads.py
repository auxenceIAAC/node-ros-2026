"""Wire payloads.

The contract between the backend and the browser. Kept in one module, as plain
functions over plain data, so that the shape of every message is readable in one
place and testable without a socket.

Two conventions hold everywhere:

* **``source_utc_ms`` is when the value was produced, not when it was sent.**
  Every payload carries it. The client renders data age from it, which is the
  only way an operator on an intermittent link can tell a current position from
  a stale one (safety rule 7).
* **Detail levels shrink the payload, they do not lie about it.** A ``minimal``
  vessel payload has fewer fields; the fields it does have mean exactly what
  they mean at ``full``. Nothing is rounded into uselessness and presented as
  precise.
"""

from __future__ import annotations

import math

from asket_common.heading import HeadingEstimate
from asket_common.mode_arbitration import arming_block_reason
from asket_common.survey import SurveyPlan

from .streams import DETAIL_FULL, DETAIL_MINIMAL, DETAIL_REDUCED


def _f(value: float | None, digits: int = 6) -> float | None:
    """Round for the wire, and turn NaN into ``null``.

    JSON has no NaN. Serialising it produces invalid JSON that some parsers
    accept and others reject; ``null`` means "not available", which is what a
    NaN meant anyway.
    """
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return round(float(value), digits)


def _opt_bool(value):
    """``None`` stays ``None``; anything else becomes a real bool.

    ``bool(None)`` is ``False``, and ``False`` here means "the Pico told us it
    is not so". Collapsing the two turns a field that was never sent into a
    confident negative — for ``rc_link_ok`` that reads on screen as "RC lost",
    which is the single worst lie this panel could tell.
    """
    return None if value is None else bool(value)


def _opt_int(value):
    """Same, for counts. ``int(None)`` does not fail quietly — it raises, and
    it raised here the moment a real STATE line reached this function."""
    return None if value is None else int(value)


# -- vessel ---------------------------------------------------------------


def vessel_payload(sample, detail: str = DETAIL_FULL) -> dict:
    """Position and motion.

    Position survives into every detail level, including LTE-M. If the link is
    down to a beacon, where the boat is remains the one thing worth a kilobyte.
    """
    out = {
        "lat": _f(sample.lat, 7),
        "lon": _f(sample.lon, 7),
        "heading_deg": _f(sample.heading_deg, 1),
        "heading_valid": bool(sample.heading_valid),
    }
    if detail == DETAIL_MINIMAL:
        return out

    out.update(
        {
            "cog_deg": _f(sample.cog_deg, 1),
            "sog_ms": _f(sample.sog_ms, 2),
            "heading_source": sample.heading_source,
        }
    )
    if detail == DETAIL_REDUCED:
        return out

    out.update(
        {
            "alt_m": _f(sample.alt, 2),
            "roll_deg": _f(sample.roll_deg, 2),
            "pitch_deg": _f(sample.pitch_deg, 2),
            "gnss_fix_type": _opt_int(sample.gnss_fix_type),
            # Absent, not zero, and therefore not int() either.
            #
            # `vessel_from_odometry` sets this to None on purpose: neither
            # NavSatFix nor Odometry carries a satellite count and this stack
            # has no MAVROS GPSRAW, so "0 satellites" would read as a GNSS
            # failure and ground a healthy vessel. int(None) raises, and it
            # raised on the first real Gazebo fix that reached this function —
            # killing the hub's tick loop and with it every stream, at every
            # detail level, for as long as the page stayed open.
            #
            # `_opt_int` and `_opt_bool` were written for exactly this in
            # `pico_payload` and never applied here. SimSource always supplies
            # a real count, so nothing in sim could reach the branch.
            "num_sats": _opt_int(sample.num_sats),
            "hdop": _f(sample.hdop, 2),
            "distance_travelled_m": _f(sample.distance_travelled_m, 1),
            "on_survey": bool(sample.on_survey),
        }
    )
    return out


def heading_payload(estimate: HeadingEstimate, detail: str = DETAIL_FULL) -> dict:
    """Heading with its provenance.

    Heading error translates directly into seabed position error — about 90 cm
    per degree at 50 m — so the number never travels without its source, its
    validity and how far it has drifted from course over ground.
    """
    out = {
        "heading_deg": _f(estimate.heading_deg, 1),
        "source": estimate.source,
        "valid": bool(estimate.valid),
        "divergence_deg": _f(estimate.divergence_deg, 1),
        "divergence_meaningful": bool(estimate.divergence_meaningful),
        "divergence_suspicious": bool(estimate.divergence_suspicious),
    }
    if detail == DETAIL_FULL:
        out["accuracy_deg"] = _f(estimate.accuracy_deg, 2)
        # Whether the device said so or we assumed it for the class of device.
        # Shown identically otherwise, the two would be indistinguishable, and
        # a compass reporting a degraded accuracy is the case worth seeing.
        out["accuracy_reported"] = bool(estimate.accuracy_reported)
        out["cog_deg"] = _f(estimate.cog_deg, 1)
        out["sog_ms"] = _f(estimate.sog_ms, 2)
        out["seabed_error_at_50m_m"] = _f(estimate.position_error_at_m(50.0), 2)
    return out


# -- vessel systems -------------------------------------------------------

MODE_NAMES = {0: "ESTOP", 1: "MANUAL", 2: "AUTONOMOUS"}


def pico_payload(sample, detail: str = DETAIL_FULL) -> dict:
    """Confirmed vessel state.

    Everything here is what the Pico says the vessel *is* doing. Nothing in this
    payload ever reflects a request (safety rule 4) — with one labelled
    exception, ``mode_requested_auto``, which is carried precisely so the two
    can be told apart on screen.
    """
    out = {
        "mode": MODE_NAMES.get(sample.mode, "UNKNOWN"),
        "armed": _opt_bool(sample.armed),
        "estop_latched": _opt_bool(sample.estop_latched),
        # Carried on every profile, beacon included. A GUI parsing a firmware
        # it does not understand must say so at any bandwidth, because every
        # other field in this payload is then suspect.
        "firmware_version": _opt_int(getattr(sample, "firmware_version", None)),
        "version_mismatch": _opt_bool(getattr(sample, "version_mismatch", None)),
    }
    if detail == DETAIL_MINIMAL:
        return out

    out.update(
        {
            "rc_link_ok": _opt_bool(sample.rc_link_ok),
            "rc_channel8_raw": _opt_int(getattr(sample, "rc_channel8_raw", None)),
            # The comparison is made once, against the firmware's own
            # threshold, in pico_state. The GUI renders the verdict rather than
            # re-deriving it from a number whose scale it would have to know.
            "ch8_asserting_estop": _opt_bool(getattr(sample, "ch8_asserting_estop", None)),
        }
    )
    if detail == DETAIL_REDUCED:
        return out

    out.update(
        {
            "relay_states": [bool(r) for r in sample.relay_states],
            "esc_status": [int(e) for e in sample.esc_status],
            # Observable, never commandable. Shown so an operator can confirm
            # the sovereign path is where they think it is.
            "hardware_killswitch_engaged": _opt_bool(
                getattr(sample, "hardware_killswitch_engaged", None)
            ),
            "rc_channel7_raw": _opt_int(getattr(sample, "rc_channel7_raw", None)),
            # What the Jetson asked for, next to what it got. "I asked for AUTO
            # and it is still MANUAL" then reads as a refusal by channel 8
            # rather than as a lost command.
            "mode_requested_auto": _opt_bool(getattr(sample, "mode_requested_auto", None)),
            # The Pico's view of our heartbeat. Zero while pico_bridge is
            # running points at the cable, not the software.
            "link_live": _opt_bool(getattr(sample, "link_live", None)),
            # The only measurement of radio *quality* in the system. A rising
            # bad count warns of range loss before the link actually drops.
            "sbus_frames_ok": _opt_int(getattr(sample, "sbus_frames_ok", None)),
            "sbus_frames_bad": _opt_int(getattr(sample, "sbus_frames_bad", None)),
            "sbus_failsafe": _opt_bool(getattr(sample, "sbus_failsafe", None)),
            "sbus_frame_lost": _opt_bool(getattr(sample, "sbus_frame_lost", None)),
            # Why the propellers cannot turn, in one sentence, or absent when
            # nothing is blocking them.
            #
            # Arming is channel 7 and nothing else — deliberately not
            # commandable from software, because somebody is physically present
            # to launch this boat and flipping that switch is their consent.
            # The cost is a state that reads as a fault: a mission permitted,
            # engaged and confirmed in the STATE line, and the vessel moves
            # nothing. The reason is computed once, in asket_common, so the
            # panel, the chip and the log cannot drift into three different
            # explanations of the same switch.
            "arming_block": arming_block_reason(
                _opt_bool(sample.armed),
                rc_arm_high=_opt_bool(getattr(sample, "ch7_arm_high", None)),
                estop_latched=_opt_bool(sample.estop_latched),
            ),
        }
    )
    return out


def power_payload(sample, detail: str = DETAIL_FULL, survey_remaining_m: float | None = None,
                  speed_ms: float | None = None) -> dict:
    """Battery, and the comparison that actually matters.

    With one sonar unit covering one side, the survey distance roughly doubles.
    So the useful question is not "how much charge is left" but "is there enough
    to finish", and that comparison is computed here rather than left to the
    operator's arithmetic on a beach.
    """
    out = {
        "state_of_charge": _f(sample.state_of_charge, 3),
        "voltage": _f(sample.voltage, 2),
    }
    if detail == DETAIL_MINIMAL:
        return out

    out.update(
        {
            "current": _f(sample.current, 2),
            "endurance_s": _f(sample.endurance_s, 0),
        }
    )
    if detail == DETAIL_REDUCED:
        return out

    out.update(
        {
            "power_w": _f(sample.power_w, 1),
            "remaining_wh": _f(sample.remaining_wh, 1),
            "consumed_wh": _f(sample.consumed_wh, 1),
        }
    )

    if survey_remaining_m is not None and speed_ms and speed_ms > 0.1:
        survey_time_s = survey_remaining_m / speed_ms
        out["survey_remaining_m"] = _f(survey_remaining_m, 0)
        out["survey_remaining_s"] = _f(survey_time_s, 0)
        endurance = sample.endurance_s
        if endurance and not math.isinf(endurance):
            out["endurance_margin_s"] = _f(endurance - survey_time_s, 0)
            out["can_finish_survey"] = bool(endurance > survey_time_s)
    return out


# -- sensors --------------------------------------------------------------


def sonar_payload(health, detail: str = DETAIL_FULL) -> dict:
    out = {
        "connected": bool(health.connected),
        "actual_ping_rate_hz": _f(health.actual_ping_rate_hz, 2),
        "clock_offset_ms": int(health.clock_offset_ms),
        "clock_ok": bool(health.clock_ok),
        "clock_compromised": bool(health.clock_compromised),
    }
    if detail != DETAIL_FULL:
        return out

    out.update(
        {
            "commanded_ping_rate_hz": _f(health.commanded_ping_rate_hz, 2),
            "ping_rate_ok": bool(health.ping_rate_ok),
            "points_per_ping": int(health.points_per_ping),
            "valid_points_per_ping": int(health.valid_points_per_ping),
            "speed_of_sound": _f(health.speed_of_sound, 1),
            "range_setting_m": _f(health.range_setting_m, 1),
            "gain_setting": int(health.gain_setting),
            "packet_loss_ratio": _f(health.packet_loss_ratio, 4),
            "pitch_deg": _f(health.pitch_deg, 2),
            "roll_deg": _f(health.roll_deg, 2),
            "checksum_errors": int(health.checksum_errors),
            "bytes_discarded": int(health.bytes_discarded),
            "seconds_since_data": _f(health.seconds_since_data, 1),
            # Whether the ping rate is the device's own figure or our
            # measurement of arrivals. Ours also measures the network, so the
            # panel says which it is showing.
            "rate_from_device": bool(health.rate_from_device),
        }
    )
    return out


def lidar_payload(scan, detail: str = DETAIL_FULL, decimation: int = 1) -> dict:
    """Obstacle returns, top-down and vessel-centred.

    Returns are sent as ``[bearing_deg, range_m]`` pairs rather than a dense
    array of nulls: on a 720-beam scanner over open water, most beams return
    nothing, and sending 720 nulls to say so is the kind of waste that sinks a
    4G link.

    Raw and filtered are both sent at full detail because the operator must be
    able to tell whether the sensor or the filter is at fault.
    """

    def pairs(ranges):
        out = []
        for i in range(0, len(ranges), max(1, decimation)):
            r = ranges[i]
            if r is not None:
                out.append([round(scan.angle_min_deg + i * scan.angle_increment_deg, 1),
                            round(r, 2)])
        return out

    nearest = scan.nearest(use_filtered=True)
    payload = {
        "rotation_hz": _f(scan.rotation_hz, 2),
        "points_per_revolution": int(scan.points_per_revolution),
        # How many beams the scanner swept, as against how many came back with
        # anything. Without it, open water and a blind sensor both read "0", and
        # those need very different responses.
        "beams_per_revolution": len(scan.ranges_m),
        "nearest_range_m": _f(nearest[0], 2) if nearest else None,
        "nearest_bearing_deg": _f(nearest[1], 1) if nearest else None,
        "filtered": pairs(scan.filtered_m),
    }
    if detail == DETAIL_FULL:
        payload["raw"] = pairs(scan.ranges_m)
    return payload


def link_payload(sample, profile: str, profile_manual: bool, clients: int,
                 rate_bytes_per_s: float, detail: str = DETAIL_FULL) -> dict:
    out = {
        "active_link": sample.active_link,
        "quality": _f(sample.quality, 2),
        "rtt_ms": _f(sample.rtt_ms, 0),
        "profile": profile,
        "profile_manual": bool(profile_manual),
    }
    if detail == DETAIL_FULL:
        out.update(
            {
                "capacity_bytes_per_s": _f(sample.capacity_bytes_per_s, 0),
                "rate_bytes_per_s": _f(rate_bytes_per_s, 0),
                "connected_clients": int(clients),
                "distance_m": _f(sample.distance_m, 0),
            }
        )
    return out


# -- map layers -----------------------------------------------------------


def track_payload(points: list[tuple[float, float]], cursor: int, step: int = 1) -> dict:
    """The vessel track, as an increment.

    ``from`` is where this increment starts; the client appends. Coordinates go
    out as ``[lon, lat]`` pairs rounded to seven places — about a centimetre,
    which is finer than anything we can actually measure.
    """
    new = points[cursor:]
    if step > 1:
        new = new[::step]
    return {
        "from": cursor,
        "total": len(points),
        "points": [[round(lon, 7), round(lat, 7)] for lat, lon in new],
    }


#: One coverage sample on the wire, as a fixed-order array rather than an
#: object: ``[lat, lon, heading_deg, half_width_m, inner_gap_m]``. A gap in the
#: data is ``null``. Objects cost about ninety bytes a sample and arrays about
#: forty, and over a three-hour survey that difference is megabytes.
COVERAGE_FIELDS = ["lat", "lon", "heading_deg", "half_width_m", "inner_gap_m"]


def coverage_payload(segments: list[dict], cursor: int, side: str, step: int = 1) -> dict:
    """The swath ribbon, as an increment.

    A ``null`` entry is a gap — a stretch where the sonar was not ensonifying,
    because the vessel was turning, the heading was invalid, or the sonar had
    dropped out. The client renders gaps as gaps. With one sonar unit covering
    one side only, a gap that renders as filled is the most expensive way this
    GUI could mislead an operator.
    """
    new = segments[cursor:]
    if step > 1:
        new = new[::step]
    encoded: list[list[float] | None] = []
    for segment in new:
        if segment.get("gap"):
            encoded.append(None)
        else:
            encoded.append([
                round(segment["lat"], 7),
                round(segment["lon"], 7),
                round(segment["heading_deg"], 1),
                round(segment["half_width_m"], 1),
                round(segment["inner_gap_m"], 1),
            ])
    return {"from": cursor, "total": len(segments), "side": side, "segments": encoded}


def plan_payload(plan: SurveyPlan, geofence: list[tuple[float, float]] | None = None) -> dict:
    """Survey lines and the geofence. Sent on subscribe, then on change."""
    return {
        "lines": [
            {"index": i, "coords": [[lon, lat] for lat, lon in seg]}
            for i, seg in enumerate(plan.line_segments_geo())
        ],
        "sonar_side": plan.sonar_side,
        "line_spacing_m": _f(plan.line_spacing_m, 1),
        "total_survey_distance_m": _f(plan.total_survey_distance_m(), 0),
        "geofence": [[lon, lat] for lat, lon in (geofence or [])],
    }
