"""Smarter review: line-annotated diff, file context, evidence verification, skeptical second pass."""
import copy
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mr_pilot.config import DEFAULTS  # noqa: E402
from mr_pilot.formatting import build_messages  # noqa: E402
from mr_pilot.review_context import annotate_diff, order_diffs, verify_findings  # noqa: E402
from mr_pilot.reviewer import Reviewer, normalize  # noqa: E402

GO_FILE = """package quota

func Get(repo Repo, id string) (*Quota, error) {
	data, _ := repo.Find(id)
	q := "SELECT * FROM t WHERE id=" + id
	return data, nil
}
"""
DIFF = """@@ -1,3 +1,7 @@
 package quota
 
-func Get(id string) {
+func Get(repo Repo, id string) (*Quota, error) {
+	data, _ := repo.Find(id)
+	q := "SELECT * FROM t WHERE id=" + id
+	return data, nil
+}
"""


def test_annotate_diff_uses_new_file_line_numbers():
    out = annotate_diff(DIFF).splitlines()
    assert out[1].startswith("    1 ") and "package quota" in out[1]
    removed = next(x for x in out if x.strip().startswith("-func"))
    assert not removed.strip()[0].isdigit()
    assert any(x.startswith("    4 +\tdata, _ := repo.Find(id)") for x in out)


def test_code_is_reviewed_before_tests_and_docs():
    diffs = [{"new_path": "README.md", "diff": "+a\n"}, {"new_path": "x_test.go", "diff": "+a\n+b\n"},
             {"new_path": "x.go", "diff": "+a\n"}, {"new_path": "go.sum", "diff": "+z\n"}]
    keep, skipped = order_diffs(diffs, ["go.sum"])
    assert [d["new_path"] for d in keep] == ["x.go", "x_test.go", "README.md"] and skipped == ["go.sum"]


def test_hallucinated_evidence_is_downgraded_and_lines_fixed():
    fs = [{"severity": "blocker", "file": "q.go", "line": 1, "evidence": 'q := "SELECT * FROM t WHERE id=" + id'},
          {"severity": "major", "file": "q.go", "line": 3, "evidence": "db.Exec(rawSQL)"}]
    verify_findings(fs, DIFF, {"q.go": GO_FILE})
    assert fs[0]["verified"] is True and fs[0]["severity"] == "blocker" and fs[0]["line"] == 5
    assert fs[1]["verified"] is False and fs[1]["severity"] == "minor"


def test_normalize_reads_path_colon_line_and_new_fields():
    rv = normalize({"verdict": "approve", "risk": "HIGH", "tests": "Belum ada test",
                    "questions": ["Kenapa?"], "findings": [{"severity": "high", "file": "a.go:42", "title": "x",
                                                            "suggestion": "pakai param"}]}, "llm")
    f = rv["findings"][0]
    assert (f["file"], f["line"], f["severity"]) == ("a.go", 42, "major")
    assert rv["risk"] == "high" and rv["verdict"] == "NEEDS_ATTENTION" and rv["questions"] == ["Kenapa?"]


class GL:
    def get_diffs(self, pid, iid):
        return [{"new_path": "internal/quota/q.go", "diff": DIFF}]

    def get_file_raw(self, pid, path, ref):
        assert ref == "abc123"
        return GO_FILE

    def get_commits(self, pid, iid):
        return [{"title": "feat(IDAS-1): quota per user"}]


class AI:
    def __init__(self, first, verdicts):
        self.first, self.verdicts, self.calls = first, verdicts, []

    def available(self, task):
        return ["x"]

    def complete(self, system, user, task, validate=None):
        self.calls.append((system, user))
        if len(self.calls) == 1:
            return json.dumps(self.first), "fake"
        return json.dumps({"checks": self.verdicts}), "fake"


MR = {"project_id": 7, "iid": 1, "title": "feat: quota", "source_branch": "f", "target_branch": "staging",
      "description": "Kuota", "sha": "abc123"}


def _review(first, verdicts):
    cfg = copy.deepcopy(DEFAULTS)
    ai = AI(first, verdicts)
    return Reviewer(cfg, GL(), ai).from_llm(MR), ai


def test_prompt_has_context_and_second_pass_drops_false_positive():
    first = {"verdict": "REQUEST_CHANGES", "summary": "s", "risk": "high", "findings": [
        {"severity": "blocker", "file": "internal/quota/q.go", "line": 5, "title": "SQL injection",
         "evidence": 'q := "SELECT * FROM t WHERE id=" + id', "suggestion": "Pakai placeholder"},
        {"severity": "major", "file": "internal/quota/q.go", "line": 4, "title": "Error diabaikan",
         "evidence": "data, _ := repo.Find(id)"}]}
    rv, ai = _review(first, [{"id": 0, "valid": True, "severity": "blocker"}, {"id": 1, "valid": False}])
    system, user = ai.calls[0]
    assert "Checklist" in system and "evidence" in system
    assert "    5 +\tq := " in user                          # line-annotated diff
    assert "isi lengkap setelah perubahan" in user and "    4| \tdata" in user  # numbered file context
    assert "feat(IDAS-1): quota per user" in user              # commit messages
    assert len(ai.calls) == 2                                   # skeptical second pass ran
    assert [f["title"] for f in rv["findings"]] == ["SQL injection"]
    assert rv["rejected_findings"] == 1 and rv["verdict"] == "REQUEST_CHANGES"
    assert "flags" not in rv  # small change (< 20 lines): no "missing tests" flag


def test_unproven_request_changes_becomes_needs_attention():
    first = {"verdict": "REQUEST_CHANGES", "summary": "s", "findings": [
        {"severity": "blocker", "file": "internal/quota/q.go", "title": "Race condition", "evidence": "mu.Lock()"}]}
    rv, ai = _review(first, [{"id": 0, "valid": True, "severity": "major"}])
    # evidence not in code -> downgraded to major, so no blocker left -> not REQUEST_CHANGES
    assert rv["findings"][0]["verified"] is False and rv["verdict"] == "NEEDS_ATTENTION"


def test_card_shows_line_suggestion_risk_questions_and_unverified():
    rv = normalize({"verdict": "NEEDS_ATTENTION", "risk": "medium", "tests": "Belum ada test untuk Get",
                    "questions": ["Apakah id sudah divalidasi?"],
                    "findings": [{"severity": "major", "file": "q.go", "line": 4, "title": "Error diabaikan",
                                  "suggestion": "Tangani err dari repo.Find"}]}, "llm")
    rv["findings"][0]["verified"] = False
    mr = {"title": "t", "iid": 1, "source_branch": "f", "target_branch": "staging", "web_url": "http://x",
          "author": {"name": "Dewi"}, "references": {"full": "g/r!1"}}
    _, card = build_messages(mr, rv, [], quality=None)
    for s in ("<code>q.go:4</code>", "💡 Tangani err", "Risiko: sedang", "🧪 Belum ada test",
              "Apakah id sudah divalidasi?", "belum terbukti"):
        assert s in card, s


def test_missing_tests_flag():
    from mr_pilot.review_context import code_change_size, has_test_change
    big = [{"new_path": "a.go", "diff": "".join(f"+l{i}\n" for i in range(25))}]
    assert code_change_size(big) == 25 and not has_test_change(big)
    assert has_test_change(big + [{"new_path": "a_test.go", "diff": "+t\n"}])
