"""Stream registry and bandwidth policy.

The single biggest architectural risk in this system is a GUI that works
perfectly on the bench and collapses 200 m offshore. The defence is structural,
not incidental: **the backend never pushes anything by default.** A client
subscribes to named streams at a requested rate and detail level, and the server
decides what it will actually send.

Three rules, all enforced here:

1. **Nothing is sent unless it was asked for.** A stream with no subscriber
   costs nothing.
2. **The server always wins.** A client can ask for 20 Hz lidar on an LTE-M
   link; it will get told no, and told why.
3. **The client is told what it is actually getting.** A silently decimated
   stream is worse than no stream, because the operator cannot tell the
   difference between "nothing is happening" and "I am not being sent it".

Rule 3 is why :meth:`StreamPolicy.resolve` returns a reason string. It ends up
on screen.
"""

from __future__ import annotations

from dataclasses import dataclass

# Detail levels, coarsest last.
DETAIL_FULL = "full"
DETAIL_REDUCED = "reduced"
DETAIL_MINIMAL = "minimal"
DETAIL_ORDER = [DETAIL_FULL, DETAIL_REDUCED, DETAIL_MINIMAL]

# Link profiles.
PROFILE_FULL = "full"
PROFILE_REDUCED = "reduced"
PROFILE_MINIMAL = "minimal"
PROFILE_ORDER = [PROFILE_FULL, PROFILE_REDUCED, PROFILE_MINIMAL]


@dataclass(frozen=True)
class StreamSpec:
    """A thing a client can subscribe to."""

    name: str
    description: str
    #: What a client gets if it does not ask for a specific rate.
    default_rate_hz: float
    #: Ceiling regardless of profile. Nothing is served faster than this.
    max_rate_hz: float
    #: Critical streams survive into every profile, including LTE-M. Keep this
    #: list short: everything marked critical competes for a 1 kB/s budget.
    critical: bool = False
    #: Sent once on subscribe and then only when it changes, rather than at a
    #: rate. Survey lines and the geofence do not need a refresh rate.
    on_change_only: bool = False
    #: Rough payload size at full detail, bytes. Used for the budget estimate
    #: shown in the link panel — an estimate, and labelled as one.
    typical_bytes: int = 200


#: Everything the GUI can ask for. Adding a stream here and nowhere else is
#: enough for it to be negotiable; the hub looks it up by name.
STREAMS: dict[str, StreamSpec] = {
    s.name: s
    for s in [
        StreamSpec(
            "vessel", "Position, heading, course, attitude, GNSS quality",
            default_rate_hz=5.0, max_rate_hz=10.0, critical=True, typical_bytes=320,
        ),
        StreamSpec(
            "pico", "Confirmed mode, arming, relays, ESCs, RC link",
            default_rate_hz=2.0, max_rate_hz=10.0, critical=True, typical_bytes=220,
        ),
        StreamSpec(
            "power", "Battery voltage, current, state of charge, endurance",
            default_rate_hz=1.0, max_rate_hz=2.0, critical=True, typical_bytes=200,
        ),
        StreamSpec(
            "alarms", "Active alarms",
            default_rate_hz=1.0, max_rate_hz=2.0, critical=True, typical_bytes=180,
        ),
        StreamSpec(
            "link", "Shore link quality, latency, profile",
            default_rate_hz=1.0, max_rate_hz=2.0, critical=True, typical_bytes=180,
        ),
        StreamSpec(
            "heading", "Heading provenance, validity, divergence from COG",
            default_rate_hz=2.0, max_rate_hz=5.0, typical_bytes=200,
        ),
        StreamSpec(
            "mission", "Recording state, elapsed time, disk",
            default_rate_hz=1.0, max_rate_hz=2.0, typical_bytes=220,
        ),
        # Both of these are append-only and send only what is new since the
        # client last heard, so their cost per frame is bounded by the sample
        # rate rather than by how long the mission has been running. Sending
        # the whole history each time reached 18 kB per frame after ninety
        # seconds and would have been about a megabyte after three hours.
        StreamSpec(
            "coverage", "Swath ribbon painted so far",
            default_rate_hz=1.0, max_rate_hz=4.0, typical_bytes=250,
        ),
        StreamSpec(
            "track", "Vessel track history",
            default_rate_hz=1.0, max_rate_hz=4.0, typical_bytes=150,
        ),
        StreamSpec(
            "sonar", "Sonar health: ping rate, points, packet loss, clock offset",
            default_rate_hz=1.0, max_rate_hz=2.0, typical_bytes=280,
        ),
        StreamSpec(
            # Sized for the WORST case, not the typical one: over open water a
            # 720-beam scanner returns almost nothing, but alongside a moored
            # vessel it returns on most beams, and at full detail both the raw
            # and the filtered set are sent so the operator can compare them.
            # The estimate exists to answer "will this subscription survive the
            # link", and that question is decided by the peak.
            "lidar", "Obstacle returns, top-down",
            default_rate_hz=5.0, max_rate_hz=10.0, typical_bytes=12000,
        ),
        StreamSpec(
            "diagnostics", "Built-in test results and node health",
            default_rate_hz=0.2, max_rate_hz=1.0, typical_bytes=1500,
        ),
        StreamSpec(
            "plan", "Survey lines, waypoints, geofence",
            default_rate_hz=0.0, max_rate_hz=1.0, on_change_only=True, typical_bytes=2000,
        ),
        StreamSpec(
            # The one stream that changes the economics of the link, and the
            # only one sent as binary rather than JSON.
            #
            # 45 kB is a 640x480 JPEG of a marine scene at quality 80 — a
            # real-world figure, deliberately NOT measured from the simulator,
            # whose flat synthetic frames compress to about seven. Tuning a
            # budget against the mock would understate this stream sixfold.
            #
            # 5 Hz by default rather than 1: with the directional link this is
            # about 1.8 Mbit/s against a bearer measured in tens, so the
            # constraint is no longer the link. It is the Jetson — encode cost,
            # plus vision_node already running YOLO on the CPU — and nobody has
            # measured that, which is why the ceiling is 15 and not 30.
            "camera", "Forward camera, with its age on every frame",
            default_rate_hz=5.0, max_rate_hz=15.0, typical_bytes=45_000,
        ),
    ]
}


@dataclass(frozen=True)
class StreamPolicy:
    """What one profile allows for one stream."""

    max_rate_hz: float
    detail: str = DETAIL_FULL


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    #: Streams this profile serves at all. A stream absent from here is refused,
    #: with a reason, rather than quietly starved.
    policies: dict[str, StreamPolicy]
    #: Rough ceiling on what the link can carry, bytes per second.
    budget_bytes_per_s: float

    def allows(self, stream: str) -> bool:
        return stream in self.policies


def _full_profile() -> Profile:
    """The directional link: everything, at each stream's own ceiling."""
    return Profile(
        name=PROFILE_FULL,
        description="Directional link — everything, including video",
        policies={
            name: StreamPolicy(spec.max_rate_hz, DETAIL_FULL)
            for name, spec in STREAMS.items()
        },
        # Two megabytes a second. Raised from 200 kB, which was set when "fast
        # WiFi" meant an access point on the beach: a sector antenna on a
        # tripod against a boat-mounted omni carries tens of megabits at
        # survey range, and video alone would have eaten the whole of the old
        # figure. PROVISIONAL — it is a policy ceiling rather than a
        # measurement, and the link budget it is drawn from has not been
        # calibrated on the water.
        budget_bytes_per_s=2_000_000.0,
    )


def _reduced_profile() -> Profile:
    """4G: position, heading, mode, power, coverage at low rate, alarms.

    Lidar is dropped rather than decimated. A 1 Hz lidar view invites an
    operator to trust it for obstacle awareness, and obstacle avoidance is the
    navigation stack's job anyway — the GUI informs, it does not alert.
    """
    return Profile(
        name=PROFILE_REDUCED,
        description="4G — position, heading, mode, power, coverage, alarms",
        policies={
            "vessel": StreamPolicy(2.0, DETAIL_FULL),
            "pico": StreamPolicy(1.0, DETAIL_FULL),
            "power": StreamPolicy(0.5, DETAIL_FULL),
            "alarms": StreamPolicy(1.0, DETAIL_FULL),
            "link": StreamPolicy(0.5, DETAIL_FULL),
            "heading": StreamPolicy(1.0, DETAIL_FULL),
            "mission": StreamPolicy(0.5, DETAIL_REDUCED),
            "coverage": StreamPolicy(0.2, DETAIL_REDUCED),
            "track": StreamPolicy(0.2, DETAIL_REDUCED),
            "sonar": StreamPolicy(0.5, DETAIL_REDUCED),
            "diagnostics": StreamPolicy(0.1, DETAIL_REDUCED),
            "plan": StreamPolicy(0.0, DETAIL_REDUCED),
            # `camera` is deliberately absent, so resolve() refuses it with
            # this profile's own sentence rather than granting a trickle.
            #
            # A single 320x240 frame is about 10 kB and this budget is 12 kB/s,
            # so even 0.2 fps would be a sixth of everything, permanently, for
            # a picture five seconds old at every glance — the freeze problem
            # on a timer. A one-shot "fetch one frame now" is the right shape
            # here and is not built: it is a middle rung, and whether this
            # profile is ever reached depends on Q6a, which is open.
        },
        budget_bytes_per_s=12_000.0,
    )


def _minimal_profile() -> Profile:
    """LTE-M beacon: position, mode, battery, alarms. Nothing else fits.

    The budget is about a kilobyte a second. Everything here is deliberately
    coarse — this profile exists so that an operator who has lost the WiFi link
    still knows where the boat is, what mode it is in, and whether it is about
    to run out of charge.
    """
    return Profile(
        name=PROFILE_MINIMAL,
        description="LTE-M — position, mode, battery, alarms only",
        policies={
            "vessel": StreamPolicy(0.2, DETAIL_MINIMAL),
            "pico": StreamPolicy(0.2, DETAIL_MINIMAL),
            "power": StreamPolicy(0.1, DETAIL_MINIMAL),
            "alarms": StreamPolicy(0.5, DETAIL_MINIMAL),
            "link": StreamPolicy(0.1, DETAIL_MINIMAL),
        },
        budget_bytes_per_s=1_000.0,
    )


PROFILES: dict[str, Profile] = {
    PROFILE_FULL: _full_profile(),
    PROFILE_REDUCED: _reduced_profile(),
    PROFILE_MINIMAL: _minimal_profile(),
}


@dataclass
class Resolution:
    """What a client will actually get, and why."""

    stream: str
    granted: bool
    rate_hz: float
    detail: str
    reason: str

    @property
    def degraded(self) -> bool:
        return bool(self.reason)


def resolve(
    stream: str,
    requested_rate_hz: float | None,
    requested_detail: str | None,
    profile_name: str,
) -> Resolution:
    """Decide what a subscription actually gets.

    The reason string is not decoration. It goes on screen, because an operator
    who cannot tell "nothing is happening" from "I am not being sent it" will
    eventually make a decision on the wrong one.
    """
    spec = STREAMS.get(stream)
    if spec is None:
        return Resolution(stream, False, 0.0, DETAIL_MINIMAL, f"no such stream {stream!r}")

    profile = PROFILES[profile_name]
    if not profile.allows(stream):
        return Resolution(
            stream, False, 0.0, DETAIL_MINIMAL,
            f"not carried on the {profile_name} link profile ({profile.description})",
        )

    policy = profile.policies[stream]
    reasons: list[str] = []

    if spec.on_change_only:
        rate = 0.0
    else:
        rate = requested_rate_hz if requested_rate_hz is not None else spec.default_rate_hz
        if rate > spec.max_rate_hz:
            rate = spec.max_rate_hz
            reasons.append(f"capped at the stream's own maximum of {spec.max_rate_hz:g} Hz")
        if rate > policy.max_rate_hz:
            rate = policy.max_rate_hz
            reasons.append(f"limited to {policy.max_rate_hz:g} Hz by the {profile_name} profile")

    detail = requested_detail or policy.detail
    if DETAIL_ORDER.index(detail) < DETAIL_ORDER.index(policy.detail):
        detail = policy.detail
        reasons.append(f"detail reduced to {policy.detail!r} by the {profile_name} profile")

    return Resolution(stream, True, rate, detail, "; ".join(reasons))


def estimate_bytes_per_s(resolutions: list[Resolution]) -> float:
    """Rough outbound rate for a set of subscriptions.

    An estimate, and the GUI labels it as one — real payloads vary with how many
    lidar returns there are and how much track has accumulated. It is here so
    the link panel can show whether a subscription set is plausible for the
    current profile *before* the link falls over, rather than after.
    """
    total = 0.0
    for res in resolutions:
        if not res.granted:
            continue
        spec = STREAMS[res.stream]
        scale = {DETAIL_FULL: 1.0, DETAIL_REDUCED: 0.4, DETAIL_MINIMAL: 0.15}[res.detail]
        rate = res.rate_hz if res.rate_hz > 0 else 0.05  # on-change streams
        total += spec.typical_bytes * scale * rate
    return total
