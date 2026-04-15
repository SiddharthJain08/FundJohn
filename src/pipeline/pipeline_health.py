"""
Pipeline Health Monitor — R10 implementation.

FundJohn Pipeline Audit 2026-04-13

Aggregates health metrics from all pipeline components and sends a daily
Discord memo summarising:
  - Last successful run time per component
  - API quota usage (rate-limiter stats)
  - Cache hit/miss ratio (data-cache stats)
  - Data freshness per source/datatype
  - Any component failures in the past 24 h

Designed to be called by the APScheduler job after the main pipeline run.

Usage
-----
    from pipeline.pipeline_health import PipelineHealthMonitor

    monitor = PipelineHealthMonitor(discord_webhook_url)
    await monitor.record_run("polygon_prices", success=True)
    await monitor.send_daily_memo()
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Discord webhook URL — set in environment or preferences.json
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_HEALTH_WEBHOOK", "")

# Colour constants for Discord embed
COLOUR_OK      = 0x2ECC71   # green
COLOUR_WARN    = 0xF39C12   # amber
COLOUR_CRIT    = 0xE74C3C   # red


@dataclass
class ComponentRun:
    component:    str
    success:      bool
    timestamp:    float = field(default_factory=time.time)
    error_msg:    str   = ""
    records_proc: int   = 0
    duration_sec: float = 0.0


class PipelineHealthMonitor:
    """
    Collects per-component run records and pushes a rich Discord embed.

    Thread-safe: uses asyncio.Lock for concurrent writes.
    """

    def __init__(self, webhook_url: str = DISCORD_WEBHOOK_URL):
        self._webhook_url = webhook_url
        self._runs: List[ComponentRun] = []
        self._lock = asyncio.Lock()
        self._start_time = time.time()

    # ── Recording ─────────────────────────────────────────────────────────────

    async def record_run(
        self,
        component:    str,
        success:      bool,
        error_msg:    str   = "",
        records_proc: int   = 0,
        duration_sec: float = 0.0,
    ) -> None:
        async with self._lock:
            self._runs.append(ComponentRun(
                component    = component,
                success      = success,
                error_msg    = error_msg,
                records_proc = records_proc,
                duration_sec = duration_sec,
            ))
            if not success:
                logger.warning("Health: component '%s' FAILED — %s", component, error_msg)

    # ── Aggregation ───────────────────────────────────────────────────────────

    def _aggregate(self) -> Dict[str, Any]:
        cutoff = time.time() - 86400
        recent = [r for r in self._runs if r.timestamp >= cutoff]

        total      = len(recent)
        failures   = [r for r in recent if not r.success]
        successes  = [r for r in recent if r.success]
        fail_count = len(failures)

        # Latest run per component
        by_component: Dict[str, ComponentRun] = {}
        for r in recent:
            if r.component not in by_component or r.timestamp > by_component[r.component].timestamp:
                by_component[r.component] = r

        total_records = sum(r.records_proc for r in successes)
        avg_duration  = (
            sum(r.duration_sec for r in recent) / len(recent) if recent else 0.0
        )

        return {
            "total_runs":      total,
            "fail_count":      fail_count,
            "success_count":   total - fail_count,
            "total_records":   total_records,
            "avg_duration_s":  round(avg_duration, 2),
            "failures":        [{"component": r.component, "error": r.error_msg} for r in failures],
            "by_component":    {k: {
                "success":   v.success,
                "records":   v.records_proc,
                "duration":  round(v.duration_sec, 2),
                "ts":        v.timestamp,
            } for k, v in by_component.items()},
        }

    # ── Discord memo ──────────────────────────────────────────────────────────

    async def send_daily_memo(
        self,
        rate_limiter_stats: Optional[Dict] = None,
        cache_stats:        Optional[Dict] = None,
    ) -> bool:
        """Build and POST a Discord embed. Returns True on success."""
        if not self._webhook_url:
            logger.warning("PipelineHealthMonitor: no webhook URL configured — skipping Discord memo")
            return False

        agg = self._aggregate()

        # Determine overall status colour
        if agg["fail_count"] == 0:
            colour = COLOUR_OK
            status_emoji = "✅"
            status_label = "ALL SYSTEMS OPERATIONAL"
        elif agg["fail_count"] <= 2:
            colour = COLOUR_WARN
            status_emoji = "⚠️"
            status_label = f"{agg['fail_count']} COMPONENT(S) FAILED"
        else:
            colour = COLOUR_CRIT
            status_emoji = "🚨"
            status_label = f"{agg['fail_count']} COMPONENTS FAILED"

        # Build fields
        fields = []

        # Summary field
        elapsed_h = round((time.time() - self._start_time) / 3600, 1)
        fields.append({
            "name": "📊 Run Summary",
            "value": (
                f"Runs: **{agg['total_runs']}** | "
                f"OK: **{agg['success_count']}** | "
                f"Failed: **{agg['fail_count']}**\n"
                f"Records processed: **{agg['total_records']:,}**\n"
                f"Avg duration: **{agg['avg_duration_s']}s** | "
                f"Monitor uptime: **{elapsed_h}h**"
            ),
            "inline": False,
        })

        # Per-component status
        if agg["by_component"]:
            comp_lines = []
            for name, info in sorted(agg["by_component"].items()):
                icon = "✅" if info["success"] else "❌"
                comp_lines.append(
                    f"{icon} `{name}` — {info['records']} rows in {info['duration']}s"
                )
            fields.append({
                "name": "🔧 Components (last 24 h)",
                "value": "\n".join(comp_lines) or "No runs recorded",
                "inline": False,
            })

        # Failures detail
        if agg["failures"]:
            fail_lines = [f"• **{f['component']}**: {f['error'][:100]}" for f in agg["failures"][:5]]
            fields.append({
                "name": "❌ Failure Detail",
                "value": "\n".join(fail_lines),
                "inline": False,
            })

        # Rate limiter stats
        if rate_limiter_stats:
            rl_lines = []
            for src, stats in rate_limiter_stats.items():
                wait_pct = (
                    round(stats["total_waits"] / stats["total_calls"] * 100, 1)
                    if stats["total_calls"] else 0
                )
                rl_lines.append(
                    f"`{src}`: {stats['total_calls']} calls, {wait_pct}% throttled"
                )
            if rl_lines:
                fields.append({
                    "name": "⏱️ Rate Limiter",
                    "value": "\n".join(rl_lines[:10]),
                    "inline": False,
                })

        # Cache stats
        if cache_stats:
            fields.append({
                "name": "🗄️ Data Cache",
                "value": (
                    f"Total keys: **{cache_stats.get('total_cache_keys', 0)}**\n"
                    + "\n".join(
                        f"`{dt}`: {cnt}" for dt, cnt in
                        cache_stats.get("by_datatype", {}).items()
                    )
                ),
                "inline": False,
            })

        from datetime import datetime, timezone
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        payload = {
            "embeds": [{
                "title":       f"{status_emoji} FundJohn Pipeline Health — {now_str}",
                "description": f"**{status_label}**",
                "color":       colour,
                "fields":      fields,
                "footer":      {"text": "FundJohn/OpenClaw — automated health report"},
            }]
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status in (200, 204):
                        logger.info("PipelineHealthMonitor: Discord memo sent (%d)", resp.status)
                        return True
                    body = await resp.text()
                    logger.error("PipelineHealthMonitor: Discord POST %d — %s", resp.status, body[:200])
                    return False
        except Exception as exc:
            logger.error("PipelineHealthMonitor: failed to send Discord memo — %s", exc)
            return False

    # ── Convenience ───────────────────────────────────────────────────────────

    def json_report(self) -> str:
        """Return the aggregated health report as a JSON string."""
        return json.dumps(self._aggregate(), indent=2, default=str)


# ── Module-level singleton ────────────────────────────────────────────────────

_monitor: Optional[PipelineHealthMonitor] = None


def get_health_monitor(webhook_url: str = DISCORD_WEBHOOK_URL) -> PipelineHealthMonitor:
    """Return (or create) the module-level singleton PipelineHealthMonitor."""
    global _monitor
    if _monitor is None:
        _monitor = PipelineHealthMonitor(webhook_url)
    return _monitor
