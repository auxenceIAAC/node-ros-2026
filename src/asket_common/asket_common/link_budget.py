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

Most defaults here are now the equipment's **published figures** — the sector's
120 degree beamwidth, and the receiver sensitivity and per-rate transmit power
straight off the mANTBox ax 15s datasheet. What remains genuinely PROVISIONAL
is marked as such, and the one that matters most is the boat antenna's
*height*, which dominates useful range (see :func:`two_ray_db`) and is
currently a guess. See ``docs/open_questions.md``.

A note on which radio. The plain mANTBox 15s is 802.11ac; only the **mANTBox
ax 15s** is Wi-Fi 6. This module models the ax, since Wi-Fi 6 was the stated
intent — worth confirming the order matches.
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


#: The sector ashore: 15 dBi, **120 degrees**, checked against MikroTik's own
#: product pages and their resellers rather than taken from memory. Both the
#: mANTBox 15s and the Wi-Fi 6 mANTBox ax 15s publish the same figure, so the
#: 60 degrees this was briefly thought to be would have halved the working
#: area before somebody had to turn the tripod — in the pessimistic direction,
#: as it happens, but wrong either way.
#:
#: The elevation beamwidth is still a guess. MikroTik publish the elevation
#: pattern as a polar plot and state no number, and 10 degrees is typical for
#: a 15 dBi sector. It matters only very close in, where the depression angle
#: to the boat gets large — the cone of silence under the tripod.
SHORE_ANTENNA = Antenna(
    gain_dbi=15.0, azimuth_beamwidth_deg=120.0, elevation_beamwidth_deg=10.0
)

#: The boat end. **Omnidirectional, decided** — the gain lives ashore, where
#: the tripod does not move during a survey line and is cheap to aim, and the
#: boat is free to point wherever the survey does.
#:
#: The 6.7 dBi is the HGO-Antenna-OUT's figure and is still PROVISIONAL; the
#: *pattern* is not a guess any more. This matters more than it sounds: the
#: whole heading term below drops out, which is why the boat's off-boresight
#: angle is reported as ``None`` rather than zero, and why nothing in this
#: model depends on knowing which way the boat is facing.
VESSEL_ANTENNA_OMNI = Antenna(
    gain_dbi=6.7, azimuth_beamwidth_deg=360.0, elevation_beamwidth_deg=360.0
)

#: A directional boat antenna, kept because :class:`Antenna` has to handle one
#: and a tested path is better than an untested branch — not because anything
#: uses it. If it is ever fitted, note that facing it forward points it away
#: from the station on every outbound survey line, which costs the backlobe's
#: 25 dB; the mounting would be a design decision rather than a detail.
VESSEL_ANTENNA_DIRECTIONAL = Antenna(
    gain_dbi=6.7, azimuth_beamwidth_deg=60.0, elevation_beamwidth_deg=60.0
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
    #: See DEFAULT_CHANNEL_WIDTH_MHZ: every doubling costs 3 dB of range and
    #: buys twice the throughput, and this link needs range more than it needs
    #: bandwidth it will not use.
    channel_width_mhz: float = 40.0


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
    """One rung of the modulation ladder, as the radio's datasheet states it.

    Two things here are easy to get wrong and both change the answer by
    several decibels.

    **Sensitivity depends on channel width.** A wider channel spreads the same
    transmit power over more spectrum, so the receiver needs a stronger signal:
    about 3 dB per doubling. The datasheet quotes 20 MHz, so that is what is
    stored and :meth:`sensitivity_dbm` does the arithmetic.

    **Transmit power depends on the rate.** A radio backs off as it moves to
    denser modulation to keep the constellation clean — 28 dBm at MCS0 down to
    22 dBm at MCS11 on this one. That is not a detail: it means stepping *down*
    the ladder buys 6 dB as well as a lower rate, which is a large part of how
    rate adaptation extends range, and a model with one flat transmit power
    misses it entirely.
    """

    index: int
    #: Receiver sensitivity in a 20 MHz channel, dBm.
    sensitivity_20mhz_dbm: float
    #: Conducted transmit power at this rate, dBm.
    tx_power_dbm: float
    #: PHY rate in a 20 MHz channel, one spatial stream, Mbit/s.
    phy_mbps_20mhz: float

    @property
    def name(self) -> str:
        return f"MCS{self.index}"

    def sensitivity_dbm(self, channel_width_mhz: float = 40.0) -> float:
        return self.sensitivity_20mhz_dbm + 10.0 * math.log10(channel_width_mhz / 20.0)

    def phy_mbps(self, channel_width_mhz: float = 40.0) -> float:
        """Approximate: a wider channel carries proportionally more subcarriers.

        Slightly pessimistic at 80 MHz, where the usable fraction of the band
        is a little higher — which is the direction to be wrong in.
        """
        return self.phy_mbps_20mhz * (channel_width_mhz / 20.0)

    def usable_bytes_per_s(self, channel_width_mhz: float = 40.0) -> float:
        return (
            self.phy_mbps(channel_width_mhz) * 1e6 * USABLE_FRACTION_OF_PHY / 8.0
        )


#: What fraction of the PHY rate survives as application throughput, after
#: preamble, contention, acknowledgement and TCP. PROVISIONAL, and generous
#: rather than optimistic: measure it at the ramp before believing it.
USABLE_FRACTION_OF_PHY = 0.45

#: 802.11ax, one spatial stream, 0.8 us guard interval.
#:
#: **Sensitivity and transmit power are the mANTBox ax 15s datasheet's own
#: figures**, not the conventional numbers this table first held. The
#: datasheet states four rungs at 5 GHz — MCS0 at -96 dBm / 28 dBm, MCS7 at
#: -75 / 25, MCS9 at -70 / 23, MCS11 at -67 / 22 — and the rungs between them
#: are interpolated along the standard's own modulation and coding steps,
#: which land exactly on all four anchors.
#:
#: The first draft of this table guessed -82 dBm for MCS0 and a flat 25 dBm
#: of transmit power. Both were pessimistic, and together by about 11 dB,
#: which in the two-ray regime is very nearly a factor of two in range: it put
#: the cliff at 2.7 km when the gear supports something closer to 6. Worth
#: remembering the next time a "conventional figure" looks harmless.
#:
#: One spatial stream, not two. The antenna is dual-polarity so two streams
#: are likely available, which would double every rate — but not move a single
#: sensitivity, so it widens the pipe without extending the range. The cliff
#: below is where it is either way.
MCS_TABLE: tuple[Mcs, ...] = (
    Mcs(0, -96.0, 28.0, 8.6),
    Mcs(1, -93.0, 28.0, 17.2),
    Mcs(2, -91.0, 27.0, 25.8),
    Mcs(3, -88.0, 27.0, 34.4),
    Mcs(4, -84.0, 26.0, 51.6),
    Mcs(5, -80.0, 26.0, 68.8),
    Mcs(6, -78.0, 25.0, 77.4),
    Mcs(7, -75.0, 25.0, 86.0),
    Mcs(8, -72.0, 24.0, 103.2),
    Mcs(9, -70.0, 23.0, 114.7),
    Mcs(10, -68.0, 22.0, 129.0),
    Mcs(11, -67.0, 22.0, 143.4),
)

#: Channel width, MHz. PROVISIONAL, and a real deployment choice rather than a
#: constant: every doubling costs 3 dB of range and buys twice the throughput.
#:
#: 40 MHz because this link's job is a camera and telemetry. Even 20 MHz at a
#: middling modulation carries more than the video needs, so spending range on
#: bandwidth nobody uses is a poor trade — and range is the thing that decides
#: whether the boat keeps its link at the far end of the survey box.
DEFAULT_CHANNEL_WIDTH_MHZ = 40.0

#: Headroom above the cliff at which the link counts as unimpaired. Used only
#: to map decibels onto the 0..1 quality the profile selector already speaks.
FULL_QUALITY_HEADROOM_DB = 30.0


def noise_floor_dbm(channel_width_mhz: float = DEFAULT_CHANNEL_WIDTH_MHZ) -> float:
    """The signal the most robust modulation needs. Below this, no link.

    A function rather than a constant because it moves with channel width,
    and a constant that silently meant "at 20 MHz" is the shape of the bug
    this whole table just had.
    """
    return MCS_TABLE[0].sensitivity_dbm(channel_width_mhz)


def select_mcs(
    path_gain_db: float, channel_width_mhz: float = DEFAULT_CHANNEL_WIDTH_MHZ
) -> Mcs | None:
    """The fastest modulation this path supports, or None below the floor.

    ``path_gain_db`` is everything between the transmitter's output and the
    receiver's input — antenna gains, path loss, multipath — with the
    transmit power left out, because each rung transmits at a different power.
    Each rung is therefore tested on its own terms.

    The staircase, and then the cliff: throughput drops in jumps as the radio
    steps down, and at the bottom rung there is nothing below.
    """
    best: Mcs | None = None
    for mcs in MCS_TABLE:
        if mcs.tx_power_dbm + path_gain_db >= mcs.sensitivity_dbm(channel_width_mhz):
            best = mcs
    return best


def headroom_db(
    path_gain_db: float, channel_width_mhz: float = DEFAULT_CHANNEL_WIDTH_MHZ
) -> float:
    """Decibels in hand before the link is lost entirely.

    Measured on the bottom rung — its transmit power against its sensitivity —
    because that is the rung the radio will be on when the link finally goes,
    and it is deliberately not margin-to-the-next-rate-step. Margin to a rate
    step is what an RF engineer wants; an operator watching a boat cannot see
    rate steps and cannot act on them. What they can act on is how much signal
    they can afford to lose.
    """
    bottom = MCS_TABLE[0]
    return (bottom.tx_power_dbm + path_gain_db) - bottom.sensitivity_dbm(
        channel_width_mhz
    )


def quality_from_headroom(headroom: float) -> float:
    """Headroom, expressed as the 0..1 the profile selector already consumes.

    Keeping the old vocabulary matters: ``ProfileSelector`` and the link panel
    are tuned against quality, and this change is meant to give those
    thresholds a physical meaning rather than to invalidate them.
    """
    return max(0.0, min(1.0, headroom / FULL_QUALITY_HEADROOM_DB))


# -- the budget ------------------------------------------------------------


@dataclass(frozen=True)
class LinkEstimate:
    #: Signal at the receiver on the modulation actually in use. None past the
    #: cliff, where there is no modulation and so no signal to quote.
    rssi_dbm: float | None
    #: What this range would give with the antennas pointed at each other, no
    #: sea in the way, and the most robust modulation's transmit power. The
    #: *difference* is the diagnostic: a signal that matches its range means
    #: you are simply far away, and one that does not means alignment, an
    #: obstruction, or a multipath null.
    expected_rssi_dbm: float
    headroom_db: float
    quality: float
    mcs: Mcs | None
    #: Everything between transmitter output and receiver input, transmit
    #: power excluded. Negative, and large.
    path_gain_db: float
    #: Loss from both antenna patterns, dB, positive.
    pattern_loss_db: float
    #: Sea-surface interference, dB, signed.
    multipath_db: float
    channel_width_mhz: float
    geometry: Geometry

    @property
    def connected(self) -> bool:
        return self.mcs is not None

    @property
    def deviation_db(self) -> float:
        """Negative means worse than this range should give."""
        bottom = MCS_TABLE[0]
        on_the_floor = bottom.tx_power_dbm + self.path_gain_db
        return on_the_floor - self.expected_rssi_dbm

    @property
    def usable_bytes_per_s(self) -> float:
        if self.mcs is None:
            return 0.0
        return self.mcs.usable_bytes_per_s(self.channel_width_mhz)


def estimate(
    station: ShoreStation,
    vessel: VesselRadio,
    propagation: Propagation,
    geom: Geometry,
) -> LinkEstimate:
    """The whole budget, from geometry to a modulation. No randomness.

    Modelled in the boat-to-shore direction, because that is the way the video
    travels and therefore the direction that decides what the operator sees.
    Path loss is reciprocal and both ends are the same family of radio, so the
    reverse differs only in which antenna gain sits at which end — and both
    gains are in the sum either way.
    """
    path_loss = fspl_db(geom.range_m, propagation.freq_mhz)

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

    path_gain = station_gain + vessel_gain - path_loss + multipath
    width = propagation.channel_width_mhz
    mcs = select_mcs(path_gain, width)
    headroom = headroom_db(path_gain, width)

    peak_gains = station.antenna.gain_dbi + vessel.antenna.gain_dbi
    return LinkEstimate(
        rssi_dbm=(mcs.tx_power_dbm + path_gain) if mcs else None,
        expected_rssi_dbm=MCS_TABLE[0].tx_power_dbm + peak_gains - path_loss,
        headroom_db=headroom,
        quality=quality_from_headroom(headroom),
        mcs=mcs,
        path_gain_db=path_gain,
        pattern_loss_db=pattern_loss,
        multipath_db=multipath,
        channel_width_mhz=width,
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
