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
