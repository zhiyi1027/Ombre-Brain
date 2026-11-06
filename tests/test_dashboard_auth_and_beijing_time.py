import json
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_authentication_initializes_memory_list_and_dates_use_beijing():
    script = Path(__file__).with_name("dashboard_auth_and_beijing_time.cjs")
    completed = subprocess.run(
        [shutil.which("node"), str(script)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    result = json.loads(completed.stdout)
    assert result == {
        "authScenarios": 7,
        "timeAssertions": 42,
        "syntheticOnly": True,
    }
