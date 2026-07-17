import json
import os
import subprocess
import pytest

# Integration/heavy: spawns the real universe_resolver CLI over the live
# metadata snapshots — memory-heavy and data-dependent.
pytestmark = pytest.mark.integration

def test_resolver_cli_returns_json():
    env = {**os.environ}
    if "POSTGRES_URI" not in env:
        with open("/root/openclaw/.env") as f:
            for line in f:
                if line.startswith("POSTGRES_URI="):
                    env["POSTGRES_URI"] = line.split("=", 1)[1].strip()
                    break
    out = subprocess.check_output(
        ["python3", "-m", "src.strategies.universe_resolver",
         "--as-of", "2026-05-22", "--states", "live", "--json"],
        text=True, env=env, cwd="/root/openclaw",
    )
    data = json.loads(out)
    assert isinstance(data, list)
    assert all(isinstance(t, str) for t in data)
