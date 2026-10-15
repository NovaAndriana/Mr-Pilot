"""Merge order between open MRs on the same target branch."""
import copy
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from mr_pilot.config import DEFAULTS  # noqa: E402
from mr_pilot.formatting import e  # noqa: E402
from mr_pilot.merge_order import MergeOrder, affected_comment, analyze, card_lines, must_wait, relations  # noqa: E402
from mr_pilot.store import Store  # noqa: E402


def d(path, body, new=False, old_start=10):
    if new:
        return {"new_path": path, "old_path": path, "new_file": True,
                "diff": f"@@ -0,0 +1,{body.count(chr(10))} @@\n{body}"}
    return {"new_path": path, "old_path": path, "diff": f"@@ -{old_start},3 +{old_start},4 @@\n{body}"}


ADD_FN = d("internal/quota/usecase.go", " func A() {}\n+func GetQuotaDetail(id string) (*Quota, error) {\n+\treturn nil, nil\n+}\n")
USE_FN = d("internal/http/handler.go", " x\n+\tq, err := GetQuotaDetail(id)\n", old_start=40)
RENAME = d("internal/money/format.go", "-func FormatAmount(v int) string {\n+func FormatRupiah(v int) string {\n", old_start=1)
USE_OLD = d("web/report.go", " a\n+\ts := FormatAmount(total)\n", old_start=70)


def kinds(rels):
    return {(r["kind"], r["first"], r["second"]) for r in rels}


def test_new_symbol_used_by_other_mr():
    a, b = analyze([ADD_FN]), analyze([USE_FN])
    assert "GetQuotaDetail" in a["defined"] and "GetQuotaDetail" in b["refs"]
    rels = relations(a, b, 1, 2)
    assert ("uses_new", 1, 2) in kinds(rels)
    assert must_wait(rels, 2) and not must_wait(rels, 1)


def test_removed_symbol_breaks_other_mr():
    a, b = analyze([RENAME]), analyze([USE_OLD])
    assert a["removed"] == ["FormatAmount"] and "FormatRupiah" in a["defined"]
    rels = relations(a, b, 10, 11)
    assert ("breaks", 11, 10) in kinds(rels), "pemakai harus masuk dulu"
    assert "FormatAmount" in rels[0]["reason"]


def test_signature_change_counts_as_break():
    a = analyze([d("pkg/a.go", "-func Charge(id string) error {\n+func Charge(ctx context.Context, id string) error {\n")])
    b = analyze([d("pkg/b.go", " x\n+\terr := Charge(userID)\n", old_start=90)])
    assert a["changed"] == ["Charge"] and ("breaks", 2, 1) in kinds(relations(a, b, 1, 2))


def test_stacked_branch_and_explicit_dependency():
    a = analyze([ADD_FN], [{"id": "c1"}])
    b = analyze([USE_FN], [{"id": "c1"}, {"id": "c2"}])
    rels = relations(a, b, 5, 6)
    assert ("stacked", 5, 6) in kinds(rels) and not any(r["kind"] == "uses_new" for r in rels)
    c = analyze([d("x.go", "+x\n")], [], "Tolong merge setelah !5 ya")
    assert ("explicit", 5, 7) in kinds(relations(analyze([]), c, 5, 7))


def test_migrations_versions_and_lockfiles():
    m1 = analyze([d("db/migrations/000042_add_quota.up.sql", "+ALTER TABLE a;\n", new=True),
                  d("go.mod", "-\tgithub.com/idas/sdk v1.2.0\n+\tgithub.com/idas/sdk v1.3.0\n", old_start=5),
                  d("go.sum", "+x\n", old_start=1)])
    m2 = analyze([d("db/migrations/000043_x.up.sql", "+SELECT 1;\n", new=True),
                  d("go.mod", "-\tgithub.com/idas/sdk v1.2.0\n+\tgithub.com/idas/sdk v1.4.0\n", old_start=5),
                  d("go.sum", "+y\n", old_start=1)])
    k = kinds(relations(m1, m2, 1, 2))
    assert ("migration_order", 1, 2) in k and ("dep_version", None, None) in k and ("lockfile", None, None) in k
    assert not any(x[0] == "conflict" and x for x in k if False)
    m3 = analyze([d("db/migrations/000042_other.up.sql", "+x\n", new=True)])
    assert ("migration_clash", None, None) in kinds(relations(m1, m3, 1, 3))


def test_same_lines_conflict_but_not_far_apart():
    a = analyze([d("svc/a.go", "-\told := 1\n+\told := 2\n", old_start=14)])
    b = analyze([d("svc/a.go", "-\told := 1\n+\told := 3\n", old_start=14)])
    c = analyze([d("svc/a.go", "-\tz := 1\n+\tz := 2\n", old_start=300)])
    assert ("conflict", None, None) in kinds(relations(a, b, 1, 2))
    assert not relations(a, c, 1, 3), "baris berjauhan: aman"


def test_generic_names_and_unrelated_mrs_are_quiet():
    a = analyze([d("a/main.go", "-func main() {\n+func main() {\n\tx()\n", old_start=1)])
    b = analyze([d("b/main.go", " x\n+\tmain()\n+\tNew()\n", old_start=50)])
    assert relations(a, b, 1, 2) == []
    assert relations(analyze([ADD_FN]), analyze([d("web/x.ts", "+const total = sum(items)\n")]), 1, 2) == []


def test_card_and_comment_text_are_natural():
    rels = relations(analyze([ADD_FN]), analyze([USE_FN]), 1, 2)
    for r in rels:
        r.update(other=1)
    lines = card_lines({"relations": rels}, 2, e)
    assert "Merge !1 dulu, baru !2" in "\n".join(lines)
    br = relations(analyze([RENAME]), analyze([USE_OLD]), 10, 11)
    c = affected_comment(10, "staging", br, lambda opts: opts[0])
    assert "!10 baru saja di-merge ke `staging`" in c and "`FormatAmount`" in c and "rebase" in c
    assert not re.search(r"[\U0001F300-\U0001FAFF]|<sub>|\bAI\b", c)


class GL:
    def __init__(self):
        self.mrs = {1: {"iid": 1, "project_id": 7, "sha": "s1", "target_branch": "staging", "title": "rename",
                        "web_url": "u1", "author": {"name": "Bima"}},
                    2: {"iid": 2, "project_id": 7, "sha": "s2", "target_branch": "staging", "title": "report",
                        "web_url": "u2", "author": {"name": "Dewi"}},
                    3: {"iid": 3, "project_id": 7, "sha": "s3", "target_branch": "main", "title": "other"}}
        self.diffs = {1: [RENAME], 2: [USE_OLD], 3: [USE_OLD]}
        self.calls, self.disc = [], {}

    def project_mrs(self, pid, target_branch=None, state="opened"):
        return [m for m in self.mrs.values() if m["target_branch"] == target_branch]

    def get_diffs(self, pid, iid):
        self.calls.append(iid)
        return self.diffs[iid]

    def get_commits(self, pid, iid):
        return [{"id": f"c{iid}"}]

    def mr_discussion(self, pid, iid, body, position=None):
        did = f"d{len(self.disc) + 1}"
        self.disc[did] = {"iid": iid, "notes": [{"body": body, "author": {"username": "nova"}, "resolvable": True}]}
        return {"id": did}

    def get_discussion(self, pid, iid, did):
        return {"notes": self.disc[did]["notes"]}

    def reply_discussion(self, pid, iid, did, body):
        self.disc[did]["notes"].append({"body": body})

    def resolve_discussion(self, pid, iid, did, resolved=True):
        self.disc[did]["resolved"] = resolved


def test_service_check_cache_after_merge_and_followup(tmp_path):
    gl, store = GL(), Store(str(tmp_path / "db"))
    mo = MergeOrder(copy.deepcopy(DEFAULTS), gl, store)
    res = mo.check(gl.mrs[1])
    assert [r["other"] for r in res["relations"]] == [2], "hanya MR ke branch tujuan yang sama"
    assert must_wait(res["relations"], 1)[0]["first"] == 2
    n = len(gl.calls)
    mo.check(gl.mrs[1])
    assert len(gl.calls) == n, "analisis di-cache per commit"
    # !1 is merged anyway: !2 is affected, its author gets one comment
    merged = gl.mrs.pop(1)
    aff = mo.after_merge(merged)
    assert [a["iid"] for a in aff] == [2] and aff[0]["relations"][0]["kind"] == "breaks"
    assert mo.record_affected(merged, aff) == 1 and mo.record_affected(merged, aff) == 0
    did = next(iter(gl.disc))
    assert gl.disc[did]["iid"] == 2 and "FormatAmount" in gl.disc[did]["notes"][0]["body"]
    assert mo.affected_by(7, 2)["merged"] == 1
    # same commit: nothing changes; still using the old name after a new commit: still open
    assert mo.followup(gl.mrs[2]) == []
    gl.mrs[2]["sha"] = "s2b"
    assert mo.followup(gl.mrs[2]) == [] and not gl.disc[did].get("resolved")
    # adapted to the new name: closed with a short reply
    gl.mrs[2]["sha"] = "s2c"
    gl.diffs[2] = [d("web/report.go", " a\n+\ts := FormatRupiah(total)\n", old_start=70)]
    assert mo.followup(gl.mrs[2]) and gl.disc[did]["resolved"] and mo.affected_by(7, 2) is None
    assert "sudah" in gl.disc[did]["notes"][-1]["body"].lower()


def test_disabled():
    c = copy.deepcopy(DEFAULTS)
    c["merge_order"]["enabled"] = False
    mo = MergeOrder(c, GL(), Store(":memory:"))
    assert mo.check(GL().mrs[1]) is None and mo.after_merge(GL().mrs[1]) == []
