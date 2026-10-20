# Canonical systemd unit snapshot

Verbatim copies of every installed OpenClaw unit, taken from the live VPS:

- `*.service` / `*.timer` — system scope (`/etc/systemd/system/`), including
  the three long-running services `finbert-sentiment`, `fundjohn-dashboard`,
  and `mastermind-chat` (the latter sanitized: it runs as `claudebot`, which
  cannot read the root-only `.env`, so its `POSTGRES_URI`/`REDIS_URL` are
  injected inline — set the real values when installing).
- `*.service.d/` — drop-in overrides (OnFailure notify hooks etc.)
- `weekend-swap/` — timer schedule overrides installed as
  `<name>.timer.d/override.conf` (Saturday/Sunday research-lane swap).
- `user/` — ROOT **user-scope** units (`/root/.config/systemd/user/`).
  `johnbot.service` (the Discord bridge + :3000 dashboard host) runs HERE,
  user scope, NOT system scope. Never install or enable a system-scope
  johnbot unit — a second copy means a split-brain double bot. The
  `user/johnbot.service.d/oom-policy.conf` drop-in (OOMPolicy=continue) is
  load-bearing on the 8GB box.

Install on a fresh box (idempotent, fixes the scope/glob/rename pitfalls of
hand-copying):

    sudo bash scripts/install_systemd.sh

Then enable only the units you want live — the enablement set is listed in
`docs/bootstrap.md`. Timers with `Persistent=true` fire a catch-up run the
moment they are enabled; touch their stamp files first when a surprise run
would be harmful.

To refresh this snapshot after changing a deployed unit, copy the installed
file back here byte-for-byte and commit.

## Job-stat drop-in (not installed)

`job-stat/job-stat.conf` adds run accounting (`scheduled_job_runs`, migration 167)
to systemd units: `ExecStartPre=-` records the start, `ExecStopPost=-` records the
result. `ExecStartPre` (not `ExecStartPost`) because the timer services are
`Type=oneshot`, where `ExecStartPost` runs only after the main process exits. The
leading `-` means a hook failure never fails the unit; `%n` keeps template
instances (`openclaw-edgar-8k@0715.service`) distinct.

It deliberately lives in `docs/systemd/job-stat/`, NOT in a `*.service.d/`
directory: `scripts/install_systemd.sh` installs every `*.service.d/*.conf`
verbatim, so a drop-in there would be applied fleet-wide by the next install.
Installing it is an operator decision. For each `docs/systemd/*.timer`, the target
is its `Unit=` value, else the same-stem `.service` (template instances use
`<name>@.service.d`). Copy the file to
`/etc/systemd/system/<target>.d/job-stat.conf`, then `systemctl daemon-reload`.

Units without `EnvironmentFile=/root/openclaw/.env` log
`[job_stat] skipped: POSTGRES_URI unset` and record nothing. Until the drop-in is
installed only in-process cron rows exist; the `scheduled_jobs_overdue` system
check (`src/system_checks/checks/scheduled_jobs.py`) judges whatever it sees.
Mode-gated crons (registration gated in `cron-schedule.js`) keep WARNing for up to 30 days after a mode flip, until their last row ages out; this is expected.
