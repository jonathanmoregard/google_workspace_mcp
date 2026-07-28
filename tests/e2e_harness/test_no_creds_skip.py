"""CI-mode contract: with no credentials, `pytest e2e` is 100% clean skips.

Runs the real e2e suite as a subprocess with WORKSPACE_MCP_CREDENTIALS_DIR
pointed at an empty tmpdir - exactly what CI (and a runner without the
OAuth token) experiences.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_pytest_e2e_without_creds_skips_cleanly(tmp_path):
    empty_creds = tmp_path / "creds"
    empty_creds.mkdir()
    env = dict(os.environ)
    env["WORKSPACE_MCP_CREDENTIALS_DIR"] = str(empty_creds)
    env["E2E_RUN_REPORT_PATH"] = str(tmp_path / "last_run.md")

    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "e2e", "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    stdout = completed.stdout

    assert completed.returncode == 0, (
        f"expected clean exit, got {completed.returncode}:\n{stdout}\n"
        f"{completed.stderr}"
    )
    summary = re.search(r"(\d+) skipped", stdout)
    assert summary, f"no skip summary found:\n{stdout}"
    assert int(summary.group(1)) > 0
    assert " passed" not in stdout
    assert " failed" not in stdout
    assert " error" not in stdout
    # The skip UX must tell the runner exactly what to do.
    assert "E2E SKIPPED" in stdout
    assert "bootstrap_auth.py" in stdout
    assert "pending_for_human.md" in stdout
    # The run report artifact is written even for all-skip runs.
    assert (tmp_path / "last_run.md").is_file()
