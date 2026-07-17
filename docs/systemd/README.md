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
