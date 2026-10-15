"""Minimal GitLab REST v4 client (only what MR Pilot needs)."""
import urllib.parse
import warnings

import requests
import urllib3

from .util import retry_session


def _pid(pid):
    return urllib.parse.quote(str(pid), safe="")


class GitLab:
    def __init__(self, url, token, verify=True, timeout=60):
        self.base = url.rstrip("/") + "/api/v4"
        # GET/HEAD retried on network errors, 429 and 5xx. Writes (merge, notes) are never retried
        # automatically, so a slow response can't cause a double merge or duplicate comment.
        self.s = retry_session(total=3, backoff=1.0)
        self.s.headers["PRIVATE-TOKEN"] = token
        self.s.headers["User-Agent"] = "mr-pilot"
        self.s.verify = verify
        if not verify:  # self-signed GitLab: one notice at startup instead of a warning per request
            warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        self.timeout = timeout

    def _req(self, method, path, **kw):
        # verify passed per request: requests lets REQUESTS_CA_BUNDLE override a session-level
        # verify=False, which would silently re-enable checks the user turned off
        kw.setdefault("verify", self.s.verify)
        return self.s.request(method, self.base + path, timeout=self.timeout, **kw)

    def get(self, path, params=None):
        r = self._req("GET", path, params=params)
        r.raise_for_status()
        return r.json()

    def get_all(self, path, params=None, max_pages=20):
        params = dict(params or {})
        params.setdefault("per_page", 100)
        out, page = [], 1
        while page and page <= max_pages:
            params["page"] = page
            r = self._req("GET", path, params=params)
            r.raise_for_status()
            out += r.json()
            nxt = r.headers.get("X-Next-Page")
            page = int(nxt) if nxt else None
        return out

    # --- users / MRs --------------------------------------------------------
    def me(self):
        return self.get("/user")

    def list_review_mrs(self, username, watch=("reviewer", "assignee")):
        """Open MRs where `username` is reviewer and/or assignee (per `watch`), each listed once."""
        base = {"scope": "all", "state": "opened"}
        out, seen = [], set()
        for role, param in (("reviewer", "reviewer_username"), ("assignee", "assignee_username")):
            if role not in watch:
                continue
            for m in self.get_all("/merge_requests", {**base, param: username}):
                if m["id"] not in seen:
                    seen.add(m["id"])
                    out.append(m)
        return out

    def project_mrs(self, pid, target_branch=None, state="opened", max_pages=3):
        params = {"state": state, "order_by": "updated_at", "sort": "desc"}
        if target_branch:
            params["target_branch"] = target_branch
        return self.get_all(f"/projects/{_pid(pid)}/merge_requests", params, max_pages=max_pages)

    def get_mr(self, pid, iid):
        return self.get(f"/projects/{_pid(pid)}/merge_requests/{iid}")

    def get_diffs(self, pid, iid):
        path = f"/projects/{_pid(pid)}/merge_requests/{iid}"
        try:
            return self.get_all(path + "/diffs")
        except requests.HTTPError as e:  # GitLab < 15.7
            if e.response is not None and e.response.status_code == 404:
                return self.get(path + "/changes").get("changes", [])
            raise

    def get_file_raw(self, pid, path, ref):
        """File content at `ref` (commit sha / branch). Raises for missing files."""
        enc = urllib.parse.quote(path, safe="")
        r = self._req("GET", f"/projects/{_pid(pid)}/repository/files/{enc}/raw", params={"ref": ref})
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        return r.text

    def mr_pipelines(self, pid, iid):
        """Pipelines of an MR, newest first."""
        return self.get(f"/projects/{_pid(pid)}/merge_requests/{iid}/pipelines", {"per_page": 5})

    def failed_jobs(self, pid, pipeline_id):
        return self.get(f"/projects/{_pid(pid)}/pipelines/{pipeline_id}/jobs",
                        {"scope[]": "failed", "per_page": 20})

    def job_trace(self, pid, job_id, tail=20000):
        """Last `tail` characters of a job log."""
        r = self._req("GET", f"/projects/{_pid(pid)}/jobs/{job_id}/trace")
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        return r.text[-tail:]

    def get_commits(self, pid, iid):
        """Newest first (GitLab order), all pages."""
        return self.get_all(f"/projects/{_pid(pid)}/merge_requests/{iid}/commits")

    def get_notes(self, pid, iid):
        return self.get(f"/projects/{_pid(pid)}/merge_requests/{iid}/notes",
                        {"sort": "desc", "order_by": "updated_at", "per_page": 100})

    def get_commit_diff(self, pid, sha):
        return self.get_all(f"/projects/{_pid(pid)}/repository/commits/{sha}/diff")

    # --- read-only: security reports + delivery (DORA) ---------------------
    def get_project(self, pid):
        return self.get(f"/projects/{_pid(pid)}")

    def pipelines(self, pid, ref=None, status=None, per_page=10, updated_after=None):
        """Newest first."""
        params = {"per_page": per_page, "order_by": "id", "sort": "desc"}
        if ref:
            params["ref"] = ref
        if status:
            params["status"] = status
        if updated_after:
            params["updated_after"] = updated_after
        return self.get(f"/projects/{_pid(pid)}/pipelines", params)

    def pipeline_jobs(self, pid, pipeline_id):
        return self.get_all(f"/projects/{_pid(pid)}/pipelines/{pipeline_id}/jobs", {"include_retried": "false"},
                            max_pages=5)

    def job_artifact(self, pid, job_id, path, max_bytes=30_000_000):
        """One file from the job's artifacts archive; None if it isn't there (404)."""
        enc = "/".join(urllib.parse.quote(p, safe="") for p in path.split("/"))
        r = self._req("GET", f"/projects/{_pid(pid)}/jobs/{job_id}/artifacts/{enc}", stream=True)
        if r.status_code == 404:
            r.close()
            return None
        r.raise_for_status()
        body = b""
        for chunk in r.iter_content(65536):
            body += chunk
            if len(body) > max_bytes:
                r.close()
                raise ValueError(f"artifact {path} terlalu besar (> {max_bytes // 1_000_000} MB)")
        return body

    def environments(self, pid):
        return self.get_all(f"/projects/{_pid(pid)}/environments", max_pages=3)

    def deployments(self, pid, environment, since_iso, max_pages=10):
        """Successful deployments to `environment` finished after `since_iso`, oldest first."""
        return self.get_all(f"/projects/{_pid(pid)}/deployments",
                            {"environment": environment, "status": "success", "order_by": "finished_at",
                             "sort": "asc", "finished_after": since_iso}, max_pages=max_pages)

    def compare(self, pid, frm, to):
        return self.get(f"/projects/{_pid(pid)}/repository/compare", {"from": frm, "to": to, "straight": "false"})

    def merged_mrs(self, pid, target_branch, since_iso, max_pages=10):
        return self.get_all(f"/projects/{_pid(pid)}/merge_requests",
                            {"state": "merged", "target_branch": target_branch, "updated_after": since_iso,
                             "order_by": "updated_at", "sort": "asc"}, max_pages=max_pages)

    def issues(self, pid, since_iso, **filters):
        return self.get_all(f"/projects/{_pid(pid)}/issues", {"created_after": since_iso, "scope": "all",
                                                             **filters}, max_pages=5)

    # --- code-standard reporting -------------------------------------------
    def commit_comment(self, pid, sha, note, path=None, line=None):
        payload = {"note": note}
        if path and line:
            payload.update(path=path, line=int(line), line_type="new")
        r = self._req("POST", f"/projects/{_pid(pid)}/repository/commits/{sha}/comments", json=payload)
        r.raise_for_status()
        return r.json()

    def mr_discussion(self, pid, iid, body, position=None):
        payload = {"body": body}
        if position:
            payload["position"] = position
        r = self._req("POST", f"/projects/{_pid(pid)}/merge_requests/{iid}/discussions", json=payload)
        r.raise_for_status()
        return r.json()

    def list_discussions(self, pid, iid):
        return self.get_all(f"/projects/{_pid(pid)}/merge_requests/{iid}/discussions", max_pages=10)

    def get_discussion(self, pid, iid, did):
        return self.get(f"/projects/{_pid(pid)}/merge_requests/{iid}/discussions/{did}")

    def reply_discussion(self, pid, iid, did, body):
        r = self._req("POST", f"/projects/{_pid(pid)}/merge_requests/{iid}/discussions/{did}/notes", json={"body": body})
        r.raise_for_status()
        return r.json()

    def resolve_discussion(self, pid, iid, did, resolved=True):
        r = self._req("PUT", f"/projects/{_pid(pid)}/merge_requests/{iid}/discussions/{did}",
                      json={"resolved": bool(resolved)})
        r.raise_for_status()
        return r.json()

    def edit_note(self, pid, iid, note_id, body):
        r = self._req("PUT", f"/projects/{_pid(pid)}/merge_requests/{iid}/notes/{note_id}", json={"body": body})
        r.raise_for_status()
        return r.json()

    def commit_status(self, pid, sha, state, name, description="", target_url=None, ref=None):
        payload = {"state": state, "name": name, "description": description[:250]}
        if target_url:
            payload["target_url"] = target_url
        if ref:
            payload["ref"] = ref
        r = self._req("POST", f"/projects/{_pid(pid)}/statuses/{sha}", json=payload)
        r.raise_for_status()
        return r.json()

    # --- actions ------------------------------------------------------------
    def add_note(self, pid, iid, body):
        r = self._req("POST", f"/projects/{_pid(pid)}/merge_requests/{iid}/notes", json={"body": body})
        r.raise_for_status()
        return r.json()

    def approve(self, pid, iid, sha=None):
        payload = {"sha": sha} if sha else {}
        return self._req("POST", f"/projects/{_pid(pid)}/merge_requests/{iid}/approve", json=payload)

    def set_remove_source_branch(self, pid, iid, value):
        r = self._req("PUT", f"/projects/{_pid(pid)}/merge_requests/{iid}", json={"remove_source_branch": bool(value)})
        r.raise_for_status()
        return r

    def merge(self, pid, iid, sha=None, remove_source_branch=False, squash=False):
        payload = {"should_remove_source_branch": remove_source_branch, "squash": squash}
        if sha:
            payload["sha"] = sha
        return self._req("PUT", f"/projects/{_pid(pid)}/merge_requests/{iid}/merge", json=payload)
