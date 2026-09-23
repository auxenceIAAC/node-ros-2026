import { Chip, Panel } from '../components/Panel.jsx';
import { ConfirmButton } from '../components/ConfirmButton.jsx';
import { streamPayload } from '../lib/connection.js';
import { COMMAND_STATUS_LABELS } from '../lib/labels.js';

/**
 * Pre-flight built-in test.
 *
 * The purpose is narrow and worth restating: when something is wrong on a
 * Namibian beach, identify the faulty link in thirty seconds rather than an
 * hour. So every failing item shows its **remedy**, not just its status — a red
 * word is not an instruction.
 *
 * A check that could not run shows as "unknown", never as a pass. Where the
 * unknown check is one we must be able to confirm, the overall verdict is
 * NO-GO: "GO, 14 checks could not run" is the most dangerous sentence this
 * panel could produce.
 */
const STATUS_LEVEL = { PASS: 'ok', WARN: 'warn', FAIL: 'alarm', SKIPPED: '' };
const STATUS_LABEL = { PASS: 'ok', WARN: 'warn', FAIL: 'fail', SKIPPED: 'unknown' };

export function DiagnosticsPanel({ state, connection }) {
  const report = streamPayload(state, 'diagnostics');

  // The most recent press, whatever became of it.
  //
  // This panel used to render only `report`, so a command that failed left no
  // trace on screen at all: the button's pending state went true and false in
  // the same breath and the panel sat exactly as before. On the Jetson the
  // command failed every time — `run_system_test` was never dispatched by
  // RosSource — and the operator's evidence for that was nothing whatsoever.
  //
  // A button that produces no visible outcome is the same silence that has
  // cost this project hours three times over. It now always resolves to
  // something: a verdict, a refusal, or "the vessel never answered".
  const lastRun = Object.values(state.commands)
    .filter((c) => c.name === 'run_system_test')
    .sort((a, b) => (a.issued_utc_ms || 0) - (b.issued_utc_ms || 0))
    .pop();
  const pending = lastRun?.status === 'pending';
  // Checks waiting on a value somebody has to enter, rather than on something
  // being repaired. A check only carries `setup_field` when the honest answer
  // is "nobody has told us this yet".
  const awaiting = items.filter((item) => item.setup_field && item.status !== 'PASS');

  // A report that predates the press has not answered it. Saying "GO" from a
  // report taken before the button was touched would be the panel answering a
  // question nobody asked.
  const answered =
    lastRun
    && lastRun.status !== 'pending'
    && report
    && report.run_utc_ms >= (lastRun.issued_utc_ms || 0);

  const items = report?.items || [];
  const degrading = report?.history?.degrading || [];

  // Anything short of a clean GO opens the list. The verdict sentence names the
  // first problem; the list is what you act on, and having to ask for it is one
  // step too many when it is already telling you something is wrong.
  const notClean = items.filter((i) => i.status !== 'PASS');
  const forced = report && !report.go
    ? 'the vessel is NO-GO'
    : notClean.length
      ? `${notClean.length} check(s) not passing`
      : '';

  return (
    <Panel
      title="Pre-flight"
      id="preflight"
      collapsible
      forceOpen={Boolean(forced)}
      forceReason={forced}
      aside={
        report ? (
          <Chip level={report.go ? 'ok' : 'alarm'}>{report.go ? 'GO' : 'NO-GO'}</Chip>
        ) : null
      }
      summary={
        <>
          {report ? (
            <p
              className={report.go ? '' : 'errline'}
              style={{ margin: '0 0 8px', fontWeight: 600 }}
            >
              {report.summary}
            </p>
          ) : (
            <p className="hint" style={{ margin: '0 0 8px' }}>
              No pre-flight has run yet in this session.
            </p>
          )}
          <div className="button-row">
            <ConfirmButton
              label="Run pre-flight"
              prompt="Run all passive checks?"
              pending={pending}
              disabled={!state.connected}
              onConfirm={() => connection.command('run_system_test', {})}
            />
          </div>
          {lastRun && !answered && (
            <p
              className={lastRun.status === 'failed' ? 'errline' : 'hint'}
              style={{ margin: '6px 0 0' }}
            >
              {COMMAND_STATUS_LABELS[lastRun.status] || lastRun.status}
              {lastRun.detail ? ` — ${lastRun.detail}` : ''}
              {/* Accepted, and then nothing came back. The request reached the
                  vessel and no fresh report followed, which is a different
                  fault from a refusal and sends you somewhere different. */}
              {lastRun.status === 'confirmed' && !report
                ? ' — the request was sent, but no report has arrived'
                : ''}
            </p>
          )}
        </>
      }
    >
      <p className="hint" style={{ marginTop: 6, marginBottom: 0 }}>
        Passive checks only. Motor tests never run automatically and are not
        available from this panel.
      </p>

      {/*
        "Nobody has told us this yet" is not the same problem as "this is
        broken", and until now the panel rendered them identically: both amber,
        both with a remedy, both easy to read past.

        That is not a theory. `sonar.mounting` has returned the same amber
        warning on every run for two weeks, naming a YAML file that lives
        behind an SSH session nobody in the club has, and nothing has happened.
        Separating the two is the smallest change that could plausibly get it
        cleared — a missing value is a job somebody can finish, and a list of
        them is short enough to finish today.

        The names are still fields rather than links because the setup page
        does not exist yet. When it does, each becomes a link that opens it
        with that field in view; nothing else here changes.
      */}
      {awaiting.length > 0 && (
        <div className="awaiting-setup">
          <p className="awaiting-setup-headline">
            {awaiting.length === 1
              ? 'One value has never been entered'
              : `${awaiting.length} values have never been entered`}
          </p>
          <p className="hint" style={{ marginTop: 0 }}>
            Not faults — things nobody has told the vessel yet. They stay amber
            until somebody does.
          </p>
          <ul className="awaiting-setup-list">
            {awaiting.map((item) => (
              <li key={item.id}>
                <span className="setup-field">{item.setup_field}</span>
                <span className="hint"> — {item.name}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {items.length > 0 && (
        <div style={{ marginTop: 10 }}>
          {items.map((item) => (
            <div
              key={item.id}
              style={{ borderTop: '1px solid var(--line)', padding: '6px 0' }}
            >
              <div style={{ display: 'flex', justifyContent: 'space-between', gap: 8 }}>
                <span>{item.name}</span>
                <Chip level={STATUS_LEVEL[item.status]}>{STATUS_LABEL[item.status]}</Chip>
              </div>
              <div className="hint">{item.message}</div>
              {item.remedy && item.status !== 'PASS' && (
                <div className={item.status === 'FAIL' ? 'errline' : 'warnline'}>
                  {item.remedy}
                </div>
              )}
              {item.setup_field && item.status !== 'PASS' && (
                <div className="hint">
                  Waiting on <span className="setup-field">{item.setup_field}</span>
                </div>
              )}
              {degrading.includes(item.id) && (
                <div className="warnline">
                  Drifting across recent runs — still passing, but getting worse.
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
