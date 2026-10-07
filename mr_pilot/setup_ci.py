"""`setup-ci`: wire up CI/CD for this repo automatically.

1. Detect where the repo is hosted (GitHub or GitLab) from `git remote get-url origin`.
2. Create a dedicated SSH deploy key, install it on the server (asks the server password once).
3. Prepare the server: Docker (installed if missing), deploy folder, compose file, config + secrets.
4. Store DEPLOY_* secrets in GitHub Actions secrets / GitLab CI/CD variables via API.
After that every push to the default branch: test -> build image -> deploy to the server."""
import base64
import os
import re
import shlex
import subprocess
import sys
import urllib.parse

import requests

from .setup_wizard import Asker, h, read_env, say, write_env

SECRET_NAMES = ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_PORT", "DEPLOY_PATH", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS")


def parse_remote(url):
    """-> (platform, host, project_path) from https or ssh git URLs."""
    url = url.strip()
    m = re.match(r"^(?:ssh://)?git@([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", url)
    if not m:
        m = re.match(r"^https?://(?:[^@/]+@)?([^/]+)/(.+?)(?:\.git)?/?$", url)
    if not m:
        raise ValueError(f"Remote git tidak dikenali: {url}")
    host, path = m.group(1), m.group(2)
    platform = "github" if host.lower() in ("github.com", "www.github.com") else "gitlab"
    return platform, host, path


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


# ------------------------------------------------------------- platform APIs
def github_set_secrets(repo, token, secrets_):
    from nacl import encoding, public  # PyNaCl, in requirements.txt
    api = f"https://api.github.com/repos/{repo}/actions/secrets"
    hd = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28"}
    r = requests.get(api + "/public-key", headers=hd, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"GitHub {r.status_code}: {r.json().get('message', r.text[:200])}. "
                           "Token butuh akses repo (Secrets: read & write).")
    key = r.json()
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    for name, value in secrets_.items():
        enc = base64.b64encode(box.encrypt(str(value).encode())).decode()
        r = requests.put(f"{api}/{name}", headers=hd, timeout=20, json={"encrypted_value": enc, "key_id": key["key_id"]})
        if r.status_code not in (201, 204):
            raise RuntimeError(f"Gagal set secret {name}: HTTP {r.status_code} {r.text[:200]}")
        say(f"secret {name}", "ok")


def gitlab_set_variables(base_url, project, token, variables, verify=True):
    api = f"{base_url.rstrip('/')}/api/v4/projects/{urllib.parse.quote(project, safe='')}/variables"
    hd = {"PRIVATE-TOKEN": token}
    for name, value in variables.items():
        payload = {"key": name, "value": str(value), "protected": False,
                   "variable_type": "file" if name in ("DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS") else "env_var",
                   "masked": False, "raw": True}
        r = requests.put(f"{api}/{name}", headers=hd, data=payload, timeout=20, verify=verify)
        if r.status_code == 404:
            r = requests.post(api, headers=hd, data=payload, timeout=20, verify=verify)
        if r.status_code not in (200, 201):
            msg = r.json().get("message", r.text[:200]) if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]
            raise RuntimeError(f"Gagal set variable {name}: HTTP {r.status_code} {msg}. Token butuh role Maintainer.")
        say(f"variable {name}", "ok")


# ------------------------------------------------------------------ ssh
class Server:
    def __init__(self, host, user, port, key, known_hosts):
        self.host, self.user, self.port, self.key, self.kh = host, user, str(port), key, known_hosts

    def opts(self, with_key=True):
        o = ["-o", f"UserKnownHostsFile={self.kh}", "-o", "StrictHostKeyChecking=accept-new", "-p", self.port]
        if with_key:
            o = ["-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes"] + o
        return o

    def ssh(self, command, with_key=True, input_=None, interactive=False):
        cmd = ["ssh"] + self.opts(with_key) + [f"{self.user}@{self.host}", command]
        if interactive:
            return subprocess.run(cmd, text=True, input=input_)
        return run(cmd, input=input_)

    def scp(self, src, dst):
        o = [x if x != "-p" else "-P" for x in self.opts()]
        return run(["scp", "-r"] + o + [src, f"{self.user}@{self.host}:{dst}"])


def run_setup_ci(src_dir, data_dir, interactive=True):
    env_path = os.path.join(data_dir, ".env")
    env = read_env(env_path)
    a = Asker(env, interactive)
    deploy_dir = os.path.join(data_dir, "deploy")
    os.makedirs(deploy_dir, exist_ok=True)

    # 1. repo ---------------------------------------------------------------
    h("1. Repository")
    r = run(["git", "-C", src_dir, "remote", "get-url", "origin"])
    if r.returncode != 0:
        say("Folder ini belum punya remote git `origin`. Push dulu ke GitHub/GitLab: "
            "git remote add origin <url> && git push -u origin main", "bad")
        return 1
    platform, host, project = parse_remote(r.stdout)
    say(f"{platform}: {host}/{project}", "ok")

    # 2. server ----------------------------------------------------------------
    h("2. Server tujuan deploy")
    srv_host = a.ask("DEPLOY_HOST", "Host/IP server")
    srv_user = a.ask("DEPLOY_USER", "User SSH", "root")
    srv_port = a.ask("DEPLOY_PORT", "Port SSH", "22")
    srv_path = a.ask("DEPLOY_PATH", "Folder di server", "/opt/mr-pilot")
    key = os.path.join(deploy_dir, "id_ed25519")
    kh = os.path.join(deploy_dir, "known_hosts")
    if not os.path.exists(key):
        r = run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"mr-pilot-deploy@{project}", "-f", key])
        if r.returncode:
            say("ssh-keygen gagal: " + r.stderr, "bad")
            return 1
        say("Deploy key dibuat", "ok")
    srv = Server(srv_host, srv_user, srv_port, key, kh)

    if srv.ssh("echo ok").returncode != 0:
        print(f"  Memasang deploy key ke {srv_user}@{srv_host}. Masukkan password SSH server bila diminta.")
        with open(key + ".pub", encoding="utf-8") as f:
            pub = f.read().strip()
        cmd = ("umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && "
               f"grep -qxF {shlex.quote(pub)} ~/.ssh/authorized_keys || echo {shlex.quote(pub)} >> ~/.ssh/authorized_keys")
        if srv.ssh(cmd, with_key=False, interactive=True).returncode != 0 or srv.ssh("echo ok").returncode != 0:
            say("Tidak bisa login ke server dengan deploy key. Cek host/user/port/password.", "bad")
            return 1
    say("SSH dengan deploy key berhasil", "ok")

    # 3. prepare server -----------------------------------------------------
    h("3. Siapkan server")
    sudo = "" if srv_user == "root" else "sudo "
    r = srv.ssh("command -v docker >/dev/null && docker compose version >/dev/null 2>&1 && echo HAVE")
    if "HAVE" not in r.stdout:
        say("Docker belum ada di server, menginstal (get.docker.com)…", "warn")
        r = srv.ssh(f"curl -fsSL https://get.docker.com | {sudo}sh && {sudo}usermod -aG docker {shlex.quote(srv_user)} || true",
                    interactive=True)
        if r.returncode:
            say("Instal Docker gagal. Instal manual lalu jalankan setup-ci lagi.", "bad")
            return 1
    say("Docker tersedia", "ok")
    qp = shlex.quote(srv_path)
    srv.ssh(f"{sudo}mkdir -p {qp}/data/home && {sudo}chown -R $(id -u):$(id -g) {qp}")
    for f in ("docker-compose.yml", "deploy/remote-deploy.sh"):
        res = srv.scp(os.path.join(src_dir, f), f"{srv_path}/{os.path.basename(f)}")
        if res.returncode:
            say(f"Upload {f} gagal: {res.stderr.strip()}", "bad")
            return 1
    has_cfg = "YES" in srv.ssh(f"test -f {qp}/data/config.yaml && test -f {qp}/data/.env && echo YES").stdout
    if not has_cfg or a.yes("Server sudah punya config. Timpa dengan config lokal (data/config.yaml, .env, standards)?", False):
        if os.path.exists(os.path.join(data_dir, "config.yaml")) and os.path.exists(env_path):
            for f in ("config.yaml", ".env", "standards", "ai_overrides.json"):
                p = os.path.join(data_dir, f)
                if os.path.exists(p):
                    srv.scp(p, f"{srv_path}/data/")
            srv.ssh(f"chmod 600 {qp}/data/.env")
            say("Config & secrets disalin ke server", "ok")
        else:
            say("Config lokal belum ada. Jalankan `setup` dulu, atau jalankan setup di server.", "warn")
    srv.ssh(f"cd {qp} && touch .env && (grep -q '^MRP_UID=' .env || echo \"MRP_UID=$(id -u)\" >> .env) && "
            f"(grep -q '^MRP_GID=' .env || echo \"MRP_GID=$(id -g)\" >> .env)")

    # 4. CI secrets -----------------------------------------------------------
    h(f"4. Secrets CI/CD ({platform})")
    with open(key, encoding="utf-8") as f:
        priv = f.read()
    with open(kh, encoding="utf-8") as f:  # only contains this server (written by accept-new above)
        known = f.read()
    values = {"DEPLOY_HOST": srv_host, "DEPLOY_USER": srv_user, "DEPLOY_PORT": srv_port, "DEPLOY_PATH": srv_path,
              "DEPLOY_SSH_KEY": priv, "DEPLOY_KNOWN_HOSTS": known.strip() + "\n"}
    try:
        if platform == "github":
            print(f"  {C_dim('Token GitHub: fine-grained PAT untuk repo ini dengan izin Secrets (read/write) & Actions.')}")
            tok = a.ask("GITHUB_TOKEN", "GitHub token", secret=True)
            github_set_secrets(project, tok, values)
        else:
            base = f"https://{host}"
            same = env.get("GITLAB_URL", "").rstrip("/").endswith(host)
            tok = env.get("GITLAB_TOKEN") if same else a.ask("GITLAB_CI_TOKEN", f"Token GitLab {host} (scope api, role Maintainer)", secret=True)
            verify = env.get("GITLAB_VERIFY_SSL", "true") != "false"
            gitlab_set_variables(base, project, tok, values, verify)
    except Exception as ex:
        say(str(ex), "bad")
        say("Isi manual secrets berikut di pengaturan CI/CD: " + ", ".join(SECRET_NAMES)
            + f". Private key ada di {key}", "warn")
        return 1
    write_env(env_path, {k: values[k] for k in ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_PORT", "DEPLOY_PATH")})

    print()
    say("CI/CD siap. Setiap push ke branch utama: test → build image → deploy ke server.", "ok")
    wf = ".github/workflows/mr-pilot.yml" if platform == "github" else ".gitlab-ci.yml"
    print(f"  Commit & push agar pipeline jalan:\n    git add . && git commit -m \"ci: deploy MR Pilot\" && git push\n"
          f"  File pipeline: {wf}")
    return 0


def C_dim(s):
    return f"\033[2m{s}\033[0m" if sys.stdout.isatty() else s
