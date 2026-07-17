# Security Policy

FundJohn is a self-hosted **paper-trading** system. It holds broker API keys
for a paper account, Discord bot tokens, and LLM API credentials — treat a
deployment as sensitive even though no real money moves.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Report a security
vulnerability" under the Security tab) rather than a public issue. Include
reproduction steps and affected paths.

## Secrets policy

- `.env` is root-only, never committed, and **not shell-safe** (never
  `source` it). `.env.example` ships with every secret blanked.
- Discord **webhook URLs are credentials** — anyone holding one can post as
  the bot. Never hardcode them; use env vars (`TRADEDESK_TRADE_SIGNALS_WEBHOOK`)
  or the `agent_registry.webhook_urls` DB lookup.
- The secret-redaction middleware strips known secrets from agent contexts;
  do not paste raw `.env` contents into prompts, Discord, or docs.
- `src/security/integrity.js` hash-verifies the root instruction files
  (CLAUDE.md, AGENTS.md, IDENTITY.md, SOUL.md) at boot against a
  machine-local manifest (`npm run integrity:generate`).
- The dashboards on :7870/:7871/:7872 bind to localhost only; the :80/:3000
  user dashboard is the only outward surface — front it with auth or a
  firewall if your deployment is reachable from the internet.

## No warranty

MIT-licensed, provided as-is. Nothing here is investment advice, and the
system's outputs are not warranted correct, profitable, or safe.
