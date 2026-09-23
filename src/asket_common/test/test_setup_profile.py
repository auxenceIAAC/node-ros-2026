"""What the vessel has been told, and what it has not.

The model behind the setup page. It exists because of a two-week-old failure:
the sonar's mounting geometry has never been measured, the pre-flight has said
so in amber on every run, and nothing has happened — because the warning names
a YAML file behind an SSH session nobody in the club has.

Most of these tests are about a distinction rather than a value. "Nobody has
told us this" is not the same as "this is wrong", a guess is not the same as a
measurement, and a value with no default at all is not the same as one running
on a plausible one. Collapsing any of those is how twenty amber warnings become
one thing nobody reads.
"""

import pytest
from asket_common.setup_profile import (
    ANSWERED,
    BY_CHOICE,
    CAPTURED,
    DEFAULT,
    DEPLOYMENT_STALE_AFTER_MS,
    ENTERED,
    FIELD_BY_ID,
    FIELDS,
    MEASURED,
    TIER_DEPLOYMENT,
    TIER_VESSEL,
    FieldValue,
    SetupProfile,
    default_profile,
)

NOW = 1_789_932_000_000
DAY_MS = 24 * 60 * 60 * 1000

MOUNTING = "vessel.sonar.mounting.tilt_deg"
STATION_LAT = "deployment.station.lat"
SONAR_RANGE = "deployment.sonar.range_m"


# -- the inventory ---------------------------------------------------------


def test_the_sonar_appears_in_both_tiers_and_that_is_correct():
    """Mounting geometry is Vessel: offset and angle depend on how the head is
    bolted on, not on where you are surveying, so it is measured once on a
    given hull and kept. Range and gain are Deployment: they follow the
    expected depth, so they change with the beach.

    Two different kinds of fact about the same instrument, and putting them in
    one tier would make the staleness rule wrong for one of them.
    """
    assert FIELD_BY_ID[MOUNTING].tier == TIER_VESSEL
    assert FIELD_BY_ID["vessel.sonar.side"].tier == TIER_VESSEL
    assert FIELD_BY_ID[SONAR_RANGE].tier == TIER_DEPLOYMENT
    assert FIELD_BY_ID["deployment.sonar.gain"].tier == TIER_DEPLOYMENT


def test_only_deployment_values_go_stale():
    """The whole argument for splitting rather than listing. Mounting geometry
    measured in July is still true in September; a station position from July
    is almost certainly a different beach."""
    for spec in FIELDS:
        assert spec.goes_stale == (spec.tier == TIER_DEPLOYMENT), spec.id


def test_every_field_says_what_goes_wrong():
    """"tilt_deg" tells nobody why they should fetch a tape measure. The
    consequence is what gets somebody off the pontoon."""
    for spec in FIELDS:
        assert spec.consequence, f"{spec.id} does not say why it matters"
        assert spec.id.startswith(f"{spec.tier}."), f"{spec.id} is not tier-first"


def test_the_silently_corrupting_group_is_the_narrow_one():
    """Not "important" — values whose being wrong produces plausible-looking
    wrong data. A wrong sonar range gives no bottom returns and you know
    within a minute; a wrong lever arm displaces the whole survey and looks
    perfectly fine."""
    corrupting = {spec.id for spec in FIELDS if spec.silently_corrupts}
    assert MOUNTING in corrupting
    assert "deployment.origin.lat" in corrupting
    assert "vessel.sonar.side" in corrupting
    assert SONAR_RANGE not in corrupting
    assert "vessel.battery.capacity_wh" not in corrupting


def test_a_field_with_no_sensible_default_is_marked_as_having_none():
    """There is no sensible default for where a tripod is standing. That is a
    different problem from a guessed tilt of 35 degrees: one is a panel that
    cannot draw, the other is a survey that may be silently displaced."""
    assert not FIELD_BY_ID[STATION_LAT].has_default
    assert not FIELD_BY_ID["deployment.station.boresight_deg"].has_default
    assert FIELD_BY_ID[MOUNTING].has_default
    assert FIELD_BY_ID[SONAR_RANGE].has_default


def test_choice_fields_offer_words_rather_than_numbers():
    """Nobody knows the RMS wave height. Everybody can tell glassy from
    choppy, and a field inviting a number invites a made-up one."""
    sea = FIELD_BY_ID["deployment.sea.state"]
    assert sea.supplied == BY_CHOICE
    assert sea.choices == ("glassy", "slight", "moderate")


# -- provenance ------------------------------------------------------------


def test_a_fresh_vessel_has_answered_nothing():
    profile = default_profile()
    assert not profile.answered(MOUNTING)
    assert profile.entry(MOUNTING).provenance == DEFAULT
    assert profile.get(MOUNTING) == FIELD_BY_ID[MOUNTING].default


def test_the_shipped_default_is_still_readable_before_anybody_answers():
    """The vessel has to run on something. The point is not to withhold the
    value, it is to be honest that nobody has checked it."""
    assert default_profile().get(SONAR_RANGE) == 30.0


def test_answering_records_who_and_when_and_how():
    profile = default_profile().set(
        MOUNTING, 34.2, MEASURED, utc_ms=NOW, by="Auxence", note="tape, bow up",
    )
    entry = profile.entry(MOUNTING)
    assert (entry.value, entry.provenance, entry.set_utc_ms) == (34.2, MEASURED, NOW)
    assert entry.set_by == "Auxence"
    assert entry.note == "tape, bow up"
    assert entry.answered


def test_a_capture_can_record_the_fix_it_was_taken_on():
    """A captured value that looks identical to a measured one is the same
    class of lie as a predicted signal shown as a measurement. If the fix was
    poor when somebody pressed the button, that has to travel with it."""
    profile = default_profile().set(
        STATION_LAT, -22.9576, CAPTURED, utc_ms=NOW,
        by="Auxence", note="8 satellites, HDOP 1.1",
    )
    assert "HDOP" in profile.entry(STATION_LAT).note


def test_default_is_not_an_answer_anybody_can_give():
    """The one thing that must not be settable. If a field could be *set* to
    DEFAULT, the distinction between a guess and an answer would be something
    a caller could erase by accident."""
    with pytest.raises(ValueError):
        default_profile().set(MOUNTING, 35.0, DEFAULT, utc_ms=NOW)
    assert DEFAULT not in ANSWERED


def test_clearing_returns_a_field_to_its_default_and_to_the_list():
    profile = default_profile().set(MOUNTING, 34.2, MEASURED, utc_ms=NOW)
    cleared = profile.clear(MOUNTING)

    assert not cleared.answered(MOUNTING)
    assert cleared.get(MOUNTING) == FIELD_BY_ID[MOUNTING].default
    assert MOUNTING in {o.id for o in cleared.outstanding()}


def test_a_profile_is_not_modified_in_place():
    """The page will want to show what would change before committing, and a
    half-applied setup is a state no file describes."""
    before = default_profile()
    after = before.set(MOUNTING, 34.2, MEASURED, utc_ms=NOW)
    assert not before.answered(MOUNTING)
    assert after.answered(MOUNTING)


def test_an_unknown_field_is_refused_rather_than_stored():
    with pytest.raises(KeyError):
        default_profile().set("vessel.sonar.tiltt_deg", 1.0, ENTERED, utc_ms=NOW)


def test_a_choice_field_refuses_a_word_it_does_not_offer():
    with pytest.raises(ValueError):
        default_profile().set("deployment.sea.state", "calm-ish", ENTERED, utc_ms=NOW)
    assert default_profile().set(
        "deployment.sea.state", "glassy", ENTERED, utc_ms=NOW,
    ).get("deployment.sea.state") == "glassy"


# -- what still needs doing ------------------------------------------------


def test_everything_is_outstanding_on_a_fresh_vessel():
    assert len(default_profile().outstanding()) == len(FIELDS)


def test_the_things_with_no_default_come_first():
    """Then the ones that ruin a survey quietly, then the rest. That is the
    order somebody with twenty minutes of daylight should work in."""
    outstanding = default_profile().outstanding()
    blocking = [o for o in outstanding if o.blocking]
    assert blocking, "nothing is blocking, so the ordering test proves nothing"
    assert outstanding[:len(blocking)] == blocking

    after_blocking = outstanding[len(blocking):]
    corrupting = [o for o in after_blocking if o.silently_corrupts]
    assert after_blocking[:len(corrupting)] == corrupting


def test_answering_removes_a_field_from_the_list():
    profile = default_profile().set(MOUNTING, 34.2, MEASURED, utc_ms=NOW)
    assert MOUNTING not in {o.id for o in profile.outstanding()}


def test_outstanding_can_be_asked_about_one_tier():
    profile = default_profile()
    vessel = profile.outstanding(TIER_VESSEL)
    assert vessel
    assert all(o.spec.tier == TIER_VESSEL for o in vessel)
    assert len(vessel) + len(profile.outstanding(TIER_DEPLOYMENT)) == len(FIELDS)


# -- the sentence at the top of the page -----------------------------------


def answer_all(profile: SetupProfile, tier: str, utc_ms: int = NOW) -> SetupProfile:
    for spec in FIELDS:
        if spec.tier != tier:
            continue
        value = spec.choices[0] if spec.choices else (spec.default or 1.0)
        profile = profile.set(spec.id, value, ENTERED, utc_ms=utc_ms)
    return profile


def test_a_fresh_vessel_separates_the_three_kinds_of_unanswered():
    """The fresh-Jetson case, which is the one that decides whether anybody
    but Auxence can use this. It must not read as "twenty blank fields"."""
    headline = default_profile().headline()
    assert "no sensible default" in headline
    assert "ruin a survey" in headline
    assert "less useful" in headline


def test_capturing_the_station_clears_the_blocking_group():
    profile = default_profile()
    for field_id in (
        STATION_LAT, "deployment.station.lon",
        "deployment.station.boresight_deg",
        "deployment.origin.lat", "deployment.origin.lon",
    ):
        profile = profile.set(field_id, 1.0, CAPTURED, utc_ms=NOW)

    assert "no sensible default" not in profile.headline()
    assert "ruin a survey" in profile.headline()


def test_measuring_the_hull_clears_the_corrupting_group():
    profile = answer_all(default_profile(), TIER_VESSEL)
    profile = profile.set("deployment.origin.lat", 1.0, CAPTURED, utc_ms=NOW)
    profile = profile.set("deployment.origin.lon", 1.0, CAPTURED, utc_ms=NOW)

    assert "ruin a survey" not in profile.headline()
    assert "nobody has checked" in profile.headline()


def test_a_fully_answered_vessel_says_so_plainly():
    profile = answer_all(answer_all(default_profile(), TIER_VESSEL), TIER_DEPLOYMENT)
    assert profile.headline() == "Everything has been filled in."


def test_small_numbers_are_words():
    """The page is skim-read in sunlight. "Three values" beats "3 values"."""
    profile = answer_all(default_profile(), TIER_VESSEL)
    profile = profile.set("deployment.sea.state", "glassy", ENTERED, utc_ms=NOW)
    assert "One" in profile.headline() or "Two" in profile.headline() \
        or "Three" in profile.headline() or "Four" in profile.headline() \
        or "Five" in profile.headline()


# -- staleness -------------------------------------------------------------


def test_a_deployment_value_goes_stale_after_a_day():
    profile = default_profile().set(STATION_LAT, -22.9, CAPTURED, utc_ms=NOW)
    assert profile.stale(NOW + DAY_MS // 2) == []

    stale = profile.stale(NOW + 2 * DAY_MS)
    assert [s.spec.id for s in stale] == [STATION_LAT]
    assert stale[0].age_ms == 2 * DAY_MS


def test_a_vessel_value_never_does():
    """However old. A hull measured a year ago is a hull measured a year ago."""
    profile = default_profile().set(MOUNTING, 34.2, MEASURED, utc_ms=NOW)
    assert profile.stale(NOW + 400 * DAY_MS) == []


def test_an_unanswered_field_is_not_stale_it_is_outstanding():
    """Two different states with two different remedies. A stale value may
    well still be correct and only a human on the beach can say; an
    unanswered one has never been right."""
    profile = default_profile()
    assert profile.stale(NOW + 400 * DAY_MS) == []
    assert STATION_LAT in {o.id for o in profile.outstanding()}


def test_the_threshold_survives_two_mornings_at_one_beach():
    """A survey trip can run over two days without the tripod moving. A
    threshold that cried wolf on the second morning would be another amber
    warning people learn to read past."""
    assert DEPLOYMENT_STALE_AFTER_MS >= 12 * 60 * 60 * 1000


def test_the_stalest_is_reported_first():
    profile = (
        default_profile()
        .set(STATION_LAT, 1.0, CAPTURED, utc_ms=NOW - 5 * DAY_MS)
        .set("deployment.station.lon", 1.0, CAPTURED, utc_ms=NOW - 9 * DAY_MS)
    )
    assert [s.spec.id for s in profile.stale(NOW)] == [
        "deployment.station.lon", STATION_LAT,
    ]


# -- serialisation ---------------------------------------------------------


def test_a_profile_round_trips():
    profile = (
        default_profile()
        .set(MOUNTING, 34.2, MEASURED, utc_ms=NOW, by="Auxence", note="tape")
        .set("deployment.sea.state", "glassy", ENTERED, utc_ms=NOW)
    )
    restored = SetupProfile.from_dict(profile.to_dict())

    assert restored.get(MOUNTING) == 34.2
    assert restored.entry(MOUNTING).provenance == MEASURED
    assert restored.entry(MOUNTING).set_by == "Auxence"
    assert restored.entry(MOUNTING).note == "tape"
    assert restored.get("deployment.sea.state") == "glassy"


def test_a_field_this_version_does_not_know_is_kept_and_reported():
    """Never dropped. mounting.yaml taught this: an unrecognised entry is
    almost always a typo in a value somebody DID measure, and silently
    ignoring it means the measurement never reaches the instrument."""
    restored = SetupProfile.from_dict({
        "fields": {
            MOUNTING: {"value": 34.2, "provenance": MEASURED},
            "vessel.sonar.mounting.tilt_dgrees": {"value": 34.2, "provenance": MEASURED},
        },
    })
    assert restored.unknown_fields == ("vessel.sonar.mounting.tilt_dgrees",)
    assert restored.get(MOUNTING) == 34.2


def test_an_unrecognised_provenance_reads_as_no_answer():
    """Fails in the safe direction. A typo must never be able to promote a
    guess to a measurement — that is the one way round this cannot go wrong."""
    entry = FieldValue.from_dict({"value": 34.2, "provenance": "meassured"})
    assert entry.provenance == DEFAULT
    assert not entry.answered


def test_an_empty_document_is_a_vessel_nobody_has_told_anything():
    profile = SetupProfile.from_dict({})
    assert len(profile.outstanding()) == len(FIELDS)
    assert profile.unknown_fields == ()


def test_the_hash_follows_the_answers_and_not_the_clock():
    """So the pre-flight can say "this is the setup that was pushed". Two
    profiles with the same answers written at different times are the same
    setup, and telling somebody otherwise would be noise."""
    first = default_profile().set(MOUNTING, 34.2, MEASURED, utc_ms=NOW)
    later = default_profile().set(MOUNTING, 34.2, MEASURED, utc_ms=NOW + 99_999)
    changed = default_profile().set(MOUNTING, 34.3, MEASURED, utc_ms=NOW)

    assert first.content_hash() == later.content_hash()
    assert first.content_hash() != changed.content_hash()
    assert first.content_hash() != default_profile().content_hash()


def test_the_hash_notices_a_change_of_provenance_alone():
    """The same number, measured rather than guessed, is a different setup —
    it is the difference the whole model exists to carry."""
    typed = default_profile().set(MOUNTING, 35.0, ENTERED, utc_ms=NOW)
    measured = default_profile().set(MOUNTING, 35.0, MEASURED, utc_ms=NOW)
    assert typed.content_hash() != measured.content_hash()
