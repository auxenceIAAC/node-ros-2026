import { Panel, Row, Rows, Chip } from '../components/Panel.jsx';
import { PanelAge, Value } from '../components/Value.jsx';
import { streamAgeMs, streamPayload } from '../lib/connection.js';
import { coordinate, int, num } from '../lib/format.js';
import { hullSummary } from '../lib/hull.js';
import { MODE_LABELS } from '../lib/labels.js';

/**
 * What the vessel is actually doing.
 *
 * Every value here comes from the Pico or from the GNSS, and every one carries
 * its age. Nothing in this panel ever reflects a command that was sent
 * (safety rule 4) — the mode shown is the mode the Pico reports, full stop.
 *
 * **Heading is deliberately not here.** It has its own panel, and it used to
 * appear in both — sampled from two streams at two rates, so the two rows
 * disagreed by a degree and an operator had no way to tell which to believe.
 * A value shown in two places is a value that will eventually contradict
 * itself. One source, one place.
 */
export function VesselState({ state }) {
  const vessel = streamPayload(state, 'vessel');
  const pico = streamPayload(state, 'pico');
  const vesselAge = streamAgeMs(state, 'vessel');
  const picoAge = streamAgeMs(state, 'pico');
  const vesselRate = state.subscriptions.vessel?.rate_hz;
  const picoRate = state.subscriptions.pico?.rate_hz;

  const mode = pico?.mode ?? 'UNKNOWN';
  const hull = hullSummary(pico);

  // A firmware this GUI cannot read makes every other field on this panel
  // suspect, so it outranks everything — including the faults, which may
  // themselves be misreadings.
  const forced = pico?.version_mismatch
    ? `the Pico is running firmware v${pico.firmware_version}, which this GUI does not read`
    : pico?.estop_latched
      ? 'propulsion is cut'
      : pico?.hardware_killswitch_engaged
        ? 'the hardware killswitch is engaged'
        : pico?.rc_link_ok === false
          ? 'the RC link is lost'
          : pico?.ch8_asserting_estop
            ? 'channel 8 is in the ESTOP zone'
            : hull.alert
              ? hull.alert
              : vessel?.num_sats !== undefined && vessel.num_sats < 6
                ? `only ${vessel.num_sats} satellites`
                : '';

  return (
    <Panel
      title="Vessel"
      id="vessel"
      collapsible
      forceOpen={Boolean(forced)}
      forceReason={forced}
      aside={<PanelAge ageMs={vesselAge} rateHz={vesselRate} />}
      summary={
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
            <Value ageMs={picoAge} rateHz={picoRate}>
              <span className={`mode-badge mode-${mode}`}>{MODE_LABELS[mode] || mode}</span>
            </Value>
            <div className="spacer" />
            <Chip
              level={pico?.armed === undefined ? '' : pico.armed ? 'warn' : 'ok'}
              title={pico?.arming_block || undefined}
            >
              {pico?.armed === undefined ? 'Arming not sent' : pico.armed ? 'Armed' : 'Disarmed'}
            </Chip>
          </div>
          {/*
            Why the propellers cannot turn, standing — not only after somebody
            presses something, because the state exists before they do.

            Arming is RC channel 7 and nothing else; there is no software path
            to it, deliberately. The cost is a state that reads as a fault: the
            mode is right, the vessel confirms it, and nothing turns. Without
            this line an operator cannot tell whether the boat ignored them or
            the switch did, and the two send you to different places.

            The backend sends the sentence or sends nothing; this never
            composes one, so the panel and the log cannot disagree.
          */}
          {pico?.arming_block && (
            <p className="warnline" style={{ margin: '0 0 8px' }}>
              {pico.arming_block}
            </p>
          )}
          <Rows>
            <Row label="Position">
              <Value ageMs={vesselAge} rateHz={vesselRate} showAge={false}>
                <span className="mono">{coordinate(vessel?.lat, vessel?.lon)}</span>
              </Value>
            </Row>
            <Row label="Speed">
              <Value ageMs={vesselAge} rateHz={vesselRate} showAge={false}>
                {num(vessel?.sog_ms, 2, ' m/s')}
              </Value>
            </Row>
          </Rows>
        </>
      }
    >
      <Rows>
        {/* Hull attitude from the IMU. The sonar panel shows the
            *transducer's* attitude, which is a different sensor and is
            labelled as such — two rows reading "roll / pitch" with different
            numbers is an inconsistency an operator cannot resolve. */}
        <Row label="Roll / pitch">
          <Value ageMs={vesselAge} rateHz={vesselRate} showAge={false}>
            {num(vessel?.roll_deg, 1, '°')} / {num(vessel?.pitch_deg, 1, '°')}
          </Value>
        </Row>
        <Row label="GNSS">
          <Value ageMs={vesselAge} rateHz={vesselRate} showAge={false}>
            {int(vessel?.num_sats)} sats, HDOP {num(vessel?.hdop, 1)}
          </Value>
        </Row>
      </Rows>

      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
        {/* RC link and channel 8 are shown because they are the sovereign
            path. Software can observe them; it can never move them.

            Both are absent by design on the reduced and minimal profiles, and
            "not sent" must never render as "lost" — reporting a healthy RC
            link as lost because the shore link is narrow is precisely the kind
            of lie this GUI exists to avoid. */}
        <Chip level={rcLevel(pico?.rc_link_ok)} title={rcTitle(pico?.rc_link_ok)}>
          RC {pico?.rc_link_ok === undefined ? 'not sent' : pico.rc_link_ok ? 'linked' : 'lost'}
        </Chip>
        {/* The verdict, not the arithmetic. The backend compares against the
            firmware's own threshold on the firmware's own scale; this renders
            what it concluded. The previous version compared here, against a
            field whose name claimed percent while it carried a raw SBUS count,
            so the alarm never fired — least of all at the bottom of the
            travel, which is the position it existed to catch. */}
        <Chip
          level={ch8Level(pico?.ch8_asserting_estop)}
          title="Channel 8 selects the mode in hardware: bottom third is ESTOP. Nothing in this GUI can move it."
        >
          Ch8 {ch8Text(pico)}
        </Chip>
        {pico?.version_mismatch && (
          <Chip level="alarm" title="This GUI parses a different firmware than the Pico is running. Every field on this panel is suspect.">
            Firmware v{pico.firmware_version} — not readable
          </Chip>
        )}
        {pico?.link_live === false && (
          <Chip level="warn" title="The Pico is not hearing our heartbeat. It will not grant autonomy while this is so.">
            Pico hears no heartbeat
          </Chip>
        )}
        {pico?.mode_requested_auto && mode !== 'AUTONOMOUS' && (
          <Chip level="warn" title="AUTO was requested and is not in effect. Channel 8 must be in its top position, and the Pico must be hearing our heartbeat.">
            AUTO requested, not granted
          </Chip>
        )}
        {pico?.estop_latched && <Chip level="alarm">Propulsion cut</Chip>}
        {pico?.hardware_killswitch_engaged && <Chip level="alarm">Killswitch engaged</Chip>}
      </div>

      {/* A fault is never hidden behind a disclosure triangle. Anything
          off-nominal is promoted out of the collapse; the rest stays folded
          away, because relay positions are debugging detail on a normal day. */}
      {hull.alert && (
        <p className="errline" style={{ marginBottom: 0, marginTop: 8 }}>
          {hull.alert}
        </p>
      )}

      {hull.relays.length > 0 && (
        <details className="advanced">
          <summary>Advanced — {hull.summary}</summary>
          <Rows>
            {hull.relays.map((r) => (
              <Row key={`r${r.index}`} label={r.name}>
                <span className={r.closed ? '' : 'dim-value'} title={r.description}>
                  {r.text}
                </span>
              </Row>
            ))}
            {hull.escs.map((e) => (
              <Row key={`e${e.index}`} label={e.name}>
                <span
                  className={e.ok ? '' : e.unknown ? 'warnline' : 'errline'}
                  title={
                    e.unknown
                      ? 'This status code is hull-specific and not documented here (open question Q7). It is reported, not interpreted.'
                      : ''
                  }
                >
                  {e.text}
                </span>
                <span className="raw-code"> ({e.code})</span>
              </Row>
            ))}
          </Rows>
          {/* The relay is named because its function is confirmed in firmware
              source (GPIO21, ESC power). The ESC codes are not, and the two
              are described differently on purpose — a panel that hedges about
              everything teaches an operator to discount all of it. */}
          <p className="hint" style={{ marginBottom: 0 }}>
            The relay cuts ESC power — open while disarmed is correct. ESC status
            codes beyond 0 are hull-specific and not interpreted here; see
            docs/open_questions.md Q7.
          </p>
        </details>
      )}
    </Panel>
  );
}

function ch8Level(asserting) {
  // No level at all when the value was not sent: an uncoloured chip reads as
  // "unknown", which is what it is. Green would claim it is fine and red would
  // claim it is not, and neither is known.
  if (asserting === null || asserting === undefined) return '';
  return asserting ? 'alarm' : 'ok';
}

function ch8Text(pico) {
  const raw = pico?.rc_channel8_raw;
  if (raw === null || raw === undefined) return 'not sent';
  // The count is shown as well as the verdict, because 1811 vs 1400 is the
  // difference between "autonomy permitted" and "manual forced" and an
  // operator debugging a switch needs the number.
  return pico.ch8_asserting_estop ? `${raw} — ESTOP` : String(raw);
}

function rcLevel(ok) {
  if (ok === undefined || ok === null) return '';
  return ok ? 'ok' : 'alarm';
}

function rcTitle(ok) {
  if (ok === undefined || ok === null) {
    return 'RC link state is not carried on the current link profile. This says nothing about the RC link itself.';
  }
  return 'RC channel 8 cuts propulsion in hardware. Nothing in this GUI can move it.';
}
