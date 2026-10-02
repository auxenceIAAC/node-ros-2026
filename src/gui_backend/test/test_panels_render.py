"""Every panel must survive being rendered, on a live state and on an empty one.

This file exists because of a bug it would have caught the same day. The
pre-flight panel referenced `items` four lines above the `const items = ...`
that declares it — legal to parse, a temporal-dead-zone `ReferenceError` the
instant React renders it. Nothing in this repository ran the JSX, so the panel
that is meant to be the first thing looked at on arriving at a site threw on
every render, and the suite was green.

So the panels are rendered, under Node, through `react-dom/server`. Two states,
because they fail differently:

* **A live state**, from the mock connection with the real subscription list —
  the ordinary paths, with data in them.
* **The empty state**, straight from `initialState()` — which is not a
  contrived fixture but the state the app genuinely starts in, and the one the
  GUI spent an hour in on its first real deployment with every panel blank.

The setup page gets a third pass of its own. `renderToStaticMarkup` runs no
effects, so rendering `SetupPage` only ever reaches its "Reading the setup…"
branch — which would make the largest surface here the least covered one. So
its field rows, file note and save bar are rendered directly against two real
documents built from the generated inventory: nothing answered, and everything
answered with the Deployment values old enough to be stale. That is every
field in both states, which is where this page's branching actually is.

This is a smoke test and makes no claim about what the panels say. It claims
only that they do not throw, which is the thing nothing else here checks.

`MissionMap` is excluded: it is the one panel that reaches for WebGL through
maplibre, and a headless stub for it would be testing the stub.

Skips when Node, `node_modules` or esbuild are absent — a cross-language check,
not a build dependency.
"""

import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

GUI = Path(__file__).resolve().parents[2] / "asket_gui"
ESBUILD = GUI / "node_modules" / ".bin" / "esbuild"

#: Everything in `src/panels` and `src/components` that takes `state`, less the
#: map. Listed rather than globbed so that adding a panel is a deliberate line
#: here — a glob would quietly stop covering a panel whose export was renamed.
PANELS = (
    ("AlarmPanel", "panels/AlarmPanel.jsx"),
    ("DiagnosticsPanel", "panels/DiagnosticsPanel.jsx"),
    ("HeadingPanel", "panels/HeadingPanel.jsx"),
    ("CameraPanel", "panels/CameraPanel.jsx"),
    ("LidarPanel", "panels/LidarPanel.jsx"),
    ("LinkStatus", "panels/LinkStatus.jsx"),
    ("MissionPanel", "panels/MissionPanel.jsx"),
    ("ModeCommands", "panels/ModeCommands.jsx"),
    ("PowerPanel", "panels/PowerPanel.jsx"),
    ("SonarPanel", "panels/SonarPanel.jsx"),
    ("VesselState", "panels/VesselState.jsx"),
    ("StatusStrip", "components/StatusStrip.jsx"),
    # Not a cockpit panel, but the largest surface here and the one most worth
    # rendering: it is read by somebody standing on a beach with a tape
    # measure, and every state it shows is a state nobody had looked at.
    ("SetupPage", "panels/SetupPage.jsx"),
)

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not ESBUILD.exists(),
    reason="node or the frontend's node_modules is not present",
)


def _entry_source() -> str:
    imports = "\n".join(
        f"import {{ {name} }} from './src/{path}';" for name, path in PANELS
    )
    listed = ", ".join(f"['{name}', {name}]" for name, _ in PANELS)
    return textwrap.dedent(
        f"""
        import {{ renderToStaticMarkup }} from 'react-dom/server';
        import {{ createMockConnection }} from './src/lib/mock/index.js';
        import {{ initialState }} from './src/lib/connection.js';
        import {{ SUBSCRIPTIONS }} from './src/App.jsx';
        {imports}

        const PANELS = [{listed}];

        // The real client with the real subscription list, so what is rendered
        // is fed by the same path the browser is fed by.
        const connection = createMockConnection({{ timeScale: 40 }});
        connection.connect();
        connection.subscribe(SUBSCRIPTIONS);
        // A pre-flight report only exists once one has been run, so the panel
        // that this file was written for would otherwise be rendered on its
        // "nothing has run yet" branch in both states.
        connection.command('run_system_test', {{}});
        await new Promise((r) => setTimeout(r, 2500));
        const live = connection.getSnapshot();

        const out = {{ streams: Object.keys(live.streams || {{}}), errors: [], rows: 0 }};
        for (const [label, state] of [['live', live], ['empty', initialState()]]) {{
          for (const [name, Panel] of PANELS) {{
            try {{
              renderToStaticMarkup(
                <Panel
                  state={{state}}
                  connection={{connection}}
                  subscribed={{false}}
                  showRaw={{false}}
                  onShowRawChange={{() => {{}}}}
                  onClose={{() => {{}}}}
                />,
              );
            }} catch (err) {{
              out.errors.push(`${{name}} (${{label}} state): ${{err && err.message}}`);
            }}
          }}
        }}

        // -- the setup page's rows, against real documents ----------------
        const S = await import('./src/lib/mock/setupProfile.js');
        const SP = await import('./src/panels/SetupPage.jsx');
        const now = Date.now();
        const documents = {{
          empty: S.setupDocument({{}}, now, {{ path: '/x', missing: true }}),
          filled: S.setupDocument(S.filledSetupValues(now), now, {{ path: '/x', found: true }}),
          broken: S.setupDocument({{}}, now, {{ path: '/x', error: 'line 3: bad indent' }}),
        }};
        for (const [label, doc] of Object.entries(documents)) {{
          const staleIds = new Map(doc.stale.map((x) => [x.id, x]));
          const outstandingIds = new Set(doc.outstanding.map((o) => o.id));
          try {{
            renderToStaticMarkup(<SP.SetupFileNote doc={{doc}} />);
          }} catch (err) {{
            out.errors.push(`SetupFileNote (${{label}}): ${{err && err.message}}`);
          }}
          for (const spec of doc.fields) {{
            // Each row twice: as it stands, and with an unsaved edit on it.
            for (const edit of [undefined, '12.5']) {{
              try {{
                renderToStaticMarkup(
                  <SP.SetupField
                    spec={{spec}}
                    entry={{doc.values[spec.id] ?? null}}
                    effective={{doc.effective[spec.id]}}
                    outstanding={{outstandingIds.has(spec.id)}}
                    stale={{staleIds.get(spec.id) ?? null}}
                    edit={{edit}}
                    onEdit={{() => {{}}}}
                    onRevert={{() => {{}}}}
                    focused={{false}}
                    focusRef={{null}}
                  />,
                );
                out.rows += 1;
              }} catch (err) {{
                out.errors.push(
                  `SetupField ${{spec.id}} (${{label}}, ${{edit ? 'edited' : 'as saved'}}): `
                  + `${{err && err.message}}`,
                );
              }}
            }}
          }}
          // The save bar with nothing dirty, and with two values dirty.
          for (const dirty of [[], doc.fields.slice(0, 2).map((f) => f.id)]) {{
            const edits = {{}};
            for (const id of dirty) edits[id] = '1';
            try {{
              renderToStaticMarkup(
                <SP.SetupSaveBar
                  dirty={{dirty}}
                  edits={{edits}}
                  doc={{doc}}
                  gate={{{{ allowed: false, reason: 'armed', remedy: 'channel 7' }}}}
                  state={{live}}
                  connection={{connection}}
                  onDiscard={{() => {{}}}}
                  onSaved={{() => {{}}}}
                />,
              );
            }} catch (err) {{
              out.errors.push(`SetupSaveBar (${{label}}, ${{dirty.length}} dirty): `
                + `${{err && err.message}}`);
            }}
          }}
        }}

        console.log(JSON.stringify(out));
        process.exit(0);
        """
    )


@pytest.fixture(scope="module")
def rendered(tmp_path_factory) -> dict:
    # The entry lives in the frontend directory so that `react` and the
    # relative imports resolve the way they do for a real build. Named with a
    # leading dot and removed afterwards.
    entry = GUI / ".render-smoke-entry.jsx"
    bundle = GUI / ".render-smoke-bundle.mjs"
    entry.write_text(_entry_source())
    try:
        build = subprocess.run(
            [
                str(ESBUILD), str(entry),
                "--bundle", "--format=esm", "--platform=node",
                f"--outfile={bundle}",
                "--loader:.jsx=jsx", "--jsx=automatic",
                # Stylesheets are not what this renders, and `App.jsx` pulls
                # maplibre's in through the map. Discarded rather than left
                # external, which hands Node a .css file it cannot load.
                "--loader:.css=empty",
                # React is left to Node so its CommonJS server build loads the
                # way it expects.
                "--external:react", "--external:react-dom",
                "--external:react-dom/server", "--external:react/jsx-runtime",
            ],
            capture_output=True, text=True, timeout=180,
        )
        if build.returncode != 0:
            pytest.fail(f"esbuild failed:\n{build.stderr}")
        # esbuild's warnings are the cheapest static check this repository has,
        # and it is how the duplicate `waveHeightM` in the mock world was
        # found. Treated as failures so the next one is not read past.
        if "warning" in build.stderr.lower():
            pytest.fail(f"esbuild warnings:\n{build.stderr}")
        run = subprocess.run(
            ["node", str(bundle)], capture_output=True, text=True, timeout=180,
            cwd=str(GUI),
        )
        if run.returncode != 0:
            pytest.fail(f"node failed:\n{run.stdout[-2000:]}\n{run.stderr[-4000:]}")
        return json.loads(run.stdout.strip().splitlines()[-1])
    finally:
        entry.unlink(missing_ok=True)
        bundle.unlink(missing_ok=True)


def test_no_panel_throws(rendered):
    assert rendered["errors"] == []


def test_every_setup_field_renders_in_every_state(rendered):
    """Twenty fields, three documents, two edit states. If this number falls to
    nothing the page is being declared sound without having been drawn."""
    assert rendered["rows"] >= 100


def test_the_live_state_actually_has_data_in_it(rendered):
    """Otherwise this file quietly becomes the empty case rendered twice, and
    stops covering the paths that have values in them — which is most of the
    code in a panel."""
    streams = set(rendered["streams"])
    for name in ("vessel", "pico", "link", "power", "sonar", "diagnostics"):
        assert name in streams, f"the mock served no {name}"
