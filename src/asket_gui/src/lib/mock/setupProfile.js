// A mirror of `asket_common/setup_profile.py`'s ranking, for mock mode.
//
// `setupFields.json` is generated from the Python inventory, so the fields,
// their consequences and the tier split cannot drift. What cannot be generated
// is the *judgement*: which outstanding value somebody should deal with first,
// and the sentence at the top of the page that says so. That ranking —
// nothing-at-all before wrong-quietly before an unchecked default — is the
// whole reason the page is more useful than an all-amber list, and the case
// most worth reviewing in mock mode is a beach arrival with nothing filled in.
//
// So it is reimplemented here, and `test_mock_setup_profile_matches.py` runs
// this file under Node and compares it with the Python over a set of profiles.
// It will fail rather than let the two drift, which is the same arrangement
// that holds the negotiation table and the link budget honest.

import inventory from './setupFields.json' with { type: 'json' };

export const FIELDS = inventory.fields;
export const TIERS = inventory.tiers;
export const STALE_AFTER_MS = inventory.stale_after_ms;

const BY_ID = new Map(FIELDS.map((spec) => [spec.id, spec]));

// Mirrors ANSWERED. DEFAULT is the only provenance that does not count as
// somebody having answered, which is the entire mechanism.
const ANSWERED = new Set(['entered', 'captured', 'measured']);

/** Whether a field has been answered in `values`, a map of id -> entry. */
export function answered(values, id) {
  const entry = values[id];
  return Boolean(entry && ANSWERED.has(entry.provenance));
}

/** What the vessel is using: the answer if there is one, else the default. */
export function effective(values, id) {
  const entry = values[id];
  if (entry && ANSWERED.has(entry.provenance)) return entry.value;
  return BY_ID.get(id)?.default ?? null;
}

/**
 * Fields nobody has answered, worst first.
 *
 * Mirrors SetupProfile.outstanding(). The sort is the point: a field with no
 * default does not work at all, which is at least visible; a field on a
 * plausible guess is the one that ruins a survey without anybody noticing, and
 * it comes second only because the first group stops a panel drawing at all.
 */
export function outstanding(values, tier = null) {
  return FIELDS.filter(
    (spec) => (tier === null || spec.tier === tier) && !answered(values, spec.id),
  )
    .map((spec) => ({
      id: spec.id,
      blocking: !spec.has_default,
      silently_corrupts: spec.silently_corrupts,
    }))
    .sort((a, b) => {
      if (a.blocking !== b.blocking) return a.blocking ? -1 : 1;
      if (a.silently_corrupts !== b.silently_corrupts) return a.silently_corrupts ? -1 : 1;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
}

/**
 * Deployment answers old enough to be worth a second look.
 *
 * Vessel values never appear, however old. That is what the tiers are for:
 * mounting geometry measured in July is still true in September, and a station
 * position from July is almost certainly a different beach.
 */
export function stale(values, nowUtcMs) {
  const out = [];
  for (const spec of FIELDS) {
    if (!spec.goes_stale) continue;
    const entry = values[spec.id];
    if (!entry || !ANSWERED.has(entry.provenance) || entry.set_utc_ms == null) continue;
    const age = nowUtcMs - entry.set_utc_ms;
    if (age > STALE_AFTER_MS) {
      out.push({ id: spec.id, set_utc_ms: entry.set_utc_ms, age_ms: age });
    }
  }
  return out.sort((a, b) => b.age_ms - a.age_ms);
}

// Mirrors _count(). Numbers under ten read as words on a page somebody is
// skim-reading in sunlight.
const WORDS = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine'];

function count(n, singular, plural) {
  return `${WORDS[n] ?? n} ${n === 1 ? singular : plural}`;
}

/** Mirrors SetupProfile.headline(). The sentence at the top of the page. */
export function headline(values) {
  const missing = outstanding(values);
  if (missing.length === 0) return 'Everything has been filled in.';

  const blocking = missing.filter((o) => o.blocking);
  const guesses = missing.filter((o) => !o.blocking);
  const corrupting = guesses.filter((o) => o.silently_corrupts);

  const parts = [];
  if (blocking.length) {
    parts.push(
      `${count(blocking.length, 'value has', 'values have')} never been set, and `
      + 'there is no sensible default for them.',
    );
  }
  if (corrupting.length) {
    parts.push(
      `${count(corrupting.length, 'value is', 'values are')} still on a guess that `
      + 'can ruin a survey without it looking wrong.',
    );
  } else if (guesses.length) {
    parts.push(
      `${count(guesses.length, 'value is', 'values are')} still on a default `
      + 'nobody has checked.',
    );
  }
  if (corrupting.length && guesses.length > corrupting.length) {
    const rest = guesses.length - corrupting.length;
    parts.push(
      `${count(rest, 'other is', 'others are')} on unchecked defaults that only `
      + 'make the screen less useful.',
    );
  }
  return parts.join(' ');
}

/**
 * The whole `/api/setup` document, assembled from mock values.
 *
 * Same shape as `gui_backend/core/setup_api.payload`, so the page has one
 * contract rather than two.
 */
export function setupDocument(values, nowUtcMs, fileState = {}) {
  const effectiveAll = {};
  for (const spec of FIELDS) effectiveAll[spec.id] = effective(values, spec.id);
  return {
    tiers: TIERS,
    fields: FIELDS,
    values,
    effective: effectiveAll,
    headline: headline(values),
    outstanding: outstanding(values),
    stale: stale(values, nowUtcMs),
    stale_after_ms: STALE_AFTER_MS,
    written_utc_ms: Object.keys(values).length ? nowUtcMs : null,
    written_by: Object.keys(values).length ? 'mock' : '',
    content_hash: 'mock',
    unknown_fields: [],
    file: fileState,
    server_utc_ms: nowUtcMs,
  };
}

/**
 * Every field answered, for the dev panel's "Setup filled in".
 *
 * Built from the inventory rather than written out, so it cannot fall behind
 * the field list — a hand-kept fixture would stop covering the next field
 * added and the "filled in" state would quietly become "mostly filled in",
 * which is the one state this page must never show as finished.
 *
 * Deployment values are dated three days back so the staleness notes are
 * visible, and the mounting geometry is `measured` rather than `entered`
 * because the difference between those two words is the reason the provenance
 * exists at all.
 */
export function filledSetupValues(nowUtcMs = Date.now()) {
  const threeDays = 3 * 86_400_000;
  const values = {};
  for (const spec of FIELDS) {
    const isMounting = spec.id.includes('.mounting.');
    const captured = spec.supplied.startsWith('from_');
    values[spec.id] = {
      // A default where there is one, so the filled state is plausible; a
      // number where there is not, because those fields are all numeric and
      // the point is that the page renders an answer rather than a blank.
      value: spec.default !== null && spec.default !== undefined
        ? spec.default
        : (spec.choices.length ? spec.choices[0] : 0),
      provenance: isMounting ? 'measured' : captured ? 'captured' : 'entered',
      set_utc_ms: spec.goes_stale ? nowUtcMs - threeDays : nowUtcMs - 90 * 86_400_000,
      set_by: 'Auxence',
      note: isMounting ? 'tape measure, bow up' : '',
    };
  }
  return values;
}
