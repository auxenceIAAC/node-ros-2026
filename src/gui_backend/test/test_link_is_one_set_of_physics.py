"""The vessel and the simulator must compute the link the same way.

The question this file answers is Auxence's, and it is worth writing down
because for a while the answer was no: ``link_budget`` was put in
``asket_common`` so that both paths could share it, and only the simulator
used it. ``RosSource`` built its link record from ``link_from_measurements``,
which carried a round-trip time, a quality guess, and ``distance_m = NaN`` —
no signal, no headroom, no geometry at all. One set of physics existed and the
real path was not using it, which is the same thing as having none.

So these tests check the wiring rather than the arithmetic. The arithmetic is
checked in ``asket_common/test/test_link_budget.py``; what matters here is
that the boat's GNSS fix goes into the same functions the simulated boat's
position does, and comes out the other side as the same fields.

The other half is honesty about provenance. Nothing on the vessel reads the
radio today, so the signal the real path reports is *computed from range*, not
measured — and the payload says so. A modelled number presented as a
measurement is exactly the failure this interface exists to prevent, and it
would be an easy one to ship, because a predicted RSSI looks entirely
plausible on a panel.
"""

import math
from types import SimpleNamespace

import pytest
from asket_common.link_budget import RadioConfig, ShoreStation
from gui_backend.core import adapters, payloads
from gui_backend.core.ros_source import _radio_from_config
from gui_backend.core.streams import DETAIL_FULL, DETAIL_REDUCED

STATION_LAT = -22.9576
STATION_LON = 14.5053


def station_config(**overrides) -> dict:
    config = {
        "configured": True,
        "lat": STATION_LAT,
        "lon": STATION_LON,
        "height_m": 2.9,
        "boresight_deg": 270.0,        # facing out to sea, westward
        "antenna": {
            "gain_dbi": 15.0,
            "azimuth_beamwidth_deg": 120.0,
            "elevation_beamwidth_deg": 10.0,
        },
        "vessel_radio": {"height_m": 1.0},
        "freq_mhz": 5500.0,
        "channel_width_mhz": 40.0,
        "wave_height_m": 0.5,
    }
    config.update(overrides)
    return {"shore_station": config}


def offshore(metres_west: float) -> tuple[float, float]:
    """A fix that far west of the station, in degrees."""
    metres_per_deg_lon = (
        math.pi * 6_371_008.8 / 180.0 * math.cos(math.radians(STATION_LAT))
    )
    return STATION_LAT, STATION_LON - metres_west / metres_per_deg_lon


# -- the wiring ------------------------------------------------------------


def test_the_config_builds_a_radio():
    radio, origin = _radio_from_config(station_config())
    assert isinstance(radio, RadioConfig)
    assert radio.station.antenna.azimuth_beamwidth_deg == 120.0
    assert radio.station.boresight_deg == 270.0
    assert radio.propagation.channel_width_mhz == 40.0
    assert (origin.lat_deg, origin.lon_deg) == (STATION_LAT, STATION_LON)


def test_an_unconfigured_station_builds_nothing():
    """Nothing, not a default. The tripod moves between missions and can be
    turned by hand mid-mission, so there is no surveyed position to fall back
    on — and a default one would draw a confident bearing to a station that is
    somewhere else."""
    assert _radio_from_config({}) == (None, None)
    assert _radio_from_config(station_config(configured=False)) == (None, None)


def test_the_vessel_gets_the_same_numbers_the_simulator_would():
    """The point of the whole exercise. Same module, same arithmetic; the only
    difference is that the position came from a GNSS fix rather than from a
    simulated vessel."""
    radio, origin = _radio_from_config(station_config())
    lat, lon = offshore(1500.0)
    east_m, north_m = origin.to_enu(lat, lon)

    record = adapters.link_from_measurements(
        "wifi", 0.9, 7.0, 1_000_000.0,
        radio=radio, east_m=east_m, north_m=north_m,
    )
    expected = radio.estimate_at(east_m, north_m)

    assert record.distance_m == pytest.approx(expected.geometry.range_m)
    assert record.rssi_dbm == pytest.approx(expected.rssi_dbm)
    assert record.headroom_db == pytest.approx(expected.headroom_db)
    assert record.expected_rssi_dbm == pytest.approx(expected.expected_rssi_dbm)
    assert record.off_boresight_deg == pytest.approx(expected.geometry.off_boresight_deg)
    assert record.mcs_index == expected.mcs.index


def test_a_boat_straight_out_from_the_sector_is_on_boresight():
    """Sanity on the frame conversion, which is the one place this could be
    wrong while every number still looked reasonable. The station faces west
    and the boat is due west of it."""
    radio, origin = _radio_from_config(station_config())
    east_m, north_m = origin.to_enu(*offshore(1500.0))
    record = adapters.link_from_measurements(
        "wifi", 0.9, 7.0, 1e6, radio=radio, east_m=east_m, north_m=north_m,
    )
    assert record.off_boresight_deg == pytest.approx(0.0, abs=0.5)
    assert record.distance_m == pytest.approx(1500.0, rel=0.01)


def test_the_measurements_are_not_overwritten_by_the_model():
    """Round-trip time and byte rate are measured — the backend times its own
    pings and counts what it sends. The budget supplies what nothing measures,
    and must not touch what something does."""
    radio, origin = _radio_from_config(station_config())
    east_m, north_m = origin.to_enu(*offshore(1500.0))
    record = adapters.link_from_measurements(
        "wifi", 0.42, 37.0, 12_345.0,
        radio=radio, east_m=east_m, north_m=north_m,
    )
    assert record.rtt_ms == 37.0
    assert record.quality == 0.42
    assert record.capacity_bytes_per_s == 12_345.0


# -- provenance ------------------------------------------------------------


def test_a_computed_signal_says_it_is_computed():
    """Nothing on this vessel reads the radio yet. A predicted RSSI looks
    entirely plausible on a panel, which is exactly why it has to carry a
    label — the day a RouterOS poller lands this becomes "measured" and
    nothing else changes."""
    radio, origin = _radio_from_config(station_config())
    east_m, north_m = origin.to_enu(*offshore(1500.0))
    record = adapters.link_from_measurements(
        "wifi", 0.9, 7.0, 1e6, radio=radio, east_m=east_m, north_m=north_m,
    )
    assert record.rssi_source == "predicted"


def test_the_simulator_says_its_signal_is_measured():
    """Because it is — of a simulated radio. The dev panel is never in any
    doubt about that, and the distinction that matters on the wire is between
    a number something read and a number something computed."""
    from asket_sim.core.link import LinkSim

    sample = LinkSim().sample(0, 0.0, 0.0)
    assert sample.rssi_source == "measured"


def test_provenance_reaches_the_wire():
    radio, origin = _radio_from_config(station_config())
    east_m, north_m = origin.to_enu(*offshore(1500.0))
    record = adapters.link_from_measurements(
        "wifi", 0.9, 7.0, 1e6, radio=radio, east_m=east_m, north_m=north_m,
    )
    payload = payloads.link_payload(record, "full", False, 1, 1000.0, DETAIL_FULL)
    assert payload["rssi_source"] == "predicted"
    assert payload["rssi_dbm"] is not None


def test_nothing_is_claimed_when_the_station_is_not_configured():
    """Every geometry field null, and NaN never reaching the wire. The panel
    then says the station has not been entered, which is true and actionable —
    whereas a zero bearing would be a confident lie about a tripod nobody has
    told us the position of."""
    record = adapters.link_from_measurements("wifi", 0.9, 7.0, 1e6)
    payload = payloads.link_payload(record, "full", False, 1, 1000.0, DETAIL_FULL)

    for field in ("rssi_dbm", "rssi_source", "headroom_db", "off_boresight_deg",
                  "expected_rssi_dbm", "mcs_index", "sector_beamwidth_deg",
                  "distance_m"):
        assert payload[field] is None, f"{field} was invented"


def test_the_reduced_profile_does_not_carry_any_of_it():
    """These fields are debugging detail for a link that is working. On a
    profile chosen because the link is *not* working, spending bytes on them
    would be the wrong trade — and the panel already renders their absence."""
    radio, origin = _radio_from_config(station_config())
    east_m, north_m = origin.to_enu(*offshore(1500.0))
    record = adapters.link_from_measurements(
        "wifi", 0.9, 7.0, 1e6, radio=radio, east_m=east_m, north_m=north_m,
    )
    payload = payloads.link_payload(record, "full", False, 1, 1000.0, DETAIL_REDUCED)
    assert "rssi_dbm" not in payload
    assert payload["quality"] == 0.9, "the essentials still go out"


# -- past the cliff --------------------------------------------------------


def test_a_boat_past_the_cliff_reports_no_signal_but_still_reports_where_it_is():
    """Geometry needs no radio, so it survives the link being gone — and that
    is when somebody most wants it. "How far past the cliff, and in which part
    of the sector" is the difference between driving back and turning the
    tripod."""
    radio, origin = _radio_from_config(station_config())
    east_m, north_m = origin.to_enu(*offshore(20_000.0))
    record = adapters.link_from_measurements(
        "none", 0.0, float("inf"), 0.0,
        radio=radio, east_m=east_m, north_m=north_m,
    )
    assert record.rssi_dbm is None
    assert record.rssi_source is None, "no modulation, so no signal to predict"
    assert record.mcs_index is None
    assert record.headroom_db < 0.0
    assert record.off_boresight_deg == pytest.approx(0.0, abs=0.5)
    assert record.distance_m == pytest.approx(20_000.0, rel=0.01)


def test_an_infinite_round_trip_becomes_null_rather_than_invalid_json():
    record = adapters.link_from_measurements("none", 0.0, float("inf"), 0.0)
    payload = payloads.link_payload(record, "minimal", False, 0, 0.0, DETAIL_FULL)
    assert payload["rtt_ms"] is None


# -- the guard that keeps this honest --------------------------------------


def test_the_real_path_would_notice_if_the_budget_stopped_being_shared():
    """A record built with a radio and one built without must differ, or this
    whole file is asserting that two code paths agree about nothing.

    This is the check that would have failed for the version of RosSource that
    prompted these tests — it computed no geometry at all, so wiring a radio
    in would have changed precisely nothing.
    """
    radio, origin = _radio_from_config(station_config())
    east_m, north_m = origin.to_enu(*offshore(1500.0))

    with_radio = adapters.link_from_measurements(
        "wifi", 0.9, 7.0, 1e6, radio=radio, east_m=east_m, north_m=north_m,
    )
    without = adapters.link_from_measurements("wifi", 0.9, 7.0, 1e6)

    assert with_radio.rssi_dbm is not None
    assert without.rssi_dbm is None
    assert math.isnan(without.distance_m)
    assert not math.isnan(with_radio.distance_m)


def test_a_fix_that_has_not_arrived_is_not_a_boat_on_top_of_the_tripod():
    """The origin of this frame is where the tripod stands, so a missing fix
    defaulting to (0, 0) would put the boat on the antenna and report a perfect
    link. Absence has to stay absence all the way down."""
    source = SimpleNamespace(
        _radio=RadioConfig(station=ShoreStation()),
        _vessel_record=lambda: None,
        _station_origin=None,
    )
    from gui_backend.core.ros_source import RosSource

    assert RosSource._vessel_enu(source) == {}
