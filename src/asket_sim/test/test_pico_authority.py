"""The Pico is the authority, and the hardware outranks the Pico.

These tests encode safety rules 1 and 4 from docs/safety.md. If one of them ever
fails, something in this repository has started believing it can override the
killswitch, and that is not a bug to negotiate with.
"""

from asket_sim.core.pico import MODE_AUTONOMOUS, MODE_ESTOP, MODE_MANUAL, PicoConfig, PicoSim


def step(pico, seconds, dt=0.05):
    for _ in range(int(seconds / dt)):
        pico.step(dt)


def test_a_mode_request_is_not_immediately_confirmed():
    """The gap between request and confirmation is what the GUI must render."""
    pico = PicoSim(PicoConfig(confirm_delay_s=0.5))
    assert pico.mode == MODE_MANUAL
    pico.request_mode(MODE_AUTONOMOUS)
    step(pico, 0.2)
    assert pico.sample(0).mode == MODE_MANUAL, "must not report the requested mode"
    step(pico, 0.5)
    assert pico.sample(0).mode == MODE_AUTONOMOUS


def test_a_lost_request_never_confirms():
    """The GUI's command timeout path has to be reachable."""
    pico = PicoSim(PicoConfig(request_loss_probability=1.0))
    assert pico.request_mode(MODE_AUTONOMOUS) is True   # accepted...
    step(pico, 5.0)
    assert pico.sample(0).mode == MODE_MANUAL           # ...but never confirmed


def test_hardware_killswitch_forces_estop_and_software_cannot_leave_it():
    pico = PicoSim()
    pico.set_hardware_killswitch(True)
    assert pico.sample(0).mode == MODE_ESTOP

    pico.request_mode(MODE_AUTONOMOUS)
    step(pico, 5.0)
    assert pico.sample(0).mode == MODE_ESTOP, "software must not override the killswitch"
    assert not pico.sample(0).armed


def test_rc_channel_8_is_sovereign_too():
    pico = PicoSim()
    pico.request_mode(MODE_AUTONOMOUS)
    step(pico, 2.0)
    assert pico.sample(0).mode == MODE_AUTONOMOUS

    pico.set_rc_channel8(0)
    assert pico.sample(0).mode == MODE_ESTOP
    pico.request_mode(MODE_MANUAL)
    step(pico, 5.0)
    assert pico.sample(0).mode == MODE_ESTOP


def test_recovery_is_possible_once_the_hardware_releases():
    pico = PicoSim()
    pico.set_hardware_killswitch(True)
    step(pico, 1.0)
    pico.set_hardware_killswitch(False)
    pico.request_mode(MODE_MANUAL)
    step(pico, 2.0)
    assert pico.sample(0).mode == MODE_MANUAL


def test_invalid_mode_is_rejected():
    pico = PicoSim()
    assert pico.request_mode(99) is False


# -- one relay, because there is one relay ---------------------------------


def test_the_simulator_has_exactly_one_relay():
    """There is one relay on this hull: ESTOP_RELAY_PIN on GPIO21, cutting ESC
    power. The firmware has never had more.

    ``num_relays`` was 4 — a placeholder nobody revisited — and the GUI
    faithfully rendered whatever length of array arrived, so the panel read
    ``0/4 relays closed``. That is a statement about the e-stop path, and it
    was false: three quarters of a safety mechanism appearing not to exist.

    The real path was never wrong (``adapters.py`` builds ``[relay_closed]``
    from the single ``relay=`` field), so the lie lived only in simulation —
    which is exactly where this GUI gets reviewed.
    """
    pico = PicoSim(PicoConfig())
    assert PicoConfig().num_relays == 1
    assert len(pico.sample(0).relay_states) == 1


def test_the_relay_follows_arming():
    """Open while disarmed is correct, not a fault — and it stays one relay."""
    pico = PicoSim(PicoConfig(confirm_delay_s=0.0))

    pico.request_mode(MODE_ESTOP)
    step(pico, 0.2)
    assert pico.sample(0).armed is False
    assert pico.sample(0).relay_states == [False]

    pico.request_mode(MODE_MANUAL)
    step(pico, 0.2)
    assert pico.sample(0).armed is True
    assert pico.sample(0).relay_states == [True]
