"""Loading the mounting geometry, and knowing whether to trust it.

Two things live here, and the second is the reason the first is not just a
``yaml.safe_load`` at the call site.

**The numbers.** Tilt, yaw, pitch and the lever arm from the GNSS antenna to
the transducer. :mod:`omniscan_bridge.core.geometry` turns them into a point on
the seabed.

**Whether anybody measured them.** A lever arm is a systematic offset: it does
not average out, it does not look like noise, and the survey it displaces looks
entirely plausible. The failure mode this module exists to prevent is a
deployment on the transcribed defaults, discovered months later at the desk.

So the file carries its own provenance — ``measured``, by whom, when — and
:class:`MountingProvenance` is passed to the pre-flight check, which returns
WARN for as long as that flag is false (docs/open_questions.md Q2).

The bridge and the pre-flight check read the **same file**. A check that
validated a config the bridge did not use would be worse than no check at all.

**The setup overlay.** ``mounting.yaml`` is the package's own default and
lives in the source tree. The setup page writes a per-vessel file instead
(``asket_common.setup_profile.default_setup_path``), and this module applies it
*on top*, field by field. The page never edits ``mounting.yaml``: a page that
wrote into six packages' config files would have to know the layout of every
one of them, every future package would have to be taught about the page, and
a partly applied write would leave the vessel in a state no file describes.

Only **answered** overlay fields take effect. A field nobody has set carries
the shipped default in the profile, and letting that through would mean the
setup page silently reverting a measurement in ``mounting.yaml`` the moment
anybody saved anything at all.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from asket_common.setup_profile import (
    ANSWERED,
    MEASURED,
    SetupProfile,
    default_setup_path,
)

from .geometry import SonarMounting

#: Fields taken as geometry; anything else in the file is provenance or noise.
_GEOMETRY_FIELDS = (
    "tilt_deg", "yaw_deg", "pitch_deg",
    "lever_x_m", "lever_y_m", "lever_z_m",
)


#: Setup fields that place the transducer, and the geometry name each one
#: overrides. Written out rather than derived from a string prefix, so a field
#: renamed in `asket_common` fails a test here instead of quietly ceasing to
#: apply — which would look exactly like a reload that did not work.
SETUP_FIELD_FOR_GEOMETRY = {
    "tilt_deg": "vessel.sonar.mounting.tilt_deg",
    "yaw_deg": "vessel.sonar.mounting.yaw_deg",
    "pitch_deg": "vessel.sonar.mounting.pitch_deg",
    "lever_x_m": "vessel.sonar.mounting.lever_x_m",
    "lever_y_m": "vessel.sonar.mounting.lever_y_m",
    "lever_z_m": "vessel.sonar.mounting.lever_z_m",
}
SETUP_FIELD_FOR_SIDE = "vessel.sonar.side"


@dataclass
class OverlayProvenance:
    """What the setup file contributed, and what went wrong if anything.

    Kept apart from the base file's provenance rather than merged into it. "No
    overlay" and "an overlay that will not parse" and "`mounting.yaml` is
    malformed" are three different problems that send somebody to three
    different places, and one `error` string for all of them would be the kind
    of message that gets read as "config broken" and ignored.
    """

    path: str = ""
    #: There is a file and it was read.
    found: bool = False
    #: No file. The ordinary state on a vessel nobody has set up from the page,
    #: and not a fault: the base config applies unchanged.
    missing: bool = False
    #: The file exists and could not be used. Dangerous in the same way the
    #: base file's error is: it usually means somebody measured the vessel on
    #: the setup page and the numbers are being ignored.
    error: str = ""
    #: Geometry names this overlay actually supplied, in the order they are
    #: applied. Empty when a file was read but nothing in it was answered.
    applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "found": self.found,
            "missing": self.missing,
            "error": self.error,
            "applied": list(self.applied),
        }


@dataclass
class MountingProvenance:
    """Where the numbers came from, and how much they are worth."""

    #: Somebody measured this vessel and said so. Until then: PROVISIONAL.
    measured: bool = False
    measured_by: str = ""
    measured_utc: str = ""
    notes: str = ""

    path: str = ""
    #: The file was found and parsed. False means the geometry below is the
    #: hard-coded default, which is a worse position to be in than PROVISIONAL:
    #: there is not even a file to point at.
    found: bool = False
    #: No file at that path, or no path configured at all. Distinct from
    #: ``error``: nothing was measured, so nothing is being ignored — it is the
    #: same hazard as PROVISIONAL, without even a file to point at.
    missing: bool = False
    #: Non-empty when the file exists but could not be used. This is the most
    #: dangerous of the states — it usually means somebody *did* measure the
    #: vessel and the numbers are being silently ignored in favour of defaults.
    error: str = ""
    #: Fields present in the file but not recognised. Almost always a typo in a
    #: measured value, which would otherwise be discarded in silence.
    unknown_fields: list[str] = field(default_factory=list)
    #: A short digest of the geometry actually in use.
    #:
    #: Exists so that "the node reloaded" can be confirmed rather than
    #: inferred. The setup page knows the digest of what it wrote; when the
    #: bridge reports the same one, the numbers somebody measured are the
    #: numbers the sonar is being placed by — and not before. Safety rule 4
    #: applied to configuration: displayed state is confirmed state, and a
    #: file having been written is not the vessel having read it.
    #:
    #: Of the geometry, deliberately, not of the whole file. Editing a comment
    #: or a `notes:` line does not change where the transducer is, and a
    #: confirmation that moved on cosmetic edits would train people to ignore
    #: it.
    fingerprint: str = ""
    #: What the setup page's file contributed on top of the base config.
    overlay: OverlayProvenance = field(default_factory=OverlayProvenance)
    #: The numbers actually in use, after the overlay.
    #:
    #: Reported, not just digested. The fingerprint says *whether* the vessel
    #: is on the geometry somebody sent; this says *what* it is on, which is
    #: what lets the setup page work out the digest to wait for before it
    #: sends anything — and what lets a panel show a tilt rather than a hash.
    geometry: dict = field(default_factory=dict)

    @property
    def provisional(self) -> bool:
        return not self.measured

    def to_dict(self) -> dict:
        return {
            "measured": self.measured,
            "measured_by": self.measured_by,
            "measured_utc": self.measured_utc,
            "notes": self.notes,
            "path": self.path,
            "found": self.found,
            "missing": self.missing,
            "error": self.error,
            "unknown_fields": list(self.unknown_fields),
            "fingerprint": self.fingerprint,
            "overlay": self.overlay.to_dict(),
            "geometry": dict(self.geometry),
        }


def geometry_fingerprint(mounting: SonarMounting) -> str:
    """A short digest of the numbers that place the transducer.

    Rounded before hashing, to the precision anybody could actually measure to:
    a lever arm is quoted to the centimetre and a tilt to a tenth of a degree,
    so a float that differs in its sixteenth decimal place is the same
    measurement and must not read as a different one.
    """
    canonical = ",".join(
        f"{round(float(getattr(mounting, name)), 4):.4f}" for name in _GEOMETRY_FIELDS
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def load_mounting(
    path: str | Path | None,
    setup_path: str | Path | None = None,
    *,
    use_overlay: bool = True,
) -> tuple[SonarMounting, MountingProvenance]:
    """Read ``mounting.yaml``, apply the setup overlay, and stamp the result.

    Three steps in one function because the fingerprint must be of what is
    *actually in use*. :func:`_load_mounting` returns from a good many places —
    missing file, unreadable, bad YAML, empty, each kind of malformed — and the
    overlay can change the geometry after any of them, so a fingerprint taken
    anywhere but here would describe something the sonar is not being placed
    by. That is the one thing it must never do: the setup page says a change
    took effect when the node reports this digest, and a digest of the wrong
    numbers is a confirmation of a change that did not happen.

    ``setup_path`` defaults to the shared location. ``use_overlay=False`` is
    for reading the package default on its own, which is what the pre-flight
    wants when it reports where each number came from.
    """
    mounting, provenance = _load_mounting(path)
    if use_overlay:
        mounting = _apply_overlay(
            mounting,
            provenance,
            setup_path if setup_path is not None else default_setup_path(),
        )
    provenance.fingerprint = geometry_fingerprint(mounting)
    provenance.geometry = {
        **{name: float(getattr(mounting, name)) for name in _GEOMETRY_FIELDS},
        "side": mounting.side,
    }
    return mounting, provenance


def _apply_overlay(
    mounting: SonarMounting,
    provenance: MountingProvenance,
    setup_path: str | Path,
) -> SonarMounting:
    """Lay the setup page's answers over the package default, field by field.

    Only **answered** fields. A field nobody has set carries the shipped
    default inside the profile, and letting those through would mean that
    saving anything at all on the setup page silently reverted every
    measurement in ``mounting.yaml`` — the page would appear to work and would
    be destroying exactly the numbers it exists to collect.

    Never raises, for the same reason the base loader does not: a typo in a
    config file must not take the sonar bridge down at sea.
    """
    overlay = provenance.overlay
    overlay.path = str(setup_path)

    p = Path(setup_path)
    try:
        raw = yaml.safe_load(p.read_text())
    except FileNotFoundError:
        # The ordinary state on a vessel nobody has set up from the page. Not
        # a fault: the base config applies unchanged.
        overlay.missing = True
        return mounting
    except OSError as exc:
        overlay.error = f"cannot read: {exc.strerror or exc}"
        return mounting
    except yaml.YAMLError as exc:
        overlay.error = f"not valid YAML: {_one_line(exc)}"
        return mounting

    if raw is None:
        overlay.error = "file is empty"
        return mounting
    if not isinstance(raw, dict):
        overlay.error = f"expected a mapping, found {type(raw).__name__}"
        return mounting

    overlay.found = True
    profile = SetupProfile.from_dict(raw)

    values: dict[str, float] = {}
    measured_fields: list[str] = []
    for name, field_id in SETUP_FIELD_FOR_GEOMETRY.items():
        entry = profile.entry(field_id)
        if entry.provenance not in ANSWERED:
            continue
        try:
            values[name] = float(entry.value)
        except (TypeError, ValueError):
            # Refused, not coerced. A lever arm that silently became 0.0
            # because somebody typed a stray character is the exact failure
            # the whole provenance apparatus exists to prevent, and it would
            # look like a successful reload.
            overlay.error = f"{field_id} is not a number: {entry.value!r}"
            return mounting
        overlay.applied.append(name)
        if entry.provenance == MEASURED:
            measured_fields.append(name)

    side = mounting.side
    side_entry = profile.entry(SETUP_FIELD_FOR_SIDE)
    if side_entry.provenance in ANSWERED:
        if side_entry.value not in ("port", "starboard"):
            overlay.error = (
                f"{SETUP_FIELD_FOR_SIDE} must be 'port' or 'starboard', "
                f"found {side_entry.value!r}"
            )
            return mounting
        side = str(side_entry.value)
        overlay.applied.append("side")

    if not overlay.applied:
        # A file that exists and answers none of the geometry. Reported by
        # `applied` being empty rather than by pretending there was no file:
        # "the overlay is there and said nothing about the sonar" is a
        # different thing from "there is no overlay".
        return mounting

    # `measured` is per-field now, so the single flag has to be earned by all
    # six. A geometry where the tilt was measured on the page and the lever
    # arm is still an unchecked default is not a measured geometry, and the
    # pre-flight's WARN is exactly right to stay up.
    base_was_measured = provenance.measured
    provenance.measured = all(
        # Measured on the page, or left to a base file that claims it was
        # measured. Anything else — an ENTERED value, a shipped default, a
        # base file with `measured: false` — and the whole geometry is not
        # measured.
        name in measured_fields or (name not in values and base_was_measured)
        for name in SETUP_FIELD_FOR_GEOMETRY
    )
    if measured_fields:
        newest = max(
            (profile.entry(SETUP_FIELD_FOR_GEOMETRY[name]) for name in measured_fields),
            key=lambda e: e.set_utc_ms or 0,
        )
        provenance.measured_by = newest.set_by or provenance.measured_by
        if newest.note:
            provenance.notes = newest.note

    return replace_mounting(mounting, side=side, **values)


def replace_mounting(mounting: SonarMounting, **changes) -> SonarMounting:
    """A new geometry with some fields replaced.

    ``SonarMounting`` is a plain dataclass in a package that must not grow a
    dependency on ``dataclasses.replace``'s behaviour for subclasses, so this
    is explicit about reading the current values first. It also keeps
    :func:`_apply_overlay` readable about what survives from the base file.
    """
    current = {name: getattr(mounting, name) for name in _GEOMETRY_FIELDS}
    current["side"] = mounting.side
    current.update(changes)
    return SonarMounting(**current)


def _load_mounting(path: str | Path | None) -> tuple[SonarMounting, MountingProvenance]:
    """Read ``mounting.yaml``. Never raises — the caller gets a usable geometry
    and an honest account of where it came from.

    Raising here would take the sonar bridge down over a config typo, which is
    the wrong trade at sea. Instead the defaults are used, the reason is
    recorded, and the pre-flight check is the thing that makes noise about it.
    """
    if not path:
        return SonarMounting(), MountingProvenance(path="", found=False, missing=True)

    p = Path(path)
    prov = MountingProvenance(path=str(p))

    try:
        raw = yaml.safe_load(p.read_text())
    except FileNotFoundError:
        # Not an error: there is nothing to ignore. The pre-flight reports it
        # as unmeasured geometry, which is what it is.
        prov.missing = True
        return SonarMounting(), prov
    except OSError as exc:
        prov.error = f"cannot read: {exc.strerror or exc}"
        return SonarMounting(), prov
    except yaml.YAMLError as exc:
        prov.error = f"not valid YAML: {_one_line(exc)}"
        return SonarMounting(), prov

    if raw is None:
        prov.error = "file is empty"
        return SonarMounting(), prov
    if not isinstance(raw, dict):
        prov.error = f"expected a mapping, found {type(raw).__name__}"
        return SonarMounting(), prov

    values: dict[str, float] = {}
    for key in _GEOMETRY_FIELDS:
        if key not in raw:
            continue
        try:
            values[key] = float(raw[key])
        except (TypeError, ValueError):
            prov.error = f"{key} is not a number: {raw[key]!r}"
            return SonarMounting(), prov

    side = raw.get("side", "starboard")
    if side not in ("port", "starboard"):
        prov.error = f"side must be 'port' or 'starboard', found {side!r}"
        return SonarMounting(), prov

    known = set(_GEOMETRY_FIELDS) | {
        "side", "measured", "measured_by", "measured_utc", "notes",
    }
    prov.unknown_fields = sorted(k for k in raw if k not in known)

    prov.found = True
    prov.measured = bool(raw.get("measured", False))
    prov.measured_by = str(raw.get("measured_by") or "")
    prov.measured_utc = str(raw.get("measured_utc") or "")
    prov.notes = str(raw.get("notes") or "")

    return SonarMounting(side=side, **values), prov


def _one_line(exc: Exception) -> str:
    return " ".join(str(exc).split())


def mounting_state_from_keyvalues(kv: dict) -> dict | None:
    """Rebuild the mounting state from the node's diagnostic key/values.

    The inverse of what ``omniscan_bridge_node._diagnostics`` writes, and it
    lives beside it so the two cannot drift. It has two consumers now — the
    pre-flight, which asks whether anybody measured the hull, and the GUI
    backend, which waits for the geometry digest before saying a setup change
    took effect — and a second hand-written parser would be a reload that
    confirms in one of them and not the other.

    Returns ``None`` when these key/values are not an omniscan mounting report,
    so a caller can keep looking rather than treat an absent key as a false.

    Shaped exactly like :meth:`MountingProvenance.to_dict`, because that is
    what every reader already expects. A "state" that was nearly the dict would
    be worse than one that was obviously different.
    """
    if "mounting_path" not in kv:
        return None

    geometry: dict = {}
    for pair in (kv.get("mounting_geometry") or "").split(","):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        if key == "side":
            geometry[key] = value
            continue
        try:
            geometry[key] = float(value)
        except ValueError:
            # A field this version does not understand, or a malformed one.
            # Dropped rather than guessed at: the digest is computed from the
            # whole geometry, so a wrong value here would predict a digest that
            # never arrives, and the page would report a failed reload that in
            # fact succeeded.
            continue

    return {
        "path": kv.get("mounting_path", ""),
        "found": kv.get("mounting_found") == "True",
        "missing": kv.get("mounting_missing") == "True",
        "measured": kv.get("mounting_measured") == "True",
        "measured_by": kv.get("mounting_measured_by", ""),
        "measured_utc": kv.get("mounting_measured_utc", ""),
        "notes": kv.get("mounting_notes", ""),
        "error": kv.get("mounting_error", ""),
        "unknown_fields": [
            f for f in (kv.get("mounting_unknown_fields") or "").split(",") if f
        ],
        "fingerprint": kv.get("mounting_fingerprint", ""),
        "geometry": geometry,
        "overlay": {
            "path": kv.get("mounting_overlay_path", ""),
            "found": bool(kv.get("mounting_overlay_applied")),
            "missing": kv.get("mounting_overlay_missing") == "True",
            "error": kv.get("mounting_overlay_error", ""),
            "applied": [
                f for f in (kv.get("mounting_overlay_applied") or "").split(",") if f
            ],
        },
    }
