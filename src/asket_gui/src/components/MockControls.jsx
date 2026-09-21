import { useState } from 'react';

import { SCENARIOS } from '../lib/mock/scenarios.js';

/**
 * The dev control panel. Mock mode only.
 *
 * Its job is to make every condition that changes what the interface may claim
 * reachable by hand, in a browser, in a few seconds — so those conditions get
 * reviewed rather than assumed. Each control carries the behaviour it is meant
 * to demonstrate, so somebody who did not write the GUI can tell whether what
 * they are looking at is correct.
 *
 * It never appears against a real backend: `App` renders it only when
 * `connection.isMock` is true, and `isMock` is set only by the mock factory.
 */
export function MockControls({ connection, open, onOpenChange }) {
  const [, force] = useState(0);
  const [explained, setExplained] = useState(null);

  const world = connection.mock?.world;
  if (!world) return null;

  const redraw = () => force((n) => n + 1);

  const toggleFault = (id) => {
    world.setFault(id, !world.hasFault(id));
    redraw();
  };

  const setProfile = (id) => {
    connection.setProfile(id === 'auto' ? 'auto' : id);
    redraw();
  };

  // The one control that is not a toggle: a sequence, run through the ordinary
  // command path so that what it produces is what pressing the same buttons
  // produces. Mirrors SimSource.bring_up_healthy() in the backend.
  const bringUpHealthy = () => {
    for (const group of SCENARIOS) {
      for (const item of group.items) {
        if (item.kind === 'fault' && world.hasFault(item.id)) world.setFault(item.id, false);
      }
    }
    connection.setProfile('auto');
    connection.command('set_mode', { mode: 'AUTONOMOUS' });
    connection.command('start_mission', { name: 'demo' });
    connection.command('run_system_test', {});
    redraw();
  };

  const toggle = (id) => {
    if (id === 'confirm_slow') {
      world.confirmDelayS = world.confirmDelayS > 1 ? 0.6 : 2.5;
    } else if (id === 'confirm_fails') {
      world.confirmAlwaysFails = !world.confirmAlwaysFails;
    } else if (id === 'tiles') {
      world.mockTilesAvailable = !world.mockTilesAvailable;
      connection.notifyTilesChanged();
    }
    redraw();
  };

  const isOn = (item) => {
    if (item.kind === 'action') return false;
    if (item.kind === 'fault') return world.hasFault(item.id);
    if (item.kind === 'profile') {
      return item.id === 'auto'
        ? !connection.state.profile?.manual
        : connection.state.profile?.manual && connection.state.profile.profile === item.id;
    }
    if (item.id === 'confirm_slow') return world.confirmDelayS > 1;
    if (item.id === 'confirm_fails') return world.confirmAlwaysFails;
    if (item.id === 'tiles') return Boolean(world.mockTilesAvailable);
    return false;
  };

  const act = (item) => {
    if (item.kind === 'fault') toggleFault(item.id);
    else if (item.kind === 'profile') setProfile(item.id);
    else if (item.kind === 'action') bringUpHealthy();
    else toggle(item.id);
  };

  return (
    <div className={`mock-panel ${open ? '' : 'collapsed'}`}>
      <button className="mock-handle" onClick={() => onOpenChange(!open)}>
        {open ? '▸ Hide mock controls' : '◂ Mock controls'}
      </button>

      {open && (
        <div className="mock-body">
          <p className="hint" style={{ marginTop: 0 }}>
            Simulated data, generated in this browser. No backend, no vessel.
          </p>

          {SCENARIOS.map((group) => (
            <div key={group.group} className="mock-group">
              <h3>{group.group}</h3>
              <div className="button-row">
                {group.items.map((item) => (
                  <button
                    key={item.id}
                    className={isOn(item) ? 'mock-on' : ''}
                    onClick={() => act(item)}
                    onMouseEnter={() => setExplained(item)}
                    onFocus={() => setExplained(item)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
          ))}

          <div className="mock-group">
            <h3>Survey speed</h3>
            <div className="button-row">
              {[1, 4, 12].map((scale) => (
                <button
                  key={scale}
                  className={connection.mock.transport?.timeScale === scale ? 'mock-on' : ''}
                  onClick={() => {
                    if (connection.mock.transport) connection.mock.transport.timeScale = scale;
                    redraw();
                  }}
                >
                  {scale}×
                </button>
              ))}
            </div>
            <p className="hint" style={{ margin: '4px 0 0' }}>
              Speeds the vessel up, not the clock — timestamps stay real so data
              age keeps meaning what it says.
            </p>
          </div>

          <div className="mock-group">
            <h3>Heading source</h3>
            <div className="button-row">
              {['magnetometer', 'gnss_compass'].map((source) => (
                <button
                  key={source}
                  className={(world.cfg.headingSource || 'magnetometer') === source ? 'mock-on' : ''}
                  onClick={() => {
                    world.cfg.headingSource = source;
                    redraw();
                  }}
                >
                  {source === 'gnss_compass' ? 'GNSS compass' : 'Magnetometer'}
                </button>
              ))}
            </div>
            <p className="hint" style={{ margin: '4px 0 0' }}>
              Compare the seabed error at 50 m: about 6 m on the magnetometer,
              about 17 cm on the compass.
            </p>
          </div>

          <div className="button-row" style={{ marginTop: 8 }}>
            <button
              onClick={() => {
                world.faults.clear();
                world.confirmAlwaysFails = false;
                world.confirmDelayS = 0.6;
                connection.setProfile('auto');
                redraw();
              }}
            >
              Clear everything
            </button>
          </div>

          {explained && (
            <div className="mock-explain">
              <strong>{explained.label}</strong>
              <div className="hint">{explained.detail}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
