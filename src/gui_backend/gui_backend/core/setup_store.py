"""Reading and writing the setup file.

The model is in :mod:`asket_common.setup_profile`, which has no file handling
and no ROS so that the whole of it is testable on a bare Python. This is the
other half: YAML, a path, and the small number of ways loading can go wrong.

## One file, overlaying the packages' own defaults

The setup file does not replace ``mounting.yaml``, ``topics.yaml`` or
``mission_defaults.yaml``. Those stay as the shipped defaults each package
reads; this file is applied on top.

The alternative — the page writing directly into six packages' configuration
— was rejected for two reasons. It makes the page know the layout of every
package, so every future package has to be taught about the page. And a write
that fails half way leaves the vessel in a state no single file describes,
which is a debugging problem nobody should have on a beach.

## What loading is careful about

**A file that will not parse is a fault, not an absence.** Returning an empty
profile would silently put the vessel back on shipped defaults while somebody
believes their measurements are in use — which is the exact failure the
mounting check already guards against, and the worst of the three states it
can report. So a broken file raises, and the caller decides what to say.

**Fields this version does not recognise are kept and reported**, never
dropped. ``mounting.yaml`` taught that too: unrecognised entries are almost
always a typo in a value somebody *did* measure, and quietly ignoring them
means the measurement never reaches the instrument and nobody finds out.

**A missing file is not an error.** It is a vessel nobody has told anything,
which is a legitimate and expected state — a fresh Jetson on a beach — and
the page exists to get somebody out of it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import yaml
from asket_common.setup_profile import SetupProfile, default_setup_path

#: Where the setup file lives when nothing says otherwise.
#:
#: Resolved by ``asket_common`` rather than defined here, because
#: ``omniscan_bridge`` reads the same file as an overlay on its own config and
#: must not import this package to find out where it is. Two definitions of
#: this path would be a page writing to one file and a sonar bridge reading
#: another, with every symptom pointing at the reload.
DEFAULT_PATH = default_setup_path()

_HEADER = """\
# The mission setup for this vessel: what somebody has told it, and how.
#
# WRITTEN BY THE SETUP PAGE. Hand-editing works and is not forbidden, but the
# page is the reason this file exists — every value here used to live in a
# YAML somebody had to reach over SSH, which is why the sonar's mounting
# geometry went unmeasured for weeks while the pre-flight warned about it on
# every single run.
#
# Each value carries where it came from:
#
#   default   nobody has said; the value is whatever the repository shipped
#   entered   a human typed it
#   captured  the vessel supplied it (its own position, bearing, or first fix)
#   measured  a human measured the hull and said so
#
# A field still on `default` is what the page lists and the pre-flight warns
# about. Writing a number in here without changing its provenance therefore
# changes the value and NOT the warning, which is deliberate: the warning is
# about whether anybody checked, and only a person can answer that.
"""


class SetupFileError(RuntimeError):
    """The file exists and could not be used.

    Deliberately not the same as "there is no file". Somebody may well have
    measured the vessel and have their numbers silently ignored in favour of
    defaults, and that is a fault to report loudly rather than an absence to
    shrug at.
    """


def load(path: Path | str | None = None) -> SetupProfile:
    """Read the setup file, or return an untold vessel if there is none."""
    path = Path(path) if path is not None else DEFAULT_PATH
    if not path.is_file():
        return SetupProfile()

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise SetupFileError(f"{path} could not be read: {exc}") from exc

    if not isinstance(raw, dict):
        raise SetupFileError(
            f"{path} is not a mapping — it holds {type(raw).__name__}"
        )
    return SetupProfile.from_dict(raw)


def save(
    profile: SetupProfile,
    path: Path | str | None = None,
    *,
    utc_ms: int,
    by: str = "",
) -> SetupProfile:
    """Write the setup file, and return the profile as it was written.

    Written atomically — to a neighbouring temporary file, then renamed —
    because the one thing worse than no setup file is half of one. A power cut
    or a yanked cable mid-write would otherwise leave a file that parses,
    holds some of the answers, and looks entirely fine.
    """
    path = Path(path) if path is not None else DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    written = SetupProfile(
        values=dict(profile.values),
        written_utc_ms=int(utc_ms),
        written_by=by,
        unknown_fields=profile.unknown_fields,
    )
    document = written.to_dict()
    # Not security. It is so the pre-flight can say "this is the setup that was
    # pushed", and so a vessel can tell that what it loaded is what somebody
    # thought they sent — which matters most in the case this page exists to
    # fix, where a value looks saved and a node is still running the old one.
    document["content_hash"] = written.content_hash()

    body = _HEADER + "\n" + yaml.safe_dump(document, sort_keys=False)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(body)
    os.replace(temporary, path)
    return written


def describe(path: Path | str | None = None) -> dict:
    """What the pre-flight needs to know about the file, without raising.

    Shaped like the ``mounting`` state the existing checks already consume:
    a dict that says found / missing / error rather than a value or an
    exception, so a check can report the difference between "no file", "a file
    that will not load" and "a file with nothing answered in it". Those are
    three different problems and they send somebody to three different places.
    """
    path = Path(path) if path is not None else DEFAULT_PATH
    state: dict = {"path": str(path)}
    if not path.is_file():
        state.update(found=False, missing=True)
        return state

    try:
        profile = load(path)
    except SetupFileError as exc:
        state.update(found=True, error=str(exc))
        return state

    state.update(
        found=True,
        written_utc_ms=profile.written_utc_ms,
        written_by=profile.written_by,
        content_hash=profile.content_hash(),
        unknown_fields=list(profile.unknown_fields),
        outstanding=[o.id for o in profile.outstanding()],
    )
    return state


#: The trace of setup changes, beside the setup file itself.
#:
#: One JSON object per line, appended. Append-only and line-oriented on
#: purpose: a half-written line is one unreadable record rather than a
#: corrupted history, and a record is worth nothing if the act of adding the
#: next one can destroy it.
#:
#: Beside the setup file rather than inside it because they answer different
#: questions. The file says what the vessel believes now; this says who
#: changed what, when, and what the boat was doing at the time — which is the
#: question somebody opening a survey in three months actually has.
CHANGE_LOG_NAME = "asket_setup_changes.jsonl"


def change_log_path(path: Path | str | None = None) -> Path:
    setup_path = Path(path) if path is not None else DEFAULT_PATH
    return setup_path.parent / CHANGE_LOG_NAME


def append_change(record, path: Path | str | None = None) -> Path:
    """Append one change record. Returns the log's path.

    Never raises on a full or read-only disk: a setup change that succeeded
    must not be reported as failed because its trace could not be written, and
    the operator finding out at that moment helps nobody. The failure is
    logged by the caller and the file write — the thing that matters — stands.
    """
    log = change_log_path(path)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record.to_dict(), sort_keys=True) + "\n")
    return log


def read_changes(path: Path | str | None = None) -> list[dict]:
    """Every readable record, oldest first.

    A line that will not parse is skipped rather than raising. The point of an
    append-only log is that one bad record does not cost the rest, and a
    history that refuses to open is a history nobody has.
    """
    log = change_log_path(path)
    if not log.is_file():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out
