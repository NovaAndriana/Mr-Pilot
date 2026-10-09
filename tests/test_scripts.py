"""Static guards for the setup scripts (Windows PowerShell 5.1 can't be run in CI)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_ps1_never_redirects_native_stderr():
    # With $ErrorActionPreference = "Stop", Windows PowerShell 5.1 turns redirected stderr of a native
    # command (docker progress like "Container x Stopping") into a terminating error. Use Invoke-DockerQuiet.
    with open(os.path.join(ROOT, "setup.ps1"), encoding="utf-8-sig") as f:
        lines = f.read().splitlines()
    bad = [f"{i}: {ln.strip()}" for i, ln in enumerate(lines, 1)
           if re.search(r"^\s*(&\s*)?docker\b.*(2>\s*\$null|2>&1|\*>)", ln)]
    assert not bad, "pakai Invoke-DockerQuiet:\n" + "\n".join(bad)


def test_ps1_commands_match_sh():
    with open(os.path.join(ROOT, "setup.ps1"), encoding="utf-8-sig") as f:
        ps = f.read()
    with open(os.path.join(ROOT, "setup.sh"), encoding="utf-8") as f:
        sh = f.read()
    ps_cmds = set(re.findall(r'"(\w[\w-]*)"', re.search(r"ValidateSet\(([^)]*)\)", ps).group(1)))
    sh_cmds = set(re.search(r"\n\s*(install\|[\w|-]+)\) CMD=", sh).group(1).split("|"))
    assert ps_cmds == sh_cmds


# ------------------------------------------------- update installs Claude Code when it's used
import json  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402

import pytest  # noqa: E402

PWSH = shutil.which("pwsh") or os.environ.get("PWSH")
MOCK_DOCKER = """#!/bin/bash
echo "docker $*" >> "$(dirname "$0")/../calls.log"
exit 0
"""


def _sandbox(tmp_path, script, overrides=None, data_env="", root_env="MRP_PORT=8787\\nINSTALL_CLAUDE_CODE=false\\n"):
    (tmp_path / "bin").mkdir()
    for name, body in (("docker", MOCK_DOCKER), ("curl", "#!/bin/bash\nexit 0\n")):
        (tmp_path / "bin" / name).write_text(body)
        (tmp_path / "bin" / name).chmod(0o755)
    app = tmp_path / "app"
    (app / "data").mkdir(parents=True)
    shutil.copy(os.path.join(ROOT, script), app / script)
    (app / ".env").write_text(root_env)
    (app / "data" / ".env").write_text(data_env)
    if overrides is not None:
        (app / "data" / "ai_overrides.json").write_text(json.dumps(overrides))
    env = {"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", "HOME": str(tmp_path), "MRP_HEALTH_WAIT_TRIES": "1"}
    return app, env


CASES = [
    ({"providers": {"claude_code": {"enabled": True}}}, "", True),
    ({"providers": {"claude_code": {"api_key": "sk-ant-oat01-x"}}}, "", True),
    (None, "CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-y\n", True),
    ({"providers": {"claude_code": {"enabled": False}, "gemini": {"enabled": True}}}, "", False),
    (None, "", False),
]


@pytest.mark.parametrize("overrides,data_env,want", CASES)
def test_setup_sh_update_installs_claude_code_when_used(tmp_path, overrides, data_env, want):
    app, env = _sandbox(tmp_path, "setup.sh", overrides, data_env)
    r = subprocess.run(["bash", "setup.sh", "update"], cwd=app, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert ("INSTALL_CLAUDE_CODE=true" in (app / ".env").read_text()) is want
    assert "docker compose build" in (tmp_path / "calls.log").read_text()


@pytest.mark.skipif(not PWSH, reason="pwsh tidak ada")
@pytest.mark.parametrize("overrides,data_env,want", CASES)
def test_setup_ps1_update_installs_claude_code_when_used(tmp_path, overrides, data_env, want):
    app, env = _sandbox(tmp_path, "setup.ps1", overrides, data_env)
    r = subprocess.run([PWSH, "-NoProfile", "-File", "setup.ps1", "update"], cwd=app, env=env,
                       capture_output=True, text=True, timeout=120)
    assert ("INSTALL_CLAUDE_CODE=true" in (app / ".env").read_text()) is want, r.stdout + r.stderr
    assert "docker compose build" in (tmp_path / "calls.log").read_text()


@pytest.mark.skipif(not PWSH, reason="pwsh tidak ada")
def test_setup_ps1_update_with_flag(tmp_path):
    app, env = _sandbox(tmp_path, "setup.ps1")
    subprocess.run([PWSH, "-NoProfile", "-File", "setup.ps1", "update", "-WithClaudeCode"], cwd=app, env=env,
                   capture_output=True, text=True, timeout=120)
    assert "INSTALL_CLAUDE_CODE=true" in (app / ".env").read_text()
