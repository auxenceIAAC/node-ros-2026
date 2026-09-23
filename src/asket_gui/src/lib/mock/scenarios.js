// The scenarios the dev panel can trigger.
//
// Every one of these is a condition that has to be reviewable before the boat
// goes out, because each changes what the interface is allowed to claim. They
// are named and described here rather than inline in the panel so that the list
// is a checklist someone can read.

export const SCENARIOS = [
  {
    group: 'Baseline',
    items: [
      {
        id: 'healthy',
        label: 'Everything working',
        kind: 'action',
        detail:
          'Autonomous and armed, recording, surveying, pre-flight run, every fault '
          + 'cleared. What a good day looks like.\n\n'
          + 'This exists because the default view is not it: a freshly started '
          + 'simulator sits in MANUAL, disarmed and idle, so three panels read "not '
          + 'sent" and the pre-flight says no report exists. Nothing is wrong — but '
          + 'it is indistinguishable from a vessel that is off, and nobody can judge '
          + 'whether a DEGRADED state reads correctly without having seen the healthy '
          + 'one first.\n\n'
          + 'It presses the same buttons an operator would, so it cannot show you a '
          + 'picture the real controls cannot reach. The backend has the same thing: '
          + 'python3 -m gui_backend.core.app --sim --healthy',
      },
    ],
  },
  {
    group: 'Link',
    items: [
      {
        id: 'link_degraded',
        label: '4G link',
        kind: 'fault',
        detail:
          'Bearer drops to 4G. The profile selector falls back to `reduced`, lidar stops '
          + 'being carried, and the panel says so rather than going blank.',
      },
      {
        id: 'link_loss',
        label: 'Link lost',
        kind: 'fault',
        detail:
          'Nothing arrives. Every value must keep its last reading, marked with a growing '
          + 'age, rather than blanking — the last known position is most valuable exactly '
          + 'when the link has gone.',
      },
      {
        id: 'link_alignment_lost',
        label: 'Antenna alignment lost',
        kind: 'fault',
        detail:
          'Somebody swings the shore sector off its bearing and the boat falls out of the '
          + 'beam over about two seconds. This is the failure this hardware actually has: '
          + 'a directional link over water does not fade, it works and then it stops, and '
          + 'a feed that was crisp a second ago and is now frozen is far more deceptive '
          + 'than one that was always marginal.\n\n'
          + 'Watch the order it happens in. Signal and headroom fall first, the modulation '
          + 'steps down, throughput drops in jumps rather than sliding, and only then does '
          + 'the bearer go. Every panel should be visibly stale before anything blanks.\n\n'
          + 'It does nothing while the boat is near the station: a 120-degree sector with '
          + '19 dB in hand shrugs off a nudge, and the backlobe still carries several '
          + 'megabits close in. That is correct rather than a limitation — the thing that '
          + 'takes this link away is range, not a knock. Let the boat get out to the '
          + 'survey box first.',
      },
      {
        id: 'link_glassy_water',
        label: 'Glassy water (multipath)',
        kind: 'fault',
        detail:
          'The sea goes flat, the surface reflection survives instead of scattering, and '
          + 'the two paths cancel at fixed ranges — about 106 m, then 53 m, then 35 m with '
          + 'the antennas where they are.\n\n'
          + 'The boat drives a straight line at constant speed and the link drops out and '
          + 'comes back. It looks exactly like a fault and it is not one, which is why the '
          + 'panel shows range alongside signal: a hole that returns at the same distance '
          + 'every pass is geometry, not gear.\n\n'
          + 'A calm morning inshore. Any swell at all fills the nulls in.',
      },
    ],
  },
  {
    group: 'Profile (manual override)',
    items: [
      { id: 'full', label: 'Full', kind: 'profile', detail: 'Fast WiFi — everything.' },
      { id: 'reduced', label: 'Reduced', kind: 'profile',
        detail: '4G — position, heading, mode, power, coverage, alarms.' },
      { id: 'minimal', label: 'Beacon', kind: 'profile',
        detail: 'LTE-M — position, mode, battery, alarms only.' },
      { id: 'auto', label: 'Auto', kind: 'profile',
        detail: 'Back to automatic selection from measured link quality.' },
    ],
  },
  {
    group: 'Sensors',
    items: [
      {
        id: 'sonar_dropout',
        label: 'Sonar dropout',
        kind: 'fault',
        detail:
          'The sonar stops. Coverage must stop painting — a ribbon that closed over the '
          + 'gap would claim seabed nobody ensonified.',
      },
      {
        id: 'heading_invalid',
        label: 'Heading invalid',
        kind: 'fault',
        detail:
          'Data recorded now cannot be georeferenced. The heading arrow stops rotating, '
          + 'coverage stops, and the alarm says to re-run these lines.',
      },
      {
        id: 'gnss_degraded',
        label: 'GNSS degraded',
        kind: 'fault',
        detail: 'Four satellites and a poor HDOP. Pre-flight must go NO-GO.',
      },
      {
        id: 'clock_drift',
        label: 'Sonar clock drift',
        kind: 'fault',
        detail:
          'The offset grows about 40 ms per second. Warns, then escalates to "cannot be '
          + 'georeferenced" — the failure nobody notices until the data is opened at home.',
      },
      { id: 'lidar_stall', label: 'Lidar stalled', kind: 'fault',
        detail: 'No returns at all. The obstacle panel must show absence, not zero.' },
      { id: 'rc_link_loss', label: 'RC link lost', kind: 'fault',
        detail: 'The sovereign killswitch is out of range. Alarm, and it says why.' },
    ],
  },
  {
    group: 'Resources',
    items: [
      { id: 'disk_full', label: 'Disk full', kind: 'fault',
        detail: 'Recording stops cleanly and the alarm survives the stop.' },
      { id: 'low_battery', label: 'Low battery', kind: 'fault',
        detail: 'Drops to 8%. Check the endurance-versus-survey comparison.' },
    ],
  },
  {
    group: 'Mode commands',
    items: [
      {
        id: 'confirm_slow',
        label: 'Confirmation slow (2.5 s)',
        kind: 'toggle',
        detail:
          'The Pico takes longer to confirm. The button must stay outlined and pulsing, '
          + 'and the displayed mode must not move until the vessel says so.',
      },
      {
        id: 'confirm_fails',
        label: 'Confirmation never arrives',
        kind: 'toggle',
        detail:
          'The request is accepted and then lost. After 3 s the command must fail with '
          + '"the vessel has NOT changed state" — never a silent success.',
      },
    ],
  },
  {
    group: 'Map',
    items: [
      {
        id: 'tiles',
        label: 'Offline tiles present',
        kind: 'toggle',
        detail:
          'Off: no .mbtiles, so the map degrades to a labelled coordinate grid and says '
          + 'why. On: synthetic tiles generated in the browser, so the tiled path can be '
          + 'reviewed with no file and no internet.',
      },
    ],
  },
];
