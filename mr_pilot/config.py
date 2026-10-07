"""Load config.yaml + .env, with ${ENV_VAR} expansion and sane defaults."""
import copy
import os
import re

import yaml

_ENV = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")

DEFAULTS = {
    "gitlab": {
        "url": "",
        "token": "",
        "username": "",
        "verify_ssl": True,
        "poll_interval_seconds": 120,
        "skip_draft": True,
        "also_assigned_to_me": False,
        "projects": [],
    },
    "telegram": {
        "bot_token": "",
        "chat_id": 0,
        "allowed_user_ids": [],
        "proxy": "",
        "startup_message": True,
    },
    "review": {
        "mode": "bot_then_llm",  # llm | bot | bot_then_llm
        "bot": {"usernames": ["system"], "marker": "AI Code Review", "wait_minutes": 15},
        "llm": {
            "provider": "anthropic",  # anthropic | openai
            "api_key": "",
            "model": "claude-sonnet-5-5",
            "base_url": "",
            "max_diff_chars": 120000,
            "language": "Indonesia",
            "extra_rules": "",
        },
        "ignore_files": ["*.lock", "go.sum", "package-lock.json", "yarn.lock",
                         "pnpm-lock.yaml", "vendor/*", "*.min.js", "*.snap"],
    },
    "merge": {
        "require_pipeline_success": True,
        "allow_no_pipeline": True,
        "approve_before_merge": True,
        "remove_source_branch": True,
        "squash": False,
        "confirm_if_not_approved": True,
        "confirm_if_quality_errors": True,
    },
    "code_quality": {
        "enabled": False,
        "standards_dir": "standards",
        "general_document": "general.md",
        "rules_file": "rules.yaml",
        "stacks": {
            "go": {"name": "Backend Go", "paths": ["**/*.go"], "document": "go.md"},
            "react": {"name": "Frontend React", "paths": ["**/*.ts", "**/*.tsx", "**/*.js", "**/*.jsx"],
                      "exclude": ["mobile/**", "**/node_modules/**"], "document": "react.md"},
            "react-native": {"name": "Mobile React Native", "paths": ["mobile/**/*.ts", "mobile/**/*.tsx"],
                             "document": "react-native.md"},
        },
        "conventions": {
            "mr_title_pattern": r"^(feat|fix|refactor|perf|test|docs|chore|ci|build|revert)(\([A-Z][A-Z0-9]+-\d+\))?!?: .+",
            "commit_message_pattern": r"^(feat|fix|refactor|perf|test|docs|chore|ci|build|revert)(\(.+\))?!?: .+",
            "commit_message_severity": "info",
            "pr_checklist": "pr-checklist.md",
            "require_pr_checklist": "warn_unchecked",
        },
        "ai_check": True,
        "ai_min_confidence": "high",
        "ai_max_violations": 8,
        "ai_max_diff_chars": 60000,
        "report": {
            "commit_comments": True,
            "min_severity_to_post": "warning",
            "max_comments_per_commit": 10,
            "ai_inline_comments": True,
            "mr_summary_note": True,
            "commit_status": True,
            "status_name": "code-standard",
            "status_fail_on": "none",
            "comment_footer": "Pengecekan standar kode otomatis",
        },
    },
    "ai": {
        # enabled: auto = aktif otomatis jika API key / login tersedia
        "providers": {
            "claude_code": {"type": "claude_code", "enabled": "auto"},
            "anthropic": {"type": "anthropic", "enabled": "auto"},
            "gemini": {"type": "gemini", "enabled": "auto"},
            "openrouter": {"type": "openrouter", "enabled": "auto"},
            "groq": {"type": "groq", "enabled": "auto"},
            "openai": {"type": "openai", "enabled": "auto"},
            "local": {"type": "local", "enabled": "auto"},
        },
        "order": ["claude_code", "anthropic", "gemini", "openrouter", "groq", "openai", "local"],
        "tasks": {},
        "timeout": 300,
        "overrides_file": "ai_overrides.json",
    },
    "dashboard": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8787,
        "password": "",
        "public_url": "",
    },
    "teams": {
        "mode": "power_automate",  # power_automate | telegram_copy | off
        "webhook_url": "",
        "templates": [
            "Halo tim, MR {ref} ({title}) sudah saya merge ke {target_branch}. Thanks {author_first}",
        ],
    },
    "storage": {"db_path": "mr_pilot.db", "log_file": "logs/mr-pilot.log"},
}


def _coerce(s):
    low = s.strip().lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if re.fullmatch(r"-?(0|[1-9]\d*)", s.strip()):  # "007" tetap string
        return int(s)
    return s


# Nilai rahasia/teks bebas: jangan pernah diubah jadi bool/int (password "007007" atau "yes" tetap utuh)
NO_COERCE = {("dashboard", "password"), ("gitlab", "token"), ("telegram", "bot_token"), ("teams", "webhook_url"),
             ("gitlab", "url"), ("dashboard", "public_url")}


def _expand(v, coerce=True, path=()):
    if isinstance(v, str):
        whole = _ENV.fullmatch(v.strip())
        out = _ENV.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), v)
        # "${X}" sendirian -> boleh jadi bool/int (mis. enabled: ${CODE_QUALITY_ENABLED:-true})
        return _coerce(out) if coerce and whole and out != "" and tuple(path[-2:]) not in NO_COERCE else out
    if isinstance(v, dict):
        return {k: _expand(x, coerce, path + (k,)) for k, x in v.items()}
    if isinstance(v, list):
        return [_expand(x, coerce, path) for x in v]
    return v


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def parse_env_line(line):
    """KEY=VALUE -> (key, value) or None. Mendukung `export`, kutip, dan komentar di akhir baris
    (`PASSWORD=abc123  # catatan` -> abc123). Di dalam kutip, # dianggap bagian dari nilai."""
    line = line.strip().lstrip("\ufeff")
    if not line or line.startswith("#") or "=" not in line:
        return None
    k, v = line.split("=", 1)
    k = k.strip()
    if k.startswith("export "):
        k = k[7:].strip()
    v = v.strip()
    if v[:1] in ('"', "'"):
        q = v[0]
        end = v.find(q, 1)
        v = v[1:end] if end > 0 else v[1:]
    else:
        v = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    return k, v


def read_env_file(path):
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8-sig") as f:  # -sig: aman untuk file dari Notepad (BOM)
            for line in f:
                kv = parse_env_line(line)
                if kv:
                    data[kv[0]] = kv[1]
    return data


def load_dotenv(path):
    """Isi os.environ dari .env. Variabel environment yang sudah terisi tetap menang;
    yang kosong ditimpa nilai dari file."""
    for k, v in read_env_file(path).items():
        if not os.environ.get(k):
            os.environ[k] = v


def load_config(path="config.yaml"):
    base_dir = os.path.dirname(os.path.abspath(path))
    load_dotenv(os.path.join(base_dir, ".env"))
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cfg = _merge(DEFAULTS, _expand(raw))

    # paths relative to config folder
    for k in ("db_path", "log_file"):
        p = cfg["storage"][k]
        if not os.path.isabs(p):
            cfg["storage"][k] = os.path.join(base_dir, p)

    cfg["_base_dir"] = base_dir
    sd = cfg["code_quality"]["standards_dir"]
    if not os.path.isabs(sd):
        cfg["code_quality"]["standards_dir"] = os.path.join(base_dir, sd)

    t = cfg["telegram"]
    t["chat_id"] = int(t["chat_id"] or 0)
    t["allowed_user_ids"] = [int(x) for x in (t["allowed_user_ids"] or [t["chat_id"]]) if x]

    if cfg["review"]["mode"] not in ("llm", "bot", "bot_then_llm"):
        raise ValueError("review.mode harus llm | bot | bot_then_llm")
    if cfg["teams"]["mode"] not in ("power_automate", "telegram_copy", "off"):
        raise ValueError("teams.mode harus power_automate | telegram_copy | off")
    return cfg


def require(cfg, *keys):
    """Raise a readable error if required settings are empty."""
    missing = []
    for dotted in keys:
        node = cfg
        for part in dotted.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        if not node:
            missing.append(dotted)
    if missing:
        raise ValueError("Config belum diisi: " + ", ".join(missing))
