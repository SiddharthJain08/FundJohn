# Paper-account drop-ins (pre-live cutover, 2026-07-21)

`paper-account.conf` is installed at
`/etc/systemd/system/<unit>.service.d/paper-account.conf` for the ten
weekend/research units (sunday-research-ingest/code, sunday-code-review,
weekend-saturday/sunday, weekend-maintenance-sat/sun,
weekly-strategy-weights, backtest-refresh, strategy-backtest-refresh).

It appends `EnvironmentFile=/root/openclaw/.env.paper` AFTER the unit's main
`.env`, so the paper Alpaca keys win for the Saturday research/adjustment
stack. While `.env` itself still holds paper keys this is a no-op.

## Cutover procedure (when live credentials arrive)
1. Edit `/root/openclaw/.env` ONLY:
   - ALPACA_API_KEY / ALPACA_SECRET_KEY → live keys
   - ALPACA_LIVE_TRADE=true
   - ALPACA_BASE_URL=https://api.alpaca.markets
2. `.env.paper` keeps the paper keys — do not touch.
3. Restart long-running consumers: `johnbot` (user scope: XDG_RUNTIME_DIR=/run/user/0
   systemctl --user restart johnbot.service), fundjohn-dashboard, mastermind-chat.
   Timer-spawned scripts pick up .env on next run.
4. Weekend/research units keep trading the PAPER account automatically via
   these drop-ins — no unit edits needed at cutover.
