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
