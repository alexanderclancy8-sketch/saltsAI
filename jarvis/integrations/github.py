"""GitHub REST client for the Salts FSM source repository (read code, open fix PRs, deploy)."""

from __future__ import annotations

import base64
import io
import logging
import tarfile
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)
API = "https://api.github.com"


class GitHub:
    def __init__(self, token: str, repo: str, http: httpx.AsyncClient, default_branch: str = "main"):
        self.repo = repo
        self.default_branch = default_branch
        self.http = http
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        r = await self.http.request(method, f"{API}{path}", headers=self.headers, timeout=60, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub {method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json() if r.content else None

    async def check(self) -> str:
        data = await self._req("GET", f"/repos/{self.repo}")
        return f"GitHub repo {data['full_name']} reachable"

    # -- reading code -----------------------------------------------------------
    async def branch_sha(self, branch: str | None = None) -> str:
        data = await self._req("GET", f"/repos/{self.repo}/git/ref/heads/{branch or self.default_branch}")
        return data["object"]["sha"]

    async def download_tree(self, dest: Path, ref: str) -> Path:
        """Download the repository at ``ref`` as a tarball and extract it (read-only snapshot)."""
        r = await self.http.get(f"{API}/repos/{self.repo}/tarball/{ref}", headers=self.headers,
                                follow_redirects=True, timeout=120)
        r.raise_for_status()
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as tar:
            members = [m for m in tar.getmembers() if m.isfile() or m.isdir()]  # no links/devices
            tar.extractall(dest, members=members, filter="data")
        roots = [p for p in dest.iterdir() if p.is_dir()]
        return roots[0] if len(roots) == 1 else dest

    async def read_file(self, path: str, ref: str | None = None) -> str:
        data = await self._req("GET", f"/repos/{self.repo}/contents/{path.lstrip('/')}",
                               params={"ref": ref or self.default_branch})
        if isinstance(data, list):
            return "\n".join(f"{'dir ' if i['type'] == 'dir' else 'file'} {i['path']}" for i in data)
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    async def search_code(self, query: str) -> list[dict[str, str]]:
        data = await self._req("GET", "/search/code", params={"q": f"{query} repo:{self.repo}", "per_page": 20})
        return [{"path": i["path"], "url": i["html_url"]} for i in data.get("items", [])]

    # -- writing: branch + commit + PR ---------------------------------------------
    async def commit_files(self, branch: str, base_sha: str, files: dict[str, str | None], message: str) -> str:
        """Create ``branch`` from ``base_sha`` with ``files`` changed (None = delete). Returns commit sha."""
        base_commit = await self._req("GET", f"/repos/{self.repo}/git/commits/{base_sha}")
        tree = []
        for path, content in files.items():
            if content is None:
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            else:
                blob = await self._req("POST", f"/repos/{self.repo}/git/blobs",
                                       json={"content": content, "encoding": "utf-8"})
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        new_tree = await self._req("POST", f"/repos/{self.repo}/git/trees",
                                   json={"base_tree": base_commit["tree"]["sha"], "tree": tree})
        commit = await self._req("POST", f"/repos/{self.repo}/git/commits",
                                 json={"message": message, "tree": new_tree["sha"], "parents": [base_sha]})
        await self._req("POST", f"/repos/{self.repo}/git/refs", json={"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
        return commit["sha"]

    async def open_pr(self, branch: str, title: str, body: str) -> dict[str, Any]:
        pr = await self._req("POST", f"/repos/{self.repo}/pulls",
                             json={"title": title, "head": branch, "base": self.default_branch, "body": body})
        return {"number": pr["number"], "url": pr["html_url"], "head_sha": pr["head"]["sha"]}

    async def pr(self, number: int) -> dict[str, Any]:
        return await self._req("GET", f"/repos/{self.repo}/pulls/{number}")

    async def checks_summary(self, sha: str) -> dict[str, Any]:
        data = await self._req("GET", f"/repos/{self.repo}/commits/{sha}/check-runs", params={"per_page": 100})
        runs = data.get("check_runs", [])
        pending = [r["name"] for r in runs if r["status"] != "completed"]
        failed = [r["name"] for r in runs if r["status"] == "completed"
                  and r["conclusion"] not in ("success", "neutral", "skipped")]
        state = "none" if not runs else "pending" if pending else "failure" if failed else "success"
        return {"state": state, "total": len(runs), "pending": pending, "failed": failed}

    async def merge_pr(self, number: int, title: str) -> str:
        data = await self._req("PUT", f"/repos/{self.repo}/pulls/{number}/merge",
                               json={"merge_method": "squash", "commit_title": title})
        return data["sha"]

    async def dispatch_workflow(self, workflow: str, ref: str | None = None) -> None:
        await self._req("POST", f"/repos/{self.repo}/actions/workflows/{workflow}/dispatches",
                        json={"ref": ref or self.default_branch})

    async def latest_runs(self, head_sha: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"per_page": limit}
        if head_sha:
            params["head_sha"] = head_sha
        data = await self._req("GET", f"/repos/{self.repo}/actions/runs", params=params)
        return [{"id": r["id"], "name": r["name"], "status": r["status"], "conclusion": r["conclusion"],
                 "url": r["html_url"], "created_at": r["created_at"]} for r in data.get("workflow_runs", [])]

    async def run_jobs(self, run_id: int) -> list[dict[str, Any]]:
        """The jobs of one GitHub Actions run (read-only): id, name, status, conclusion and the first failed step."""
        data = await self._req("GET", f"/repos/{self.repo}/actions/runs/{int(run_id)}/jobs", params={"per_page": 100})
        jobs = []
        for j in data.get("jobs", []):
            failed = next((s["name"] for s in j.get("steps") or []
                           if s.get("status") == "completed" and s.get("conclusion") not in (
                               "success", "neutral", "skipped", None)), "")
            jobs.append({"id": j["id"], "name": j["name"], "status": j["status"], "conclusion": j["conclusion"],
                         "failed_step": failed})
        return jobs

    async def job_log(self, job_id: int, max_bytes: int = 4_000_000) -> str:
        """Plain-text log of one job (read-only). GitHub answers with a redirect to the log file; only the last
        `max_bytes` are kept, since the failure is at the end and full logs can be huge."""
        r = await self.http.get(f"{API}/repos/{self.repo}/actions/jobs/{int(job_id)}/logs", headers=self.headers,
                                follow_redirects=True, timeout=120)
        if r.status_code >= 400:
            raise RuntimeError(f"GitHub GET job log {job_id} -> {r.status_code}: {r.text[:200]}")
        return r.content[-max_bytes:].decode("utf-8", errors="replace")

    async def create_issue(self, title: str, body: str, labels: list[str] | None = None) -> dict[str, Any]:
        issue = await self._req("POST", f"/repos/{self.repo}/issues",
                                json={"title": title, "body": body, "labels": labels or []})
        return {"number": issue["number"], "url": issue["html_url"]}

    async def download_zip(self, ref: str) -> bytes:
        r = await self.http.get(f"{API}/repos/{self.repo}/zipball/{ref}", headers=self.headers,
                                follow_redirects=True, timeout=180)
        r.raise_for_status()
        return r.content
