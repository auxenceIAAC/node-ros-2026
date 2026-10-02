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
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .geometry import SonarMounting

#: Fields taken as geometry; anything else in the file is provenance or noise.
_GEOMETRY_FIELDS = (
    "tilt_deg", "yaw_deg", "pitch_deg",
    "lever_x_m", "lever_y_m", "lever_z_m",
)


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


def load_mounting(path: str | Path | None) -> tuple[SonarMounting, MountingProvenance]:
    """Read ``mounting.yaml``, and stamp the result with its fingerprint.

    A wrapper rather than eight edits: :func:`_load_mounting` returns from a
    good many places — missing file, unreadable, bad YAML, empty, each kind of
    malformed — and a fingerprint set at seven of them would be a fingerprint
    absent at the eighth, which is the one somebody would hit.
    """
    mounting, provenance = _load_mounting(path)
    provenance.fingerprint = geometry_fingerprint(mounting)
    return mounting, provenance


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
