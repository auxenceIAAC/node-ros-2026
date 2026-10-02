// The setup document, and the words the page puts round it.
//
// The field inventory is not here. It arrives with the document from
// `/api/setup`, generated from `asket_common/setup_profile.py`, so a field
// added in Python appears on this page with its consequence sentence and
// nothing in this directory changes. A JS table of labels and units would be
// a second copy of the thing the page exists to display, and the two would
// drift the way `mounting.yaml` and the screen already have.
//
// What *is* here is presentation: how a provenance reads to somebody standing
// on a beach, how old is too old to trust without saying so, and the gate the
// button has to agree with.

/** Fetch the setup document. The caller decides what to do with a failure. */
export async function fetchSetup(connection) {
  if (connection?.setupDoc) return connection.setupDoc();
  const response = await fetch('/api/setup');
  if (!response.ok) throw new Error(`/api/setup returned ${response.status}`);
  return response.json();
}

// -- provenance ------------------------------------------------------------

//: How each provenance reads on the page.
//
// `measured` and `entered` are deliberately different words. The whole reason
// the mounting geometry has its own provenance is that a number somebody typed
// and a number somebody put a tape measure on are not the same claim, and a
// page that rendered both as "set" would erase the distinction it exists to
// record.
export const PROVENANCE_LABELS = {
  default: 'Nobody has said',
  entered: 'Typed in',
  captured: 'Captured from the boat',
  measured: 'Measured',
};

export const PROVENANCE_LEVELS = {
  default: 'warn',
  entered: 'ok',
  captured: 'ok',
  measured: 'ok',
};

export const ANSWERED = new Set(['entered', 'captured', 'measured']);

export function isAnswered(entry) {
  return Boolean(entry && ANSWERED.has(entry.provenance));
}

// -- how a value is supplied ----------------------------------------------

export const SUPPLIED_NOTES = {
  by_hand: '',
  by_choice: '',
  // Named rather than left as a bare button, because the method is the reason
  // to trust the number: standing next to the tripod with a GNSS receiver in
  // your hands beats reading a position off a phone.
  from_vessel_position: 'Can be captured: put the boat beside the tripod and press.',
  from_vessel_bearing: 'Can be captured: sail out, aim the antenna at the boat, press.',
  from_first_fix: "Taken from the mission's first fix automatically.",
};

// -- when a saved value starts being used ---------------------------------

export const EFFECT_NOTES = {
  immediately: '',
  on_reload: 'Saving is not enough — the node has to re-read it.',
  on_restart: 'Saving is not enough — the node reads this at startup.',
};

// -- the gate --------------------------------------------------------------

export const GATE_ARMED = 'armed';
export const GATE_RECORDING = 'recording';
export const GATE_UNKNOWN = 'unknown_state';

/**
 * Whether the vessel will take a setup change.
 *
 * A mirror of `gui_backend/core/setup_gate.evaluate`, and it exists here for
 * one reason only: so the button can say *why* it is not offering itself,
 * before anybody presses it. It is **not** the guard. The Jetson evaluates the
 * same conditions off the same vessel report at dispatch, and that is what
 * actually refuses — a guard the page could satisfy by claiming to be
 * satisfied would be no guard at all, and a page left open on a laptop is the
 * exact accident this is for.
 *
 * `test_setup_gate_mirror.py` runs this against the Python so the two cannot
 * come to different conclusions and leave somebody pressing a button that
 * always fails.
 */
export function gateDecision(state) {
  const pico = state?.streams?.pico?.payload ?? null;
  const mission = state?.streams?.mission?.payload ?? null;
  const armed = pico ? pico.armed : null;
  const recording = String(mission?.state ?? '').toUpperCase() === 'RECORDING';

  if (armed === null || armed === undefined) {
    return {
      allowed: false,
      code: GATE_UNKNOWN,
      reason: 'The vessel is not reporting whether it is armed.',
      remedy:
        "Setup cannot be changed while the Pico's state is unknown. "
        + 'Check the Pico link on the Vessel panel.',
    };
  }
  if (armed) {
    return {
      allowed: false,
      code: GATE_ARMED,
      reason: 'The Pico reports the vessel is armed.',
      remedy:
        'Disarm with RC channel 7 and try again. Setup is not changed on a '
        + 'vessel that can move.',
    };
  }
  if (recording) {
    return {
      allowed: false,
      code: GATE_RECORDING,
      reason: 'A mission is recording.',
      remedy:
        'Stop the recording first. Changing geometry part way through a '
        + 'mission would leave half the survey placed one way and half the '
        + 'other, with nothing in the file to say where the change fell.',
    };
  }
  return { allowed: true, code: '', reason: '', remedy: '' };
}

// -- dates -----------------------------------------------------------------

/** "14 March, 09:21" — a date somebody can compare with their own memory. */
export function setDate(utcMs) {
  if (utcMs == null) return '';
  const d = new Date(utcMs);
  return d.toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
}

/**
 * "three days ago" for a staleness note.
 *
 * Deliberately coarse. The question a stale Deployment value raises is "is
 * this still the same beach", and that is answered in days, not in minutes.
 */
export function daysOld(ageMs) {
  const days = Math.floor(ageMs / 86_400_000);
  if (days >= 2) return `${days} days ago`;
  const hours = Math.floor(ageMs / 3_600_000);
  return hours >= 1 ? `${hours} h ago` : 'just now';
}

// -- grouping --------------------------------------------------------------

/**
 * Fields in each tier, in the order the inventory declares them.
 *
 * `FIELDS` is ordered roughly by what blocks what, which is a judgement worth
 * keeping; sorting here would throw it away.
 */
export function fieldsByTier(doc) {
  const out = new Map();
  for (const tier of doc?.tiers ?? []) out.set(tier.id, []);
  for (const spec of doc?.fields ?? []) {
    if (!out.has(spec.tier)) out.set(spec.tier, []);
    out.get(spec.tier).push(spec);
  }
  return out;
}

/** The set of ids the backend says nobody has answered. */
export function outstandingIds(doc) {
  return new Set((doc?.outstanding ?? []).map((o) => o.id));
}

/** id -> {set_utc_ms, age_ms} for the Deployment values that have gone stale. */
export function staleById(doc) {
  return new Map((doc?.stale ?? []).map((s) => [s.id, s]));
}
