import { Panel, Row, Rows, Chip } from '../components/Panel.jsx';
import { streamPayload } from '../lib/connection.js';
import { bytes, int, num } from '../lib/format.js';
import { LINK_LABELS, PROFILE_LABELS } from '../lib/labels.js';

/**
 * The shore link, always visible.
 *
 * Shows what is actually being carried and, when a subscription has been cut
 * back, why. An operator who cannot tell "nothing is happening" from "I am not
 * being sent it" will eventually act on the wrong one.
 */
export function LinkStatus({ state, connection }) {
  const link = streamPayload(state, 'link');
  const profile = state.profile || {};
  const degraded = Object.values(state.subscriptions).filter((s) => s.reason);

  const quality = link?.quality;
  // Half the quoted beamwidth: the angle at which the boat reaches the edge of
  // the sector. Absent rather than zero when the backend did not send it —
  // zero would draw as "dead centre", which is a claim.
  const sector =
    link?.off_boresight_deg === undefined
    || link?.off_boresight_deg === null
    || !link?.sector_beamwidth_deg
      ? null
      : { off: link.off_boresight_deg, halfWidth: link.sector_beamwidth_deg / 2 };
  const level =
    !state.connected || link?.active_link === 'none'
      ? 'alarm'
      : quality !== undefined && quality < 0.35
        ? 'warn'
        : 'ok';

  // What the collapsed panel has to say is not "63 kB/s" but which detail level
  // is in force and why — that is what tells an operator whether a missing
  // value is missing because nothing is happening or because it is not sent.
  const forced = !state.connected
    ? 'the link is down'
    : link?.active_link === 'none'
      ? 'no bearer'
      : profile.profile === 'minimal'
        ? 'beacon profile — most streams are not carried'
        : degraded.length
          ? `${degraded.length} subscription(s) cut back`
          : '';

  return (
    <Panel
      title="Link"
      id="link"
      collapsible
      forceOpen={Boolean(forced)}
      forceReason={forced}
      aside={
        <Chip level={level}>
          {state.connected ? LINK_LABELS[link?.active_link] || 'connected' : 'disconnected'}
        </Chip>
      }
      summary={
        <Rows>
          <Row label="Detail level">
            {PROFILE_LABELS[profile.profile] || profile.profile}
            {profile.manual ? ' (manual)' : ''}
          </Row>
        </Rows>
      }
    >
      <Rows>
        <Row label="Quality">{quality === undefined ? '—' : num(quality * 100, 0, '%')}</Row>
        <Row label="Latency">{num(link?.rtt_ms, 0, ' ms')}</Row>
        <Row label="Carrying">{bytes(link?.rate_bytes_per_s)}/s</Row>
        <Row label="This client">≈ {bytes(state.estimatedBytesPerS)}/s</Row>
      </Rows>

      {/*
        The radio, and the geometry behind it.

        Headroom is measured against the point at which the link is lost, not
        against the next modulation step. Rate steps are invisible to an
        operator and they cannot act on them; how much signal they can afford
        to lose before the link goes is the number they can.

        Range is shown beside signal on purpose. On a directional link over
        water the two belong together: a weak signal that matches its range
        means the boat is simply far out, and a weak signal at short range
        means something else — alignment, an obstruction, or a multipath null
        that will return at the same distance on the next pass.
      */}
      <Rows>
        <Row label="Signal">
          {link?.rssi_dbm === undefined || link?.rssi_dbm === null ? (
            <span className="dim-value" title={rssiTitle(link)}>not reported</span>
          ) : (
            <>
              {num(link.rssi_dbm, 0, ' dBm')}
              <span className="hint">
                {' '}
                — {num(link.headroom_db, 0, ' dB')} before the link goes
              </span>
            </>
          )}
        </Row>
        <Row label="Range">{num(link?.distance_m, 0, ' m')}</Row>
        <Row label="Rate">
          {link?.mcs_index === undefined || link?.mcs_index === null
            ? <span className="dim-value">not reported</span>
            : `MCS${link.mcs_index} — ${num(link.phy_mbps, 0, ' Mbit/s')}`}
        </Row>
        {/*
          Pure geometry: boat position against the station's position and
          bearing. It needs nothing from the radio, so it survives the radio
          telling us nothing — and it is the one number here that says what is
          *about* to happen rather than what already has. A boat working toward
          the edge of the sector is a tripod somebody can turn before the link
          goes, rather than after.
        */}
        {sector && (
          <Row label="In the sector">
            <span title={SECTOR_TITLE}>
              {num(Math.abs(sector.off), 0, '°')} of {num(sector.halfWidth, 0, '°')}
            </span>
            <span className="hint"> {sector.off >= 0 ? 'right' : 'left'} of centre</span>
          </Row>
        )}
      </Rows>

      <p className="hint" style={{ marginTop: 6, marginBottom: 6 }}>
        {profile.reason}
      </p>

      <div className="button-row">
        {['auto', 'full', 'reduced', 'minimal'].map((option) => (
          <button
            key={option}
            onClick={() => connection.setProfile(option)}
            disabled={
              option === 'auto' ? !profile.manual : profile.manual && profile.profile === option
            }
          >
            {option === 'auto' ? 'Auto' : PROFILE_LABELS[option]}
          </button>
        ))}
      </div>

      {degraded.length > 0 && (
        <details className="debug" style={{ marginTop: 8 }}>
          <summary>{degraded.length} stream(s) cut back by the link</summary>
          <Rows>
            {degraded.map((s) => (
              <Row key={s.name} label={s.name}>
                <span className="hint">
                  {s.granted ? `${num(s.rate_hz, 1)} Hz — ` : 'not sent — '}
                  {s.reason}
                </span>
              </Row>
            ))}
          </Rows>
        </details>
      )}

      {!state.connected && (
        <p className="errline" style={{ marginBottom: 0 }}>
          No connection to the vessel. Everything on screen is the last value
          received{state.attempts ? `; retrying (attempt ${state.attempts})` : ''}.
        </p>
      )}
    </Panel>
  );
}

const SECTOR_TITLE =
  'Where the boat sits in the shore antenna\u2019s beam, worked out from its '
  + 'position and the station\u2019s. Past the edge the signal falls away '
  + 'quickly; turn the tripod before that rather than after.';

function rssiTitle(link) {
  if (link?.active_link === '4g' || link?.active_link === 'ltem') {
    return 'Signal strength is only modelled for the directional link. This says '
      + 'nothing about the cellular bearer currently carrying the data.';
  }
  return 'No signal to report: there is no link to the shore station.';
}
