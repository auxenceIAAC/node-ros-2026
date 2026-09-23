"""Pre-flight checks.

The requirement these are written against: on a Namibian beach, identify the
faulty link in thirty seconds rather than an hour.
"""

import pytest
from system_test.core.checks import (
    CHECKS,
    FAIL,
    PASS,
    SKIPPED,
    WARN,
    Thresholds,
    run_checks,
)

HEALTHY = dict(
    pico_age_s=0.1, rc_link_ok=True,
    # Raw SBUS counts, 172..1811. The firmware's thresholds are on this scale.
    rc_channel8_raw=1811, rc_channel7_raw=1811, pico_firmware_version=4,
    num_sats=14, gnss_fix_type=3, hdop=0.8,
    heading_valid=True, heading_source="gnss_compass", heading_accuracy_deg=0.2,
    heading_divergence_deg=2.0, heading_divergence_meaningful=True,
    roll_deg=1.0, pitch_deg=0.5,
    lidar_rotation_hz=10.0, lidar_points_per_revolution=400,
    sonar_connected=True, sonar_ping_rate_hz=5.0, sonar_points_per_ping=256,
    clock_offset_ms=12,
    # The Jetson's own clock against GPS time. A healthy vessel is
    # disciplined; the check that reads this measures drift rather than
    # asking whether the NTP daemon says it is locked.
    gps_time_offset_ms=40, gps_time_age_s=1.0,
    disk_free_bytes=200 * 1024**3, disk_write_mbps=120.0,
    state_of_charge=0.9, battery_voltage=28.0,
    link_rtt_ms=25.0, link_active="wifi",
    expected_nodes=["omniscan_bridge"], present_nodes=["omniscan_bridge"],
    pico_state_format_verified=True, pico_state_parsed=True, pico_state_unknown_keys=[],
    pico_estop_feedback_enabled=True,
    mounting=dict(
        path="/etc/asket/mounting.yaml", found=True, measured=True,
        measured_by="AD", measured_utc="2026-03-02", error="", unknown_fields=[],
    ),
)

#: What the repository ships with today: a file nobody has measured against.
PROVISIONAL_MOUNTING = dict(
    path="/etc/asket/mounting.yaml", found=True, measured=False,
    measured_by="", measured_utc="", error="", unknown_fields=[],
)


def item(report, check_id):
    return next(i for i in report.items if i.id == check_id)


def test_a_healthy_vessel_is_go():
    report = run_checks(HEALTHY)
    assert report.go
    assert report.summary == "GO — everything checks out"


def test_every_failing_check_says_what_to_do_about_it():
    """A red word is not an instruction. 'GPS: ERROR' costs an hour."""
    broken = dict(
        HEALTHY, num_sats=2, gnss_fix_type=1, sonar_connected=False, clock_offset_ms=5000,
        disk_free_bytes=1024**3, disk_write_mbps=3.0, battery_voltage=19.0,
        rc_link_ok=False, lidar_rotation_hz=0.0, heading_valid=False,
        present_nodes=[], pico_age_s=30.0,
    )
    report = run_checks(broken)
    assert not report.go
    for result in report.items:
        if result.status in (FAIL, WARN, SKIPPED):
            assert result.remedy, f"{result.id} has no remedy"
            assert len(result.message) > 10


def test_messages_carry_the_number_and_the_requirement():
    """Not 'GPS: ERROR' but '4 satellites, 6 required'."""
    report = run_checks(dict(HEALTHY, num_sats=4))
    gnss = item(report, "gnss.fix")
    assert gnss.status == FAIL
    assert "4 satellites" in gnss.message
    assert "6 required" in gnss.message


def test_nothing_reporting_is_no_go_not_go():
    """'GO — 14 checks could not run' is the most dangerous sentence this panel
    could produce."""
    report = run_checks({})
    assert not report.go
    assert "cannot confirm" in report.summary
    assert all(i.status == SKIPPED for i in report.items)


def test_a_skipped_non_critical_check_does_not_ground_the_vessel():
    without_link = {k: v for k, v in HEALTHY.items() if k != "link_rtt_ms"}
    report = run_checks(without_link)
    assert report.go
    assert item(report, "link.quality").status == SKIPPED


def test_a_sonar_on_the_network_and_a_sonar_absent_are_different_faults():
    """They send you to different cables, so they must be different messages."""
    absent = item(run_checks(dict(HEALTHY, sonar_connected=False, sonar_discovered=False)),
                  "sonar.link")
    silent = item(run_checks(dict(HEALTHY, sonar_connected=False, sonar_discovered=True)),
                  "sonar.link")

    assert absent.status == silent.status == FAIL
    assert absent.message != silent.message
    assert "Ethernet cable" in absent.remedy
    assert "Power-cycle" in silent.remedy


def test_clock_drift_is_a_hard_fail_and_says_what_it_costs():
    """Post-mission fusion pairs on this timestamp. Drift does not degrade the
    survey, it destroys it."""
    result = item(run_checks(dict(HEALTHY, clock_offset_ms=5000)), "sonar.clock")
    assert result.status == FAIL
    assert "un-georeferenceable" in result.remedy
    assert "NTP" in result.remedy


def test_heading_accuracy_is_reported_as_seabed_error():
    """The number that makes heading matter."""
    result = item(
        run_checks(dict(HEALTHY, heading_source="magnetometer", heading_accuracy_deg=15.0)),
        "heading.valid",
    )
    assert result.status == WARN
    assert " m of seabed error at 50 m" in result.message


def test_divergence_is_skipped_rather_than_failed_when_stationary():
    """Sitting still with a 40 degree divergence is not a fault."""
    result = item(
        run_checks(dict(HEALTHY, heading_divergence_deg=40.0,
                        heading_divergence_meaningful=False)),
        "heading.divergence",
    )
    assert result.status == SKIPPED
    assert "Too slow" in result.message


def test_disk_space_is_expressed_in_hours_of_recording():
    """'42 GB free' means nothing on a beach. 'About 12 hours' does."""
    result = item(run_checks(HEALTHY), "disk.space")
    assert "hours of recording" in result.message


def test_a_slow_disk_fails_because_the_sonar_will_outrun_it():
    result = item(run_checks(dict(HEALTHY, disk_write_mbps=5.0)), "disk.speed")
    assert result.status == FAIL
    assert "SSD" in result.remedy


def test_a_low_battery_warns_and_points_at_the_endurance_estimate():
    result = item(run_checks(dict(HEALTHY, state_of_charge=0.3)), "battery.charge")
    assert result.status == WARN
    assert "endurance" in result.remedy


def test_a_flat_battery_fails_outright():
    result = item(run_checks(dict(HEALTHY, battery_voltage=19.0)), "battery.charge")
    assert result.status == FAIL
    assert "Do not launch" in result.remedy


def test_the_killswitch_channel_being_asserted_is_a_warning_not_a_failure():
    """It is the safe state. Failing it would teach people to ignore the panel."""
    result = item(run_checks(dict(HEALTHY, rc_channel8_raw=200)), "rc.link")
    assert result.status == WARN
    assert "ESTOP zone" in result.message


def test_the_channel_8_threshold_is_on_the_sbus_scale_not_a_percentage():
    """The defect this replaces.

    The field was named ``rc_channel8_raw_pct`` and the check compared it with
    25, but what arrived was a raw SBUS count of 172..1811. Every real value
    cleared the threshold — including 200, which is the bottom of the travel
    and the exact position the warning exists for. The alarm was silent in the
    one case it was written for.
    """
    # Bottom of the travel: the firmware is in ESTOP, and so must the check be.
    assert item(run_checks(dict(HEALTHY, rc_channel8_raw=172)), "rc.link").status == WARN
    # Just inside the ESTOP zone.
    assert item(run_checks(dict(HEALTHY, rc_channel8_raw=699)), "rc.link").status == WARN
    # MODE_LOW_MAX itself: the firmware compares with `<`, so this is MANUAL.
    assert item(run_checks(dict(HEALTHY, rc_channel8_raw=700)), "rc.link").status == PASS
    # A value that would have been "fine" under the old percentage reading and
    # is in fact the kill position.
    assert item(run_checks(dict(HEALTHY, rc_channel8_raw=300)), "rc.link").status == WARN


# -- firmware version ------------------------------------------------------
#
# The check that closes the whole class of bug: for months the Jetson parsed a
# format the flashed firmware did not emit, and nothing said so.


def test_a_firmware_version_mismatch_grounds_the_vessel():
    """FAIL, not WARN, and critical.

    A version this software does not know is a firmware whose field layout is
    unknown. Every other row in the report is then built from fields that may
    have been read against the wrong layout, so a confident-looking GO would be
    the most dangerous output this check could produce.
    """
    report = run_checks(dict(HEALTHY, pico_firmware_version=3))
    result = item(report, "pico.firmware_version")
    assert result.status == FAIL
    assert "v3" in result.message
    assert "pico-node_v4" in result.remedy
    assert not report.go


def test_a_matching_firmware_version_passes():
    result = item(run_checks(HEALTHY), "pico.firmware_version")
    assert result.status == PASS


def test_a_pico_that_never_reports_a_version_is_unknown_not_assumed_good():
    state = dict(HEALTHY)
    del state["pico_firmware_version"]
    result = item(run_checks(state), "pico.firmware_version")
    assert result.status != PASS


def test_a_missing_node_names_the_node():
    result = item(
        run_checks(dict(HEALTHY, expected_nodes=["omniscan_bridge", "mission_recorder"],
                        present_nodes=["omniscan_bridge"])),
        "ros.nodes",
    )
    assert result.status == FAIL
    assert "mission_recorder" in result.message


def test_running_a_subset_runs_only_that_subset():
    report = run_checks(HEALTHY, only=["gnss.fix", "sonar.clock"])
    assert {i.id for i in report.items} == {"gnss.fix", "sonar.clock"}


def test_the_verdict_leads_with_the_worst_problem():
    report = run_checks(dict(HEALTHY, num_sats=2, state_of_charge=0.3))
    assert report.summary.startswith("NO-GO")
    assert "satellites" in report.summary


@pytest.mark.parametrize("registered", CHECKS, ids=lambda c: c.id)
def test_every_check_survives_a_completely_empty_state(registered):
    """A check that throws on missing data takes the whole pre-flight with it,
    on the day the pre-flight is most needed."""
    result = registered.fn({}, Thresholds())
    assert result.status in (PASS, WARN, FAIL, SKIPPED)
    assert result.message


# -- mounting geometry (Q2) ----------------------------------------------
#
# Nothing in software can measure a lever arm. All these checks do is refuse to
# let the vessel leave the beach without somebody having said, in writing, that
# they did.


def test_unmeasured_mounting_geometry_warns_but_does_not_ground_the_vessel():
    report = run_checks(dict(HEALTHY, mounting=PROVISIONAL_MOUNTING))
    result = item(report, "sonar.mounting")
    assert result.status == WARN
    assert "PROVISIONAL" in result.message
    assert "measured: true" in result.remedy
    # A warning, deliberately: an unmeasured lever arm ruins the survey, it
    # does not endanger the boat. Blocking launch on it would teach the crew to
    # ignore the verdict.
    assert report.go
    assert "warning" in report.summary


def test_the_warning_names_the_file_you_have_to_edit():
    report = run_checks(dict(HEALTHY, mounting=dict(
        PROVISIONAL_MOUNTING, path="/opt/asket/config/mounting.yaml")))
    assert "/opt/asket/config/mounting.yaml" in item(report, "sonar.mounting").message


def test_measured_geometry_passes_and_says_who_measured_it():
    result = item(run_checks(HEALTHY), "sonar.mounting")
    assert result.status == PASS
    # Not just "PASS": the crew can tell a measurement from March from one taken
    # after the transducer was remounted in June.
    assert "AD" in result.message and "2026-03-02" in result.message


def test_a_mounting_file_that_could_not_be_loaded_is_a_failure_not_a_warning():
    """The dangerous case: somebody measured the vessel and the numbers are
    being silently ignored in favour of the defaults."""
    report = run_checks(dict(HEALTHY, mounting=dict(
        PROVISIONAL_MOUNTING, found=False, error="not valid YAML: line 12")))
    result = item(report, "sonar.mounting")
    assert result.status == FAIL
    assert "not valid YAML" in result.message
    assert "defaults" in result.message
    assert not report.go


def test_a_typo_in_a_measured_value_is_not_swallowed():
    """`lever_y` instead of `lever_y_m` would otherwise be discarded in silence,
    leaving the default in place with the file looking filled in."""
    report = run_checks(dict(HEALTHY, mounting=dict(
        HEALTHY["mounting"], unknown_fields=["lever_y", "tilt"])))
    result = item(report, "sonar.mounting")
    assert result.status == WARN
    assert "lever_y" in result.message


def test_no_mounting_file_at_all_warns_with_the_geometry_named_as_a_guess():
    report = run_checks(dict(HEALTHY, mounting=dict(
        PROVISIONAL_MOUNTING, found=False, path="")))
    result = item(report, "sonar.mounting")
    assert result.status == WARN
    assert "guess" in result.message


def test_a_vessel_that_does_not_report_its_mounting_is_not_assumed_to_be_fine():
    """Unknown is not pass — the bridge may be running geometry nobody has seen."""
    state = dict(HEALTHY)
    del state["mounting"]
    result = item(run_checks(state), "sonar.mounting")
    assert result.status == SKIPPED


def test_the_disk_check_says_when_the_battery_is_what_actually_binds():
    """A 200 GB drive is 57 hours of recording. The battery is three. Reading
    the first number as an endurance is the mistake this wording prevents
    (docs/open_questions.md Q5)."""
    result = item(run_checks(dict(HEALTHY, battery_endurance_s=3.2 * 3600)), "disk.space")
    assert result.status == PASS
    assert "battery gives 3.2" in result.message


def test_an_unknown_endurance_is_not_quoted_as_a_number():
    result = item(run_checks(dict(HEALTHY, battery_endurance_s=None)), "disk.space")
    assert "battery" not in result.message


def test_a_disk_smaller_than_the_battery_is_the_binding_one_and_says_so():
    """Ten hours of battery, under nine of disk: the disk figure is the honest
    one and no battery caveat belongs on it."""
    result = item(run_checks(dict(
        HEALTHY, disk_free_bytes=30 * 1024**3, battery_endurance_s=10 * 3600,
    )), "disk.space")
    assert result.status == PASS
    assert "battery" not in result.message


# -- the Pico's STATE format (Q7) ----------------------------------------
#
# /pico/status is a std_msgs/String of raw firmware lines. The GUI parses that
# text, and the format it parses has never been checked against a real line. A
# parser reading the wrong format does not look broken — it looks like a vessel
# that is not reporting much — so the pre-flight has to say so out loud.


def test_an_unverified_state_format_warns_on_every_run():
    report = run_checks(dict(HEALTHY, pico_state_format_verified=False))
    result = item(report, "pico.state_format")
    assert result.status == WARN
    assert "never been checked" in result.message
    assert "ros2 topic echo /pico/status" in result.remedy
    # A warning, not a blocker: the vessel is fine, our reading of it is what
    # is in doubt.
    assert report.go


def test_a_line_the_parser_cannot_read_is_a_failure():
    """Not a warning. If the text cannot be parsed, every field in the vessel
    panel is absent and nothing there can be trusted."""
    report = run_checks(dict(
        HEALTHY, pico_state_parsed=False, pico_state_line="STATE 0x41 0x00 0x1f"))
    result = item(report, "pico.state_format")
    assert result.status == FAIL
    assert not report.go


def test_fields_the_parser_ignores_are_reported_rather_than_dropped():
    """An unrecognised key is usually the one that matters."""
    result = item(run_checks(dict(
        HEALTHY, pico_state_unknown_keys=["batt_mv", "faults"])), "pico.state_format")
    assert result.status == WARN
    assert "batt_mv" in result.message


def test_no_pico_status_at_all_is_unknown_not_a_pass():
    state = dict(HEALTHY)
    del state["pico_state_format_verified"]
    assert item(run_checks(state), "pico.state_format").status == SKIPPED


# -- the e-stop feedback that does not exist yet ---------------------------


def test_the_estop_feedback_warns_on_every_run_while_it_is_compiled_out():
    """What the repository ships with today.

    ESTOP_FEEDBACK_ENABLED is 0, so nothing in the firmware confirms the ESC
    rail actually collapsed when the relay was commanded open. Commanding a
    relay and observing the rail drop are two different claims and only the
    first is being made — so the pre-flight says so, every run, until somebody
    enables it.
    """
    report = run_checks(dict(HEALTHY, pico_estop_feedback_enabled=False))
    result = item(report, "pico.estop_feedback")
    assert result.status == WARN
    # The message has to say what is *not verified*, not merely that a flag is
    # off. A crew reading "ESTOP_FEEDBACK_ENABLED is 0" learns nothing.
    assert "collapsed" in result.message or "verified" in result.message.lower()
    assert "relay was commanded open" in result.message
    assert "ESTOP_FEEDBACK_ENABLED" in result.remedy

    # Amber, not red: the vessel is not unsafe to operate — the hardware
    # killswitch and RC channel 8 both cut propulsion independently — it is
    # unverified, which is a different claim. Blocking launch on it would teach
    # the crew to ignore the verdict.
    assert report.go


def test_the_estop_feedback_passes_once_the_firmware_confirms_the_rail():
    report = run_checks(dict(HEALTHY, pico_estop_feedback_enabled=True))
    assert item(report, "pico.estop_feedback").status == PASS


def test_a_build_that_does_not_report_the_flag_is_unknown_not_fine():
    """Unknown is not pass, here as everywhere else."""
    state = dict(HEALTHY)
    del state["pico_estop_feedback_enabled"]
    result = item(run_checks(state), "pico.estop_feedback")
    assert result.status == SKIPPED
    assert result.remedy


def test_the_shipped_default_is_off_so_the_warning_is_the_normal_case():
    """If somebody flips the Python mirror without the firmware, this fails —
    and the cross-check test in asket_common fails alongside it."""
    from asket_common.mode_arbitration import ESTOP_FEEDBACK_ENABLED

    assert ESTOP_FEEDBACK_ENABLED is False
