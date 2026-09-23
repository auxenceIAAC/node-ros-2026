"""The link budget, checked against arithmetic rather than against itself.

Every assertion here is either a closed-form value computed by hand, or a
qualitative property the simulator depends on. None of it validates the model
against the real hardware — nothing can, until somebody stands on a beach with
the tripod up — so the point is narrower: the model must be internally right,
and it must keep producing the three behaviours the freeze contract is going to
be tuned against.
"""

import math

import pytest
from asket_common.link_budget import (
    DEFAULT_CHANNEL_WIDTH_MHZ,
    MCS_TABLE,
    SHORE_ANTENNA,
    VESSEL_ANTENNA_DIRECTIONAL,
    VESSEL_ANTENNA_OMNI,
    Antenna,
    Propagation,
    RadioConfig,
    ShoreStation,
    VesselRadio,
    fspl_db,
    geometry,
    headroom_db,
    noise_floor_dbm,
    quality_from_headroom,
    select_mcs,
    specular_coefficient,
    two_ray_db,
    wavelength_m,
)

STATION = ShoreStation(east_m=0.0, north_m=0.0, boresight_deg=0.0)


# -- path loss -------------------------------------------------------------


def test_free_space_loss_matches_the_closed_form():
    """FSPL at 1 km and 5.5 GHz is 107.25 dB. If this drifts, every number
    downstream is wrong by the same amount and nothing else would notice."""
    assert fspl_db(1000.0, 5500.0) == pytest.approx(107.25, abs=0.05)


def test_doubling_the_range_costs_six_decibels():
    near = fspl_db(500.0, 5500.0)
    far = fspl_db(1000.0, 5500.0)
    assert far - near == pytest.approx(6.02, abs=0.02)


def test_wavelength_is_about_five_centimetres_in_this_band():
    assert wavelength_m(5500.0) == pytest.approx(0.0545, abs=0.0005)


# -- antenna patterns ------------------------------------------------------


def test_the_sector_edge_is_the_half_power_point():
    """The quoted beamwidth means 3 dB down at its edge. That is the definition,
    and the panel's "approaching the edge of the sector" warning is only
    meaningful if the model honours it."""
    off = SHORE_ANTENNA.azimuth_beamwidth_deg / 2.0
    assert SHORE_ANTENNA.gain_at(off) == pytest.approx(SHORE_ANTENNA.gain_dbi - 3.0, abs=0.01)


def test_boresight_is_full_gain():
    assert SHORE_ANTENNA.gain_at(0.0, 0.0) == SHORE_ANTENNA.gain_dbi


def test_the_pattern_is_symmetric():
    assert SHORE_ANTENNA.gain_at(37.0) == SHORE_ANTENNA.gain_at(-37.0)


def test_loss_is_floored_at_the_front_to_back_ratio():
    """Not unbounded. A boat behind the antenna is 25 dB down, not 200, and an
    unfloored parabola would make "behind the tripod" indistinguishable from
    "on the moon"."""
    behind = SHORE_ANTENNA.gain_at(180.0, 40.0)
    assert behind == pytest.approx(SHORE_ANTENNA.gain_dbi - SHORE_ANTENNA.front_to_back_db)


def test_an_omni_has_no_direction():
    """The case the whole heading term has to survive. If the boat's antenna
    turns out to be an omni, every heading-dependent number must vanish by
    arithmetic rather than by a branch somebody has to remember to write."""
    assert VESSEL_ANTENNA_OMNI.omnidirectional
    for angle in (0.0, 45.0, 90.0, 180.0, -170.0):
        assert VESSEL_ANTENNA_OMNI.gain_at(angle) == VESSEL_ANTENNA_OMNI.gain_dbi


def test_a_directional_antenna_does_have_one():
    """Guard the guard: if the test above passed for a directional antenna too,
    it would be asserting nothing."""
    assert not VESSEL_ANTENNA_DIRECTIONAL.omnidirectional
    assert VESSEL_ANTENNA_DIRECTIONAL.gain_at(180.0) < VESSEL_ANTENNA_DIRECTIONAL.gain_dbi - 20


# -- geometry --------------------------------------------------------------


def test_bearing_and_off_boresight_agree_with_the_compass():
    """Station facing north, boat to the east: 90 degrees off boresight."""
    geom = geometry(STATION, east_m=500.0, north_m=0.0)
    assert geom.bearing_deg == pytest.approx(90.0)
    assert geom.off_boresight_deg == pytest.approx(90.0)


def test_off_boresight_is_signed_and_wrapped():
    """A boat to port of boresight must read negative, not 350. The panel draws
    this on a dial; an unwrapped value puts it on the wrong side."""
    west = geometry(STATION, east_m=-500.0, north_m=0.0)
    assert west.off_boresight_deg == pytest.approx(-90.0)

    turned = geometry(
        ShoreStation(east_m=0.0, north_m=0.0, boresight_deg=350.0),
        east_m=0.0, north_m=500.0,
    )
    assert turned.off_boresight_deg == pytest.approx(10.0)


def test_turning_the_tripod_moves_the_boat_through_the_sector():
    """Which is the whole mechanism behind the alignment fault, and behind the
    panel's promise to show the boat working toward the edge."""
    offsets = [
        geometry(ShoreStation(east_m=0.0, north_m=0.0, boresight_deg=b),
                 east_m=0.0, north_m=800.0).off_boresight_deg
        for b in (0.0, 20.0, 40.0, 60.0)
    ]
    assert offsets == pytest.approx([0.0, -20.0, -40.0, -60.0])


def test_range_is_slant_not_ground():
    """Small at survey ranges and not small beside the ramp, where the height
    difference is most of the distance."""
    close = geometry(ShoreStation(east_m=0.0, north_m=0.0, height_m=2.9),
                     east_m=0.0, north_m=4.0, vessel=VesselRadio(height_m=1.0))
    assert close.range_m == pytest.approx(math.hypot(4.0, 1.9))


def test_the_boat_is_below_the_tripod_so_depression_is_positive():
    geom = geometry(STATION, east_m=0.0, north_m=100.0)
    assert geom.depression_deg > 0.0
    assert geom.depression_deg < 2.0


def test_an_omni_boat_antenna_reports_no_off_boresight_angle():
    """None, not zero. Zero would render on the panel as "perfectly aligned",
    which is a claim about a thing that has no alignment."""
    geom = geometry(STATION, 0.0, 800.0, vessel_heading_deg=37.0,
                    vessel=VesselRadio(antenna=VESSEL_ANTENNA_OMNI))
    assert geom.vessel_off_boresight_deg is None


def test_a_directional_boat_antenna_tracks_the_boat_heading():
    """Boat 800 m north of the station, so the station is astern. Pointed north
    the antenna faces away; turned about, it faces the station."""
    directional = VesselRadio(antenna=VESSEL_ANTENNA_DIRECTIONAL)
    away = geometry(STATION, 0.0, 800.0, vessel_heading_deg=0.0, vessel=directional)
    toward = geometry(STATION, 0.0, 800.0, vessel_heading_deg=180.0, vessel=directional)
    assert abs(away.vessel_off_boresight_deg) == pytest.approx(180.0)
    assert toward.vessel_off_boresight_deg == pytest.approx(0.0)


# -- two-ray propagation ---------------------------------------------------


def test_a_rough_sea_scatters_the_reflection_away():
    """At a hundred metres the grazing angle is wide enough that half a metre of
    swell destroys the specular ray, and the link behaves like free space."""
    rho = specular_coefficient(100.0, 2.9, 1.0, wave_height_m=0.5)
    assert rho < 0.01
    assert two_ray_db(100.0, 2.9, 1.0, wave_height_m=0.5) == pytest.approx(0.0, abs=0.1)


def test_a_glassy_sea_keeps_it():
    rho = specular_coefficient(100.0, 2.9, 1.0, wave_height_m=0.02)
    assert rho > 0.9


def test_the_first_null_falls_where_the_path_difference_is_one_wavelength():
    """d = 2 h1 h2 / lambda — about 106 m with the tripod at 2.9 m and the boat
    antenna at 1 m. Deep, narrow, and only on calm water. It is the reason the
    simulator can now produce a link that drops out and comes back while the
    boat is driving in a straight line."""
    expected = 2.0 * 2.9 * 1.0 / wavelength_m()
    assert expected == pytest.approx(106.4, abs=1.0)

    at_null = two_ray_db(expected, 2.9, 1.0, wave_height_m=0.02)
    beside_null = two_ray_db(expected * 1.25, 2.9, 1.0, wave_height_m=0.02)
    assert at_null < -20.0, "the null is not deep enough to be worth modelling"
    assert beside_null > at_null + 15.0, "the null is not narrow"


def test_the_null_moves_when_the_antennas_move():
    """Halving the boat antenna height halves the null range. This is the knob
    that matters, so it must be the knob the model actually responds to."""
    low = 2.0 * 2.9 * 0.5 / wavelength_m()
    assert two_ray_db(low, 2.9, 0.5, wave_height_m=0.02) < -20.0


def test_beyond_the_last_null_the_rays_cancel_and_range_costs_double():
    """The far cliff. Past the last null, received power falls with the fourth
    power of range rather than the square, and *that* meeting the receiver
    floor is where this link ends — not some tuned range constant."""
    near = two_ray_db(1000.0, 2.9, 1.0, wave_height_m=0.5)
    far = two_ray_db(2000.0, 2.9, 1.0, wave_height_m=0.5)
    assert far < near
    # Doubling the range costs 6 dB of free space plus about 6 dB more here.
    assert near - far == pytest.approx(6.0, abs=1.5)


def test_raising_the_boat_antenna_buys_range():
    """The single most valuable change available to this link, and worth saying
    out loud: it beats transmit power, which is regulated, and it beats the
    tripod, which is already as tall as it gets."""
    low = RadioConfig(station=STATION, vessel=VesselRadio(height_m=1.0))
    high = RadioConfig(station=STATION, vessel=VesselRadio(height_m=2.5))
    gain = (high.estimate_at(0.0, 4000.0).path_gain_db
            - low.estimate_at(0.0, 4000.0).path_gain_db)
    assert gain > 5.0, f"raising the antenna bought only {gain:.1f} dB"


def test_a_perfect_null_does_not_produce_minus_infinity():
    """Floored, because one NaN here would poison every number the panel shows
    and the panel would have no way to tell it had happened."""
    value = two_ray_db(2.0 * 2.9 * 1.0 / wavelength_m(), 2.9, 1.0, wave_height_m=0.0)
    assert math.isfinite(value)
    assert value < -50.0


# -- the modulation ladder -------------------------------------------------


def test_the_table_is_a_staircase_in_both_directions():
    """Faster modulations need more signal and transmit at less power. A row
    out of order would make rate selection non-monotonic, and the staircase
    would have a step down in it."""
    for lower, higher in zip(MCS_TABLE, MCS_TABLE[1:]):
        assert higher.phy_mbps_20mhz > lower.phy_mbps_20mhz
        assert higher.sensitivity_20mhz_dbm > lower.sensitivity_20mhz_dbm
        assert higher.tx_power_dbm <= lower.tx_power_dbm


def test_the_table_still_matches_the_four_rungs_the_datasheet_states():
    """The mANTBox ax 15s publishes MCS0, 7, 9 and 11 at 5 GHz; the rest are
    interpolated. If somebody retunes the interpolation, these four must not
    move — they are the only numbers here that are not a judgement."""
    published = {0: (-96.0, 28.0), 7: (-75.0, 25.0), 9: (-70.0, 23.0), 11: (-67.0, 22.0)}
    for mcs in MCS_TABLE:
        if mcs.index in published:
            assert (mcs.sensitivity_20mhz_dbm, mcs.tx_power_dbm) == published[mcs.index]


def test_a_wider_channel_costs_sensitivity_and_buys_rate():
    """Three decibels per doubling, both ways. This is a real deployment
    choice — range against throughput — and getting the sign wrong would make
    the model recommend the opposite of the right one."""
    mcs = MCS_TABLE[0]
    assert mcs.sensitivity_dbm(20.0) == pytest.approx(mcs.sensitivity_20mhz_dbm)
    assert mcs.sensitivity_dbm(40.0) == pytest.approx(mcs.sensitivity_20mhz_dbm + 3.01, abs=0.02)
    assert mcs.sensitivity_dbm(80.0) == pytest.approx(mcs.sensitivity_20mhz_dbm + 6.02, abs=0.02)
    assert mcs.phy_mbps(40.0) == pytest.approx(2 * mcs.phy_mbps_20mhz)


def test_stepping_down_the_ladder_buys_transmit_power():
    """Six decibels between the top rung and the bottom, on this radio. A model
    with one flat transmit power misses a large part of how rate adaptation
    extends range — and the first draft of this table did exactly that."""
    assert MCS_TABLE[0].tx_power_dbm - MCS_TABLE[-1].tx_power_dbm == pytest.approx(6.0)


def test_rate_selection_picks_the_fastest_that_fits():
    """Argument is path gain, not signal: each rung transmits at its own power,
    so there is no single signal until a rung has been chosen."""
    width = DEFAULT_CHANNEL_WIDTH_MHZ
    for mcs in MCS_TABLE:
        just_enough = mcs.sensitivity_dbm(width) - mcs.tx_power_dbm
        assert select_mcs(just_enough, width).index >= mcs.index


def test_below_the_floor_there_is_no_rung():
    """The cliff. Not a slow rate — nothing. This is the property the whole
    rewrite exists for, and the one the old smooth model could not express."""
    floor_gain = noise_floor_dbm() - MCS_TABLE[0].tx_power_dbm
    assert select_mcs(floor_gain) is not None
    assert select_mcs(floor_gain - 0.1) is None
    assert select_mcs(-250.0) is None


def test_throughput_steps_rather_than_slides():
    """Between two adjacent rungs, throughput does not move at all; at the rung
    it jumps. An operator watching bandwidth on this link sees steps."""
    width = DEFAULT_CHANNEL_WIDTH_MHZ
    rungs = [select_mcs(g, width) for g in (-120.0, -119.5, -110.0)]
    rates = [m.usable_bytes_per_s(width) if m else 0.0 for m in rungs]
    assert rates[0] == rates[1]
    assert rates[2] > rates[1]


def test_headroom_is_measured_against_the_cliff():
    """Not against the current modulation's requirement. Rate steps are
    invisible to an operator; losing the link is not."""
    floor_gain = noise_floor_dbm() - MCS_TABLE[0].tx_power_dbm
    assert headroom_db(floor_gain) == pytest.approx(0.0)
    assert headroom_db(floor_gain + 10.0) == pytest.approx(10.0)


def test_the_floor_moves_with_the_channel_width():
    """A constant that silently meant "at 20 MHz" is the shape of the bug this
    table just had."""
    assert noise_floor_dbm(40.0) == pytest.approx(noise_floor_dbm(20.0) + 3.01, abs=0.02)


def test_quality_still_means_what_the_profile_selector_thinks_it_means():
    """The 0..1 the rest of the system already speaks, now with decibels
    behind it. Monotonic, clamped, and zero exactly at the cliff."""
    assert quality_from_headroom(0.0) == 0.0
    assert quality_from_headroom(-20.0) == 0.0
    assert quality_from_headroom(60.0) == 1.0
    assert 0.0 < quality_from_headroom(10.0) < 1.0


# -- the whole budget ------------------------------------------------------


def test_the_link_carries_megabits_at_survey_range():
    """The claim the hardware was bought on: kilometres at tens of megabits.
    If this fails the model is wrong, or the gear is."""
    estimate = RadioConfig(station=STATION).estimate_at(0.0, 1000.0)
    assert estimate.connected
    assert estimate.usable_bytes_per_s * 8 / 1e6 > 20.0


def test_and_stops_carrying_anything_a_little_further_out():
    """Between two and three kilometres, with these antenna heights. The number
    is not the point — the shape is: there is a range at which this link is
    fine and a range a few hundred metres later at which it is gone."""
    radio = RadioConfig(station=STATION)
    assert radio.estimate_at(0.0, 4000.0).connected
    assert not radio.estimate_at(0.0, 9000.0).connected


def test_the_deviation_names_the_difference_between_far_away_and_misaligned():
    """The diagnostic the link panel is built on. Two boats at the same range,
    one in the beam and one out of it: the one in the beam reads close to what
    its range predicts, the other does not."""
    radio = RadioConfig(station=STATION)
    aligned = radio.estimate_at(0.0, 1200.0)
    off_axis = radio.estimate_at(1200.0, 0.0)

    assert aligned.geometry.range_m == pytest.approx(off_axis.geometry.range_m, rel=0.05)
    assert abs(aligned.deviation_db) < 6.0
    assert off_axis.deviation_db < aligned.deviation_db - 3.0


def test_expected_signal_ignores_alignment_and_weather_on_purpose():
    """It is the reference, not a second estimate. If it moved with the sector's
    bearing it could never reveal that the sector is pointed wrong."""
    straight = RadioConfig(station=STATION).estimate_at(0.0, 1200.0)
    turned = RadioConfig(
        station=ShoreStation(east_m=0.0, north_m=0.0, boresight_deg=95.0)
    ).estimate_at(0.0, 1200.0)
    assert straight.expected_rssi_dbm == pytest.approx(turned.expected_rssi_dbm)
    assert turned.path_gain_db < straight.path_gain_db


def test_a_boat_behind_the_tripod_loses_the_link_at_range_but_not_close_in():
    """Both halves matter. The first is why the alignment fault works; the
    second is why it appears to do nothing at the ramp, which is correct
    behaviour and has to be documented rather than tuned away."""
    radio = RadioConfig(station=STATION)
    assert not radio.estimate_at(0.0, -6000.0).connected
    assert radio.estimate_at(0.0, -120.0).connected


def test_everything_in_an_estimate_is_finite():
    """Cheap, and it catches the log-of-zero class of bug at every range the
    simulator can put a boat, including on top of the tripod."""
    radio = RadioConfig(station=STATION, propagation=Propagation(wave_height_m=0.0))
    # Including 0.0: the boat directly under the tripod, where ground range is
    # zero and a naive log or atan2 would give up.
    for north in (0.0, 0.5, 1.0, 106.4, 500.0, 5000.0):
        estimate = radio.estimate_at(0.0, north)
        for value in (estimate.path_gain_db, estimate.expected_rssi_dbm,
                      estimate.headroom_db, estimate.quality,
                      estimate.multipath_db, estimate.pattern_loss_db):
            assert math.isfinite(value), f"{value} at {north} m"


def test_the_budget_is_pure():
    """No state, no randomness. The simulator owns the weather; this module
    must give the same answer every time or the panel and the sim will
    disagree about a boat that has not moved."""
    radio = RadioConfig(station=STATION)
    first = radio.estimate_at(300.0, 700.0, 42.0)
    for _ in range(5):
        assert radio.estimate_at(300.0, 700.0, 42.0) == first


def test_a_custom_antenna_still_obeys_the_half_power_rule():
    """The pattern is a formula, not a table, so it has to hold for whatever
    beamwidth the setup page eventually collects."""
    for beamwidth in (30.0, 60.0, 90.0, 120.0):
        antenna = Antenna(gain_dbi=12.0, azimuth_beamwidth_deg=beamwidth)
        assert antenna.gain_at(beamwidth / 2.0) == pytest.approx(9.0, abs=0.01)
