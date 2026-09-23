import { useEffect, useState } from 'react';
import { Panel, Chip } from '../components/Panel.jsx';
import { cameraAgeMs, cameraSilenceMs } from '../lib/connection.js';
import { bytes, num } from '../lib/format.js';

/**
 * The forward camera.
 *
 * The lidar says something is there. A human looks at the picture and works
 * out whether it is a ship, a rock or a buoy. That is the whole job, and it is
 * why there are no detection boxes here: the most accurate classifier
 * available for this scene is the operator, and nothing should be placed
 * between them and the photograph.
 *
 * ## Why this panel is mostly about not showing a picture
 *
 * Every other panel in this GUI renders absence as "not sent" and lets the
 * value sit there with its age beside it. That is right for a number. It is
 * wrong for a photograph, because a photograph is the one payload a human will
 * believe without reading its label. A frozen video feed is the most
 * convincing lie this interface could tell.
 *
 * It matters more on this hardware than it would have on the old assumption.
 * A directional link over water does not fade — it works and then it stops, in
 * about two seconds — so the last frame before a failure is crisp, recent and
 * completely trustworthy-looking. There is no gradual degradation to warn
 * anybody.
 *
 * So the ladder below escalates fast, in frame intervals AND in absolute
 * seconds, whichever is longer:
 *
 * | age | what happens |
 * |---|---|
 * | under 2 frames | live, timestamp only |
 * | 2 frames or 0.5 s | the live dot goes out — cheap to be wrong about |
 * | 5 frames or 1.5 s | desaturated and dimmed, with the age across it |
 * | 3 s | **blanked**, with the frame retrievable on purpose |
 *
 * Both scales are needed. Two missed frames at 15 fps is a seventh of a second
 * and means nothing; at 1 fps it is two seconds and means a great deal.
 *
 * **Recovery has no hysteresis**, deliberately — the opposite polarity to the
 * link profile selector. That damps a *decision*, because subscriptions
 * churning is worse than being briefly wrong. This reports a *fact*, and a
 * fact should not be damped: the first new frame is live again immediately.
 *
 * ## Blanking, and why the frame is kept
 *
 * At three seconds the picture is replaced. This is the only place in this GUI
 * that destroys information on purpose, and it is justified by the same
 * property that makes the panel dangerous: people believe photographs.
 *
 * But the frame is kept and can be shown deliberately. With this link a stale
 * frame is high quality and genuinely useful — it just must not be presented
 * as current. So the default is blanked and looking at it is an act, labelled
 * with its age.
 */

//: Age at which the picture stops claiming to be live, in frame intervals and
//: in milliseconds. The larger of the two applies.
const DIM_FRAMES = 2;
const DIM_MS = 500;
const STALE_FRAMES = 5;
const STALE_MS = 1500;
const BLANK_MS = 3000;

//: How long without any word from the backend before the silence is the
//: link's rather than the camera's. Generous against the status cadence: at
//: 5 Hz this is ten missed status frames, so it does not fire on a hiccup.
const LINK_SILENT_MS = 2000;

export function CameraPanel({ state, subscribed = true }) {
  // Age is wall-clock, not event-driven: nothing arrives to tell the panel
  // that the picture has got older, which is the entire problem. Without this
  // the display would freeze in exactly the case it exists to catch.
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 200);
    return () => clearInterval(timer);
  }, []);

  const [showStale, setShowStale] = useState(false);
  const camera = state.camera;
  const ageMs = cameraAgeMs(state);
  const silenceMs = cameraSilenceMs(state);
  const interval = 1000 / (camera?.rateHz || 5);

  const linkSilent = silenceMs !== null && silenceMs > LINK_SILENT_MS;
  const level = cameraLevel({ camera, ageMs, interval, linkSilent, state });

  useEffect(() => {
    if (level !== 'blank') setShowStale(false);
  }, [level]);

  return (
    <Panel
      title="Camera"
      id="camera"
      collapsible
      forceOpen={level === 'blank' || level === 'dead'}
      forceReason={level === 'blank' ? 'the picture is no longer current' : ''}
      aside={
        <Chip level={chipLevel(level)} title={CHIP_TITLES[level]}>
          {chipText(level, ageMs)}
        </Chip>
      }
      summary={
        <>
          <div className="camera-frame">
            {camera?.url && (level !== 'blank' || showStale) ? (
              <>
                <img
                  src={camera.url}
                  alt="Forward camera"
                  className={level === 'stale' || showStale ? 'camera-img stale' : 'camera-img'}
                />
                {/*
                  The age goes ON the picture, not beside it. Every other panel
                  puts age in its header; this is the one place it belongs on
                  the content, because the picture is where the eye goes and
                  the header is not what anybody is looking at.
                */}
                <span className="camera-age">{ageText(ageMs)}</span>
                {(level === 'stale' || showStale) && (
                  <span className="camera-band">
                    {showStale ? 'Not current — ' : ''}
                    {ageText(ageMs)} old
                  </span>
                )}
              </>
            ) : (
              <div className="camera-blank">
                <p className="camera-blank-headline">{BLANK_HEADLINE[level]}</p>
                <p className="hint">{blankDetail(level, camera, ageMs, silenceMs)}</p>
                {level === 'blank' && camera?.url && (
                  <button type="button" onClick={() => setShowStale(true)}>
                    Show the last frame ({ageText(ageMs)} old)
                  </button>
                )}
              </div>
            )}
          </div>
          {showStale && (
            <button
              type="button"
              className="camera-hide"
              onClick={() => setShowStale(false)}
            >
              Hide it again
            </button>
          )}
        </>
      }
    >
      <p className="hint" style={{ marginTop: 0 }}>
        No detection boxes. The model aboard knows six kinds of navigation mark
        and has no class for a vessel of any kind, so it would have nothing
        useful to say about most of what this camera sees. Your eyes are the
        classifier here.
      </p>
      {camera?.width && (
        <p className="hint" style={{ marginBottom: 0 }}>
          {camera.width}&times;{camera.height}, {num(camera.rateHz, 0, ' fps')} requested
          {camera.sizeBytes ? `, ${bytes(camera.sizeBytes)} per frame` : ''}
        </p>
      )}
      {!subscribed && (
        <p className="hint" style={{ marginBottom: 0 }}>
          Nothing is subscribed, so the vessel is not encoding frames at all.
        </p>
      )}
    </Panel>
  );
}

/**
 * Which rung of the ladder we are on.
 *
 * Ordered so the diagnosis wins over the symptom. "The link is silent" and
 * "the picture is four seconds old" are both true when the link drops, but
 * only the first tells the operator where to go.
 */
function cameraLevel({ camera, ageMs, interval, linkSilent, state }) {
  if (!state.connected) return 'disconnected';
  if (!camera) return 'waiting';
  if (linkSilent) return 'link-silent';
  if (camera.reason === 'camera_dead') return 'dead';
  if (ageMs === null) return 'waiting';
  if (ageMs >= BLANK_MS) return 'blank';
  if (ageMs >= Math.max(STALE_FRAMES * interval, STALE_MS)) return 'stale';
  if (ageMs >= Math.max(DIM_FRAMES * interval, DIM_MS)) return 'late';
  return 'live';
}

const CHIP_LEVELS = {
  live: 'ok',
  late: '',
  stale: 'warn',
  blank: 'alarm',
  dead: 'alarm',
  'link-silent': 'alarm',
  waiting: '',
  disconnected: 'alarm',
};

const CHIP_TITLES = {
  live: 'Frames are arriving at the negotiated rate.',
  late: 'A frame or two later than expected. Not yet worth acting on.',
  stale: 'The picture has stopped updating. It is no longer current.',
  blank: 'The picture is too old to show. It is kept and can be viewed deliberately.',
  dead: 'The camera driver never opened the device. There has never been a picture.',
  'link-silent':
    'The vessel has stopped saying anything about the camera at all, which means '
    + 'the link rather than the camera. The boat may be fine.',
  waiting: 'Subscribed; nothing has arrived yet.',
  disconnected: 'No connection to the vessel.',
};

const BLANK_HEADLINE = {
  blank: 'Picture hidden — no longer current',
  dead: 'No camera',
  'link-silent': 'No word from the vessel',
  waiting: 'Waiting for the first frame',
  disconnected: 'Disconnected',
  live: '',
  late: '',
  stale: '',
};

function chipLevel(level) {
  return CHIP_LEVELS[level] ?? '';
}

function chipText(level, ageMs) {
  switch (level) {
    case 'live': return 'Live';
    case 'late': return ageText(ageMs);
    case 'stale': return `${ageText(ageMs)} old`;
    case 'blank': return `Stopped ${ageText(ageMs)} ago`;
    case 'dead': return 'No camera';
    case 'link-silent': return 'Link silent';
    case 'waiting': return 'No frames yet';
    default: return 'Disconnected';
  }
}

/**
 * The sentence under a blanked picture, which is the only part of this panel
 * that tells somebody what to do next.
 *
 * Four silences, four different places to go: the device, the Jetson, the
 * link, or nothing-is-wrong-yet. Collapsing them into "no video" would throw
 * away the only actionable part.
 */
function blankDetail(level, camera, ageMs, silenceMs) {
  switch (level) {
    case 'dead':
      return 'The driver could not open the device and has published nothing. '
        + 'Check the camera cable and the vessel log — this is a boat-side fault, '
        + 'and nothing about it will change from here.';
    case 'link-silent':
      return `Nothing about the camera for ${ageText(silenceMs)}. The vessel has `
        + 'stopped reaching us, so this says nothing about the camera itself — '
        + 'check the Link panel first.';
    case 'blank':
      return camera?.reason === 'camera_frozen'
        ? 'The vessel is still reporting, so the link is up and the camera has '
          + 'stopped producing frames. The last picture is kept below; it was '
          + `taken ${ageText(ageMs)} ago and does not show where the boat is now.`
        : 'Frames stopped arriving. The last picture is kept below, but it is '
          + `${ageText(ageMs)} old and does not show where the boat is now.`;
    case 'waiting':
      return 'The camera is subscribed and the vessel has not sent a picture yet.';
    case 'disconnected':
      return 'Everything on screen is the last value received.';
    default:
      return '';
  }
}

function ageText(ms) {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1000) return `${Math.round(ms / 100) / 10}s`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.round(ms / 60_000)}m`;
}
