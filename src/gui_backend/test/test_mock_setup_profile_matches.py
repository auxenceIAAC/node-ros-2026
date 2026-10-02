"""The mock's ranking and headline must be the Python's, word for word.

The inventory is generated, so the fields cannot drift. The *judgement* is not
generable: which outstanding value somebody should deal with first, and the
sentence at the top of the page that says so. That ranking is the whole reason
the page is more useful than the all-amber pre-flight list it replaces, and the
case most worth reviewing in mock mode is a beach arrival with nothing filled
in — so the sentence a reviewer reads has to be the sentence the vessel would
produce, not an approximation of it.

The JavaScript is run under Node and compared with the Python over a set of
profiles chosen to hit every branch of the headline. Skips if Node is absent;
it is a cross-language check, not a build dependency.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
from asket_common.setup_profile import (
    CAPTURED,
    DEPLOYMENT_STALE_AFTER_MS,
    ENTERED,
    FIELDS,
    MEASURED,
    TIER_DEPLOYMENT,
    default_profile,
)

GUI = Path(__file__).resolve().parents[2] / "asket_gui"
NOW = 1_789_932_000_000

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not (GUI / "src" / "lib" / "mock").is_dir(),
    reason="node or the frontend mock is not present",
)


def _blocking_ids():
    return [spec.id for spec in FIELDS if not spec.has_default]


def _corrupting_ids():
    return [spec.id for spec in FIELDS if spec.has_default and spec.silently_corrupts]


def _harmless_ids():
    return [spec.id for spec in FIELDS if spec.has_default and not spec.silently_corrupts]


def _answer(profile, ids, provenance=ENTERED, utc_ms=NOW):
    """Answer a set of fields with something their spec will accept."""
    for field_id in ids:
        spec = next(s for s in FIELDS if s.id == field_id)
        value = spec.choices[0] if spec.choices else (spec.default if spec.has_default else 1.0)
        profile = profile.set(field_id, value, provenance=provenance, utc_ms=utc_ms)
    return profile


def cases() -> dict:
    """Profiles chosen to hit every branch of the headline, and the staleness
    rule on both sides."""
    stale_at = NOW - 3 * DEPLOYMENT_STALE_AFTER_MS
    deployment = [spec.id for spec in FIELDS if spec.tier == TIER_DEPLOYMENT]

    out = {
        # A fresh Jetson. The case the page exists for.
        "nothing": default_profile(),
        # Everything answered: the headline must stop warning rather than
        # fall back to a count of zero.
        "everything": _answer(default_profile(), [s.id for s in FIELDS], MEASURED),
        # No blocking values left, so the headline leads with the guesses.
        "blocking_done": _answer(default_profile(), _blocking_ids(), CAPTURED),
        # No silently-corrupting guesses left, which switches the second
        # clause to the "only make the screen less useful" wording.
        "corrupting_done": _answer(default_profile(), _corrupting_ids(), MEASURED),
        # Only the harmless defaults outstanding.
        "harmless_only": _answer(
            default_profile(), _blocking_ids() + _corrupting_ids(), MEASURED
        ),
        # Exactly one of each kind outstanding, for the singular wording.
        "one_each": _answer(
            default_profile(),
            _blocking_ids()[1:] + _corrupting_ids()[1:] + _harmless_ids()[1:],
            ENTERED,
        ),
        # Deployment answers from three days ago: stale, with dates.
        "stale_deployment": _answer(default_profile(), deployment, CAPTURED, stale_at),
        # The same answers from a minute ago: not stale.
        "fresh_deployment": _answer(default_profile(), deployment, CAPTURED, NOW - 60_000),
    }
    return out


def python_results() -> dict:
    out = {}
    for name, profile in cases().items():
        out[name] = {
            "headline": profile.headline(),
            "outstanding": [
                {
                    "id": o.id,
                    "blocking": o.blocking,
                    "silently_corrupts": o.silently_corrupts,
                }
                for o in profile.outstanding()
            ],
            "stale": [
                {"id": s.spec.id, "set_utc_ms": s.value.set_utc_ms, "age_ms": s.age_ms}
                for s in profile.stale(NOW)
            ],
            "effective": {spec.id: profile.get(spec.id) for spec in FIELDS},
        }
    return out


def js_results(values_by_case: dict) -> dict:
    script = textwrap.dedent(
        f"""
        const base = '{GUI}/src/lib/mock';
        const S = await import(base + '/setupProfile.js');
        const cases = {json.dumps(values_by_case)};
        const out = {{}};
        for (const [name, values] of Object.entries(cases)) {{
          const effective = {{}};
          for (const spec of S.FIELDS) effective[spec.id] = S.effective(values, spec.id);
          out[name] = {{
            headline: S.headline(values),
            outstanding: S.outstanding(values),
            stale: S.stale(values, {NOW}),
            effective,
          }};
        }}
        console.log(JSON.stringify(out));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        pytest.fail(f"node failed:\n{result.stderr}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def both():
    values_by_case = {
        name: {fid: entry.to_dict() for fid, entry in profile.values.items()}
        for name, profile in cases().items()
    }
    return python_results(), js_results(values_by_case)


def test_the_headline_is_identical_in_both_languages(both):
    """Word for word. A reviewer reading the mock is reading the sentence the
    vessel would produce, or the review is worthless."""
    python, js = both
    for name in python:
        assert js[name]["headline"] == python[name]["headline"], name


def test_the_ranking_is_identical(both):
    python, js = both
    for name in python:
        assert js[name]["outstanding"] == python[name]["outstanding"], name


def test_staleness_is_identical_on_both_sides_of_the_threshold(both):
    python, js = both
    for name in python:
        assert js[name]["stale"] == python[name]["stale"], name


def test_what_the_vessel_would_be_using_is_identical(both):
    python, js = both
    for name in python:
        assert js[name]["effective"] == python[name]["effective"], name


def test_the_cases_actually_exercise_the_branches(both):
    """A parity test over profiles that all produce the same sentence proves
    nothing. These must differ, and must include the two singular forms and
    the all-clear."""
    python, _ = both
    headlines = {name: result["headline"] for name, result in python.items()}
    assert headlines["everything"] == "Everything has been filled in."
    assert "no sensible default" in headlines["nothing"]
    assert "no sensible default" not in headlines["blocking_done"]
    assert "ruin a survey" not in headlines["harmless_only"]
    assert len(set(headlines.values())) >= 5
    assert python["stale_deployment"]["stale"]
    assert python["fresh_deployment"]["stale"] == []
