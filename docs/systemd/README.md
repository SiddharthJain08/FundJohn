# Canonical systemd unit snapshot (W8, 2026-07-02)

Verbatim copies of every installed OpenClaw unit, taken from the live VPS:

- `*.service` / `*.timer` — system scope (`/etc/systemd/system/`)
- `*.service.d/` — drop-in overrides (OnFailure notify hooks etc.)
- `user/` — ROOT **user-scope** units (`/root/.config/systemd/user/`).
  `johnbot.service` (the Discord bridge + dashboards) runs HERE, user
  scope, NOT system scope — the system-scope johnbot unit is deliberately
  disabled (split-brain risk; see memory `feedback_johnbot_supervision_user_scope`).

Install on a fresh box:

    sudo cp docs/systemd/openclaw-* /etc/systemd/system/
    sudo cp -r docs/systemd/*.service.d /etc/systemd/system/
    mkdir -p ~/.config/systemd/user && cp -r docs/systemd/user/* ~/.config/systemd/user/
    sudo systemctl daemon-reload && systemctl --user daemon-reload
    # then enable the timers you want (see docs/bootstrap.md)

The older loose unit copies directly under `docs/` predate this snapshot;
where they disagree, THIS directory reflects what actually runs
(3 legacy copies had drifted: stop-reattach, weekly-strategy-weights ×2).
