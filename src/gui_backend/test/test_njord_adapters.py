"""The adapters, against the Njord stack's real message shapes.

No ROS here: the adapters are plain functions over anything with the right
attributes, which is what lets them be tested on a laptop. The stand-ins below
mirror the real messages field for field.
"""

import json
import math
from types import SimpleNamespace

import pytest
from gui_backend.core import adapters, payloads


def header(sec=1_800_000_000, nsec=0):
    return SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nsec))


def quat_from_yaw(yaw_deg):
    half = math.radians(yaw_deg) / 2.0
    return SimpleNamespace(x=0.0, y=0.0, z=math.sin(half), w=math.cos(half))


def fix(lat=-22.9576, lon=14.5053, cov0=4.0, status=0):
    return SimpleNamespace(
        header=header(), latitude=lat, longitude=lon, altitude=0.0,
        position_covariance=[cov0, 0, 0, 0, cov0, 0, 0, 0, 9.0],
        status=SimpleNamespace(status=status),
    )


def odom(yaw_deg=0.0, vx=0.0, vy=0.0):
    return SimpleNamespace(
        header=header(),
        pose=SimpleNamespace(pose=SimpleNamespace(orientation=quat_from_yaw(yaw_deg))),
        twist=SimpleNamespace(twist=SimpleNamespace(
            linear=SimpleNamespace(x=vx, y=vy, z=0.0))),
    )


# -- heading and course ---------------------------------------------------


@pytest.mark.parametrize("yaw_deg, expected_bearing", [
    (0.0, 90.0),      # ENU yaw 0 = pointing east = 090
    (90.0, 0.0),      # yaw 90 = north = 000
    (180.0, 270.0),   # west
    (-90.0, 180.0),   # south
])
def test_enu_yaw_becomes_a_compass_bearing(yaw_deg, expected_bearing):
    """ROS is counter-clockwise from east; a bearing is clockwise from north.
    Getting this backwards puts the whole survey on the wrong side of the boat,
    plausibly, and nothing on screen would look wrong."""
    record = adapters.vessel_from_odometry(fix(), odom(yaw_deg=yaw_deg))
    assert record.heading_deg == pytest.approx(expected_bearing, abs=1e-6)


def test_course_over_ground_comes_from_the_velocity_not_the_heading():
    """Heading and course must be derived independently or comparing them says
    nothing — and that comparison is the row the heading panel exists for.

    Bow pointing north, drifting east: the two must differ."""
    record = adapters.vessel_from_odometry(fix(), odom(yaw_deg=90.0, vx=0.0, vy=-2.0))
    assert record.heading_deg == pytest.approx(0.0, abs=1e-6)
    assert record.cog_deg == pytest.approx(90.0, abs=1e-6)


def test_course_is_not_computed_from_noise_at_rest():
    record = adapters.vessel_from_odometry(fix(), odom(yaw_deg=45.0, vx=0.01, vy=0.0))
    assert record.sog_ms < 0.05
    assert record.cog_deg == 0.0


def test_speed_is_the_magnitude_of_the_body_velocity():
    record = adapters.vessel_from_odometry(fix(), odom(vx=3.0, vy=4.0))
    assert record.sog_ms == pytest.approx(5.0)


def test_the_record_is_stamped_with_its_oldest_component():
    """A frozen GNSS must not hide behind a live EKF."""
    old = fix()
    old.header = header(sec=1_800_000_000)
    fresh = odom()
    fresh.header = header(sec=1_800_000_030)
    record = adapters.vessel_from_odometry(old, fresh)
    assert record.utc_ms == 1_800_000_000_000


# -- what this stack does not report --------------------------------------


def test_a_missing_satellite_count_is_absent_rather_than_zero():
    """Neither NavSatFix nor Odometry carries one, and this stack has no MAVROS
    GPSRAW. "0 satellites" would read as a GNSS failure and ground a healthy
    vessel; the pre-flight reports SKIPPED instead."""
    assert adapters.vessel_from_odometry(fix(), odom()).num_sats is None


def test_position_accuracy_comes_from_the_covariance_when_there_is_one():
    assert adapters.vessel_from_odometry(fix(cov0=4.0), odom()).hdop == pytest.approx(2.0)


def test_no_covariance_means_no_figure_invented():
    assert adapters.vessel_from_odometry(fix(cov0=0.0), odom()).hdop is None


def test_heading_is_absent_without_the_ekf_rather_than_defaulting_to_north():
    record = adapters.vessel_from_odometry(fix(), None)
    assert math.isnan(record.heading_deg)


# -- the Pico, as a string ------------------------------------------------


def test_a_string_status_is_parsed_and_stamped_with_our_receipt_time():
    """std_msgs/String has no header, so there is no sample time to use. The
    receipt time is honest about being ours."""
    msg = SimpleNamespace(data="STATE ver=4 mode=2 armed=0 ch8=1811 sbusfs=0")
    record = adapters.pico_from_ros(msg, received_utc_ms=1_800_000_000_000)
    assert record.utc_ms == 1_800_000_000_000
    # mode=2 on the wire is the firmware's MANUAL; 1 is the GUI's. The two
    # scales are bridged by name, never by an offset.
    assert record.mode == 1
    assert record.armed is False
    assert record.rc_link_ok is True
    assert record.rc_channel8_raw == 1811
    assert record.state_format_verified is False


def test_fields_the_line_does_not_carry_stay_absent():
    """None says "not reported". False would be a claim about something never
    sent — and for the killswitch that claim is the dangerous direction."""
    record = adapters.pico_from_ros(SimpleNamespace(data="STATE ver=4 mode=3 armed=1"), 0)
    assert record.estop_latched is None
    assert record.hardware_killswitch_engaged is None
    assert record.rc_channel8_raw is None
    assert record.ch8_asserting_estop is None
    # The Pico measures neither, and never did. The battery panel is fed from
    # elsewhere; looking for it here is looking in the wrong place.
    assert record.battery_voltage is None
    assert record.battery_current is None


def test_the_pico_does_report_an_estop_latch_when_it_has_one():
    """``estoplatch`` is a real field in pico-node_v4, and it explains the most
    common "why will it not arm"."""
    record = adapters.pico_from_ros(
        SimpleNamespace(data="STATE ver=4 mode=1 armed=0 estoplatch=1"), 0)
    assert record.estop_latched is True


def test_an_unreadable_status_line_yields_no_mode_at_all():
    record = adapters.pico_from_ros(SimpleNamespace(data="garbage"), 0)
    assert record.mode is None
    assert record.state_parsed is False


def test_a_structured_picostatus_still_works():
    """asket_sim publishes the proper message; both shapes reach the same GUI."""
    msg = SimpleNamespace(
        header=header(), mode=2, armed=True, estop_latched=False,
        relay_states=[True, True], esc_status=[0, 0], rc_link_ok=True,
        rc_channel8_raw=1811, rc_channel7_raw=1811, firmware_version=4,
        hardware_killswitch_engaged=False,
    )
    record = adapters.pico_from_ros(msg)
    assert record.mode == 2
    assert record.rc_channel8_raw == 1811
    assert record.ch8_asserting_estop is False
    assert record.state_format_verified is True


# -- obstacles ------------------------------------------------------------


def cloud(points):
    import struct
    fields = [SimpleNamespace(name="x", offset=0, datatype=7),
              SimpleNamespace(name="y", offset=4, datatype=7),
              SimpleNamespace(name="z", offset=8, datatype=7)]
    data = b"".join(struct.pack("<fff", x, y, 0.0) for x, y in points)
    return SimpleNamespace(header=header(), fields=fields, point_step=12,
                           is_bigendian=False, data=data)


def test_a_point_cloud_becomes_bearings_and_ranges():
    """x forward, y left, bearing clockwise from the bow."""
    pairs = adapters.obstacles_from_pointcloud(cloud([(10.0, 0.0), (0.0, -5.0)]))
    assert pairs[0] == [0.0, 10.0]      # dead ahead
    assert pairs[1] == [90.0, 5.0]      # abeam to starboard


def test_nan_padding_is_dropped_rather_than_placed_on_the_hull():
    """A NaN read as zero puts a boulder underneath the boat."""
    pairs = adapters.obstacles_from_pointcloud(cloud([(float("nan"), 0.0), (4.0, 0.0)]))
    assert pairs == [[0.0, 4.0]]


def test_a_cloud_without_x_and_y_yields_nothing_rather_than_guessing():
    empty = SimpleNamespace(header=header(), fields=[], point_step=12,
                            is_bigendian=False, data=b"")
    assert adapters.obstacles_from_pointcloud(empty) == []


# -- the class of bug, not the instance ------------------------------------
#
# Twice now a payload function has crashed on a field that is absent by design:
# `int(sample.rc_channel8_raw_pct)` on a STATE line that did not carry it, and
# `int(sample.num_sats)` on a NavSatFix, which never carries one. Both were
# unreachable in sim, because SimSource fills every field with a real value.
#
# So the check is not "does num_sats work" but "does anything the ROS adapters
# can produce survive being serialised".


def _ros_shaped_fix(**overrides):
    """A NavSatFix as Gazebo actually sends it, fields overridable."""
    header = SimpleNamespace(stamp=SimpleNamespace(sec=1789932000, nanosec=0))
    base = dict(
        header=header,
        latitude=63.430499999730479,
        longitude=10.395097370687948,
        altitude=0.39618292078375816,
        position_covariance=[0.0] * 9,
        status=SimpleNamespace(status=0),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _ros_shaped_odom(vx=1e-13, vy=1e-13):
    header = SimpleNamespace(stamp=SimpleNamespace(sec=1789932000, nanosec=0))
    return SimpleNamespace(
        header=header,
        pose=SimpleNamespace(
            pose=SimpleNamespace(
                orientation=SimpleNamespace(x=0.0, y=0.0, z=0.0, w=1.0)
            )
        ),
        twist=SimpleNamespace(
            twist=SimpleNamespace(linear=SimpleNamespace(x=vx, y=vy, z=0.0))
        ),
    )


@pytest.mark.parametrize("detail", ["minimal", "reduced", "full"])
def test_a_real_gazebo_fix_survives_the_vessel_payload(detail):
    """The exact message from the Jetson, at every detail level.

    `full` is the one that crashed, and it was only reachable once the link
    profile stopped degrading itself — so fixing one fault is what exposed
    this one. A stationary vessel publishing an identical fix at 2 Hz is
    exactly the case that was running when it went down.
    """
    record = adapters.vessel_from_odometry(
        _ros_shaped_fix(), _ros_shaped_odom(), None,
        extra={"heading_source": "ekf", "heading_valid": True,
               "distance_travelled_m": 0.0},
    )
    payload = payloads.vessel_payload(record, detail)

    if detail == "full":
        # Absent, and absent means null — never 0, which on a GNSS panel reads
        # as a failed fix and would ground a healthy vessel.
        assert "num_sats" in payload
        assert payload["num_sats"] is None


def test_no_ros_vessel_record_can_break_the_payload():
    """Every optional field, absent, one at a time and all at once."""
    variants = {
        "no covariance": _ros_shaped_fix(position_covariance=[0.0] * 9),
        "no status": _ros_shaped_fix(status=SimpleNamespace(status=-1)),
        "real covariance": _ros_shaped_fix(
            position_covariance=[2.5] + [0.0] * 8
        ),
    }
    for label, fix in variants.items():
        for odom in (None, _ros_shaped_odom()):
            record = adapters.vessel_from_odometry(fix, odom, None)
            for detail in ("minimal", "reduced", "full"):
                try:
                    body = payloads.vessel_payload(record, detail)
                except Exception as exc:                      # pragma: no cover
                    raise AssertionError(
                        f"{label} / odom={odom is not None} / {detail} "
                        f"raised {type(exc).__name__}: {exc}"
                    ) from exc
                # Serialising is the real test: a NaN or a numpy scalar that
                # survives the function still breaks the socket.
                json.dumps(body)
