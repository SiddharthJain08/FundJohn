# DataHub — Phase 2B Pub/Sub Facade

**Module:** `src/database/datahub.py`
**Topic registry:** `src/database/datahub_topics.py`
**Inventory of legacy callers:** `docs/superpowers/specs/datahub-inventory-2026-05-18.md`
**Spec:** Phase 2B of `docs/superpowers/plans/2026-05-15-fincept-imports-phase-2-master-plan.md`

## Why

Before DataHub, ~35 Redis call sites scattered across 13 files used
ad-hoc key names (`subagent:`, `agent_status:`, `agent_persona:` for
the same domain), inconsistent TTL discipline, and no per-producer
rate-limit. DataHub formalizes a single topic-key schema with
opt-in TTL / dedup / rate-limit / payload-size cap. It ships
ALONGSIDE the existing scatter; per-caller migration is incremental.

## Topic schema

Pattern: `domain:subdomain:id` (1..5 segments, each `[a-z0-9_-]+`).

| Topic | Producer | Consumer | TTL | Notes |
|---|---|---|---|---|
| `agent:status:{agent_id}` | any agent | dashboard, maintenance digest | 300s | canonical replacement for `subagent:{id}` / `agent_status:{id}` / `agent_persona:{id}` |
| `agent:presence` | pipeline_orchestrator, agent-personas | dashboard | — | broadcast; no last-value |
| `pipeline:event:{event}` | pipeline_orchestrator | dashboard, alerting | 86400 | `cycle-start`, `step-complete`, `cycle-failed` |
| `pipeline:step:{step_name}` | pipeline_orchestrator | dashboard | 86400 | per-step status |
| `pipeline:checkpoint:{date}` | pipeline_orchestrator | recovery, doctor | 86400 | replaces `CHECKPOINT_KEY` |
| `data:fetch:{source}` | collector, quote_monitor | alerting, regime gate | 300 | fetch outcome per provider |
| `data:alert:{severity}` | collector | dashboard, ops | 86400 | severity in `{info, warn, error}` |
| `handoff:{date}:{stage}` | trade_handoff_builder, trade_agent_llm | trade step, alpaca_executor, send_report | 86400 | **MIGRATED 2026-05-18** (B2.5) |
| `datahub:rate-limited` | DataHub (self) | dashboard, doctor | — | self-observability |
| `datahub:deduped` | DataHub (self) | dashboard, doctor | — | self-observability |
| `datahub:oversized` | DataHub (self) | dashboard, doctor | — | self-observability |

### Reserved prefixes (DataHub refuses to publish)

- `_steering:` — drained by `src/agent/middleware/steering.js`; raw protocol.
- `rate_limit:` / `ratelimit:` — provider-side bucket tokens (Polygon, Anthropic, OpenAI).
- `regime_blended:` — live sizer state; on the spec's fragility list.

## Usage

### Publish

```python
from database.datahub import DataHub
from database.datahub_topics import T_AGENT_STATUS

hub = DataHub.default(producer_id="tradejohn")

# Returns True on success, False on soft-fail (size cap / dedup / rate-limit).
hub.publish(
    T_AGENT_STATUS.format(agent_id="tradejohn"),
    {"state": "running", "ts": "2026-05-18T15:30:00Z"},
    ttl=300,
    dedup_window=10,  # optional — suppress identical payload within 10s
)
```

### Subscribe (psubscribe, wildcards allowed)

```python
def on_msg(channel: str, payload: str) -> None:
    print(channel, payload)

listener = hub.subscribe("agent:status:*", on_msg)
# ... when done:
listener.stop()
```

### Per-producer rate-limit (opt-in, fail-soft)

```python
hub = DataHub.default(producer_id="paperhunter")
hub.set_rate_limit(max_publishes=60, window_seconds=60)  # 1 publish/sec average
for paper in batch:
    ok = hub.publish("data:fetch:arxiv", paper.to_dict(), ttl=3600)
    if not ok:
        # rate-limit fired; back off or queue.
        pass
```

When the cap is exceeded the publish returns `False` AND DataHub
fires `datahub:rate-limited` with `{topic, producer, limit,
window_seconds}` so a dashboard widget can light up without polling.

## Hard constraints

- **MAX_PAYLOAD_BYTES = 64 KB**. Oversized publishes fail-soft and emit
  `datahub:oversized`. Bump only with operator sign-off; the cap exists
  to keep dashboard SSE buffers bounded.
- **NEVER FLUSHDB.** Tests must isolate via key prefix (`test_datahub:`)
  and delete only their own keys in teardown. The host Redis runs live
  production state.
- **Backwards compatibility.** The facade ships alongside the inventory
  of 35 legacy `redis.set`/`publish` call sites. None of those callers
  was removed in Phase 2B; per-caller migration is the topic of future
  commits (B2.6).

## Stats & observability

`DataHub.stats` (counter dict) exposes:

- `published` — successful publishes.
- `deduped` — suppressed by dedup window.
- `rate_limited` — suppressed by rate-limit.
- `oversized` — suppressed by 64 KB cap.
- `invalid_topic` — reserved for future use.

Subscribe to `datahub:*` to consume the corresponding events in real
time. Dashboard wiring is left for Phase 2B follow-up.

## Migrated callers

| Date | Caller | Commit | Notes |
|---|---|---|---|
| 2026-05-18 | `src/execution/handoff.py:write_handoff` | B2.5 | demonstrator; key path + TTL unchanged so all readers (read_handoff, alpaca_executor, send_report) work without change |

Future migrations follow the same template: pick a Python caller, route
its `set`/`setex`/`publish` through `DataHub.publish(topic, payload,
ttl=..)`, keep the key path identical so non-migrated consumers keep
reading the same Redis key.

## Branch-point decision (spec line 760)

Chose **(a) facade-then-migrate-callers-incrementally**. Atomic
refactor (option b) was explicitly forbidden by the task description
("too high blast radius for this phase"). The live pipeline depends on
several callers in the inventory, so big-bang replacement was off the
table.
