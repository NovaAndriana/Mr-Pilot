"""Standards agreed in the team doc: new rules, branch/size conventions, MR template, upgrade of standards/."""
import copy
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot.config import DEFAULTS  # noqa: E402
from mr_pilot.merge_order import analyze  # noqa: E402
from mr_pilot.setup_wizard import migrate_standards  # noqa: E402
from mr_pilot.standards import Standards, suggest_branch  # noqa: E402

STD = os.path.join(ROOT, "standards")


def std():
    c = copy.deepcopy(DEFAULTS["code_quality"])
    c["standards_dir"] = STD
    return Standards(c)


def rules(s, path, line):
    return [v["rule"] for v in s.check_file_diff(path, f"@@ -1,0 +1,1 @@\n+{line}\n")]


def test_new_rules():
    s = std()
    assert rules(s, "internal/a.go", "resp, err := http.Get(url)") == ["go-http-no-timeout"]
    assert rules(s, "internal/a.go", "c := &http.Client{}") == ["go-http-no-timeout"]
    assert rules(s, "internal/a.go", "c := &http.Client{Timeout: 10 * time.Second}") == []
    assert rules(s, "internal/quota/usecase.go", "ctx := context.Background()") == ["go-context-background"]
    assert rules(s, "cmd/api/main.go", "ctx := context.Background()") == []
    assert rules(s, "src/auth.ts", 'localStorage.setItem("access_token", t)') == ["ts-localstorage-token"]
    assert rules(s, "src/a/components/Row.tsx", '<div className="row" onClick={open}>') == ["react-div-onclick"]
    assert rules(s, "src/a/components/Row.tsx", "const r = await fetch(url)") == ["ts-raw-fetch-in-component"]
    assert rules(s, "src/api/client.ts", "const r = await fetch(url)") == []
    assert "ts-raw-fetch-in-component" in rules(s, "mobile/src/features/q/screens/QScreen.tsx", "axios.get(u)")


def test_new_rules_are_never_posted_to_gitlab():
    """Only errors get a GitLab thread: the new rules are warning/info so they stay in Telegram/dashboard."""
    s = std()
    new = {"go-http-no-timeout", "go-context-background", "ts-localstorage-token", "react-div-onclick",
           "ts-raw-fetch-in-component"}
    assert {r["id"]: r["severity"] for r in s.rules if r["id"] in new} and \
        all(r["severity"] != "error" for r in s.rules if r["id"] in new)


def test_branch_convention_accepts_both_team_formats():
    s = std()
    for ok in ("feat/IDAS-5413-quota-bundle", "fix/IDAS-1", "IDAS-5327/fix-role-signature", "hotfix/IDAS-9-x"):
        assert s.check_branch(ok) == [], ok
    v = s.check_branch("quota-bundle")[0]
    assert v["rule"] == "branch-name-convention" and v["severity"] == "info" and v["source"] == "convention"
    assert suggest_branch("idas_5327 fix role signature") == "fix/IDAS-5327-role-signature"
    s.cfg["conventions"]["branch_name_pattern"] = ""
    assert s.check_branch("anything") == []


def test_mr_size_ignores_lock_and_generated_files():
    s = std()
    s.cfg["conventions"]["max_mr_lines"] = 10
    code = {"new_path": "internal/a.go", "diff": "@@ -1,0 +1,8 @@\n" + "+x\n" * 8}
    lock = {"new_path": "go.sum", "diff": "@@ -1,0 +1,90 @@\n" + "+h\n" * 90}
    mock = {"new_path": "internal/mocks/repo.go", "diff": "@@ -1,0 +1,90 @@\n" + "+m\n" * 90}
    assert s.check_size([code, lock, mock]) == []
    big = s.check_size([code, {"new_path": "internal/b.go", "diff": "@@ -1,3 +1,0 @@\n-a\n-b\n-c\n"}])
    assert big[0]["rule"] == "mr-too-large" and "11 baris" in big[0]["message"]
    s.cfg["conventions"]["max_mr_lines"] = 0
    assert s.check_size([code] * 50) == []


def test_mr_template_has_checklist_and_dependency_line():
    with open(os.path.join(STD, "merge_request_template.md"), encoding="utf-8") as f:
        tpl = f.read()
    s = std()
    # fresh template: only the template's own unticked boxes, no "missing checklist"
    assert [v["rule"] for v in s.check_checklist(tpl)] == ["pr-checklist-unchecked"]
    # the template's own items match standards/pr-checklist.md
    s.cfg["conventions"]["require_pr_checklist"] = "match_template"
    assert "pr-checklist-incomplete" not in {v["rule"] for v in s.check_checklist(tpl)}
    # the "contoh !123" hint in an HTML comment is not a dependency; a filled-in line is
    assert analyze([], [], tpl)["depends_on"] == []
    filled = tpl.replace("Harus di-merge setelah: -", "Harus di-merge setelah: !123")
    assert analyze([], [], filled)["depends_on"] == [123]


def test_every_bundled_file_is_recorded_as_shipped():
    """Editing a file in standards/ without recording it would leave old copies un-upgraded next release."""
    with open(os.path.join(ROOT, "mr_pilot", "standards_shipped.json"), encoding="utf-8") as f:
        shipped = json.load(f)
    for root, _, files in os.walk(STD):
        for name in files:
            p = os.path.join(root, name)
            rel = os.path.relpath(p, STD).replace(os.sep, "/")
            with open(p, "rb") as fh:
                h = hashlib.sha256(fh.read().replace(b"\r\n", b"\n")).hexdigest()
            assert h in shipped.get(rel, []), f"jalankan: python -m mr_pilot.setup_wizard --record-standards ({rel})"


def test_migrate_standards(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "data"
    (src / "ci").mkdir(parents=True)
    dst.mkdir()
    (src / "go.md").write_text("baru\n")
    (src / "react.md").write_text("baru react\n")
    (src / "ci" / "x.yml").write_text("ci\n")
    (src / "rules.yaml").write_text("rules: []\n")
    # go.md: an untouched copy of an older shipped version -> upgraded
    with open(os.path.join(ROOT, "mr_pilot", "standards_shipped.json"), encoding="utf-8") as f:
        shipped = json.load(f)
    old_go = [h for h in shipped["go.md"]]
    assert len(old_go) >= 2, "versi go.md sebelum ini ikut tercatat"
    import subprocess
    try:
        old = subprocess.run(["git", "-C", ROOT, "show", "fba03c9:standards/go.md"], capture_output=True,
                             check=True).stdout
    except Exception:
        old = None
    if old:
        (dst / "go.md").write_bytes(old.replace(b"\n", b"\r\n"))
    (dst / "react.md").write_text("diubah tim\n")  # edited by the team -> left alone
    (dst / "rules.yaml").write_text("rules: []\n")  # already current
    changed = migrate_standards(str(dst), str(src))
    assert "react.md" not in changed and "rules.yaml" not in changed and "ci/x.yml" in changed
    assert (dst / "react.md").read_text() == "diubah tim\n" and (dst / "ci" / "x.yml").read_text() == "ci\n"
    if old:
        assert "go.md" in changed and (dst / "go.md").read_text() == "baru\n" and (dst / "go.md.bak").exists()
    assert migrate_standards(str(dst), str(src)) == []
