"""Reviewer/Assignee watching, old-config migration, and no silent wait for the bot review."""
import copy
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mr_pilot.config import DEFAULTS, ConfigError, _merge, validate  # noqa: E402
from mr_pilot.reviewer import Reviewer  # noqa: E402
from mr_pilot.setup_wizard import migrate_config  # noqa: E402

OLD_CFG = """gitlab:
  url: ${GITLAB_URL:-https://code.idas.id}
  poll_interval_seconds: 120      # cek MR baru tiap 2 menit
  skip_draft: true                # abaikan MR Draft
  also_assigned_to_me: false      # true = MR yang assignee-nya Anda juga ikut dicek
review:
  # bot_then_llm = tunggu komentar bot; kalau tidak muncul dalam wait_minutes, pakai API AI
  mode: ${REVIEW_MODE:-bot_then_llm}
"""


def cfg(**gitlab):
    c = _merge(copy.deepcopy(DEFAULTS), {"telegram": {"chat_id": "1"}, "gitlab": gitlab})
    validate(c)
    return c


def test_watch_defaults_to_reviewer_and_assignee():
    assert cfg()["gitlab"]["watch"] == ["reviewer", "assignee"]
    assert cfg(watch="assignee, Reviewer")["gitlab"]["watch"] == ["assignee", "reviewer"]
    with pytest.raises(ConfigError):
        cfg(watch=["owner"])


def test_migrate_old_defaults(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(OLD_CFG, encoding="utf-8")
    assert migrate_config(str(p)) == 3
    s = p.read_text(encoding="utf-8")
    assert "watch: [reviewer, assignee]" in s and "also_assigned_to_me" not in s
    assert "${GITLAB_POLL_SECONDS:-60}" in s and "langsung review" in s
    assert (tmp_path / "config.yaml.bak").read_text(encoding="utf-8") == OLD_CFG
    assert migrate_config(str(p)) == 0  # idempotent


def test_migrate_keeps_user_choices(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("gitlab:\n  poll_interval_seconds: 300\n", encoding="utf-8")
    assert migrate_config(str(p)) == 0
    assert not (tmp_path / "config.yaml.bak").exists()


class _AI:
    def __init__(self, ready):
        self.ready = ready

    def available(self, task):
        return ["x"] if self.ready else []


class _GL:
    def get_commits(self, pid, iid):
        return []

    def get_notes(self, pid, iid):
        return []  # bot has not commented yet


def _reviewer(ready):
    c = copy.deepcopy(DEFAULTS)
    c["review"]["mode"] = "bot_then_llm"
    r = Reviewer(c, _GL(), _AI(ready))
    r.from_llm = lambda mr: {"verdict": "APPROVE", "summary": "ok", "findings": [], "source": "llm"}
    return r


def test_bot_then_llm_reviews_immediately_when_ai_ready():
    rv = _reviewer(True).review({"project_id": 1, "iid": 2, "description": ""}, time.time())
    assert rv and rv["source"] == "llm"


def test_bot_then_llm_waits_only_without_ai():
    assert _reviewer(False).review({"project_id": 1, "iid": 2, "description": ""}, time.time()) is None


def test_source_branch_option_and_buttons(tmp_path):
    from mr_pilot.formatting import review_buttons
    c = _merge(copy.deepcopy(DEFAULTS), {"telegram": {"chat_id": "1"}})
    validate(c)
    assert c["merge"]["source_branch"] == "ask"
    labels = lambda mode: [b[0] for row in review_buttons(7, 1, "http://x", mode) for b in row]  # noqa: E731
    assert "✅ Merge" in labels("ask") and "🗑️ Merge + hapus branch" in labels("ask")
    assert "🗑️ Merge + hapus branch" not in labels("keep")
    assert labels("delete")[0] == "✅ Merge + hapus branch"
    c["merge"]["source_branch"] = "maybe"
    with pytest.raises(ConfigError):
        validate(c)
    p = tmp_path / "config.yaml"
    p.write_text("merge:\n  remove_source_branch: true\n  squash: false\n", encoding="utf-8")
    assert migrate_config(str(p)) == 1
    assert "source_branch: ${MERGE_SOURCE_BRANCH:-ask}" in p.read_text(encoding="utf-8")


def test_want_delete():
    from mr_pilot.app import App
    a = App.__new__(App)
    for mode, action, exp in (("ask", "m", False), ("ask", "md", True), ("ask", "mfd", True),
                              ("keep", "md", False), ("delete", "m", True)):
        a.cfg = {"merge": {"source_branch": mode}}
        assert a.want_delete(action) is exp, (mode, action)


def test_store_reset_keeps_gitlab_markers_and_sessions(tmp_path):
    from mr_pilot.store import Store
    s = Store(str(tmp_path / "db.sqlite"))
    s.upsert("7:1", project_id=7, iid=1, sha="a", status="notified")
    s.add_event("review", "x", "y", "7:1", "info")
    for k in ("cq_commit:7:a", "cq_note:7:1", "revoked_sessions", "tg_offset", "gitlab_user",
              "heartbeat", "last_poll_error", "reject:5"):
        s.kv_set(k, "1")
    deleted = s.reset()
    assert deleted["mrs"] == 1 and deleted["events"] == 1 and deleted["kv"] == 3
    assert s.counts() == {"mrs": 0, "events": 0, "violations": 0, "ai_calls": 0}
    assert s.kv_get("cq_commit:7:a") and s.kv_get("revoked_sessions") and s.kv_get("tg_offset")
    assert s.kv_get("heartbeat") is None and s.kv_get("reject:5") is None
    s.reset(everything=True)
    assert s.kv_get("cq_commit:7:a") is None
