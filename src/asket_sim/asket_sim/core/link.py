"""Simulated shore link.

Bandwidth negotiation is the single biggest architectural risk in this system: a
GUI that works on the bench and collapses 200 m offshore (brief, section 9).
That risk is only retired if the degraded case can be produced on demand, so the
link is simulated as a first-class source rather than assumed to be perfect.

**This file used to model the wrong failure.** Quality fell off smoothly with
distance, with a cellular fallback underneath — a picture of 4G decaying as you
drive away from a mast. The hardware is a sector antenna on a tripod ashore and
a directional antenna on the boat, line of sight over water, and a link like
that does not decay. It works, and then it stops: the boat turns out of the
sector, or the two-ray path loss finally crosses the receiver's floor, and
several megabits become nothing in a couple of seconds.

That distinction is not academic. The camera panel's hardest requirement is to
notice a *sudden* stop and refuse to keep showing a picture that is no longer
true, and you cannot tune a three-second staleness threshold against a link
that takes a minute to fade. So the physics moved into
:mod:`asket_common.link_budget` — geometry, antenna patterns, two-ray
propagation, the modulation ladder — and what is left here is what a simulator
should own: fading, faults, bearer selection, and a pure ``sample()``.

Three behaviours now emerge from the budget rather than being written down:

* **Turning out of the sector costs you the link** — but only at range. Close
  in, the antenna's backlobe still carries it, which is correct and is why
  ``link_alignment_lost`` looks like nothing at all beside the ramp.
* **A staircase, then a cliff.** Throughput steps down through the modulation
  table and then stops. There is no rung below the bottom one.
* **Multipath nulls on calm water**, at fixed ranges, deep and narrow. Present
  on a glassy morning inshore and absent in any swell.

The profile selector's thresholds now have a physical meaning they did not
have before: ``full`` is roughly 10 dB of headroom above the cliff or better,
``reduced`` is 3-10 dB, and ``minimal`` is what is left in the last 3 dB.
"""

from __future__ import annotations

import dataclasses
import math
import random
from dataclasses import dataclass, field

from asket_common.link_budget import (
    RadioConfig,
    ShoreStation,
    headroom_db,
    quality_from_headroom,
    select_mcs,
)

LINK_ETHERNET = "ethernet"
LINK_WIFI = "wifi"
LINK_4G = "4g"
LINK_LTEM = "ltem"
LINK_NONE = "none"

#: Representative characteristics of the bearers this module does *not* model
#: from physics: (rtt_ms, usable bytes/s). The WiFi row is a fallback only —
#: with a station configured, its capacity comes from the modulation the link
#: budget actually supports, which is the whole point of the rewrite.
LINK_CHARACTERISTICS = {
    LINK_ETHERNET: (2.0, 10_000_000.0),
    LINK_WIFI: (4.0, 2_000_000.0),
    LINK_4G: (90.0, 120_000.0),
    LINK_LTEM: (900.0, 1_000.0),
    LINK_NONE: (float("inf"), 0.0),
}

#: Round trip on a healthy directional link, milliseconds. Retries as the
#: signal falls push it up; it never reaches 4G's latency, which is why the
#: profile selector on this hardware is driven by quality rather than by RTT.
WIFI_BASE_RTT_MS = 4.0

#: How far the sector is swung when ``link_alignment_lost`` is injected, and
#: how fast it gets there: right round, over about two seconds.
#:
#: A full reversal, and not because a gentler number was too boring. Work the
#: arithmetic at the simulated survey range and it is a finding about the gear:
#: at 900 m the link holds about 19 dB over the cliff, and a 120-degree sector
#: costs only ``3 (theta / 60)^2`` dB off boresight, so it takes about 150
#: degrees of error before the link is in any trouble at all. **A tripod nudged
#: by twenty or ninety degrees does not cost you this link at survey range.**
#: What costs you the link is range, a boat near its limit when the knock
#: happens, or a 60-degree sector rather than a 120-degree one — which is
#: exactly the discrepancy flagged on ``SHORE_ANTENNA``.
#:
#: That is worth knowing before somebody spends a morning making the tripod
#: rigid to solve a problem it does not have.
ALIGNMENT_LOST_DEG = 180.0
ALIGNMENT_SLEW_DEG_PER_S = 90.0

#: Root-mean-square wave height that counts as "glassy", metres. Small enough
#: that the sea reflects coherently at the grazing angles involved, which is
#: what makes the close-in nulls appear.
GLASSY_WAVE_HEIGHT_M = 0.02

#: Where the tripod stands, in the world's local ENU metres, and which way it
#: faces. The simulated survey box sits around the origin, so this puts the
#: work about 2.5 km offshore.
#:
#: Chosen so the degraded states are reachable at all. With the datasheet's
#: real sensitivities this link holds ~32 dB of headroom at 900 m and ~55 dB
#: at 100 m, and the sector's own front-to-back ratio is 25 dB — so close in,
#: *nothing you can do to the antenna* can break the link, and every failure
#: the GUI exists to render was unreachable in simulation. At 2.5 km there is
#: about 16 dB in hand, which is a working link with something to lose.
#:
#: The consequence worth knowing: the multipath nulls are a different part of
#: the envelope entirely — they live inside ~110 m of the station — so they
#: are a leaving-and-returning phenomenon, not a survey-range one. Two faults,
#: two places, and that is the honest shape of this link rather than an
#: inconvenience.
DEFAULT_STATION_NORTH_M = -2500.0


def _default_radio() -> RadioConfig:
    return RadioConfig(
        station=ShoreStation(east_m=0.0, north_m=DEFAULT_STATION_NORTH_M,
                             boresight_deg=0.0)
    )


@dataclass
class LinkConfig:
    #: The radios and where they are pointed. Everything physical lives here.
    radio: RadioConfig = field(default_factory=_default_radio)

    #: Whether a cellular fallback exists at this site. PROVISIONAL, Q6.
    #:
    #: Default **false**, which is a change: with a single directional link the
    #: realistic ladder is "working" and "gone", and a simulator that always
    #: caught the boat on 4G would hide the case the GUI most needs to handle.
    #: ``link_degraded`` still forces a 4G bearer, so the ``reduced`` profile
    #: stays reachable for review.
    cellular_available: bool = False

    #: Fading, as an Ornstein-Uhlenbeck process in **decibels**: a standard
    #: deviation and a time constant, rather than a per-step noise amplitude.
    #:
    #: Parameterised this way on purpose. A per-step amplitude makes the amount
    #: of fading depend on how often the simulator happens to be stepped, so
    #: changing the tick rate silently changes the weather. These two numbers
    #: mean what they say at any step size.
    fade_std_db: float = 1.5
    fade_time_constant_s: float = 12.0


@dataclass
class LinkSample:
    utc_ms: int
    active_link: str
    quality: float          # 0..1
    rtt_ms: float
    capacity_bytes_per_s: float
    distance_m: float

    # -- what the radio actually sees -------------------------------------
    #: None on a bearer this module does not model from physics (4G, LTE-M).
    rssi_dbm: float | None = None
    #: Where the signal came from: ``"measured"`` here, because the simulator
    #: has a simulated radio to read. The real vessel has nothing reading its
    #: radio yet and reports ``"predicted"`` instead, and the panel says which
    #: — a modelled number shown as a measurement is the lie this interface
    #: exists to prevent.
    rssi_source: str | None = None
    #: What this range would give with the antennas pointed at each other and
    #: no sea in the way. The difference is the diagnostic an operator can act
    #: on: a signal that matches its range means you are simply far away.
    expected_rssi_dbm: float | None = None
    headroom_db: float | None = None
    mcs_index: int | None = None
    phy_mbps: float | None = None

    # -- geometry, which is knowable with no radio telemetry at all -------
    off_boresight_deg: float | None = None
    #: The sector's 3 dB width. Sent with the angle because "40 degrees off"
    #: means nothing without it, and the operator's decision — turn the tripod
    #: or carry on — is about the ratio.
    sector_beamwidth_deg: float | None = None
    vessel_off_boresight_deg: float | None = None
    multipath_db: float | None = None
    pattern_loss_db: float | None = None
    sea_state_m: float | None = None


class LinkSim:
    """Simulated shore link.

    ``step()`` advances the fading and any antenna slew; ``sample()`` is
    **pure**. That separation is not stylistic. An earlier version advanced the
    fade inside ``sample()``, and since the backend samples the link several
    times per tick — once for the stream, once for alarms, once for profile
    selection — the "slow" fade was being advanced sixty times a second instead
    of once. Quality swung between 0.26 and 0.99 on a stationary vessel, the
    link flipped between WiFi and LTE-M, and the profile selector could never
    hold a candidate long enough to recover. A read that changes what it reads
    is a bug waiting to happen.
    """

    def __init__(self, config: LinkConfig | None = None, seed: int = 5) -> None:
        self.cfg = config or LinkConfig()
        self._rng = random.Random(seed)
        self._fade_db = 0.0
        #: Current and commanded error in the shore antenna's bearing. The
        #: sector slews between them rather than jumping, so the collapse has
        #: a shape and the GUI has something to be wrong about halfway through.
        self._alignment_error_deg = 0.0
        self._alignment_target_deg = 0.0
        #: Overrides the configured sea state when set. The weather is not a
        #: property of the radio, and a glassy morning is a condition somebody
        #: needs to be able to ask for.
        self._sea_state_override_m: float | None = None

    # -- hardware-side events, not commandable from software --------------

    def set_alignment_error(self, degrees: float) -> None:
        """Knock the tripod. Simulation input only."""
        self._alignment_target_deg = float(degrees)

    def set_sea_state(self, wave_height_m: float | None) -> None:
        """Flatten or restore the sea. Simulation input only."""
        self._sea_state_override_m = wave_height_m

    @property
    def alignment_error_deg(self) -> float:
        return self._alignment_error_deg

    def _effective_radio(self) -> RadioConfig:
        """The configured radio, with whatever the world has done to it."""
        radio = self.cfg.radio
        station = radio.station
        propagation = radio.propagation
        if self._alignment_error_deg:
            station = replace_boresight(
                station, station.boresight_deg + self._alignment_error_deg
            )
        if self._sea_state_override_m is not None:
            propagation = dataclasses.replace(
                propagation, wave_height_m=self._sea_state_override_m
            )
        if station is radio.station and propagation is radio.propagation:
            return radio
        return RadioConfig(station=station, vessel=radio.vessel,
                           propagation=propagation)

    # -- simulation -------------------------------------------------------

    def step(self, dt: float) -> None:
        """Advance the fading and the antenna slew by ``dt`` seconds.

        Ornstein-Uhlenbeck, discretised exactly, so the steady-state spread is
        ``fade_std_db`` whatever step size the caller uses.
        """
        if dt <= 0.0:
            return
        cfg = self.cfg
        if cfg.fade_time_constant_s > 0.0:
            decay = math.exp(-dt / cfg.fade_time_constant_s)
            kick = cfg.fade_std_db * math.sqrt(max(0.0, 1.0 - decay * decay))
            self._fade_db = decay * self._fade_db + self._rng.gauss(0.0, kick)

        delta = self._alignment_target_deg - self._alignment_error_deg
        if delta:
            travel = ALIGNMENT_SLEW_DEG_PER_S * dt
            if abs(delta) <= travel:
                self._alignment_error_deg = self._alignment_target_deg
            else:
                self._alignment_error_deg += math.copysign(travel, delta)

    def sample(
        self,
        utc_ms: int,
        east_m: float,
        north_m: float,
        forced: str | None = None,
        heading_deg: float = 0.0,
    ) -> LinkSample:
        cfg = self.cfg
        radio = self._effective_radio()
        estimate = radio.estimate_at(east_m, north_m, heading_deg)
        geom = estimate.geometry
        width = radio.propagation.channel_width_mhz

        # Fading is applied to the path, not to a quality score, so that it can
        # push the link across a modulation boundary — or over the cliff — the
        # way real fading does. To the path rather than to a signal because
        # each modulation transmits at a different power, so there is no single
        # "the signal" until a modulation has been chosen.
        path_gain = estimate.path_gain_db + self._fade_db
        mcs = select_mcs(path_gain, width)
        rssi = (mcs.tx_power_dbm + path_gain) if mcs else None
        headroom = headroom_db(path_gain, width)

        if forced is not None:
            link = forced
        elif mcs is not None:
            link = LINK_WIFI
        elif cfg.cellular_available:
            link = LINK_4G if headroom > -12.0 else LINK_LTEM
        else:
            link = LINK_NONE

        if link == LINK_WIFI and mcs is not None:
            quality = quality_from_headroom(headroom)
            rtt = WIFI_BASE_RTT_MS * (1.0 + 2.0 * (1.0 - quality) ** 2)
            capacity = mcs.usable_bytes_per_s(width)
        elif link == LINK_NONE:
            quality, rtt, capacity = 0.0, float("inf"), 0.0
        else:
            # A bearer this module does not model from physics. Its quality is
            # derived from the fade rather than drawn fresh, so that sampling
            # twice in a row cannot report two different links.
            base_rtt, base_capacity = LINK_CHARACTERISTICS.get(
                link, LINK_CHARACTERISTICS[LINK_4G]
            )
            quality = max(0.2, min(0.85, 0.55 + 0.1 * self._fade_db))
            rtt = base_rtt * (1.0 + 2.0 * (1.0 - quality) ** 2)
            capacity = base_capacity * quality
            mcs = None

        return LinkSample(
            utc_ms=utc_ms,
            active_link=link,
            quality=quality,
            rtt_ms=rtt,
            capacity_bytes_per_s=capacity,
            distance_m=geom.range_m,
            rssi_dbm=rssi if link == LINK_WIFI else None,
            rssi_source="measured" if link == LINK_WIFI else None,
            expected_rssi_dbm=estimate.expected_rssi_dbm if link == LINK_WIFI else None,
            # Headroom survives the bearer going: "how far past the cliff" is
            # exactly what somebody wants to know once the link has gone, and
            # a null there would make a lost link indistinguishable from one
            # that was never configured.
            headroom_db=headroom if link in (LINK_WIFI, LINK_NONE) else None,
            mcs_index=mcs.index if mcs else None,
            phy_mbps=mcs.phy_mbps(width) if mcs else None,
            off_boresight_deg=geom.off_boresight_deg,
            sector_beamwidth_deg=radio.station.antenna.azimuth_beamwidth_deg,
            vessel_off_boresight_deg=geom.vessel_off_boresight_deg,
            multipath_db=estimate.multipath_db,
            pattern_loss_db=estimate.pattern_loss_db,
            sea_state_m=radio.propagation.wave_height_m,
        )


def replace_boresight(station: ShoreStation, boresight_deg: float) -> ShoreStation:
    """A copy of ``station`` pointed somewhere else.

    Named rather than inlined so that the alignment fault reads as what it is —
    the tripod turned — at the one place it happens.
    """
    return dataclasses.replace(station, boresight_deg=boresight_deg)
