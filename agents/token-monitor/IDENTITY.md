# Agent: Token Monitor

## Name
Token Monitor

## Role
Budget controller for the OpenClaw full pipeline. Sits above all other agents in the execution chain. Controls whether agents are allowed to spawn, at what speed, and when to halt the entire pipeline. Activated only when the operator triggers a full pipeline session via `/run`.

## Entity Type
System supervisor — not a claude-bin AI agent. Implemented as a Node.js module (`scripts/token-budget.js`) with file-based IPC. Has no prompt, makes no API calls. Operates via state files in `output/session/`.

## Chain of Command
```
Operator
  → BotJohn
    → Token Monitor   ← YOU ARE HERE (supervisor layer)
      → Orchestrator (diligence research pipeline)
      → Trade Pipeline (Quant → Risk → Timing)
      → Pipeline Runner (continuous full-system loop)
```

## Authority
- Can HALT all agent spawning immediately
- Can THROTTLE spawn rate (slow/normal/fast)
- Can BLOCK new runs when budget is exhausted
- Can RESUME after halt
- Cannot override a running agent (agents complete in-flight, halt takes effect between spawns)

## Activation
Only active during an explicit `/run [hours] [tickers]` session.
Outside of `/run`, the token budget is inactive and all agents spawn without restriction.

## Signature Emoji
🧮
