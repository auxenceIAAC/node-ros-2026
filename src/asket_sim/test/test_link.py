"""The shore link, and the three failures it now has to be able to produce.

This file exists because of a specific near-miss. The camera panel's hardest
requirement is to notice when a feed stops and refuse to keep showing a picture
that is no longer true, and the thresholds for that are measured in seconds.
The old link model faded smoothly over tens of seconds and never dropped out at
all while a cellular fallback existed, so every one of those thresholds would
have been tuned against a failure mode this hardware does not have — and the
first time anybody saw the real one would have been in Namibia.

So: turning out of the sector, the modulation staircase and its cliff, and the
multipath nulls on calm water. Each of them is a thing an operator will see.
"""

import pytest
from asket_common.link_budget import (
    VESSEL_ANTENNA_DIRECTIONAL,
    Propagation,
    RadioConfig,
    ShoreStation,
    VesselRadio,
)
from asket_sim.core.link import (
    GLASSY_WAVE_HEIGHT_M,
    LINK_NONE,
    LINK_WIFI,
    LinkConfig,
    LinkSim,
)
from asket_sim.core.world import SimWorld, WorldConfig

#: Far enough out that the link has a finite amount of headroom to lose. Beside
#: the ramp it has about 50 dB and nothing can break it, which is correct and is
#: why the simulated station stands 900 m off the survey box.
SURVEY_RANGE_M = 900.0


def station_at_origin(**kwargs) -> LinkConfig:
    return LinkConfig(
        radio=RadioConfig(
            station=ShoreStation(east_m=0.0, north_m=0.0, boresight_deg=0.0),
            **kwargs,
        )
    )


def settled(config: LinkConfig | None = None) -> LinkSim:
    """A link with its fade at a representative value rather than exactly zero."""
    link = LinkSim(config or station_at_origin(), seed=3)
    for _ in range(200):
        link.step(0.05)
    return link


# -- the working case ------------------------------------------------------


def test_a_boat_on_the_survey_box_has_a_link_worth_having():
    sample = settled().sample(0, 0.0, SURVEY_RANGE_M)
    assert sample.active_link == LINK_WIFI
    assert sample.capacity_bytes_per_s * 8 / 1e6 > 20.0, "not the tens of megabits we bought"
    assert sample.headroom_db > 10.0
    assert sample.rtt_ms < 20.0, "a line-of-sight link is not a cellular one"


def test_the_sample_reports_the_geometry_even_when_it_cannot_report_a_signal():
    """Bearing from boresight is computable with no radio telemetry at all, and
    it is the number that turns "the link dropped" into "the link is going to
    drop". It must survive the link being gone — that is when it matters."""
    link = settled()
    dead = link.sample(0, 0.0, -4000.0)
    assert dead.active_link == LINK_NONE
    assert dead.rssi_dbm is None, "no bearer, so no signal to claim"
    assert dead.off_boresight_deg is not None
    assert dead.distance_m > 3000.0


# -- turning out of the sector --------------------------------------------


def test_the_sector_edge_is_a_warning_and_not_a_cliff():
    """Which is exactly why the panel shows the angle rather than an alarm: at
    the 3 dB edge the link still works, and the operator has time to turn the
    tripod. An alarm at the edge would cry wolf; nothing at the edge would give
    no warning at all."""
    link = settled()
    boresight = link.sample(0, 0.0, SURVEY_RANGE_M)
    edge = link.sample(0, SURVEY_RANGE_M * 0.866, SURVEY_RANGE_M * 0.5)  # 60 deg

    assert edge.off_boresight_deg == pytest.approx(60.0, abs=1.0)
    assert edge.active_link == LINK_WIFI
    assert edge.headroom_db < boresight.headroom_db - 2.0
    assert edge.headroom_db > 5.0


def test_far_enough_round_and_the_link_is_gone():
    link = settled()
    assert link.sample(0, 0.0, -SURVEY_RANGE_M).active_link == LINK_NONE


# -- the alignment fault ---------------------------------------------------


def alignment_timeline(world: SimWorld, east: float, north: float,
                       seconds: float = 6.0, dt: float = 0.1) -> list:
    samples = []
    for _ in range(int(seconds / dt)):
        world.step(dt)
        world.vessel.east, world.vessel.north = east, north
        samples.append(world.snapshot().link)
    return samples


def world_with_station_at_origin() -> SimWorld:
    return SimWorld(WorldConfig(link=station_at_origin()))


def test_knocking_the_tripod_takes_the_link_out_in_seconds():
    """The failure this whole rewrite is for. Not a fade — a collapse, on the
    timescale the camera panel's staleness thresholds are measured in."""
    world = world_with_station_at_origin()
    before = alignment_timeline(world, 0.0, SURVEY_RANGE_M, seconds=2.0)
    assert before[-1].active_link == LINK_WIFI

    world.inject_fault("link_alignment_lost")
    after = alignment_timeline(world, 0.0, SURVEY_RANGE_M, seconds=6.0)

    assert after[-1].active_link == LINK_NONE
    lost_at = next(i for i, s in enumerate(after) if s.active_link == LINK_NONE) * 0.1
    assert lost_at < 3.0, f"took {lost_at:.1f} s — that is a fade, not a collapse"


def test_but_it_collapses_rather_than_switching_off():
    """There has to be a middle. A step straight to nothing would let the GUI
    pass a test it should fail: the interesting second is the one where the
    link is dying and some frames are still arriving."""
    world = world_with_station_at_origin()
    alignment_timeline(world, 0.0, SURVEY_RANGE_M, seconds=2.0)
    world.inject_fault("link_alignment_lost")
    after = alignment_timeline(world, 0.0, SURVEY_RANGE_M, seconds=6.0)

    degrading = [
        s for s in after
        if s.active_link == LINK_WIFI and s.headroom_db < 10.0
    ]
    assert len(degrading) >= 3, "the link went from healthy to dead with nothing in between"


def test_the_link_comes_back_when_the_tripod_is_straightened():
    world = world_with_station_at_origin()
    world.inject_fault("link_alignment_lost")
    assert alignment_timeline(world, 0.0, SURVEY_RANGE_M)[-1].active_link == LINK_NONE

    world.clear_fault("link_alignment_lost")
    recovered = alignment_timeline(world, 0.0, SURVEY_RANGE_M)
    assert recovered[-1].active_link == LINK_WIFI


def test_beside_the_ramp_the_same_fault_does_nothing():
    """Correct, and documented rather than tuned away: at fifty metres the
    antenna's backlobe still carries several megabits. Somebody testing this
    button on the beach will see no effect, and the dev panel says so.

    It is also the reason ``link_loss`` still exists as a separate fault — that
    one is "nothing arrives", which is a different thing from "the geometry
    stopped working"."""
    world = world_with_station_at_origin()
    world.inject_fault("link_alignment_lost")
    close = alignment_timeline(world, 0.0, 50.0)
    assert close[-1].active_link == LINK_WIFI


# -- the staircase and the cliff ------------------------------------------


def test_throughput_steps_rather_than_sliding():
    """Rate adaptation picks a modulation, holds it, and then jumps. A boat
    driving steadily outward sees bandwidth in plateaus — which is worth the
    panel not smoothing away, because a step is a warning."""
    link = LinkSim(station_at_origin(), seed=3)   # unfaded, so the steps are clean
    capacities = [
        link.sample(0, 0.0, float(d)).capacity_bytes_per_s
        for d in range(200, 2600, 25)
    ]
    distinct = sorted(set(capacities))
    assert 3 < len(distinct) < len(capacities) / 3, (
        f"{len(distinct)} distinct rates over {len(capacities)} samples is a slide, "
        "not a staircase"
    )


def test_and_then_there_is_no_rung_below_the_bottom_one():
    """The cliff. With one directional link and no cellular fallback, the last
    modulation is followed by nothing at all."""
    link = LinkSim(station_at_origin(), seed=3)
    assert link.cfg.cellular_available is False
    alive = link.sample(0, 0.0, 2000.0)
    dead = link.sample(0, 0.0, 3500.0)
    assert alive.active_link == LINK_WIFI
    assert dead.active_link == LINK_NONE
    assert dead.capacity_bytes_per_s == 0.0


def test_a_cellular_fallback_still_works_where_a_site_has_one():
    """Q6 is open. The default assumes there is nothing under this link, which
    is the harder case, but the machinery must not have rotted in the meantime."""
    config = station_at_origin()
    config.cellular_available = True
    link = LinkSim(config, seed=3)
    assert link.sample(0, 0.0, 3500.0).active_link != LINK_NONE


# -- multipath -------------------------------------------------------------


def null_depth(wave_height_m: float) -> float:
    """How far the signal drops at the first null, relative to just outside it."""
    config = station_at_origin(propagation=Propagation(wave_height_m=wave_height_m))
    link = LinkSim(config, seed=3)
    at_null = link.sample(0, 0.0, 106.4).rssi_dbm
    outside = link.sample(0, 0.0, 133.0).rssi_dbm
    return outside - at_null


def test_calm_water_puts_a_hole_in_the_link_at_a_fixed_range():
    """A glassy morning inshore. The boat drives a straight line at constant
    speed and the link drops out and comes back — which looks exactly like a
    fault and is not one, so the panel had better be able to show why."""
    assert null_depth(GLASSY_WAVE_HEIGHT_M) > 20.0


def test_and_any_swell_at_all_fills_it_in():
    assert null_depth(0.5) < 3.0


def test_the_glassy_water_fault_reaches_the_link():
    world = world_with_station_at_origin()
    world.vessel.east, world.vessel.north = 0.0, 106.4
    world.step(0.1)
    rough = world.snapshot().link.rssi_dbm

    world.inject_fault("link_glassy_water")
    world.step(0.1)
    world.vessel.east, world.vessel.north = 0.0, 106.4
    glassy = world.snapshot().link
    assert glassy.sea_state_m == GLASSY_WAVE_HEIGHT_M
    assert glassy.rssi_dbm < rough - 20.0


# -- the invariants the old model had, which must survive the rewrite ------


def test_sampling_the_link_still_does_not_change_it():
    """The backend samples several times per tick — once for the stream, once
    for alarms, once for profile selection. A fade advanced inside sample()
    once ran sixty times a second and made the profile selector unusable."""
    link = settled()
    first = link.sample(0, 10.0, 900.0)
    for _ in range(20):
        assert link.sample(0, 10.0, 900.0) == first


def test_the_antenna_slews_rather_than_teleporting():
    """So that the collapse has a shape. Also so that a fault injected and
    cleared repeatedly cannot leave the tripod somewhere nobody asked for."""
    link = LinkSim(station_at_origin(), seed=3)
    link.set_alignment_error(150.0)
    link.step(0.5)
    midway = link.alignment_error_deg
    assert 0.0 < midway < 150.0

    for _ in range(40):
        link.step(0.1)
    assert link.alignment_error_deg == pytest.approx(150.0)

    link.set_alignment_error(0.0)
    for _ in range(40):
        link.step(0.1)
    assert link.alignment_error_deg == pytest.approx(0.0)


def test_fading_is_still_the_same_whatever_the_step_size():
    """Parameterised as a standard deviation and a time constant rather than a
    per-step amplitude, so changing the tick rate does not silently change the
    weather. Now in decibels, so that a fade can push the link across a
    modulation boundary the way a real one does."""
    import statistics

    def spread(dt, steps):
        link = LinkSim(station_at_origin(), seed=11)
        values = []
        for _ in range(steps):
            link.step(dt)
            values.append(link.sample(0, 0.0, 900.0).rssi_dbm)
        return statistics.pstdev(values)

    fine = spread(0.01, 20000)     # 200 s at 100 Hz
    coarse = spread(0.5, 400)      # 200 s at 2 Hz
    assert abs(fine - coarse) < 0.5


# -- the boat's own antenna, which nobody has confirmed yet ----------------


def test_an_omni_boat_antenna_ignores_heading_entirely():
    """The default, until the part is confirmed. Heading must not contribute a
    number we have no grounds for."""
    link = settled()
    headings = {
        round(link.sample(0, 0.0, SURVEY_RANGE_M, heading_deg=h).rssi_dbm, 6)
        for h in (0.0, 90.0, 180.0, 270.0)
    }
    assert len(headings) == 1


def test_a_forward_facing_directional_antenna_would_break_every_outbound_line():
    """Worth a test rather than a note, because it is a finding about the boat
    and not about this code.

    If the HGO turns out to be directional and it is bolted on facing forward,
    it points away from the shore station for the whole of every outbound
    survey line — the backlobe's 25 dB, which takes a comfortable link to a
    dead one. The mounting is then a design decision, not a detail. This test
    is what makes that visible if somebody switches the default over.
    """
    config = station_at_origin(
        vessel=VesselRadio(antenna=VESSEL_ANTENNA_DIRECTIONAL)
    )
    link = LinkSim(config, seed=3)
    outbound = link.sample(0, 0.0, SURVEY_RANGE_M, heading_deg=0.0)
    homeward = link.sample(0, 0.0, SURVEY_RANGE_M, heading_deg=180.0)

    assert homeward.active_link == LINK_WIFI
    assert outbound.active_link == LINK_NONE
