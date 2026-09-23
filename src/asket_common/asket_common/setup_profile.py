"""What has been told to this vessel, by whom, and how long ago.

The model behind the mission setup page (``docs/setup_page.md``). No file
handling and no ROS: this is the logic, and it has to be usable from the
backend that serves the page, the pre-flight that reports on it, and the
bridge that consumes it. YAML reading lives in ``gui_backend``, where a YAML
dependency already exists.

## Why provenance is a field rather than a comment

``omniscan_bridge/config/mounting.yaml`` already does the important thing: it
carries one line, ``measured: false``, saying whether a human has checked the
numbers underneath it. That line is the reason the pre-flight can tell "these
are guesses" from "these were measured on the boat", and no amount of looking
at the numbers themselves would reveal it — a guessed tilt of 35 degrees and a
measured one are the same float.

Every value on the setup page has that same problem, so every value gets that
same line. A field carries where it came from — nobody has said
(:data:`DEFAULT`), somebody typed it (:data:`ENTERED`), the vessel supplied it
(:data:`CAPTURED`), or a human measured the hull (:data:`MEASURED`) — and
"has anybody actually checked this?" becomes a property of the data rather
than a special case in one file.

## Two tiers, and what the split is for

The tier is the value's **expected lifetime**, not its category:

* :data:`TIER_VESSEL` — changes when somebody rebuilds the boat. The sonar's
  mounting geometry is the archetype: offset and angle depend on how the head
  is physically bolted on, not on where you are surveying, so it is measured
  once on a given hull and kept.
* :data:`TIER_DEPLOYMENT` — changes every time somebody sets up on a beach.
  The shore station's position and bearing; the sonar's *range and gain*,
  which follow the expected depth.

That is the whole reason for splitting rather than listing: a Vessel value
three months old is fine, and a Deployment value three months old is almost
certainly wrong. Only the split lets the page say so, and knowing which is
which is exactly what somebody arriving on a beach needs when they cannot
remember what was last set.

Note where the sonar falls on both sides. Mounting geometry is Vessel; range
and gain are Deployment. They are different kinds of fact about the same
instrument.

## The ranking

:attr:`FieldSpec.silently_corrupts` is the most important flag here, and it is
narrower than "this matters". It marks the values whose being wrong produces
**plausible-looking wrong data** — a survey that is simply displaced, looks
entirely correct, and is discovered only when somebody overlays a second one.

A wrong sonar range is not in that group: you get no bottom returns and you
know within a minute. A wrong lever arm is, and so is a wrong datum.

The distinction is the one an operator with twenty minutes of daylight
actually needs, and it is what :meth:`SetupProfile.headline` is built on:
*one of these will ruin your data; the others will only make the screen less
useful*.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any

# -- tiers -----------------------------------------------------------------

TIER_VESSEL = "vessel"
TIER_DEPLOYMENT = "deployment"
TIERS = (TIER_VESSEL, TIER_DEPLOYMENT)

#: How long a Deployment value stays believable, in milliseconds.
#:
#: A day. A survey trip can run over two mornings at the same beach without
#: anything moving, and a threshold shorter than that would cry wolf on the
#: second day; one much longer would stop noticing that the tripod was packed
#: away and set up again somewhere else.
#:
#: Staleness is never an error. It is a date on the page and a note in the
#: pre-flight — the value may well still be right, and only a human standing
#: on the beach can say.
DEPLOYMENT_STALE_AFTER_MS = 24 * 60 * 60 * 1000


# -- provenance ------------------------------------------------------------

#: Nobody has said. The value is whatever this repository shipped, and every
#: warning about unset values is about this state.
DEFAULT = "default"
#: A human typed it.
ENTERED = "entered"
#: The vessel supplied it — a position from its own GNSS, a bearing from where
#: it was sitting, a datum from the first fix.
CAPTURED = "captured"
#: A human measured the hull with a tape and said so. Stronger than ENTERED,
#: and reserved for the mounting geometry, where the difference between a
#: number somebody typed and a number somebody measured is the whole question.
MEASURED = "measured"

PROVENANCES = (DEFAULT, ENTERED, CAPTURED, MEASURED)

#: Provenances that count as somebody having answered. ``DEFAULT`` is the only
#: one that does not, which is the entire mechanism: a field still on
#: ``DEFAULT`` is what the page lists and the pre-flight warns about.
ANSWERED = (ENTERED, CAPTURED, MEASURED)


# -- how a value is normally supplied --------------------------------------

#: Typed by a human.
BY_HAND = "by_hand"
#: One of a small set of words. Offered instead of a number where a number
#: would invite a made-up one — nobody knows the RMS wave height, but everybody
#: can tell glassy from choppy.
BY_CHOICE = "by_choice"
#: Taken from the vessel's own position. Put the boat beside the tripod and
#: press the button: the boat is a GNSS receiver and you are standing next to
#: the thing you are trying to locate.
FROM_VESSEL_POSITION = "from_vessel_position"
#: Taken from the bearing from the station to the vessel. Sail out, aim the
#: antenna at the boat, press the button. More accurate than a compass reading
#: and it measures the thing rather than a proxy for it.
FROM_VESSEL_BEARING = "from_vessel_bearing"
#: Taken from the first fix of the mission, automatically.
FROM_FIRST_FIX = "from_first_fix"


# -- when a value actually starts being used -------------------------------
#
# The weakest part of this whole design, named rather than hidden. ROS nodes
# read their configuration at startup, so a value saved on the page may not be
# the value the vessel is running on — and a page that writes a file and says
# "done" while the node uses the old numbers is precisely the class of problem
# the page was built to fix. It would be worse than the SSH session it
# replaces, because it also destroys the operator's reason to doubt.
#
# So every field says when it takes effect, the page says it per field, and
# where the vessel can be made to pick a value up, the page offers to do it
# rather than telling somebody to open a terminal.

#: Read live. Nothing to do.
IMMEDIATELY = "immediately"
#: The owning node can be told to re-read it, without restarting the process.
#: `reload_service` names the service that does it.
ON_RELOAD = "on_reload"
#: The process has to be restarted, and this page cannot do that. Said
#: plainly rather than implied — the operator needs to know the value they
#: just entered is not yet in use.
ON_RESTART = "on_restart"


@dataclass(frozen=True)
class FieldSpec:
    """One thing somebody has to tell the vessel."""

    id: str
    tier: str
    label: str
    default: Any
    units: str = ""
    #: What goes wrong if this is wrong, in the operator's terms. Shown on the
    #: page beside the field, because "tilt_deg" tells nobody why they should
    #: get off the pontoon and fetch a tape measure.
    consequence: str = ""
    #: True when a wrong value produces plausible-looking wrong data rather
    #: than an obvious failure. See the module docstring — this is narrower
    #: than "important", and it is the ranking that decides what an operator
    #: does first.
    silently_corrupts: bool = False
    supplied: str = BY_HAND
    #: For BY_CHOICE fields, the words on offer.
    choices: tuple[str, ...] = ()
    #: When a newly saved value starts being used. See the constants above.
    takes_effect: str = IMMEDIATELY
    #: Which node reads it. Named so the page can say "the sonar bridge" and
    #: not "some node".
    applied_by: str = "gui_backend"
    #: The service that makes the owning node re-read it, for ON_RELOAD
    #: fields. Empty otherwise.
    reload_service: str = ""

    @property
    def goes_stale(self) -> bool:
        return self.tier == TIER_DEPLOYMENT

    @property
    def has_default(self) -> bool:
        """Whether there is anything to fall back on.

        Two quite different kinds of "not set" hide under one word, and an
        operator arriving on a beach needs them apart. A sonar tilt of 35
        degrees is a *guess* — usable, plausible, and quite possibly wrong.
        A shore station position is *nothing*: there is no sensible default
        for where a tripod is standing, so the feature simply does not work
        until somebody says.

        The first is a survey that may be silently displaced. The second is a
        panel that cannot draw. They want different words and a different
        order of work.
        """
        return self.default is not None


def _mounting(name: str, label: str, default: float, units: str) -> FieldSpec:
    return FieldSpec(
        id=f"vessel.sonar.mounting.{name}",
        tier=TIER_VESSEL,
        label=label,
        default=default,
        units=units,
        consequence=(
            "A systematic offset on every sounding. It does not average out "
            "and it does not look like noise — the whole survey is displaced, "
            "consistently, and looks perfectly plausible until somebody "
            "overlays a second one."
        ),
        silently_corrupts=True,
        takes_effect=ON_RELOAD,
        applied_by="omniscan_bridge",
        reload_service="/omniscan_bridge/reload_mounting",
    )


#: Everything the page collects. Order is the order it is presented in, which
#: is roughly "what blocks what" rather than alphabetical or by category.
FIELDS: tuple[FieldSpec, ...] = (
    # -- vessel: measured once on a hull and kept ------------------------
    _mounting("tilt_deg", "Sonar downward tilt", 35.0, "deg"),
    _mounting("yaw_deg", "Sonar yaw from square", 0.0, "deg"),
    _mounting("pitch_deg", "Sonar nose-up pitch", 0.0, "deg"),
    _mounting("lever_x_m", "Sonar lever arm, forward of GNSS", -0.20, "m"),
    _mounting("lever_y_m", "Sonar lever arm, port of GNSS", -0.35, "m"),
    _mounting("lever_z_m", "Sonar lever arm, above GNSS", -0.15, "m"),
    FieldSpec(
        id="vessel.sonar.side",
        tier=TIER_VESSEL,
        label="Side the sonar looks",
        default="starboard",
        consequence=(
            "The coverage ribbon is painted on the wrong side of the track, so "
            "the map shows ground that was never surveyed as surveyed."
        ),
        silently_corrupts=True,
        supplied=BY_CHOICE,
        choices=("port", "starboard"),
        takes_effect=ON_RELOAD,
        applied_by="omniscan_bridge",
        reload_service="/omniscan_bridge/reload_mounting",
    ),
    FieldSpec(
        id="vessel.battery.capacity_wh",
        tier=TIER_VESSEL,
        label="Battery capacity",
        default=1200.0,
        units="Wh",
        consequence=(
            "Endurance is arithmetic on this. Wrong here is a confident wrong "
            "number telling somebody they have forty minutes left."
        ),
    ),
    FieldSpec(
        id="vessel.battery.hotel_load_w",
        tier=TIER_VESSEL,
        label="Hotel load",
        default=85.0,
        units="W",
        consequence="Endurance, again. Everything that is not propulsion.",
    ),
    FieldSpec(
        id="vessel.radio.antenna_height_m",
        tier=TIER_VESSEL,
        label="Boat antenna height above water",
        default=1.0,
        units="m",
        consequence=(
            "Dominates the link's useful range: the two-ray term goes as the "
            "product of the two antenna heights, so this is worth more than "
            "any transmit power we are allowed to use."
        ),
    ),
    # -- deployment: set up again on every beach --------------------------
    FieldSpec(
        id="deployment.station.lat",
        tier=TIER_DEPLOYMENT,
        label="Shore station latitude",
        default=None,
        units="deg",
        consequence=(
            "Without it the link panel cannot show range or bearing from the "
            "sector, so a link about to fail looks exactly like one that is "
            "fine."
        ),
        supplied=FROM_VESSEL_POSITION,
    ),
    FieldSpec(
        id="deployment.station.lon",
        tier=TIER_DEPLOYMENT,
        label="Shore station longitude",
        default=None,
        units="deg",
        consequence="As above; the two are captured together.",
        supplied=FROM_VESSEL_POSITION,
    ),
    FieldSpec(
        id="deployment.station.boresight_deg",
        tier=TIER_DEPLOYMENT,
        label="Shore station bearing",
        default=None,
        units="deg",
        consequence=(
            "The number that turns 'the link dropped' into 'the link is going "
            "to drop'. Without it nobody can see the boat working towards the "
            "edge of the sector."
        ),
        supplied=FROM_VESSEL_BEARING,
    ),
    FieldSpec(
        id="deployment.station.height_m",
        tier=TIER_DEPLOYMENT,
        label="Tripod height above water",
        default=2.9,
        units="m",
        consequence="Sets where the close-in multipath nulls fall, and the far cliff.",
    ),
    FieldSpec(
        id="deployment.sea.state",
        tier=TIER_DEPLOYMENT,
        label="Sea state",
        default="slight",
        consequence=(
            "Decides whether the link's multipath nulls exist at all. On a "
            "glassy morning the link drops out and comes back at a fixed "
            "range, which looks like a fault and is not one."
        ),
        supplied=BY_CHOICE,
        choices=("glassy", "slight", "moderate"),
    ),
    FieldSpec(
        id="deployment.radio.channel_width_mhz",
        tier=TIER_DEPLOYMENT,
        label="Radio channel width",
        default=40.0,
        units="MHz",
        consequence=(
            "Three decibels of range per doubling, against twice the "
            "throughput. A real trade and worth making deliberately."
        ),
        supplied=BY_CHOICE,
        choices=("20", "40", "80"),
    ),
    FieldSpec(
        id="deployment.origin.lat",
        tier=TIER_DEPLOYMENT,
        label="Local origin latitude",
        default=None,
        units="deg",
        consequence=(
            "The datum every recorded metre is relative to. Move it and the "
            "whole mission moves with it, silently."
        ),
        silently_corrupts=True,
        supplied=FROM_FIRST_FIX,
    ),
    FieldSpec(
        id="deployment.origin.lon",
        tier=TIER_DEPLOYMENT,
        label="Local origin longitude",
        default=None,
        units="deg",
        consequence="As above; the two are derived together.",
        silently_corrupts=True,
        supplied=FROM_FIRST_FIX,
    ),
    FieldSpec(
        id="deployment.sonar.range_m",
        tier=TIER_DEPLOYMENT,
        label="Sonar range",
        default=30.0,
        units="m",
        consequence=(
            "Too short and the bottom is outside the window. Obvious within a "
            "minute of pinging, which is why this is not in the group that "
            "ruins a survey quietly."
        ),
    ),
    FieldSpec(
        id="deployment.sonar.gain",
        tier=TIER_DEPLOYMENT,
        label="Sonar gain",
        default=4.0,
        consequence="Too low and the returns are weak; visible on the sonar panel.",
    ),
)

FIELD_BY_ID: dict[str, FieldSpec] = {spec.id: spec for spec in FIELDS}


# -- values ----------------------------------------------------------------


@dataclass(frozen=True)
class FieldValue:
    """One answer, and everything about how it was arrived at."""

    value: Any
    provenance: str = DEFAULT
    set_utc_ms: int | None = None
    set_by: str = ""
    #: Free text recorded with the answer. For a capture this is where the fix
    #: quality goes — "8 satellites, HDOP 1.1" — because a captured value that
    #: looks identical to a measured one is the same class of lie as a
    #: predicted signal shown as a measurement.
    note: str = ""

    @property
    def answered(self) -> bool:
        return self.provenance in ANSWERED

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "provenance": self.provenance,
            "set_utc_ms": self.set_utc_ms,
            "set_by": self.set_by,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> FieldValue:
        provenance = raw.get("provenance", DEFAULT)
        if provenance not in PROVENANCES:
            # An unrecognised provenance is treated as no answer at all. The
            # alternative — believing it — would let a typo promote a guess to
            # a measurement, which is the one direction this must never fail.
            provenance = DEFAULT
        set_utc = raw.get("set_utc_ms")
        return cls(
            value=raw.get("value"),
            provenance=provenance,
            set_utc_ms=int(set_utc) if isinstance(set_utc, (int, float)) else None,
            set_by=str(raw.get("set_by") or ""),
            note=str(raw.get("note") or ""),
        )


@dataclass(frozen=True)
class Outstanding:
    """A field nobody has answered, with enough to act on it."""

    spec: FieldSpec

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def silently_corrupts(self) -> bool:
        return self.spec.silently_corrupts

    @property
    def blocking(self) -> bool:
        """Nothing to fall back on — this does not work until somebody says."""
        return not self.spec.has_default


@dataclass(frozen=True)
class PendingEffect:
    """A saved value that the vessel is not using yet, and how to change that.

    Grouped by the action needed rather than listed per field, because a
    tape-measure session changes seven mounting numbers at once and seven
    identical "reload the sonar bridge" lines is seven chances to read past
    the one that matters.
    """

    takes_effect: str
    applied_by: str
    reload_service: str
    field_ids: tuple[str, ...]

    @property
    def can_be_applied_from_here(self) -> bool:
        return self.takes_effect == ON_RELOAD and bool(self.reload_service)

    def sentence(self) -> str:
        """What the page says. One sentence, in the operator's terms."""
        count = len(self.field_ids)
        noun = "value" if count == 1 else f"{count} values"
        subject = f"{'This ' + noun if count == 1 else noun}"
        if self.takes_effect == ON_RELOAD:
            return (
                f"{subject} saved, but {self.applied_by} is still using the "
                f"old {'one' if count == 1 else 'ones'} until it re-reads them."
            )
        return (
            f"{subject} saved, but {self.applied_by} reads this at startup and "
            "has not been restarted. This page cannot restart it."
        )


@dataclass(frozen=True)
class Stale:
    """A Deployment value old enough to be worth looking at again."""

    spec: FieldSpec
    value: FieldValue
    age_ms: int


# -- the profile -----------------------------------------------------------


@dataclass
class SetupProfile:
    """Every answer this vessel has been given.

    Values are held per field rather than merged into a settings blob, because
    the provenance and the date are as much the point as the number — and a
    blob loses both the moment somebody writes a plain dict into it.
    """

    values: dict[str, FieldValue] = field(default_factory=dict)
    written_utc_ms: int | None = None
    written_by: str = ""
    #: Field ids found in a loaded file that this version does not know about.
    #:
    #: Kept and reported rather than dropped. ``mounting.yaml`` taught this:
    #: its pre-flight check reports unrecognised entries because they are
    #: almost always a typo in a value somebody *did* measure, and silently
    #: ignoring them means the measurement never reaches the sonar and nobody
    #: finds out.
    unknown_fields: tuple[str, ...] = ()

    # -- reading ----------------------------------------------------------

    def entry(self, field_id: str) -> FieldValue:
        """The answer for a field, or the shipped default as an unanswered one."""
        existing = self.values.get(field_id)
        if existing is not None:
            return existing
        spec = FIELD_BY_ID.get(field_id)
        if spec is None:
            raise KeyError(f"no such setup field: {field_id!r}")
        return FieldValue(value=spec.default, provenance=DEFAULT)

    def get(self, field_id: str) -> Any:
        return self.entry(field_id).value

    def answered(self, field_id: str) -> bool:
        return self.entry(field_id).answered

    # -- writing ----------------------------------------------------------

    def set(
        self,
        field_id: str,
        value: Any,
        provenance: str,
        *,
        utc_ms: int,
        by: str = "",
        note: str = "",
    ) -> SetupProfile:
        """Answer a field. Returns a new profile; this one is not modified.

        Immutable because the page will want to show what would change before
        it is committed, and because a half-applied setup — some fields
        written, some not — is a state no file describes and nobody could
        debug.
        """
        spec = FIELD_BY_ID.get(field_id)
        if spec is None:
            raise KeyError(f"no such setup field: {field_id!r}")
        if provenance not in ANSWERED:
            raise ValueError(
                f"{provenance!r} is not an answer; use one of {ANSWERED}. "
                "Clearing a field is clear(), which is a different intent."
            )
        if spec.choices and str(value) not in spec.choices:
            raise ValueError(
                f"{field_id} must be one of {spec.choices}, not {value!r}"
            )
        values = dict(self.values)
        values[field_id] = FieldValue(value, provenance, utc_ms, by, note)
        return replace(self, values=values)

    def clear(self, field_id: str) -> SetupProfile:
        """Forget an answer, returning the field to its shipped default.

        Separate from ``set`` because it is a different intent, and because a
        field cleared by mistake must show up in ``outstanding`` immediately
        rather than keeping a stale value that nobody realises is stale.
        """
        if field_id not in FIELD_BY_ID:
            raise KeyError(f"no such setup field: {field_id!r}")
        values = {k: v for k, v in self.values.items() if k != field_id}
        return replace(self, values=values)

    # -- what still needs doing -------------------------------------------

    def outstanding(self, tier: str | None = None) -> list[Outstanding]:
        """Fields nobody has answered, worst first.

        Ordered by whether being wrong ruins a survey quietly, because that is
        the order somebody with limited daylight should work in — and because
        an undifferentiated list is what the current all-amber pre-flight
        already produces, to no effect.
        """
        out = [
            Outstanding(spec)
            for spec in FIELDS
            if (tier is None or spec.tier == tier) and not self.answered(spec.id)
        ]
        # Nothing-at-all before wrong-quietly before everything else. A field
        # with no default does not work at all, which is at least visible; a
        # field on a plausible guess is the one that ruins a survey without
        # anybody noticing, and it comes second only because the first group
        # stops a panel from drawing at all.
        out.sort(key=lambda o: (not o.blocking, not o.silently_corrupts, o.spec.id))
        return out

    def stale(self, now_utc_ms: int) -> list[Stale]:
        """Deployment answers old enough to be worth a second look.

        Vessel values never appear here however old they are. That is the
        point of the tiers: mounting geometry measured in July is still true
        in September, and a station position from July is almost certainly a
        different beach.
        """
        out = []
        for spec in FIELDS:
            if not spec.goes_stale:
                continue
            entry = self.entry(spec.id)
            if not entry.answered or entry.set_utc_ms is None:
                continue
            age = now_utc_ms - entry.set_utc_ms
            if age > DEPLOYMENT_STALE_AFTER_MS:
                out.append(Stale(spec, entry, age))
        out.sort(key=lambda s: -s.age_ms)
        return out

    def pending_effects(self, field_ids: list[str] | tuple[str, ...]) -> list[PendingEffect]:
        """What the vessel is not using yet, out of what was just saved.

        The page calls this straight after a save and shows the result before
        anything else. A page that writes a file and says "done" while the
        node runs on the old values is the same failure as a pre-flight
        warning naming a YAML nobody can open: it looks like the job is
        finished, and it is not.

        Fields read live produce nothing here, which is the common case and
        should stay silent.
        """
        groups: dict[tuple[str, str, str], list[str]] = {}
        for field_id in field_ids:
            spec = FIELD_BY_ID.get(field_id)
            if spec is None or spec.takes_effect == IMMEDIATELY:
                continue
            key = (spec.takes_effect, spec.applied_by, spec.reload_service)
            groups.setdefault(key, []).append(field_id)

        return [
            PendingEffect(takes_effect, applied_by, reload_service, tuple(sorted(ids)))
            # Reload-able first: it is the one somebody can act on now, and
            # burying it under something they cannot do is how a fixable
            # problem gets treated as a fact of life.
            for (takes_effect, applied_by, reload_service), ids in sorted(
                groups.items(), key=lambda kv: (kv[0][0] != ON_RELOAD, kv[0][1])
            )
        ]

    def headline(self) -> str:
        """The sentence at the top of the page.

        Generated here rather than written in the frontend so that the page,
        the pre-flight and anything else say the same thing — and so that the
        ranking cannot drift apart from the ranking the data actually carries.
        """
        missing = self.outstanding()
        if not missing:
            return "Everything has been filled in."

        blocking = [o for o in missing if o.blocking]
        guesses = [o for o in missing if not o.blocking]
        corrupting = [o for o in guesses if o.silently_corrupts]

        parts = []
        if blocking:
            parts.append(
                f"{_count(len(blocking), 'value has', 'values have')} never been "
                "set, and there is no sensible default for them."
            )
        if corrupting:
            parts.append(
                f"{_count(len(corrupting), 'value is', 'values are')} still on a "
                "guess that can ruin a survey without it looking wrong."
            )
        elif guesses:
            parts.append(
                f"{_count(len(guesses), 'value is', 'values are')} still on a "
                "default nobody has checked."
            )
        if corrupting and len(guesses) > len(corrupting):
            rest = len(guesses) - len(corrupting)
            parts.append(
                f"{_count(rest, 'other is', 'others are')} on unchecked defaults "
                "that only make the screen less useful."
            )
        return " ".join(parts)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "written_utc_ms": self.written_utc_ms,
            "written_by": self.written_by,
            "fields": {
                field_id: value.to_dict()
                for field_id, value in sorted(self.values.items())
            },
        }

    @classmethod
    def from_dict(cls, raw: dict) -> SetupProfile:
        fields = raw.get("fields") or {}
        values: dict[str, FieldValue] = {}
        unknown: list[str] = []
        for field_id, entry in fields.items():
            if field_id not in FIELD_BY_ID:
                unknown.append(field_id)
                continue
            if isinstance(entry, dict):
                values[field_id] = FieldValue.from_dict(entry)
        written = raw.get("written_utc_ms")
        return cls(
            values=values,
            written_utc_ms=int(written) if isinstance(written, (int, float)) else None,
            written_by=str(raw.get("written_by") or ""),
            unknown_fields=tuple(sorted(unknown)),
        )

    def content_hash(self) -> str:
        """A short digest of the answers, ignoring when they were written.

        Not security. It is so the pre-flight can say "this is the setup that
        was pushed", and so a vessel can tell that the file it loaded is the
        file somebody thought they sent — which matters most in the case this
        page exists to fix, where a value appears to have been saved and the
        node is still running on the old one.
        """
        canonical = json.dumps(
            {
                field_id: [entry.value, entry.provenance]
                for field_id, entry in sorted(self.values.items())
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _count(n: int, singular: str, plural: str) -> str:
    """"One value has", "three values have". Numbers under ten read as words
    on a page somebody is skim-reading in sunlight."""
    words = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five",
             6: "Six", 7: "Seven", 8: "Eight", 9: "Nine"}
    return f"{words.get(n, n)} {singular if n == 1 else plural}"


def default_profile() -> SetupProfile:
    """A vessel nobody has told anything. Every field on its shipped default."""
    return SetupProfile()
