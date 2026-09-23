"""The Jetson's own clock, checked against GPS.

The hop nothing was checking. The chain is::

    GPS time -> (NTP daemon) -> Jetson clock -> (NTP) -> sonar clock

``sonar.clock`` has always covered the second arrow. Without this check the
two clocks can agree with each other perfectly and both be wrong, which is the
worst available shape for the failure: the sonar log and the trajectory merge
without complaint and the whole survey sits in the wrong place in time. Nobody
finds out until somebody tries to tie it to anything external.

**It measures drift, not daemon lock.** A cold GPS start on a Namibian beach
can take ten minutes, and a daemon that has not converged is not a reason to
refuse a mission — a clock that is wrong is. If the clock is right, how it got
there is nobody's business. The wait, when there is one, is a wait for a
*fix*, and ``gnss.fix`` reports that in its own words.
"""

from system_test.core.checks import FAIL, PASS, SKIPPED, Thresholds, run_checks

T = Thresholds()

#: A vessel with a fix. Only the clock varies below.
FIXED = dict(num_sats=14, gnss_fix_type=3, hdop=0.8)


def clock(**state):
    results = {r.id: r for r in run_checks(dict(FIXED, **state), T).items}
    return results["clock.gps_offset"]


# -- the drift threshold ---------------------------------------------------


def test_a_disciplined_clock_passes():
    result = clock(gps_time_offset_ms=40, gps_time_age_s=1.0)
    assert result.status == PASS
    assert "40" in result.message


def test_a_clock_within_a_second_passes_however_it_got_there():
    """The whole point of the threshold. Nothing here asks whether the NTP
    daemon has achieved lock — if the clock is right, it is right."""
    assert clock(gps_time_offset_ms=999, gps_time_age_s=1.0).status == PASS
    assert clock(gps_time_offset_ms=-999, gps_time_age_s=1.0).status == PASS


def test_a_drifted_clock_is_a_hard_fail():
    result = clock(gps_time_offset_ms=4200, gps_time_age_s=1.0)
    assert result.status == FAIL
    assert result.measured_value == 4200


def test_the_failure_says_what_it_costs_rather_than_what_it_measured():
    """"4.2 s from GPS" means nothing to somebody about to launch. "Every
    point would be placed at the wrong moment" is what gets them to wait."""
    remedy = clock(gps_time_offset_ms=4200, gps_time_age_s=1.0).remedy
    assert "wrong moment" in remedy or "wrong" in remedy
    assert "NTP" in remedy


def test_drift_in_either_direction_fails():
    assert clock(gps_time_offset_ms=-4200, gps_time_age_s=1.0).status == FAIL


def test_the_threshold_is_a_whole_second():
    """Generous on purpose. Tight enough that no survey is ruined, loose
    enough that it never argues with a clock that is merely settling."""
    assert T.max_gps_offset_ms == 1000


# -- waiting for a fix is not failing --------------------------------------


def test_no_fix_is_not_this_checks_failure():
    """gnss.fix already carries it. Saying it twice in different words sends
    somebody looking for two problems."""
    result = clock(gnss_fix_type=1, num_sats=2)
    assert result.status == SKIPPED
    assert "fix" in result.message.lower()


def test_no_fix_says_the_clock_is_not_known_to_be_wrong():
    """A distinction worth the words. "Unverified" and "wrong" send somebody
    to completely different places, and on a ten-minute cold start the answer
    is that there is nothing to do but wait."""
    assert "not yet known to be right" in clock(gnss_fix_type=1, num_sats=2).remedy


def test_the_gnss_check_is_the_one_that_fails_on_no_fix():
    """Guard the division of labour: if this were wrong, a boat waiting for a
    cold start would show a clock fault and somebody would go looking at NTP."""
    results = {
        r.id: r for r in run_checks(dict(num_sats=2, gnss_fix_type=1), T).items
    }
    assert results["gnss.fix"].status == FAIL
    assert results["clock.gps_offset"].status == SKIPPED


def test_nothing_reporting_at_all_is_unknown_not_a_missing_reference():
    """Unknown is reported as unknown — this file's first principle. Claiming
    "no GPS time reference" when nothing at all is running names a specific
    fault that is not the one somebody has."""
    results = {r.id: r for r in run_checks({}, T).items}
    result = results["clock.gps_offset"]
    assert result.status == SKIPPED
    assert "position messages" in result.message


# -- an unverifiable clock is not a passing one ----------------------------


def test_a_fix_with_no_time_reference_fails():
    """There is a GPS that could tell us the time and something in the path
    carrying it is broken. Recording would be timed by a clock nobody has
    checked, which is the thing this check exists to prevent."""
    result = clock(gps_time_offset_ms=None)
    assert result.status == FAIL
    assert "unverified" in result.message
    assert "time_reference" in result.remedy


def test_a_stale_time_reference_fails():
    """A reference from five minutes ago cannot report on a drift that started
    four minutes ago."""
    result = clock(gps_time_offset_ms=10, gps_time_age_s=300.0)
    assert result.status == FAIL
    assert "300" in result.message


def test_a_recent_reference_is_fine():
    assert clock(gps_time_offset_ms=10, gps_time_age_s=5.0).status == PASS


def test_an_absent_age_does_not_fail_on_its_own():
    """Not every source can report how old its last reference is. Missing
    metadata about a good reading is not a reason to ground a vessel."""
    assert clock(gps_time_offset_ms=10).status == PASS


# -- and it is a hard fail --------------------------------------------------


def test_a_drifted_clock_grounds_the_vessel():
    report = run_checks(dict(FIXED, gps_time_offset_ms=4200, gps_time_age_s=1.0), T)
    assert not report.go


def test_it_is_a_different_check_from_the_sonar_clock():
    """Two hops, two checks. The sonar agreeing with the Jetson says nothing
    about whether the Jetson is right, and that combination — both clocks
    consistent, both wrong — is the case this was added for."""
    report = run_checks(
        dict(FIXED, clock_offset_ms=5, gps_time_offset_ms=9000, gps_time_age_s=1.0), T
    )
    results = {r.id: r for r in report.items}
    assert results["sonar.clock"].status == PASS
    assert results["clock.gps_offset"].status == FAIL
    assert not report.go
