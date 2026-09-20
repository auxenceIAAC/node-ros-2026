/**
 * Decoding the Pico's relay and ESC state into something a club member can read
 * at 06:00 on a beach.
 *
 * `Relays · · · ·` and `ESCs e1 e1` were the raw wire values. They are precise
 * and completely opaque: somebody who did not write the firmware cannot tell
 * whether `e1` is normal or catastrophic, which makes them worse than useless
 * on site.
 *
 * There is one relay, and it is named
 * -----------------------------------
 *
 * The panel used to read `0/4 relays closed`. There is no fourth relay and
 * there never was: the `4` came from `num_relays` in the *simulator's*
 * placeholder config, and the GUI rendered whatever length of array arrived.
 * The firmware has exactly one — `ESTOP_RELAY_PIN` on GPIO21 — and it cuts ESC
 * power.
 *
 * That mattered more than a wrong number usually does, because the sentence it
 * formed was about the e-stop path: three quarters of a safety mechanism
 * appearing not to exist, in the mode this GUI gets reviewed in. Both ends are
 * corrected now (`asket_sim/core/pico.py` and the mock), and the relay is named
 * here because — unlike the hull wiring — its function is confirmed in firmware
 * source rather than guessed.
 *
 * What is still not known
 * -----------------------
 *
 * ESC status codes beyond 0. The firmware does not report an ESC code over
 * serial at all; `esc_status` arrives only from `asket_sim`. Nothing here
 * invents a meaning for a non-zero code.
 *
 * `RELAY_LABELS` remains the place to add more if the hardware ever grows them.
 */

//: GPIO21, confirmed in pico-node_v4.ino: HIGH = ESC power on, LOW = power cut.
export const RELAY_LABELS = { 0: 'ESC power' };

//: What the single relay does, for the operator deciding whether an open relay
//: is a problem right now. Open while disarmed is correct, not a fault.
export const RELAY_DESCRIPTIONS = {
  0: 'Cuts power to both ESCs. Open while disarmed, which is correct.',
};

//: PROVISIONAL (Q7): ESC status codes beyond 0 are hull-specific. 0 is the
//: only value this repository can claim to know the meaning of.
export const ESC_STATUS_LABELS = { 0: 'running' };

export function relayName(index) {
  return RELAY_LABELS[index] || `Relay ${index + 1}`;
}

/** `{ index, name, description, closed, text }` per relay. */
export function decodeRelays(states) {
  return (states || []).map((closed, i) => ({
    index: i,
    name: relayName(i),
    // '' rather than a guess for any relay we cannot name. An invented
    // description of a safety component is worse than none.
    description: RELAY_DESCRIPTIONS[i] || '',
    closed: Boolean(closed),
    text: closed ? 'closed' : 'open',
  }));
}

/**
 * `{ name, code, ok, text, unknown }` per ESC.
 *
 * `unknown` marks a code we cannot interpret. It renders as a warning rather
 * than an alarm: an uninterpretable code is not evidence of a fault, and
 * calling it one would send somebody looking for a problem that may not exist.
 */
export function decodeEscs(codes) {
  return (codes || []).map((code, i) => {
    const known = ESC_STATUS_LABELS[code];
    return {
      index: i,
      name: `Thruster ${i + 1}`,
      code,
      ok: code === 0,
      unknown: known === undefined,
      text: known || `code ${code}`,
    };
  });
}

/**
 * One line for the collapsed summary, and whether anything needs promoting out
 * of it.
 *
 * A fault must never be hidden behind a disclosure triangle, so the panel shows
 * a chip outside the collapse whenever an ESC is not running **while the vessel
 * is armed**. Disarmed, an ESC that is not running is the correct state and
 * saying so in red would train everyone to ignore it.
 */
function relayText(relays) {
  if (relays.length === 1) return `${relays[0].name} ${relays[0].text}`;
  const closed = relays.filter((r) => r.closed).length;
  return `${closed}/${relays.length} relays closed`;
}

export function hullSummary(pico) {
  const relays = decodeRelays(pico?.relay_states);
  const escs = decodeEscs(pico?.esc_status);
  const closed = relays.filter((r) => r.closed).length;
  const notRunning = escs.filter((e) => !e.ok);
  const armed = pico?.armed === true;

  return {
    relays,
    escs,
    // With one relay, "0/1 relays closed" is both ugly and less informative
    // than saying what it is: the count form only earns its place once there
    // is more than one thing to count.
    summary: relays.length
      ? `${relayText(relays)}, ${escs.length - notRunning.length}/${escs.length} thrusters running`
      : '',
    alert: armed && notRunning.length > 0
      ? `${notRunning.length} of ${escs.length} thrusters not running while armed`
      : null,
  };
}
