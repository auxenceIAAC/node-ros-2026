"""The setup page's one payload, and the only shape the frontend knows.

## Why this is HTTP and not a stream

Everything else the GUI shows is a measurement arriving at some rate, and the
link profile decides which of them a client gets. Setup is neither. It is a
document somebody edits, it changes when a human changes it, and — the part
that decides this — **it is needed most when the link is worst**. A setup page
that was negotiated away on a `minimal` profile would disappear exactly when
somebody is trying to work out why the link is minimal.

So it is a plain `GET`, outside the subscription machinery, and the page can
re-ask after a save instead of waiting for a push.

## What is in it, and what is deliberately not

The field inventory travels with the values. There is no second copy of the
labels, the consequences, the units or the choices in the frontend: a field
added to `asket_common.setup_profile.FIELDS` appears on the page with its
consequence text, and one removed stops appearing, without a line of
JavaScript changing. The alternative — a JS table mirroring the Python one —
is the exact failure the mounting YAML already demonstrated, where the thing
on screen and the thing in use drifted apart and nobody could see it.

`headline`, `outstanding` and `stale` are computed here rather than in the
page, for the same reason: the ranking of "nothing at all" over "a guess that
ruins a survey quietly" over "an unchecked default" is a judgement with
consequences, and it must be the same judgement in the page, the pre-flight
and anything else that asks.

Two things are not here. **The gate** is not, because it depends on live
vessel state that the client already receives on the `pico` and `mission`
streams, and `setup_gate.evaluate` is pure — the page asks it with the state it
already has, the backend asks it again at dispatch, and they cannot disagree.
And **no secret or credential** is here; this document is read by anything that
can open the page.
"""

from __future__ import annotations

from asket_common.setup_profile import (
    ANSWERED,
    DEPLOYMENT_STALE_AFTER_MS,
    ENTERED,
    FIELD_BY_ID,
    FIELDS,
    TIER_BLURBS,
    TIER_LABELS,
    TIERS,
    SetupProfile,
)


def field_dict(spec) -> dict:
    """One field, with everything the page needs to render and explain it.

    ``consequence`` is included for every field, not only the dangerous ones.
    "Sonar lever arm, forward of GNSS" tells somebody what to type and nothing
    about whether it is worth getting off the pontoon with a tape measure;
    that sentence is the whole reason a value gets measured rather than
    guessed.
    """
    return {
        "id": spec.id,
        "tier": spec.tier,
        "label": spec.label,
        "default": spec.default,
        "units": spec.units,
        "consequence": spec.consequence,
        "silently_corrupts": spec.silently_corrupts,
        "supplied": spec.supplied,
        "choices": list(spec.choices),
        "takes_effect": spec.takes_effect,
        "applied_by": spec.applied_by,
        "reload_service": spec.reload_service,
        # Derived, and sent rather than recomputed in the page: "there is no
        # sensible default for where a tripod is standing" is the distinction
        # the headline is built on, and two implementations of it could rank
        # the work differently.
        "has_default": spec.has_default,
        "goes_stale": spec.goes_stale,
    }


def tier_dicts() -> list[dict]:
    return [
        {"id": tier, "label": TIER_LABELS[tier], "blurb": TIER_BLURBS[tier]}
        for tier in TIERS
    ]


def payload(
    profile: SetupProfile,
    now_utc_ms: int,
    file_state: dict | None = None,
) -> dict:
    """Everything the setup page renders, in one document.

    ``effective`` is the answer to "what is the vessel actually using", with
    shipped defaults filled in. It is sent alongside ``values`` rather than
    merged into it, because a field's value and whether anybody has ever
    confirmed that value are different questions and the page shows both. A
    single merged dict is what loses the second one.
    """
    return {
        "tiers": tier_dicts(),
        "fields": [field_dict(spec) for spec in FIELDS],
        # Only what somebody has answered. A field absent here is on its
        # shipped default and nobody has said so, which is what the page
        # marks.
        "values": {
            field_id: entry.to_dict() for field_id, entry in sorted(profile.values.items())
        },
        "effective": {spec.id: profile.get(spec.id) for spec in FIELDS},
        "headline": profile.headline(),
        "outstanding": [
            {
                "id": item.id,
                "blocking": item.blocking,
                "silently_corrupts": item.silently_corrupts,
            }
            for item in profile.outstanding()
        ],
        "stale": [
            {"id": item.spec.id, "set_utc_ms": item.value.set_utc_ms, "age_ms": item.age_ms}
            for item in profile.stale(now_utc_ms)
        ],
        "stale_after_ms": DEPLOYMENT_STALE_AFTER_MS,
        "written_utc_ms": profile.written_utc_ms,
        "written_by": profile.written_by,
        "content_hash": profile.content_hash(),
        # Kept and shown rather than dropped. An unrecognised field id is
        # almost always a typo in a value somebody did measure, and silently
        # ignoring it means the measurement never reaches the vessel and
        # nobody finds out.
        "unknown_fields": list(profile.unknown_fields),
        # Where the answers are stored and whether that worked. "No file at
        # all" and "a file that will not parse" are different problems on a
        # beach, and the page has to be able to say which.
        "file": file_state or {},
        "server_utc_ms": now_utc_ms,
    }


class EditError(ValueError):
    """An edit the vessel will not accept, with a sentence for the operator."""


def _coerce(spec, raw):
    """Turn what a browser sent into what the field holds.

    Every value arriving from an HTML input is a string, and a setup file whose
    sonar tilt is the string ``"35"`` would load, pass every check, and reach
    the sonar bridge as something that is not a number. Coerced here, once,
    rather than in each consumer — and refused rather than guessed at, because
    a tilt that silently became 0.0 is the precise failure this whole page is
    about.
    """
    if spec.choices:
        value = str(raw)
        if value not in spec.choices:
            raise EditError(
                f"{spec.label} must be one of {', '.join(spec.choices)}, not {raw!r}."
            )
        return value
    if isinstance(spec.default, bool) or (spec.default is None and isinstance(raw, bool)):
        return bool(raw)
    if isinstance(spec.default, float) or spec.default is None:
        # `None` defaults are the fields with nothing to fall back on — the
        # station position and the rest — and every one of them is a number.
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise EditError(
                f"{spec.label} has to be a number. {raw!r} is not one."
            ) from None
    if isinstance(spec.default, int):
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise EditError(
                f"{spec.label} has to be a whole number. {raw!r} is not one."
            ) from None
    return str(raw)


def apply_edits(
    profile: SetupProfile,
    edits: dict,
    *,
    utc_ms: int,
    by: str = "",
) -> tuple[SetupProfile, tuple[str, ...]]:
    """Answer a set of fields, returning the new profile and what changed.

    All of them or none: the profile is immutable and the caller writes the
    result, so a half-applied edit — four mounting numbers saved and three
    rejected — cannot reach the file. That state is one no file describes and
    nobody could debug, and a tape-measure session changes seven numbers at
    once.

    An edit whose value equals the one already answered, with the same
    provenance, is dropped rather than rewritten. Otherwise opening the page
    and pressing save would restamp every date, and the staleness dates — the
    only thing that says "this station position is from a different beach" —
    would reset to today on a change nobody made.
    """
    changed: list[str] = []
    out = profile
    for field_id, raw in edits.items():
        spec = FIELD_BY_ID.get(field_id)
        if spec is None:
            raise EditError(f"There is no setup field called {field_id!r}.")

        if isinstance(raw, dict):
            value = raw.get("value")
            provenance = str(raw.get("provenance") or ENTERED)
            note = str(raw.get("note") or "")
        else:
            value, provenance, note = raw, ENTERED, ""

        if provenance not in ANSWERED:
            raise EditError(
                f"{provenance!r} is not a way of answering a field. "
                f"Use one of {', '.join(ANSWERED)}."
            )

        coerced = _coerce(spec, value)
        existing = profile.entry(field_id)
        if existing.answered and existing.value == coerced and existing.provenance == provenance:
            continue

        out = out.set(field_id, coerced, provenance=provenance, utc_ms=utc_ms, by=by, note=note)
        changed.append(field_id)

    return out, tuple(changed)
