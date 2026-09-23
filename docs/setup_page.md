# The mission setup page

Design, before code. Everything here is a proposal; the parts I would argue
with are marked, and there are more of them than usual because the hardest
question this page raises — what a stranger sees on an empty one — has an
answer I only half like.

## The problem, in one paragraph

A growing list of values has to be told to this system before a mission, and
they currently live as YAML on the Jetson: sonar mounting geometry and lever
arm, battery capacity, survey area, sonar range and gain, and now the shore
station's position and bearing. Editing them needs an SSH session. Nobody in
the club except Auxence has one, which is why `measured: false` in
`omniscan_bridge/config/mounting.yaml` has survived two weeks of pre-flight
warnings naming that file on every single run. **A warning that names
something nobody can open is not a warning, it is background noise**, and the
whole club has learned to read past it.

## What this page is, and is not

It is a **preparation** page. Preparing a mission and running one are different
activities: preparation is calm, on the ground, with time to think; running is
tense and wants very few things on screen. Mixing them is what clutters a
cockpit, so this is a separate route — `/setup` on the same server, sharing
the same connection — and not a panel.

It is **not in the safety chain**, and nothing on it can arm the vessel, move
it, or change its mode. It cannot start or stop a mission. If it could make
the boat do something, it would have stopped being a preparation page.

It is also **not the mission planner**. That distinction gets its own section,
because the line is less obvious than it first looked.

## Two axes, not one

The working rule was: *does the value describe the world, or the equipment?*
World is planning, equipment is setup. That is right, and it is the answer to
**"which page?"**.

But it does not answer **"which section?"**, and the second question turns out
to matter more. The two tiers are decided by a different axis entirely:

> **The tier is the value's expected lifetime.**

* **Vessel** — changes when somebody rebuilds the boat. Mounting geometry,
  lever arm, battery capacity, camera calibration, antenna heights. A vessel
  value three months old is fine.
* **Deployment** — changes every time somebody sets up on a beach. Shore
  station position and bearing, sea state, channel width, local origin. A
  deployment value three months old is almost certainly wrong.

This is worth encoding rather than merely arranging, and it is the argument
for the split: it is the only thing that lets the page say *"this was last set
in July, and it is the kind of value that should not be"*. A flat list cannot
say that, and it is precisely what somebody arriving on a beach needs to know
when they cannot remember what was last set. Every field therefore carries a
date, and only the Deployment tier goes stale.

## What goes on it

### Vessel

| Field | Why it matters | Currently |
|---|---|---|
| **Sonar mounting geometry** — tilt, yaw, pitch, lever arm (aft/starboard/below), and which side the fan looks | Depends on how the head is physically bolted on, not on the mission. Every sounding carries the offset, it does not average out, and the survey is simply displaced and looks plausible. | `mounting.yaml`, `measured: false` |
| `measured` / `measured_by` / `measured_utc` | Provenance. Not decoration — see below. | same |
| Battery capacity (Wh), hotel load (W) | Endurance is arithmetic on these. Wrong here means a confident wrong number. | `topics.yaml` |
| Shore antenna gain, beamwidths | Link budget. | `link_budget.py` defaults |
| Boat antenna height above water | Dominates useful range: the two-ray term goes as the product of the two heights. | guess |
| Camera intrinsics | Read-only here; produced by `calibration`. Shown so somebody can see whether it has ever been done. | `front_camera.yaml` |

### Deployment

| Field | Why it matters | Currently |
|---|---|---|
| Shore station position | Range and bearing from the sector's boresight. | `topics.yaml`, `configured: false` |
| Shore station bearing (boresight) | The number that turns "the link dropped" into "the link is going to drop". | same |
| Tripod height | Two-ray, and the close-in nulls. | default 2.9 m |
| Sea state | Decides whether the multipath nulls exist at all. | default 0.5 m |
| Channel width | 3 dB of range per doubling, against throughput. | default 40 MHz |
| Local origin (datum) | Everything in metres is relative to it. | derived, no override |
| **Sonar range and gain** | The other half of the sonar, and the half that changes: both follow the expected depth, so they change with the beach. | `mission_defaults.yaml` |

## The three places the rule broke

**The sonar splits across both tiers, and that is right.** Mounting geometry
— offset and angle — depends on how the head is bolted to the hull. It is
measured once on a given boat and kept, so it is Vessel, and it is the value
that corrupts data if wrong. Range and gain depend on the expected depth, so
they change with the beach and are Deployment. Two different kinds of fact
about one instrument, and putting them in one tier would make the staleness
rule wrong for whichever half lost.

Range is *also* the place the world/equipment axis frays: it is an equipment
setting derived from a world fact. The page holds the **default**; the planner
may override it per area, because a box spanning 8 m and 40 m of water
genuinely wants two settings and forcing one is how a bad line gets recorded
and nobody notices until post-processing.

**The shore station is the exception that proves the page.** It describes
equipment, so by the world/equipment axis it is setup — but it moves every
mission and can be turned by hand mid-mission, so by lifetime it behaves like
planning. It fits neither "set once in the workshop" nor "part of the plan",
and that gap is exactly the shape of the page.

**"Survey area" hides a datum.** The area is planning. The **local origin** is
neither: it is the reference every recorded metre is relative to, fixed once
per mission and never moved. It is derived from the first fix, and overriding
it is a deliberate act behind a confirmation, because a human typing a
latitude is a human putting a typo into every metre of the mission.

## Arriving with nothing

This is the case that decides whether the rest of the club can use this, so it
gets the most space.

A fresh Jetson, a beach, somebody who is not Auxence. The two obvious answers
are both wrong. **A wizard** is wrong because this is done once and then
*edited* forever after, and a wizard you cannot skip is hostile on the second
use. **A form with twenty blank fields** is wrong because nothing on it says
which three matter today.

### 1. The page is the checklist

Fields are ordered by *what blocks what*, not by category. The top of the page
is not a form, it is a sentence and a short list:

> **Two things need filling in before this boat should survey.**
> One of them will ruin your data. The other will only make the screen less
> useful.

Everything already set collapses under a heading that says how many and when.
The ranking is the important part: **only the mounting geometry corrupts
recorded data**. A missing station position makes the link panel useless and
costs nothing that cannot be recovered afterwards. An operator with twenty
minutes of daylight needs to know which is which, and today nothing tells them
— all the warnings are amber and all of them name files.

### 2. Most of it is captured, not typed

The insight that makes an empty page tractable: **the boat is a GPS receiver,
and you are standing next to the tripod holding it.**

* **Station position** — put the vessel beside the tripod, press *Set from
  vessel*. Accuracy of a few metres against a 120° sector at 2.5 km is
  nothing. Nobody types a latitude.
* **Boresight** — sail out, aim the antenna at the boat, press *Set from
  vessel bearing*. The bearing from station to vessel becomes the boresight.
  This is not only easier than a compass reading, it is more accurate, and it
  measures the thing you actually care about rather than a proxy for it.
* **Local origin** — the first fix, automatically.
* **Sea state** — a three-way choice (*glassy / slight / moderate*), not a
  number in metres. Nobody knows the RMS wave height, and a field that invites
  a made-up number is worse than a coarse one that invites an honest answer.
* **Antenna and tripod heights** — a tape measure, typed once, Vessel tier.

Only the mounting geometry genuinely requires somebody with a tape measure and
a still boat, and that is the one that deserves the friction.

### 3. Provenance is a first-class field

Every value carries where it came from: `default`, `entered`, `captured`, or
`measured`. The page shows provenance next to the value, not just the value.

This is the mechanism that makes the whole thing work, and it generalises what
`mounting.yaml` already does with its `measured:` line. **A field still on
`default` is what the pre-flight warns about** — so "has anybody actually
checked this?" becomes a property of the data rather than a comment in a file,
and it is the same question for all twenty fields instead of one special case.

### 4. Saved deployments

A deployment can be saved by name (*Walvis Bay, north box*) and reloaded. The
first visit to a beach is the slow one; the twentieth is *load, adjust the
boresight, go*. Without this the page is a chore that gets skipped, which
returns us to `measured: false`.

## Pre-flight: from a warning to a task

The current `sonar.mounting` WARN reads:

> Measure the tilt and the lever arm from the GNSS antenna to the transducer,
> write them into `mounting.yaml`, and set `measured: true`.

Every word is true and it has achieved nothing in two weeks, because the
remedy names a file behind an SSH session. The fix is structural, not
editorial:

* `CheckResult` gains **`setup_field`** — the id of the field this check
  wants, e.g. `vessel.sonar.mounting`. Not a path. Not a sentence.
* The pre-flight panel renders any check carrying one as a **link that opens
  `/setup` focused on that field**, and the remedy is written in terms of the
  field: *"Sonar mounting geometry has never been measured."*
* The page shows the same fields flagged from the same source, so the two
  cannot disagree about what is outstanding.

Of the eighteen checks today, five would carry a `setup_field`:
`sonar.mounting`, `pico.estop_feedback` (which names a firmware flag and will
stay amber — correctly — but should say where), and the three that would be
added for station position, battery capacity and antenna height.

**The clearing has to stay honest.** `sonar.mounting` already reads the
provenance the *bridge reported*, not the file on disk, so editing the file
without restarting cannot turn the check green while the old numbers are in
use. That property becomes more important once a form can write the file, not
less: a form that appears to succeed while the node still uses the old values
is strictly worse than an SSH session, because it also destroys the operator's
reason to doubt.

## Waiving a pre-flight check

Mounting geometry stays WARN — it never changes on a given hull, so it is a
thing to do once and keep, not a thing that goes stale, and escalating it
would be treating a Vessel-tier value as if it were a Deployment one.

But the pre-flight should be hard to get round. There are real cases — testing
at the pontoon, a sensor deliberately not in use — so a waiver has to exist.
The risk is that it becomes a checkbox somebody ticks by habit, at which point
the whole pre-flight has been switched off without anybody deciding to.

### What a waiver *means*

The rule that decides everything below, and it is narrower than "are you
sure?":

> **A waiver is a declaration that a subsystem is not in use for this mission.
> It is never a declaration that you accept a degraded version of a subsystem
> you are using.**

That has teeth. Waiving `lidar.spin` says "no lidar today", which is coherent
and harmless — nothing records lidar into the survey product. Waiving
`sonar.mounting` would say "I am surveying with unknown geometry", which is
not a statement about what is in use; it is accepting corrupt output. The test
for whether a check is waivable is therefore mechanical: **does "not in use"
mean anything for it?**

### Never waivable

| Check | Why not |
|---|---|
| `sonar.mounting` | Every sounding carries the same unknown offset, and it looks entirely plausible until somebody overlays a second survey. There is no "the mounting is not in use". |
| `sonar.clock` | A wrong sonar clock makes the whole mission un-georeferenceable. If the sonar is in use, its clock is in use. |
| `gnss.fix` | Nothing on this boat works without position, and a survey recorded on a poor fix is junk that looks like data. There is no "GNSS not in use". |
| `heading.valid` | The swath is placed by heading. Invalid heading is silently displaced data. |
| `rc.link` | **The safety chain.** The transmitter is how a human takes the boat back. Not waivable under any circumstance, in any mode, at any range. |
| `pico.link` | Same. Without it the vessel cannot be commanded or observed. |
| `pico.firmware_version`, `pico.state_format` | If these fail the GUI cannot read mode or arming, so waiving them waives the ability to notice anything else. |

The first four are Auxence's rule applied literally: **anything that can
corrupt recorded data**. The last three are a second principle that falls out
of the same reasoning — *you may not waive the thing you would use to detect
that the waiver was a mistake*.

`pico.estop_feedback` is in neither group. It is the permanent amber that
exists because `ESTOP_FEEDBACK_ENABLED` is 0 and nothing confirms the ESC rail
collapsed. Waiving it would silence the only honest statement anybody makes
about the e-stop path, and since it is WARN it blocks nothing anyway. It is
**acknowledged, never waived**, and it clears by a firmware change.

### Waivable, as a declaration of non-use

| Check | The declaration | Consequence |
|---|---|---|
| `lidar.spin` | no lidar this mission | obstacle overlay off, and says why |
| `sonar.link` | not surveying — driving only | **recording refuses to start** |
| `disk.space`, `disk.speed` | not recording | recording refuses to start |
| `link.quality` | working alongside on a poor link | none; it is already a quality warning |
| `battery.charge` | short bench run | none, but endurance is shown as unknown rather than a number |
| `imu.rest`, `heading.divergence` | vessel is moving during pre-flight | heading quality shown as degraded |
| `ros.nodes` | a named node is deliberately not launched | that node's panels say "not running" |

The consequences matter as much as the permissions. Waiving `sonar.link` and
then recording anyway would produce a mission directory that looks like a
survey and contains none, so **the waiver disables the thing it excused**.
A waiver that costs nothing is a waiver that gets ticked.

### Making it visible afterwards

Five mechanisms, and the fifth is the one that actually prevents a habit:

1. **A typed reason, not a dropdown.** Free text, and a dropdown of pre-canned
   excuses is exactly the thing that becomes muscle memory. Somebody writing
   "bench test, sonar disconnected" has thought about it; somebody selecting
   *Other* has not.
2. **Scoped to one mission, and expiring.** Not a setting. The next mission
   starts with every check live again.
3. **Written into the mission directory**, beside the trajectory — so it is in
   the *data*, not only in a UI nobody will be looking at in three months. A
   survey can be audited afterwards without anybody remembering.
4. **Never a plain GO.** The verdict reads *"GO — 2 checks waived"*, in
   different words and a different colour, and the cockpit carries a standing
   badge for the whole mission rather than a toast that disappears.
5. **A count, shown on the setup page.** *"This vessel has been launched with
   waived checks 4 times."* A single waiver is a judgement; four is a pattern,
   and the only way a pattern becomes visible is if something counts it. This
   is the mechanism I would fight hardest to keep, because it is the one that
   addresses the actual failure mode — not any individual waiver, but the
   habit.

And: **a waived check that starts passing clears its own waiver.** Otherwise a
stale waiver hides a real regression later, which turns a safety mechanism
into a blindfold.

### Where I would argue with myself

**`battery.charge` may belong in the unwaivable group.** A flat battery does
not corrupt data, but it does put a drifting boat somewhere a human has to go
and get it, and "short bench run" is exactly what somebody says before a
two-hour session. I have left it waivable because pontoon testing is a real
need, but I would not object to it moving.

**`ros.nodes` is not one check.** It reports on several nodes at once, so a
waiver is all-or-nothing when what somebody wants is "not `boat_bt` today".
Either it splits per node, or the waiver takes a node name. I would split it,
and I have not designed that here.

**The count could be gamed by a fresh Jetson**, which starts at zero. It lives
in the setup file, so restoring a saved deployment restores the count — which
is right — but somebody starting clean loses the history. I think that is
acceptable; the alternative is storing it somewhere a person cannot reach,
which is a different kind of dishonest.

## Storage

**One file per deployment on the Jetson**, overlaying the per-package defaults
rather than replacing them. The packages keep reading their own YAML; the
setup file is applied on top.

The alternative — the page writing directly into six packages' config files —
was rejected because it makes the page know the layout of every package, and
every future package has to be taught about the page. It also means a partly
applied write leaves the vessel in a state no file describes.

Format: plain YAML, with `written_utc_ms`, `written_by`, a content hash, and a
per-field provenance map. The hash is not security; it is so the pre-flight
can say *"this setup was written fourteen minutes ago"* rather than silently
accepting whatever is on disk.

## Transfer

Auxence asked for cable-only while the boat is alongside, and suggested the
Jetson enforce it rather than the UI. The second half is right and I would go
further; the first half I would change.

**"Cable" is not checkable, and checking it would be theatre.** Both a cable
and the WiFi link are IP to the Jetson; distinguishing them means inspecting
the interface a request arrived on, which breaks the moment somebody uses a
USB-Ethernet adapter, a different port, or a laptop bridging both. A check
that can be wrong in the permissive direction *and* the obstructive one is
worse than no check.

What is checkable, and is what actually matters:

> The Jetson accepts a setup push only when the Pico reports **disarmed** and
> the recorder reports **not recording**.

That is the real requirement — nobody should be able to change sonar mounting
geometry with the boat under way — it is enforced where it cannot be bypassed
by opening the page from a different machine, and it reads off the same
authority the rest of the GUI already trusts. The refusal must say which
condition failed, in the vessel's terms: *"The Pico reports armed. Setup
cannot be changed while the vessel is armed."*

Cable-only then becomes an operating convention rather than a mechanism, which
is the honest place for it.

## Where I would argue with myself

**1. Writing config that needs a restart is a trap with a new coat of paint.**
Nodes read their YAML at startup. A value entered on the page does not take
effect until `omniscan_bridge` restarts, so the page must either say so and
offer to restart, or push through ROS parameters. I would take the first —
explicit, with the check still reading reported provenance — but I do not like
it: the page will sometimes have to tell somebody that what they just did has
not happened yet. The alternative is worse (a live parameter push that half
succeeds), but this is the weakest part of the design and I would want it
looked at rather than waved through.

**2. `channel_width_mhz` is in the wrong tier, probably.** It is radio
configuration, which is Vessel by the equipment axis — but it changes with
site interference, which is Deployment by the lifetime axis. I have put it in
Deployment. It is the one field where the two axes genuinely conflict and I do
not have a principled answer, only a guess about which way it will actually be
used.

**3. Unmeasured mounting geometry is WARN, and perhaps it should be FAIL.**
It is the one value that corrupts recorded data rather than merely degrading
the display: survey on defaults and the soundings carry an unknown offset that
cannot be removed afterwards. The existing WARN was a deliberate choice and I
am not overriding it here, but the page makes the fix cheap for the first
time, which weakens the argument for tolerating it. Worth revisiting once
somebody has actually filled the page in once.

**4. Capturing the station position from the vessel has a failure mode.** If
the vessel's fix is poor when the button is pressed — alongside, under a
cliff, cold-started — the captured position is quietly wrong and everything
downstream inherits it. The capture must therefore record the fix quality at
the moment of capture and refuse below a threshold, and the page must show
*"captured at 14:02, 8 satellites, HDOP 1.1"* rather than just a number. A
captured value that looks identical to a measured one is the same class of
lie as a predicted RSSI shown as a measurement.

**5. I am not certain the page should be reachable while a mission is
running.** Read-only, clearly, for somebody who wants to check what a value
is. But a read-only route is a second rendering path for the same fields, and
two paths disagree eventually. Leaning towards: reachable, read-only, with the
editing controls absent rather than disabled — absent controls cannot be
re-enabled by a stale React state.

## What is deliberately left off

* Anything the vessel measures itself — position, heading, state of charge.
* Anything in the safety chain. Arming is RC channel 7 and nothing else.
* Mission execution controls: start, stop, abort.
* The survey area and line plan — that is the planner, and it is a separate
  piece of work.
* Network configuration. Tempting, and it is how you brick a Jetson on a
  beach.

## Build order

**Step 1 is built** (`asket_common/setup_profile.py`,
`gui_backend/core/setup_store.py`). One refinement came out of writing it that
the design above did not have: *"not set"* hides two states that need
different words and a different order of work. A sonar tilt of 35 degrees is a
**guess** — usable, plausible, quite possibly wrong. A shore station position
is **nothing** — there is no sensible default for where a tripod is standing,
so the panel simply cannot draw. The first is a survey that may be silently
displaced; the second is a feature that does not work. `has_default` marks the
difference and the headline leads with it, so a fresh Jetson reads:

> Five values have never been set, and there is no sensible default for them.
> Seven values are still on a guess that can ruin a survey without it looking
> wrong. Eight others are on unchecked defaults that only make the screen less
> useful.

and after capturing the station and measuring the hull, reads:

> Eight values are still on a default nobody has checked.



1. The provenance model and the setup file format, ROS-free and testable with
   no hardware. This is the part everything else reads.
2. `setup_field` on `CheckResult`, and the pre-flight panel rendering links.
   Small, and it is the change most likely to get `measured: false` cleared.
3. The page: Vessel and Deployment tiers, staleness dates, the "two things
   need filling in" header.
4. Capture-from-vessel for station position and boresight, with fix quality
   recorded.
5. The Jetson-side accept condition, and the refusal wording.
6. Saved deployments.

Steps 1–3 are laptop work against the simulator, which is the same split that
has worked for the last three pieces.

## Open questions this closes, and does not

Closes the *mechanism* for Q2 (mounting geometry) and Q11 (station position
and bearing) — both become fields somebody can fill in rather than files
somebody cannot open. It does not close either question: that still needs a
tape measure and a beach. What it removes is the excuse.
