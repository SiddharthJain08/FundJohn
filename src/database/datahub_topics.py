"""src/database/datahub_topics.py — DataHub topic constant registry.

Topic schema: `domain:subdomain:id` (lowercase, colon-separated, segments
match `^[a-z0-9_-]+$`). Wildcards `*` and `?` are allowed in subscribe
patterns but NOT in published topics.

Constants here are the canonical names used by the DataHub facade. Code
that still writes to non-conforming keys (`agent_status:{id}` vs
`subagent:{id}` etc.) is enumerated in
`docs/archive/superpowers/specs/datahub-inventory-2026-05-18.md` and will be
migrated per-caller in follow-up commits.

When you add a new topic, append a constant here and document its
producer + consumer pair in `docs/reference/datahub.md`.
"""

from __future__ import annotations

import re

# Topic-key segment validator: lowercase alphanum + underscore + hyphen.
# Wildcards (`*` ?) are stripped before validation in subscribe paths.
TOPIC_SEGMENT_RE = re.compile(r"^[a-z0-9_-]+$")

# Maximum number of `domain:subdomain:id...` segments. 5 keeps keys short
# enough to grep and avoids accidental command-injection blobs from
# untrusted callers.
TOPIC_MAX_SEGMENTS = 5

# ── Status / presence ────────────────────────────────────────────────────
# Producer: any agent reporting health / activity. Consumer: dashboard,
# maintenance digest. TTL: 300s (heartbeats expire fast — staleness is a
# signal).
T_AGENT_STATUS = "agent:status:{agent_id}"
T_AGENT_PRESENCE = "agent:presence"  # pub/sub broadcast (no id)

# ── Pipeline events ──────────────────────────────────────────────────────
# Producer: pipeline_orchestrator. Consumer: dashboard, alerting.
# Lifecycle: cycle-start / step-complete / cycle-complete / cycle-failed.
T_PIPELINE_EVENT = "pipeline:event:{event}"
T_PIPELINE_STEP = "pipeline:step:{step_name}"
T_PIPELINE_CHECKPOINT = "pipeline:checkpoint:{date}"

# ── Data ingestion events ────────────────────────────────────────────────
# Producer: collector / quote-monitor. Consumer: alerting, regime gate.
T_DATA_FETCH = "data:fetch:{source}"
T_DATA_ALERT = "data:alert:{severity}"  # severity in {info, warn, error}

# ── Handoff (between pipeline stages) ────────────────────────────────────
# Producer: trade_handoff_builder.py and friends. Consumer: trade step
# + alpaca_executor. TTL: 86400s (one trading day).
T_HANDOFF = "handoff:{date}:{stage}"

# ── DataHub self-observability ───────────────────────────────────────────
# Emitted by the facade itself when rate-limit / dedup / size-cap trips.
# Consumer: dashboard, doctor.
T_DATAHUB_RATE_LIMITED = "datahub:rate-limited"
T_DATAHUB_DEDUPED = "datahub:deduped"
T_DATAHUB_OVERSIZED = "datahub:oversized"

# ── Reserved system prefixes (do not use as topics) ──────────────────────
# These prefixes are owned by infra layers and MUST NOT be republished
# through DataHub:
#   - `_steering:*`  (drained by `src/agent/middleware/steering.js`)
#   - `rate_limit:*` / `ratelimit:*` (provider-side bucket tokens)
#   - `regime_blended:*` (live sizer state)
RESERVED_PREFIXES = (
    "_steering:",
    "rate_limit:",
    "ratelimit:",
    "regime_blended:",
)


def validate_topic(topic: str, *, allow_wildcards: bool = False) -> None:
    """Raise ``ValueError`` if ``topic`` does not match the schema.

    Rules:
      - Non-empty string.
      - Segments separated by ``:``; 1 .. ``TOPIC_MAX_SEGMENTS`` segments.
      - Each segment matches ``TOPIC_SEGMENT_RE``; ``*`` and ``?`` allowed
        only when ``allow_wildcards=True`` (used by subscribe paths).
      - Does not start with a reserved prefix.
    """
    if not isinstance(topic, str) or not topic:
        raise ValueError("topic must be a non-empty string")
    for prefix in RESERVED_PREFIXES:
        if topic.startswith(prefix):
            raise ValueError(f"topic {topic!r} uses reserved prefix {prefix!r}")
    segments = topic.split(":")
    if not 1 <= len(segments) <= TOPIC_MAX_SEGMENTS:
        raise ValueError(
            f"topic {topic!r} has {len(segments)} segments; "
            f"must be between 1 and {TOPIC_MAX_SEGMENTS}"
        )
    for seg in segments:
        if not seg:
            raise ValueError(f"topic {topic!r} contains an empty segment")
        if allow_wildcards:
            cleaned = seg.replace("*", "").replace("?", "")
            # An all-wildcard segment is fine.
            if cleaned and not TOPIC_SEGMENT_RE.match(cleaned):
                raise ValueError(
                    f"topic {topic!r} segment {seg!r} contains invalid characters"
                )
        else:
            if not TOPIC_SEGMENT_RE.match(seg):
                raise ValueError(
                    f"topic {topic!r} segment {seg!r} contains invalid characters; "
                    "wildcards not allowed when publishing"
                )


__all__ = [
    "T_AGENT_STATUS",
    "T_AGENT_PRESENCE",
    "T_PIPELINE_EVENT",
    "T_PIPELINE_STEP",
    "T_PIPELINE_CHECKPOINT",
    "T_DATA_FETCH",
    "T_DATA_ALERT",
    "T_HANDOFF",
    "T_DATAHUB_RATE_LIMITED",
    "T_DATAHUB_DEDUPED",
    "T_DATAHUB_OVERSIZED",
    "RESERVED_PREFIXES",
    "TOPIC_MAX_SEGMENTS",
    "TOPIC_SEGMENT_RE",
    "validate_topic",
]
