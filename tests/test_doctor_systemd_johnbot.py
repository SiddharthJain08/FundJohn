"""Fix 2 — check_systemd_services must treat johnbot.service as active when
the *process* is running, even though it runs as a systemd USER service under
root (/run/user/0) and the system-bus `systemctl is-active johnbot.service`
returns 'inactive'. A duplicate disabled SYSTEM unit makes the system-bus
answer actively misleading.

johnbot is special-cased: active if ANY of (process running via pgrep,
system-bus is-active, user-bus is-active) reports it up. All OTHER
EXPECTED_SERVICES stay on the plain system-bus is-active path.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from maintenance import doctor  # noqa: E402


def _proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _fake_run(*, pgrep_pid='', system_states=None, user_states=None):
    """Build a subprocess.run side_effect keyed on argv.

    pgrep_pid: stdout for `pgrep -f channels/discord/bot.js`
    system_states / user_states: {unit: state} for is-active calls
    """
    system_states = system_states or {}
    user_states = user_states or {}

    def run(cmd, *a, **kw):
        if cmd[:1] == ['pgrep']:
            return _proc(0 if pgrep_pid else 1, pgrep_pid, '')
        if cmd[:1] == ['systemctl']:
            if cmd[1:2] == ['--version']:
                return _proc(0, 'systemd 255', '')
            is_user = '--user' in cmd
            # last arg is the unit; 'is-active' precedes it
            unit = cmd[-1]
            table = user_states if is_user else system_states
            return _proc(0, table.get(unit, 'inactive'), '')
        return _proc(0, '', '')

    return run


class TestJohnbotLiveness:
    def test_johnbot_active_via_process_even_when_system_bus_inactive(self):
        """Production reality: system-bus inactive, but the process is up and
        the dashboard is active -> overall PASS."""
        side = _fake_run(
            pgrep_pid='1650191',
            system_states={'johnbot.service': 'inactive',
                           'fundjohn-dashboard.service': 'active'},
            user_states={'johnbot.service': 'active'},
        )
        with patch('maintenance.doctor.subprocess.run', side_effect=side):
            res = doctor.check_systemd_services()
        assert res['severity'] == doctor.PASS

    def test_johnbot_active_via_user_bus_when_no_process(self):
        """If pgrep can't see it but the user bus reports active, still up."""
        side = _fake_run(
            pgrep_pid='',
            system_states={'johnbot.service': 'inactive',
                           'fundjohn-dashboard.service': 'active'},
            user_states={'johnbot.service': 'active'},
        )
        with patch('maintenance.doctor.subprocess.run', side_effect=side):
            res = doctor.check_systemd_services()
        assert res['severity'] == doctor.PASS

    def test_johnbot_genuinely_down(self):
        """No process + both buses inactive -> johnbot counted inactive.
        Dashboard active so only one inactive -> WARN (existing severity)."""
        side = _fake_run(
            pgrep_pid='',
            system_states={'johnbot.service': 'inactive',
                           'fundjohn-dashboard.service': 'active'},
            user_states={'johnbot.service': 'inactive'},
        )
        with patch('maintenance.doctor.subprocess.run', side_effect=side):
            res = doctor.check_systemd_services()
        assert res['severity'] == doctor.WARN
        assert 'johnbot.service' in res['detail']

    def test_both_down_fails(self):
        """johnbot down (no proc/bus) AND dashboard down -> 2 inactive -> FAIL."""
        side = _fake_run(
            pgrep_pid='',
            system_states={'johnbot.service': 'inactive',
                           'fundjohn-dashboard.service': 'inactive'},
            user_states={'johnbot.service': 'inactive'},
        )
        with patch('maintenance.doctor.subprocess.run', side_effect=side):
            res = doctor.check_systemd_services()
        assert res['severity'] == doctor.FAIL

    def test_other_service_uses_system_bus_only(self):
        """A non-johnbot expected service is judged purely by system-bus
        is-active; a running johnbot process does NOT rescue the dashboard."""
        side = _fake_run(
            pgrep_pid='1650191',
            system_states={'johnbot.service': 'active',
                           'fundjohn-dashboard.service': 'inactive'},
            user_states={},
        )
        with patch('maintenance.doctor.subprocess.run', side_effect=side):
            res = doctor.check_systemd_services()
        assert res['severity'] == doctor.WARN
        assert 'fundjohn-dashboard.service' in res['detail']

    def test_user_bus_error_does_not_crash(self):
        """If the user-bus call raises (no session D-Bus for invoking uid),
        the process check still carries johnbot."""
        def run(cmd, *a, **kw):
            if cmd[:1] == ['pgrep']:
                return _proc(0, '1650191', '')
            if cmd[1:2] == ['--version']:
                return _proc(0, 'systemd 255', '')
            if '--user' in cmd:
                raise FileNotFoundError('no user bus')
            if cmd[-1] == 'fundjohn-dashboard.service':
                return _proc(0, 'active', '')
            return _proc(0, 'inactive', '')  # system johnbot inactive
        with patch('maintenance.doctor.subprocess.run', side_effect=run):
            res = doctor.check_systemd_services()
        assert res['severity'] == doctor.PASS
