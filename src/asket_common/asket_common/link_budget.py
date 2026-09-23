"""Shore link budget: geometry, antenna patterns, propagation, rate selection.

Why this exists, and why it is here rather than in the simulator.

The shore link used to be modelled as quality falling off smoothly with
distance, with a cellular fallback underneath. That is a picture of 4G decaying
as you drive away from a mast, and it is the wrong picture for the hardware
this boat now carries: a sector antenna on a tripod ashore and a directional
antenna on the boat, line of sight over water. Such a link does not decay. It
works, and then it stops — because the boat turned out of the sector, or
because the two-ray path loss finally crossed the receiver's floor. A simulator
that can only produce gradual decay cannot be used to tune a GUI whose hardest
requirement is to notice a *sudden* stop.

So the sim needs real geometry. And once the geometry is real, the GUI wants
the same numbers for the panel — bearing from the sector's boresight, headroom
in dB, what this range *should* be giving — computed the same way whether they
came from a simulated boat or a real one. Hence a shared, ROS-free module with
no randomness in it: every function here is pure, and the fading, the faults
and the bearer selection stay in ``asket_sim``.

Conventions follow :mod:`asket_common.geo`: local ENU metres, compass degrees
(0 = true north, increasing clockwise).

**Every default in this file is PROVISIONAL.** The numbers are the published or
conventional figures for this class of equipment, not measurements of the
equipment we are buying, and several of them matter a great deal — see
``docs/open_questions.md``. The ones worth checking first are the sector's
azimuth beamwidth, whether the boat's antenna is directional at all, and the
boat antenna's *height*, which turns out to dominate the useful range (see
:func:`two_ray_db`).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .geo import enu_bearing_deg, wrap180

#: Speed of light, m/s.
C_M_S = 299_792_458.0

#: Centre frequency. 5 GHz band, mid-channel. PROVISIONAL — the channel plan
#: for Namibia is not chosen, and the two-ray null spacing scales with it.
DEFAULT_FREQ_MHZ = 5500.0

#: Significant wave height, metres. PROVISIONAL. This is the one term that
#: decides whether the close-in multipath nulls exist at all: the Atlantic off
#: Walvis Bay is rarely glassy, but a calm morning inshore is, and that is
#: exactly when somebody is testing at the ramp.
DEFAULT_WAVE_HEIGHT_M = 0.5


def wavelength_m(freq_mhz: float = DEFAULT_FREQ_MHZ) -> float:
    return C_M_S / (freq_mhz * 1e6)


# -- antennas --------------------------------------------------------------


@dataclass(frozen=True)
class Antenna:
    """A gain pattern, approximated the way sector antennas usually are.

    The main lobe is the standard parabolic-in-dB approximation,
    ``-12 (theta / theta_3dB)^2``, floored at the front-to-back ratio. It is
    exact at boresight and at the half-power points by construction and
    plausible in between, which is the right amount of fidelity here: we are
    modelling *that* the link dies when the boat leaves the sector, not
    predicting a sidelobe null to a decibel.

    ``azimuth_beamwidth_deg`` of 360 means omnidirectional, which is the case
    this whole model has to survive: if the boat's antenna turns out to be an
    omni, the vessel-heading term must vanish rather than quietly contribute a
    wrong number.
    """

    gain_dbi: float
    azimuth_beamwidth_deg: float
    elevation_beamwidth_deg: float = 30.0
    front_to_back_db: float = 25.0

    @property
    def omnidirectional(self) -> bool:
        return self.azimuth_beamwidth_deg >= 360.0

    def _lobe_loss_db(self, off_axis_deg: float, beamwidth_deg: float) -> float:
        if beamwidth_deg <= 0.0 or beamwidth_deg >= 360.0:
            return 0.0
        # -12 (theta / theta_3dB)^2, written in half-beamwidths so that the
        # 3 dB point falls exactly at the edge of the quoted sector.
        ratio = abs(off_axis_deg) / (beamwidth_deg / 2.0)
        return min(3.0 * ratio * ratio, self.front_to_back_db)

    def gain_at(self, off_azimuth_deg: float, off_elevation_deg: float = 0.0) -> float:
        """Gain in dBi at an off-boresight angle.

        Azimuth and elevation losses add, floored once at the front-to-back
        ratio so a boat directly behind the antenna does not accumulate two
        penalties and read as impossibly dead.
        """
        loss = self._lobe_loss_db(off_azimuth_deg, self.azimuth_beamwidth_deg)
        loss += self._lobe_loss_db(off_elevation_deg, self.elevation_beamwidth_deg)
        return self.gain_dbi - min(loss, self.front_to_back_db)


#: MikroTik mANTBox 15s, ashore. PROVISIONAL: 120 deg is this product's
#: published azimuth sector. Auxence has described the plan as a "~60 deg
#: sector", and the difference is not cosmetic — it halves the area the boat
#: can work before somebody has to turn the tripod, which is the number the
#: link panel exists to make visible. Check the part before trusting either.
SHORE_ANTENNA = Antenna(
    gain_dbi=15.0, azimuth_beamwidth_deg=120.0, elevation_beamwidth_deg=10.0
)

#: The boat end, if the HGO-Antenna-OUT turns out to be directional. Modelled,
#: tested, and **not the default** — see below.
VESSEL_ANTENNA_DIRECTIONAL = Antenna(
    gain_dbi=6.7, azimuth_beamwidth_deg=60.0, elevation_beamwidth_deg=60.0
)

#: The boat end as assumed until somebody confirms otherwise. PROVISIONAL.
#:
#: Defaulting to an omni is not laziness, it is the result of running the
#: directional case: a 60-degree antenna bolted to the boat facing forward
#: points *away from the shore station* for the whole of every outbound survey
#: line, which costs the backlobe's 25 dB and takes the link from comfortable
#: to dead at 900 m. If the antenna really is directional then the mounting is
#: the design decision, not a detail — aft-facing, or on a mast with no strong
#: pattern — and a simulator whose default models a boat nobody would build
#: teaches nothing. So the heading-dependent term is implemented and tested,
#: and switched off until the part is confirmed.
VESSEL_ANTENNA_OMNI = Antenna(
    gain_dbi=6.7, azimuth_beamwidth_deg=360.0, elevation_beamwidth_deg=360.0
)


# -- the two ends ----------------------------------------------------------


@dataclass(frozen=True)
class ShoreStation:
    """The tripod. Moves between missions and can be turned by hand mid-mission,
    so nothing here may be treated as a surveyed fixed installation — these
    values are what the setup page will eventually collect per mission.
    """

    east_m: float = 0.0
    north_m: float = -50.0
    #: Phase centre height above the water. The 2.9 m tripod, plus a little.
    height_m: float = 2.9
    #: Compass bearing the sector faces.
    boresight_deg: float = 0.0
    antenna: Antenna = SHORE_ANTENNA
    #: EIRP is regulated and the radio backs off at high MCS; 25 dBm conducted
    #: is a conventional figure for this class. PROVISIONAL.
    tx_power_dbm: float = 25.0


@dataclass(frozen=True)
class VesselRadio:
    """The boat end.

    ``height_m`` is the sleeper here. The two-ray interference term is governed
    by the product of the two antenna heights, so raising this antenna is worth
    far more than raising the tripod or adding transmit power — see the note on
    :func:`two_ray_db`. It is currently a guess.
    """

    height_m: float = 1.0
    antenna: Antenna = VESSEL_ANTENNA_OMNI
    #: Where the antenna points relative to the bow, compass-wise: 0 = ahead.
    boresight_offset_deg: float = 0.0
    tx_power_dbm: float = 25.0


@dataclass(frozen=True)
class Propagation:
    freq_mhz: float = DEFAULT_FREQ_MHZ
    wave_height_m: float = DEFAULT_WAVE_HEIGHT_M


# -- geometry --------------------------------------------------------------


@dataclass(frozen=True)
class Geometry:
    """Where the boat is, as the radio sees it. Pure geometry — no RF.

    This is deliberately computable with no radio telemetry at all. If the
    MikroTik turns out to expose nothing we can read, the link panel can still
    show the boat working toward the edge of the sector, which was the whole
    point: turning "the link dropped" into "the link is going to drop".
    """

    range_m: float
    #: Compass bearing from the station to the boat.
    bearing_deg: float
    #: Signed angle off the sector's boresight, in [-180, 180). Positive is
    #: clockwise (to the right of boresight, looking out from the tripod).
    off_boresight_deg: float
    #: How far below the station's horizontal the boat sits, in degrees. Small
    #: at range; large enough to matter a few tens of metres off the beach,
    #: which is the cone of silence under a sector antenna.
    depression_deg: float
    #: The same off-axis angle for the boat's own antenna, or None when it is
    #: omnidirectional and heading therefore cannot matter.
    vessel_off_boresight_deg: float | None

    @property
    def fraction_of_sector(self) -> float:
        """0 at boresight, 1 at the edge of the 3 dB sector, >1 outside it."""
        return abs(self.off_boresight_deg)


def geometry(
    station: ShoreStation,
    east_m: float,
    north_m: float,
    vessel_heading_deg: float = 0.0,
    vessel: VesselRadio | None = None,
) -> Geometry:
    """Resolve the boat's position into the angles the link budget needs."""
    vessel = vessel or VesselRadio()
    de = east_m - station.east_m
    dn = north_m - station.north_m
    ground_range = math.hypot(de, dn)
    bearing = enu_bearing_deg(de, dn)

    height_difference = station.height_m - vessel.height_m
    slant = math.hypot(ground_range, height_difference)
    depression = math.degrees(math.atan2(height_difference, max(ground_range, 1e-6)))

    if vessel.antenna.omnidirectional:
        vessel_off = None
    else:
        # Where the boat's antenna points, and where the station is from the
        # boat: the reciprocal bearing.
        antenna_bearing = vessel_heading_deg + vessel.boresight_offset_deg
        vessel_off = wrap180(bearing + 180.0 - antenna_bearing)

    return Geometry(
        range_m=slant,
        bearing_deg=bearing,
        off_boresight_deg=wrap180(bearing - station.boresight_deg),
        depression_deg=depression,
        vessel_off_boresight_deg=vessel_off,
    )


# -- propagation -----------------------------------------------------------


def fspl_db(range_m: float, freq_mhz: float = DEFAULT_FREQ_MHZ) -> float:
    """Free-space path loss."""
    range_m = max(range_m, 1.0)
    return 20.0 * math.log10(range_m) + 20.0 * math.log10(freq_mhz * 1e6) - 147.55


def specular_coefficient(
    range_m: float,
    h_station_m: float,
    h_vessel_m: float,
    wave_height_m: float,
    freq_mhz: float = DEFAULT_FREQ_MHZ,
) -> float:
    """How much of the sea-surface reflection survives, 0..1 (Ament).

    A rough sea scatters the specular ray away and the link behaves like free
    space. A glassy one returns it coherently and you get interference. The
    grazing angle shrinks with range, which is why a sea that destroys the
    reflection at 100 m leaves it almost intact at 2 km — and therefore why the
    close-in nulls are a calm-morning phenomenon while the long-range roll-off
    is not.
    """
    if wave_height_m <= 0.0:
        return 1.0
    grazing = (h_station_m + h_vessel_m) / max(range_m, 1.0)
    roughness = 2.0 * math.pi * wave_height_m * grazing / wavelength_m(freq_mhz)
    return math.exp(-2.0 * roughness * roughness)


def two_ray_db(
    range_m: float,
    h_station_m: float,
    h_vessel_m: float,
    wave_height_m: float = DEFAULT_WAVE_HEIGHT_M,
    freq_mhz: float = DEFAULT_FREQ_MHZ,
) -> float:
    """Interference between the direct ray and the sea-surface reflection, in dB
    relative to free space. Positive is constructive.

    Two consequences, both of which the simulator should be able to produce and
    could not before:

    **Close-in nulls on calm water.** The reflection arrives half a wavelength
    out of step at ranges ``2 h1 h2 / (n lambda)``. With the tripod at 2.9 m and
    the boat antenna at 1 m that is about 106 m, then 53 m, then 35 m — right
    where somebody first tests. They are deep, they are narrow, and on a rough
    day they are not there at all.

    **The far cliff.** Past the last null the two rays increasingly cancel, and
    received power falls with the fourth power of range instead of the square.
    That, meeting the receiver's floor, is where this link actually ends — and
    it is governed by the *product of the two antenna heights*. Doubling the
    boat's antenna height buys about 6 dB at long range, which is worth more
    than any amount of transmit power we are allowed to use.
    """
    lam = wavelength_m(freq_mhz)
    path_difference = 2.0 * h_station_m * h_vessel_m / max(range_m, 1.0)
    phase = 2.0 * math.pi * path_difference / lam
    rho = specular_coefficient(range_m, h_station_m, h_vessel_m, wave_height_m, freq_mhz)

    # Grazing reflection off water is very nearly a sign inversion, so the
    # resultant is |1 - rho e^(-j phase)|.
    real = 1.0 - rho * math.cos(phase)
    imag = rho * math.sin(phase)
    ratio = math.hypot(real, imag)
    # A perfect null is -inf dB and would make every downstream number NaN.
    # Floor it well below the receiver's floor, where it means the same thing.
    return 20.0 * math.log10(max(ratio, 1e-3))


# -- rate selection --------------------------------------------------------


@dataclass(frozen=True)
class Mcs:
    index: int
    min_rssi_dbm: float
    phy_mbps: float

    @property
    def name(self) -> str:
        return f"MCS{self.index}"

    @property
    def usable_bytes_per_s(self) -> float:
        return self.phy_mbps * 1e6 * USABLE_FRACTION_OF_PHY / 8.0


#: What fraction of the PHY rate survives as application throughput, after
#: preamble, contention, acknowledgement and TCP. PROVISIONAL, and generous
#: rather than optimistic: measure it at the ramp before believing it.
USABLE_FRACTION_OF_PHY = 0.45

#: 802.11ax, 80 MHz, one spatial stream, 0.8 us guard interval. PHY rates are
#: from the standard's MCS table; the sensitivities are conventional figures
#: for this class of radio, not measurements. PROVISIONAL.
#:
#: One stream, not two, because nothing here has confirmed the boat antenna is
#: dual-polarity. If it is, every rate doubles and the sensitivities do not
#: move, which widens the *throughput* but not the *range* — the cliff below
#: stays exactly where it is.
MCS_TABLE: tuple[Mcs, ...] = (
    Mcs(0, -82.0, 36.0),
    Mcs(1, -79.0, 72.1),
    Mcs(2, -77.0, 108.1),
    Mcs(3, -74.0, 144.1),
    Mcs(4, -70.0, 216.2),
    Mcs(5, -66.0, 288.2),
    Mcs(6, -65.0, 324.3),
    Mcs(7, -64.0, 360.3),
    Mcs(8, -59.0, 432.4),
    Mcs(9, -57.0, 480.4),
    Mcs(10, -54.0, 540.4),
    Mcs(11, -52.0, 600.5),
)

#: Below this there is no link at all. This is the cliff, and it is a real
#: number from the table rather than a tuning knob: it is the signal the
#: lowest modulation needs to stay locked.
NOISE_FLOOR_DBM = MCS_TABLE[0].min_rssi_dbm

#: Headroom above the cliff at which the link counts as unimpaired. Used only
#: to map decibels onto the 0..1 quality the profile selector already speaks.
FULL_QUALITY_HEADROOM_DB = 30.0


def select_mcs(rssi_dbm: float) -> Mcs | None:
    """The fastest modulation this signal supports, or None below the floor.

    The staircase, and then the cliff. Rate adaptation steps down through the
    table as signal falls — throughput drops in jumps, not smoothly — and then
    at the bottom step there is nothing below, which is what makes a directional
    link fail the way it does.
    """
    best: Mcs | None = None
    for mcs in MCS_TABLE:
        if rssi_dbm >= mcs.min_rssi_dbm:
            best = mcs
    return best


def headroom_db(rssi_dbm: float) -> float:
    """Decibels in hand before the link is lost entirely.

    Deliberately measured against the cliff rather than against the current
    modulation's requirement. Margin-to-next-rate-step is what an RF engineer
    wants; an operator watching a boat cannot see rate steps and does not care
    about them. What they can act on is how much they can afford to lose.
    """
    return rssi_dbm - NOISE_FLOOR_DBM


def quality_from_rssi(rssi_dbm: float) -> float:
    """Headroom, expressed as the 0..1 the profile selector already consumes.

    Keeping the old vocabulary matters: ``ProfileSelector`` and the link panel
    are tuned against quality, and this change is meant to give those thresholds
    a physical meaning rather than to invalidate them.
    """
    return max(0.0, min(1.0, headroom_db(rssi_dbm) / FULL_QUALITY_HEADROOM_DB))


# -- the budget ------------------------------------------------------------


@dataclass(frozen=True)
class LinkEstimate:
    rssi_dbm: float
    #: What this range would give with the antennas pointed at each other and
    #: no sea in the way. The *difference* is the diagnostic: a signal that
    #: matches its range means you are simply far away, and one that does not
    #: means alignment, obstruction, or a multipath null.
    expected_rssi_dbm: float
    headroom_db: float
    quality: float
    mcs: Mcs | None
    #: Loss from both antenna patterns, dB, positive.
    pattern_loss_db: float
    #: Sea-surface interference, dB, signed.
    multipath_db: float
    geometry: Geometry

    @property
    def connected(self) -> bool:
        return self.mcs is not None

    @property
    def deviation_db(self) -> float:
        """Negative means worse than this range should give."""
        return self.rssi_dbm - self.expected_rssi_dbm

    @property
    def usable_bytes_per_s(self) -> float:
        return self.mcs.usable_bytes_per_s if self.mcs else 0.0


def estimate(
    station: ShoreStation,
    vessel: VesselRadio,
    propagation: Propagation,
    geom: Geometry,
) -> LinkEstimate:
    """The whole budget, from geometry to a modulation. No randomness."""
    path_loss = fspl_db(geom.range_m, propagation.freq_mhz)
    peak = station.tx_power_dbm + station.antenna.gain_dbi + vessel.antenna.gain_dbi

    station_gain = station.antenna.gain_at(geom.off_boresight_deg, geom.depression_deg)
    if geom.vessel_off_boresight_deg is None:
        vessel_gain = vessel.antenna.gain_dbi
    else:
        vessel_gain = vessel.antenna.gain_at(geom.vessel_off_boresight_deg)
    pattern_loss = (station.antenna.gain_dbi - station_gain) + (
        vessel.antenna.gain_dbi - vessel_gain
    )

    multipath = two_ray_db(
        geom.range_m, station.height_m, vessel.height_m,
        propagation.wave_height_m, propagation.freq_mhz,
    )

    rssi = station.tx_power_dbm + station_gain + vessel_gain - path_loss + multipath
    return LinkEstimate(
        rssi_dbm=rssi,
        expected_rssi_dbm=peak - path_loss,
        headroom_db=headroom_db(rssi),
        quality=quality_from_rssi(rssi),
        mcs=select_mcs(rssi),
        pattern_loss_db=pattern_loss,
        multipath_db=multipath,
        geometry=geom,
    )


@dataclass(frozen=True)
class RadioConfig:
    """Everything the link budget needs, in one object to pass around."""

    station: ShoreStation = field(default_factory=ShoreStation)
    vessel: VesselRadio = field(default_factory=VesselRadio)
    propagation: Propagation = field(default_factory=Propagation)

    def estimate_at(
        self, east_m: float, north_m: float, heading_deg: float = 0.0
    ) -> LinkEstimate:
        geom = geometry(self.station, east_m, north_m, heading_deg, self.vessel)
        return estimate(self.station, self.vessel, self.propagation, geom)
