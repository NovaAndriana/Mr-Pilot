"""AI provider management: Local (Ollama/LM Studio), Claude Code CLI, Anthropic, Gemini,
OpenRouter, Groq, OpenAI. Fallback order, per-task routing, connection test, model lists,
usage log, and runtime overrides edited from the dashboard (ai_overrides.json)."""
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time

import requests

from .util import short_error

log = logging.getLogger("mr_pilot.ai")

TASKS = {"review": "Review MR", "standards": "Cek standar kode"}

TYPES = {
    "anthropic": {"label": "Anthropic API (Claude)", "base_url": "https://api.anthropic.com",
                  "model": "claude-sonnet-5-5", "needs_key": True, "key_env": "ANTHROPIC_API_KEY"},
    "claude_code": {"label": "Claude Code (CLI, langganan Pro/Max/Team)", "base_url": "",
                    "model": "sonnet", "needs_key": False, "key_env": "CLAUDE_CODE_OAUTH_TOKEN"},
    "gemini": {"label": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta",
               "model": "gemini-3.8-flash", "needs_key": True, "key_env": "GEMINI_API_KEY"},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "model": "anthropic/claude-sonnet-5.5", "needs_key": True, "key_env": "OPENROUTER_API_KEY"},
    "groq": {"label": "Groq", "base_url": "https://api.groq.com/openai/v1",
             "model": "openai/gpt-oss-120b", "needs_key": True, "key_env": "GROQ_API_KEY"},
    "openai": {"label": "OpenAI / OpenAI-compatible", "base_url": "https://api.openai.com/v1",
               "model": "gpt-5", "needs_key": True, "key_env": "OPENAI_API_KEY"},
    "local": {"label": "Local (Ollama / LM Studio / vLLM)", "base_url": "http://localhost:11434/v1",
              "model": "qwen2.5-coder:14b", "needs_key": False, "key_env": ""},
}
EDITABLE = ("enabled", "model", "base_url", "label", "cli_path", "timeout", "temperature")


def claude_missing_msg():
    if os.environ.get("MRP_IN_DOCKER") == "1":
        return ("CLI Claude Code belum terpasang di image Docker. Jalankan `setup.bat update -WithClaudeCode` "
                "(Linux: `./setup.sh update --with-claude-code`)")
    return "CLI `claude` tidak ditemukan. Pasang Claude Code di komputer ini atau isi cli_path"


class AIError(Exception):
    pass


def in_docker():
    return os.path.exists("/.dockerenv") or os.environ.get("MRP_IN_DOCKER") == "1"


def _clean_list(v):
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",")]
    return [x for x in (v or []) if x]


def normalize_providers(cfg):
    """Build {name: provider} from cfg['ai'] (+ legacy review.llm)."""
    ai = cfg.get("ai") or {}
    out = {}
    for name, p in (ai.get("providers") or {}).items():
        p = dict(p or {})
        t = p.get("type") or name
        if t not in TYPES:
            log.warning("Tipe AI tidak dikenal: %s (%s)", t, name)
            continue
        base = TYPES[t]
        p["type"] = t
        p["name"] = name
        p.setdefault("label", base["label"])
        p["model"] = p.get("model") or base["model"]
        p["base_url"] = (p.get("base_url") or base["base_url"]).rstrip("/")
        if t == "local" and in_docker() and "localhost" in p["base_url"]:
            p["base_url"] = p["base_url"].replace("localhost", "host.docker.internal")
        p["api_key"] = p.get("api_key") or (os.environ.get(base["key_env"], "") if base["key_env"] else "")
        en = p.get("enabled", "auto")
        p["_auto"] = str(en).lower() == "auto"
        # "auto" = aktif kalau kredensial/CLI tersedia. Local tidak bisa dideteksi murah -> harus diaktifkan manual.
        p["enabled"] = en if isinstance(en, bool) else (
            (t != "local") if p["_auto"] else str(en).lower() in ("true", "1", "yes"))
        out[name] = p
    legacy = (cfg.get("review") or {}).get("llm") or {}
    if legacy.get("api_key") and "default" not in out:
        t = "anthropic" if legacy.get("provider", "anthropic") == "anthropic" else "openai"
        out["default"] = {"name": "default", "type": t, "label": "Dari review.llm (lama)", "enabled": True, "_auto": False,
                          "_legacy": True,
                          "model": legacy.get("model") or TYPES[t]["model"], "api_key": legacy["api_key"],
                          "base_url": (legacy.get("base_url") or TYPES[t]["base_url"]).rstrip("/")}
    return out


class AIManager:
    def __init__(self, cfg, store=None):
        self.cfg = cfg
        self.store = store
        base_dir = cfg.get("_base_dir", ".")
        self.override_path = os.path.join(base_dir, (cfg.get("ai") or {}).get("overrides_file") or "ai_overrides.json")
        self.lock = threading.RLock()
        self._mtime = None
        self.reload(force=True)

    # ------------------------------------------------------------ config
    def _load_overrides(self):
        if not os.path.exists(self.override_path):
            return {}
        try:
            with open(self.override_path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            log.exception("ai_overrides.json tidak valid, diabaikan")
            return {}

    def reload(self, force=False):
        mt = os.path.getmtime(self.override_path) if os.path.exists(self.override_path) else 0
        if not force and mt == self._mtime:
            return
        with self.lock:
            self._mtime = mt
            ai = self.cfg.get("ai") or {}
            self.providers = normalize_providers(self.cfg)
            ov = self._load_overrides()
            for name, p in (ov.get("providers") or {}).items():
                if name not in self.providers:
                    t = p.get("type")
                    if t not in TYPES:
                        continue
                    self.providers[name] = {"name": name, "type": t, "label": TYPES[t]["label"], "_auto": False,
                                            "model": TYPES[t]["model"], "base_url": TYPES[t]["base_url"],
                                            "api_key": "", "enabled": True, "_custom": True}
                tgt = self.providers[name]
                for k in EDITABLE:
                    if k in p and p[k] not in (None, ""):
                        tgt[k] = p[k]
                if "enabled" in p:
                    tgt["_auto"] = False
                if p.get("api_key"):
                    tgt["api_key"] = p["api_key"]
            self.order = _clean_list(ov.get("order") or ai.get("order")) or list(self.providers)
            if "default" in self.providers and self.providers["default"].get("_legacy") and not ov.get("order"):
                self.order = ["default"] + [n for n in self.order if n != "default"]
            self.order = [n for n in self.order if n in self.providers] + \
                         [n for n in self.providers if n not in self.order]
            self.tasks = dict(ai.get("tasks") or {})
            self.tasks.update(ov.get("tasks") or {})
            self.timeout = int(ai.get("timeout", 300))

    def save_overrides(self, patch):
        """patch: {providers: {name: {...}}, order: [...], tasks: {...}, delete: [name]}"""
        with self.lock:
            ov = self._load_overrides()
            prov = ov.setdefault("providers", {})
            if not isinstance(patch, dict):
                raise ValueError("format pengaturan AI tidak valid")
            for name, p in (patch.get("providers") or {}).items():
                if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", str(name)):
                    raise ValueError(f"Nama provider tidak valid: {name!r} (huruf kecil, angka, - dan _)")
                if not isinstance(p, dict):
                    raise ValueError(f"Pengaturan {name} tidak valid")
                p = _validate_fields(name, p)
                if name not in self.providers and not p.get("type") and name not in prov:
                    raise ValueError(f"Provider {name} belum ada; sertakan type untuk menambah")
                cur = prov.setdefault(name, {})
                if p.get("type") and name not in self.providers:
                    if p["type"] not in TYPES:
                        raise ValueError(f"Tipe tidak dikenal: {p['type']}")
                    cur["type"] = p["type"]
                for k in EDITABLE:
                    if k in p:
                        cur[k] = p[k]
                if p.get("clear_key"):
                    cur.pop("api_key", None)
                elif p.get("api_key"):
                    cur["api_key"] = p["api_key"].strip()
            for name in patch.get("delete") or []:
                prov.pop(name, None)
            if "order" in patch:
                ov["order"] = _clean_list(patch["order"])
            if "tasks" in patch:
                ov["tasks"] = {k: v for k, v in (patch["tasks"] or {}).items() if k in TASKS}
            d = os.path.dirname(self.override_path)
            if d:
                os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(ov, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.override_path)
            try:
                os.chmod(self.override_path, 0o600)
            except OSError:
                pass
            self.reload(force=True)

    # ------------------------------------------------------------ status
    def ready(self, p):
        """(ok, reason) — whether provider p can be called."""
        if not p.get("enabled"):
            return False, "nonaktif"
        t = p["type"]
        if t == "claude_code":
            if not self._claude_bin(p):
                return False, claude_missing_msg()
            if not (p.get("api_key") or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
                    or os.path.exists(os.path.expanduser("~/.claude/.credentials.json"))):
                return False, "belum login (isi CLAUDE_CODE_OAUTH_TOKEN dari `claude setup-token`)"
            return True, ""
        if TYPES[t]["needs_key"] and not p.get("api_key"):
            return False, "API key belum diisi"
        return True, ""

    def available(self, task=None):
        return [n for n in self.chain(task) if self.ready(self.providers[n])[0]]

    def chain(self, task=None):
        self.reload()
        route = self.tasks.get(task) if task else None
        if route and route in self.providers:
            return [route] + [n for n in self.order if n != route]
        return list(self.order)

    def describe(self):
        self.reload()
        out = []
        for n in self.order:
            p = self.providers[n]
            ok, why = self.ready(p)
            key = p.get("api_key") or ""
            out.append({"name": n, "type": p["type"], "type_label": TYPES[p["type"]]["label"], "label": p.get("label"),
                        "model": p.get("model"), "base_url": p.get("base_url"), "enabled": bool(p.get("enabled")),
                        "ready": ok, "reason": why, "needs_key": TYPES[p["type"]]["needs_key"],
                        "key_set": bool(key), "key_hint": ("…" + key[-4:]) if len(key) > 8 else ("terisi" if key else ""),
                        "key_env": TYPES[p["type"]]["key_env"], "custom": bool(p.get("_custom"))})
        return {"providers": out, "order": self.order, "tasks": self.tasks, "task_labels": TASKS,
                "types": {k: {"label": v["label"], "model": v["model"], "base_url": v["base_url"],
                              "needs_key": v["needs_key"]} for k, v in TYPES.items()}}

    # ----------------------------------------------------------- calling
    def complete(self, system, user, task="review", validate=None):
        """Try providers in order; returns (text, provider_name). Raises AIError if all fail.
        validate(text) may raise to reject a reply (e.g. broken JSON) -> next provider is tried."""
        errors = []
        for name in self.chain(task):
            p = self.providers[name]
            ok, why = self.ready(p)
            if not ok:
                continue
            t0 = time.time()
            try:
                text = self.call(p, system, user)
                if not text or not text.strip():
                    raise AIError("respons kosong")
                if validate:
                    try:
                        validate(text)
                    except Exception as vex:
                        raise AIError(f"jawaban tidak valid: {vex}") from None
                self._log(name, task, True, t0)
                return text, name
            except Exception as ex:
                msg = _short_err(ex)
                errors.append(f"{name}: {msg}")
                self._log(name, task, False, t0, msg)
                log.warning("AI %s gagal (%s), coba provider berikutnya", name, msg)
        if not errors:
            raise AIError("Tidak ada provider AI yang aktif dan siap. Atur di dashboard > AI.")
        raise AIError("Semua provider AI gagal: " + " | ".join(errors))

    def test(self, name):
        p = self.providers.get(name)
        if not p:
            raise AIError("provider tidak ada")
        ok, why = self.ready(dict(p, enabled=True))
        if not ok:
            return {"ok": False, "error": why}
        t0 = time.time()
        try:
            text = self.call(p, "Jawab hanya dengan JSON valid.", 'Balas persis: {"ok": true, "pesan": "halo"}')
            self._log(name, "test", True, t0)
            return {"ok": True, "ms": int((time.time() - t0) * 1000), "reply": text.strip()[:200]}
        except Exception as ex:
            self._log(name, "test", False, t0, _short_err(ex))
            return {"ok": False, "ms": int((time.time() - t0) * 1000), "error": _short_err(ex)}

    def _log(self, name, task, ok, t0, err=""):
        if self.store is not None:
            try:
                self.store.add_ai_call(name, task, ok, int((time.time() - t0) * 1000), err)
            except Exception:
                log.exception("gagal mencatat pemakaian AI")

    def call(self, p, system, user):
        t = p["type"]
        timeout = int(p.get("timeout") or self.timeout)
        if t == "anthropic":
            return self._anthropic(p, system, user, timeout)
        if t == "gemini":
            return self._gemini(p, system, user, timeout)
        if t == "claude_code":
            return self._claude_code(p, system, user, timeout)
        return self._openai_compat(p, system, user, timeout)

    @staticmethod
    def _raise(r):
        if r.status_code >= 400:
            try:
                j = r.json()
                msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")
                msg = msg or j.get("message") or r.text[:200]
            except Exception:
                msg = r.text[:200]
            raise AIError(f"HTTP {r.status_code}: {msg}")

    def _anthropic(self, p, system, user, timeout):
        r = requests.post(p["base_url"] + "/v1/messages", timeout=timeout, headers={
            "x-api-key": p["api_key"], "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": p["model"], "max_tokens": int(p.get("max_tokens") or 4000), "system": system,
                  "messages": [{"role": "user", "content": user}]})
        self._raise(r)
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")

    def _openai_compat(self, p, system, user, timeout):
        headers = {"Content-Type": "application/json"}
        if p.get("api_key"):
            headers["Authorization"] = f"Bearer {p['api_key']}"
        if p["type"] == "openrouter":
            headers.update({"HTTP-Referer": "https://github.com/mr-pilot", "X-Title": "MR Pilot"})
        body = {"model": p["model"], "messages": [{"role": "system", "content": system},
                                                   {"role": "user", "content": user}]}
        if p.get("temperature") is not None or p["type"] != "openai":
            body["temperature"] = float(p.get("temperature") if p.get("temperature") is not None else 0.1)
        r = requests.post(p["base_url"] + "/chat/completions", headers=headers, json=body, timeout=timeout)
        self._raise(r)
        msg = r.json()["choices"][0]["message"]
        return msg.get("content") or ""

    def _gemini(self, p, system, user, timeout):
        url = f"{p['base_url']}/models/{p['model']}:generateContent"
        r = requests.post(url, timeout=timeout, headers={"x-goog-api-key": p["api_key"], "Content-Type": "application/json"},
                          json={"systemInstruction": {"parts": [{"text": system}]},
                                "contents": [{"role": "user", "parts": [{"text": user}]}],
                                "generationConfig": {"temperature": 0.1}})
        self._raise(r)
        cands = r.json().get("candidates") or []
        if not cands:
            raise AIError("Gemini tidak mengembalikan kandidat (mungkin diblokir safety filter)")
        return "".join(part.get("text", "") for part in (cands[0].get("content") or {}).get("parts", []))

    @staticmethod
    def _claude_bin(p):
        cand = p.get("cli_path") or "claude"
        found = shutil.which(cand)
        if not found:
            for extra in ("~/.local/bin/claude", "~/.local/bin/claude.exe", "/usr/local/bin/claude"):
                path = os.path.expanduser(extra)
                if os.path.exists(path):
                    return path
        return found

    def _claude_code(self, p, system, user, timeout):
        """Headless Claude Code: `claude -p --output-format json`. Input via stdin (no command-line length limits),
        run in an empty temp folder with dontAsk so no tool can touch real files."""
        exe = self._claude_bin(p)
        if not exe:
            raise AIError(claude_missing_msg())
        env = dict(os.environ)
        token = p.get("api_key") or env.get("CLAUDE_CODE_OAUTH_TOKEN")
        cmd = [exe]
        if token and not token.startswith("sk-ant-api"):
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
            env.pop("ANTHROPIC_API_KEY", None)  # API key outranks the OAuth token; force subscription
        elif token:
            env["ANTHROPIC_API_KEY"] = token
            cmd.append("--bare")
        cmd += ["-p", "Kerjakan instruksi dan input dari stdin. Jawab hanya sesuai format yang diminta, tanpa memakai tool.",
                "--output-format", "json", "--permission-mode", "dontAsk"]
        if p.get("model"):
            cmd += ["--model", p["model"]]
        stdin = f"=== INSTRUKSI ===\n{system}\n\n=== INPUT ===\n{user}"
        with tempfile.TemporaryDirectory(prefix="mrp-claude-") as cwd:
            try:
                res = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8",
                                     timeout=timeout, cwd=cwd, env=env)
            except subprocess.TimeoutExpired:
                raise AIError(f"Claude Code timeout ({timeout}s)")
        out = (res.stdout or "").strip()
        try:
            data = json.loads(out.splitlines()[-1] if out else "{}")
        except ValueError:
            raise AIError(f"output tidak terbaca: {(out or res.stderr)[:200]}")
        if res.returncode != 0 or data.get("is_error"):
            raise AIError(str(data.get("result") or res.stderr or f"exit {res.returncode}")[:300])
        return data.get("result") or ""

    # -------------------------------------------------------------- models
    def list_models(self, name):
        p = self.providers.get(name)
        if not p:
            raise AIError("provider tidak ada")
        t, to = p["type"], 20
        if t == "claude_code":
            return ["sonnet", "opus", "haiku"]
        if t == "anthropic":
            r = requests.get(p["base_url"] + "/v1/models", timeout=to,
                             headers={"x-api-key": p.get("api_key", ""), "anthropic-version": "2023-06-01"})
            self._raise(r)
            return [m["id"] for m in r.json().get("data", [])]
        if t == "gemini":
            r = requests.get(p["base_url"] + "/models", timeout=to, headers={"x-goog-api-key": p.get("api_key", "")})
            self._raise(r)
            return [m["name"].split("/", 1)[-1] for m in r.json().get("models", [])
                    if "generateContent" in (m.get("supportedGenerationMethods") or [])]
        headers = {"Authorization": f"Bearer {p['api_key']}"} if p.get("api_key") else {}
        r = requests.get(p["base_url"] + "/models", timeout=to, headers=headers)
        if r.status_code == 404 and t == "local":  # plain Ollama without /v1
            r = requests.get(p["base_url"].rsplit("/v1", 1)[0] + "/api/tags", timeout=to)
            self._raise(r)
            return [m["name"] for m in r.json().get("models", [])]
        self._raise(r)
        return sorted(m["id"] for m in r.json().get("data", []))


def _validate_fields(name, p):
    """Coerce/validate dashboard input so a bad value can't break calls later."""
    out = dict(p)
    if "enabled" in out:
        v = out["enabled"]
        out["enabled"] = v if isinstance(v, bool) else str(v).lower() in ("true", "1", "yes", "on")
    for k in ("model", "label", "cli_path"):
        if k in out:
            out[k] = str(out[k] or "").strip()[:200]
    if "base_url" in out:
        u = str(out["base_url"] or "").strip().rstrip("/")
        if u and not re.match(r"^https?://[^\s/]+", u):
            raise ValueError(f"Base URL {name} harus diawali http:// atau https://")
        out["base_url"] = u
    if "timeout" in out and out["timeout"] not in (None, ""):
        try:
            out["timeout"] = max(10, min(int(out["timeout"]), 1800))
        except (TypeError, ValueError):
            raise ValueError(f"Timeout {name} harus angka (detik)") from None
    if "temperature" in out and out["temperature"] not in (None, ""):
        try:
            out["temperature"] = max(0.0, min(float(out["temperature"]), 2.0))
        except (TypeError, ValueError):
            raise ValueError(f"Temperature {name} harus angka 0-2") from None
    if "api_key" in out and out["api_key"] is not None:
        out["api_key"] = str(out["api_key"]).strip()
        if any(c.isspace() for c in out["api_key"]):
            raise ValueError(f"API key {name} mengandung spasi")
    return out


def _short_err(ex):
    return short_error(ex)
