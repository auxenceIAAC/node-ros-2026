import { useEffect, useMemo, useState } from 'react';

import { AlarmPanel } from './panels/AlarmPanel.jsx';
import { DiagnosticsPanel } from './panels/DiagnosticsPanel.jsx';
import { HeadingPanel } from './panels/HeadingPanel.jsx';
import { CameraPanel } from './panels/CameraPanel.jsx';
import { LidarPanel } from './panels/LidarPanel.jsx';
import { LinkStatus } from './panels/LinkStatus.jsx';
import { MissionMap } from './panels/MissionMap.jsx';
import { MissionPanel } from './panels/MissionPanel.jsx';
import { ModeCommands } from './panels/ModeCommands.jsx';
import { PowerPanel } from './panels/PowerPanel.jsx';
import { SonarPanel } from './panels/SonarPanel.jsx';
import { VesselState } from './panels/VesselState.jsx';
import { Chip } from './components/Panel.jsx';
import { MockControls } from './components/MockControls.jsx';
import { StatusStrip } from './components/StatusStrip.jsx';
import { streamPayload } from './lib/connection.js';
import { useStore } from './lib/useStore.js';
import { MODE_LABELS } from './lib/labels.js';
import { duration } from './lib/format.js';

//: A socket can stay open long after the link behind it has gone. TCP takes
//: tens of seconds to notice, and for all of that time `connected` is true
//: while nothing is arriving. So the header reports what has actually been
//: RECEIVED, not what the socket believes about itself.
const NO_DATA_AFTER_MS = 6000;

/**
 * What this client asks for.
 *
 * These are *requests*, not entitlements. The server decides what it will
 * actually serve, based on the link profile, and tells us what we are getting
 * and why. Nothing arrives that is not listed here — the backend pushes
 * nothing by default.
 */
const SUBSCRIPTIONS = [
  { name: 'vessel', rate_hz: 5 },
  { name: 'pico', rate_hz: 2 },
  { name: 'heading', rate_hz: 2 },
  { name: 'link', rate_hz: 1 },
  { name: 'track', rate_hz: 1 },
  { name: 'coverage', rate_hz: 1 },
  { name: 'lidar', rate_hz: 5 },
  { name: 'power', rate_hz: 1 },
  { name: 'sonar', rate_hz: 1 },
  { name: 'mission', rate_hz: 1 },
  { name: 'diagnostics', rate_hz: 0.2 },
  { name: 'plan' },
  // On by default, which it would not have been on the old link assumption.
  // The reason for opt-in was bandwidth — 45 kB a frame against a 200 kB/s
  // budget — and the directional link retired that reason: five frames a
  // second is about 1.8 Mbit/s against a bearer measured in tens. It is
  // carried on the full profile only, so a narrow link drops it by itself and
  // says so.
  { name: 'camera', rate_hz: 5 },
];

export function App({ connection }) {
  const state = useStore(connection);
  const [follow, setFollow] = useState(true);
  // One toggle drives both the lidar panel and the map overlay, so the two can
  // never disagree about which point set is being looked at.
  const [showRawLidar, setShowRawLidar] = useState(false);
  // Lifted so the layout can give the dev panel a real column instead of
  // floating it over the cockpit, which made the panels it exists to review
  // impossible to see.
  // Closed by default: the dev panel stays valuable, but the default view has
  // to be what an operator actually sees, not the review rig around it.
  const [mockOpen, setMockOpen] = useState(false);

  useEffect(() => {
    connection.subscribe(SUBSCRIPTIONS);
  }, [connection]);

  const pico = streamPayload(state, 'pico');
  const sinceMessageMs = state.lastMessageAt ? Date.now() - state.lastMessageAt : null;
  const receiving = sinceMessageMs !== null && sinceMessageMs < NO_DATA_AFTER_MS;
  const worstAlarm = useMemo(() => {
    const alarms = state.alarms || [];
    if (alarms.some((a) => a.severity === 'alarm')) return 'alarm';
    if (alarms.length) return 'warn';
    return 'ok';
  }, [state.alarms]);

  // Connected, and the server has told us it is serving us nothing.
  //
  // The header already says "No data for 42 s", which is true and not
  // actionable: a quiet vessel and a refused subscription look identical from
  // there. This says WHICH, and it can say it the instant the `subscribed`
  // frame lands rather than waiting for data that is never coming.
  const starved = useMemo(() => {
    const entries = Object.values(state.subscriptions || {});
    if (!state.connected || entries.length === 0) return null;
    if (entries.some((s) => s.granted)) return null;
    const refused = entries.find((s) => s.reason);
    return {
      text: 'Connected, but the server is serving no streams.',
      detail:
        refused?.reason
        || `link profile ${state.profile?.profile ?? '?'} (${state.profile?.reason ?? ''})`,
    };
  }, [state.subscriptions, state.connected, state.profile]);

  const banner = state.notice
    ? { text: state.notice.text, detail: state.notice.detail }
    : starved;

  const layoutClass = [
    'app',
    connection.isMock ? (mockOpen ? 'with-mock' : 'with-mock-collapsed') : '',
  ]
    .filter(Boolean)
    .join(' ');

  return (
    <div className={layoutClass}>
      <header className="topbar">
        <strong>Asket</strong>
        <Chip
          level={receiving ? 'ok' : 'alarm'}
          title={
            state.connected && !receiving
              ? 'The socket is still open but nothing is arriving. Everything on screen is old.'
              : ''
          }
        >
          {receiving
            ? 'Receiving'
            : state.connected
              ? `No data for ${duration((sinceMessageMs ?? 0) / 1000)}`
              : state.connecting
                ? 'Reconnecting…'
                : 'Offline'}
        </Chip>
        <span className={`mode-badge mode-${pico?.mode ?? 'UNKNOWN'}`}>
          {MODE_LABELS[pico?.mode] ?? 'Unknown'}
        </span>
        <Chip level={worstAlarm}>
          {state.alarms?.length ? `${state.alarms.length} alarm(s)` : 'No alarms'}
        </Chip>

        {/* Battery, recording and link, in the horizontal space that was empty.
            These three answer questions nobody should have to scroll for. */}
        <StatusStrip state={state} />

        <div className="spacer" />
        {state.hello?.source?.mode === 'sim' && (
          <Chip level="mock-banner" title="Every value on this screen is simulated.">
            Simulation
          </Chip>
        )}
        {connection.isMock && (
          <Chip
            level="mock-banner"
            title="No backend and no vessel. Every value is generated in this browser."
          >
            Mock data — browser only
          </Chip>
        )}
      </header>

      {/* Never hidden behind a disclosure triangle, and never only in a log.
          On the first real deployment this exact state — socket up, pings
          answering, every panel empty — took an hour to even notice, because
          it is indistinguishable from a vessel that is switched off. */}
      {banner && (
        <div className="starved-banner" role="alert">
          <strong>{banner.text}</strong>
          {banner.detail ? <span> {banner.detail}</span> : null}
        </div>
      )}

      <MissionMap
        state={state}
        connection={connection}
        follow={follow}
        onFollowChange={setFollow}
        showRawLidar={showRawLidar}
      />

      {/* Two explicit columns rather than a CSS multi-column: inside a
          vertically-scrolling box, `column-count` overflows sideways, where
          nothing can scroll to it.

          The split is by what you need first. Everything a headline can carry
          — battery, recording, link — is in the status strip, so the columns
          are ordered by what needs the detail: what the boat is doing on the
          left, what the payload and the mission are doing on the right. */}
      <aside className="sidebar">
        <div className="sidebar-col">
          <AlarmPanel state={state} />
          {/* Directly under the alarms: it is the first thing looked at on
              arriving at the site, and the most useful panel to somebody who
              did not write the system. It does not belong at the bottom. */}
          <DiagnosticsPanel state={state} connection={connection} />
          <VesselState state={state} />
          <ModeCommands state={state} connection={connection} />
          <HeadingPanel state={state} />
        </div>
        <div className="sidebar-col">
          <PowerPanel state={state} />
          {/* Above the sonar and lidar on purpose: this is the panel somebody
              turns to when the lidar says there is something there and they
              need to know what it is. */}
          <CameraPanel
            state={state}
            subscribed={Boolean(state.subscriptions.camera?.granted)}
          />
          <SonarPanel state={state} connection={connection} />
          <LidarPanel
            state={state}
            showRaw={showRawLidar}
            onShowRawChange={setShowRawLidar}
          />
          <MissionPanel state={state} connection={connection} />
          <LinkStatus state={state} connection={connection} />
        </div>
      </aside>

      {connection.isMock && (
        <MockControls connection={connection} open={mockOpen} onOpenChange={setMockOpen} />
      )}
    </div>
  );
}
