"""Pull-request tooling for Jarvis's OWN repository, built on the existing ``GitHub`` client.

Safety by construction, not just by convention:
- Reads go through ``_get`` (GET only).
- The only writes this class can make are a PR comment, a PR merge, opening a PR, closing a PR / changing its
  base branch, and (for pr_resolve_conflicts on a host without git) merging main into a PR branch via the merges
  API or building a merge commit with blobs/trees/commits and moving that one branch ref forward, through
  ``_send``, which checks every request against ``_WRITE_ALLOWED`` (and the body of a PATCH, a merge or a new PR).
  There is no code path here that deletes a branch, edits repo settings, creates refs, moves main/master/the
  default branch or force-pushes; ``_send`` would refuse such a request even if some later change tried one. Every call is made
  against ``self.repo`` (Jarvis's own repository) - no method takes a repository name.
- ``merge`` refuses unless the PR is open, not a draft, targets the default branch, has no conflicts and its CI is
  green, and it pins the commit it checked (``sha``) so a late push to the branch can't slip through.
- Everything read from GitHub is redacted for secrets and labelled as untrusted data (see ``redact.py``).

Callers must ONLY reach ``comment``, ``merge``, ``create_pr``, ``close_pr`` and ``set_base`` through an
approval-gated tool (``brain/pr_tools.py``).
"""

from __future__ import annotations

import base64
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .github import GitHub
from .redact import UNTRUSTED_NOTICE, redact, truncate, untrusted

SAFE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,199}$")
PROTECTED_BRANCHES = frozenset({"main", "master"})

MAX_PRS = 30
MAX_FILE_CHARS = 40_000
PATCH_BUDGET = 30_000  # total diff characters in the overview
PATCH_PER_FILE = 4_000  # per file in the overview
PATCH_SINGLE_FILE = 25_000  # when one file is asked for
MAX_SEARCH_HITS = 40
MAX_SEARCH_FILE_BYTES = 400_000
COMMENT_MAX = 8_000
PR_TITLE_MAX = 256
PR_BODY_MAX = 20_000


class PRError(RuntimeError):
    """A PR request that was refused or couldn't be carried out (message is safe to show)."""


def check_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not SAFE_REF.match(ref) or ".." in ref or ref.endswith((".lock", "/", ".")) or "//" in ref:
        raise PRError(f"'{redact(ref)[:80]}' isn't a valid branch, tag or commit name.")
    return ref


def check_path(path: str) -> str:
    path = (path or "").strip().lstrip("/")
    parts = path.split("/") if path else []
    if "\x00" in path or "\\" in path or any(p in ("..", ".") for p in parts) or len(path) > 500:
        raise PRError("That isn't a valid repository path.")
    return path


_NUM = r"\d+"
# The complete list of writes this module may ever make: (method, path regex).
_WRITE_ALLOWED: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("POST", re.compile(rf"^/repos/[^/]+/[^/]+/issues/{_NUM}/comments$")),
    ("PUT", re.compile(rf"^/repos/[^/]+/[^/]+/pulls/{_NUM}/merge$")),
    ("POST", re.compile(r"^/repos/[^/]+/[^/]+/pulls$")),  # open a PR
    ("PATCH", re.compile(rf"^/repos/[^/]+/[^/]+/pulls/{_NUM}$")),  # close a PR / change its base - see _check_payload
    # pr_resolve_conflicts without a git binary: merge via the API, or build the merge commit from blobs/trees/commits
    # and move the PR branch (never main/master, never forced - see _check_payload and PRClient.update_branch).
    ("POST", re.compile(r"^/repos/[^/]+/[^/]+/merges$")),
    ("POST", re.compile(r"^/repos/[^/]+/[^/]+/git/(?:blobs|trees|commits)$")),
    ("PATCH", re.compile(r"^/repos/[^/]+/[^/]+/git/refs/heads/(?!(?:main|master)$)[A-Za-z0-9][A-Za-z0-9._/\-]*$")),
)
_CREATE_KEYS = frozenset({"title", "head", "base", "body"})
_REF_PREFIX = "/git/refs/heads/"


def _check_payload(method: str, path: str, payload: dict[str, Any], default_branch: str | None = None) -> None:
    """The path allow-list can't tell a harmless PATCH of a PR from a harmful one, so the body is checked too: a PR
    can only be closed or have its base branch changed, a new PR can never come from main, a branch ref can only be
    moved forward (``force`` false) and never main/master/the default branch, and a merge never lands in one."""
    protected = PROTECTED_BRANCHES | ({default_branch} if default_branch else set())
    if method == "PATCH" and path.startswith(_REF_PREFIX):
        branch = check_ref(path[len(_REF_PREFIX):])
        if branch in protected:
            raise PRError(f"Refused: Jarvis never moves '{branch}'.")
        if set(payload) != {"sha", "force"} or payload["force"] is not False:
            raise PRError("Refused: a branch can only be moved forward by Jarvis, never forced.")
    elif method == "POST" and path == "/merges":
        if set(payload) - {"base", "head", "commit_message"} or not {"base", "head"} <= set(payload):
            raise PRError("Refused: a branch merge needs exactly a base, head and message.")
        if payload["base"] in protected:
            raise PRError(f"Refused: Jarvis never merges into '{payload['base']}'.")
    elif method == "PATCH":
        keys = set(payload)
        if keys not in ({"state"}, {"base"}) or (keys == {"state"} and payload["state"] != "closed"):
            raise PRError("Refused: a pull request can only be closed or have its base branch changed by Jarvis.")
    elif method == "POST" and path == "/pulls":
        if set(payload) - _CREATE_KEYS or not {"title", "head", "base"} <= set(payload):
            raise PRError("Refused: a new pull request needs exactly a title, head, base and description.")
        if payload["head"] in PROTECTED_BRANCHES:
            raise PRError(f"Refused: a pull request can't come from '{payload['head']}'.")


def _conflict_state(pr: dict[str, Any]) -> str:
    if pr.get("mergeable_state") == "dirty" or pr.get("mergeable") is False:
        return "conflicts"
    if pr.get("mergeable") is True:
        return "mergeable"
    return "unknown"  # GitHub is still computing it


def _ci_state(runs: list[dict[str, Any]]) -> dict[str, Any]:
    pending = [r["name"] for r in runs if r.get("status") != "completed"]
    failed = [r["name"] for r in runs if r.get("status") == "completed"
              and r.get("conclusion") not in ("success", "neutral", "skipped")]
    state = "none" if not runs else "pending" if pending else "failure" if failed else "success"
    return {"state": state, "total": len(runs), "pending": pending, "failed": failed}


class PRClient:
    def __init__(self, gh: GitHub):
        self.gh = gh
        self.repo = gh.repo

    # ------------------------------------------------------------------ plumbing
    @property
    def _secrets(self) -> list[str]:
        return [self.gh.headers.get("Authorization", "").removeprefix("Bearer ").strip()]

    def _clean(self, text: str | None, limit: int = 2000) -> str:
        return untrusted(text, limit, self._secrets)

    async def _get(self, path: str, **params: Any) -> Any:
        await self.gh.resolve_default_branch()  # so every later gh.default_branch read is the repo's real main line
        return await self.gh._req("GET", f"/repos/{self.repo}{path}", params=params or None)  # noqa: SLF001

    async def _get_all(self, path: str, pages: int = 3, per_page: int = 100) -> list[Any]:
        out: list[Any] = []
        for page in range(1, pages + 1):
            chunk = await self._get(path, per_page=per_page, page=page)
            out.extend(chunk)
            if len(chunk) < per_page:
                break
        return out

    async def _send(self, method: str, path: str, payload: dict[str, Any]) -> Any:
        full = f"/repos/{self.repo}{path}"
        if not any(method == m and rx.match(full) for m, rx in _WRITE_ALLOWED):
            raise PRError(f"Refused: {method} {path} is not an allowed GitHub action for Jarvis.")
        await self.gh.resolve_default_branch()
        _check_payload(method, path, payload, self.gh.default_branch)
        return await self.gh._req(method, full, json=payload)  # noqa: SLF001

    # ------------------------------------------------------------------ (1) pr_list
    async def list_open_prs(self, limit: int = 20) -> dict[str, Any]:
        limit = max(1, min(limit, MAX_PRS))
        prs = await self._get("/pulls", state="open", per_page=limit, sort="updated", direction="desc")
        out = []
        for p in prs[:limit]:
            full = await self._get(f"/pulls/{p['number']}")
            try:
                runs = (await self._get(f"/commits/{full['head']['sha']}/check-runs", per_page=100)).get("check_runs", [])
                ci = _ci_state(runs)
            except Exception:  # noqa: BLE001 - one PR's CI lookup failing shouldn't hide the list
                ci = {"state": "unknown", "total": 0, "pending": [], "failed": []}
            out.append({
                "number": full["number"], "title": self._clean(full["title"], 200), "url": full["html_url"],
                "branch": self._clean(full["head"]["ref"], 120), "base": full["base"]["ref"],
                "author": self._clean((full.get("user") or {}).get("login", ""), 60), "draft": bool(full.get("draft")),
                "mergeable": full.get("mergeable"), "merge_status": _conflict_state(full),
                "ci": ci["state"], "ci_failed": ci["failed"], "ci_pending": ci["pending"],
                "files_changed": full.get("changed_files"), "head_sha": full["head"]["sha"],
                "created_at": full.get("created_at") or "", "updated_at": full.get("updated_at") or "",
            })
        return {"notice": UNTRUSTED_NOTICE, "count": len(out), "pull_requests": out}

    # ------------------------------------------------------------------ (2) pr_detail
    async def pr_detail(self, number: int, file: str | None = None) -> dict[str, Any]:
        pr = await self._get(f"/pulls/{number}")
        sha = pr["head"]["sha"]
        files = await self._get_all(f"/pulls/{number}/files", pages=3)
        listing = [{"file": f["filename"], "status": f["status"], "additions": f["additions"],
                    "deletions": f["deletions"]} for f in files]
        result: dict[str, Any] = {
            "notice": UNTRUSTED_NOTICE,
            "number": pr["number"], "title": self._clean(pr["title"], 300), "state": pr["state"],
            "draft": bool(pr.get("draft")), "url": pr["html_url"],
            "author": self._clean((pr.get("user") or {}).get("login", ""), 60),
            "branch": self._clean(pr["head"]["ref"], 120), "base": pr["base"]["ref"], "head_sha": sha,
            "merge_status": _conflict_state(pr), "mergeable_state": pr.get("mergeable_state"),
            "description": self._clean(pr.get("body"), 4000),
            "files_changed": pr.get("changed_files", len(files)), "files": listing,
        }
        if file:
            path = check_path(file)
            match = next((f for f in files if f["filename"] == path), None)
            if match is None:
                raise PRError(f"{path} isn't one of the files in PR #{number}. Files: "
                              + ", ".join(f["file"] for f in listing[:50]))
            patch, cut = truncate(self._clean(match.get("patch") or "(no text diff - binary or too large)",
                                              PATCH_SINGLE_FILE + 1000), PATCH_SINGLE_FILE)
            result["diff"] = {path: patch}
            result["diff_truncated"] = cut
        else:
            budget, diffs, cut_any = PATCH_BUDGET, {}, False
            for f in files:
                patch = f.get("patch")
                if not patch:
                    continue
                if budget <= 0:
                    cut_any = True
                    break
                text, cut = truncate(redact(patch, self._secrets), min(PATCH_PER_FILE, budget))
                diffs[f["filename"]] = text
                budget -= len(text)
                cut_any = cut_any or cut
            result["diff"] = diffs
            result["diff_truncated"] = cut_any or len(files) > len(diffs)
            if result["diff_truncated"]:
                result["diff_note"] = "Diff shortened. Call pr_detail again with `file` for one file's full diff."
        commits = await self._get(f"/pulls/{number}/commits", per_page=30)
        result["commits"] = [{"sha": c["sha"][:10], "message": self._clean(c["commit"]["message"].split("\n")[0], 200)}
                             for c in commits]
        review_comments = await self._get(f"/pulls/{number}/comments", per_page=50)
        reviews = await self._get(f"/pulls/{number}/reviews", per_page=50)
        discussion = await self._get(f"/issues/{number}/comments", per_page=50)
        result["review_comments"] = [{"author": self._clean((c.get("user") or {}).get("login", ""), 60),
                                      "file": c.get("path"), "line": c.get("line"),
                                      "text": self._clean(c.get("body"), 800)} for c in review_comments]
        result["reviews"] = [{"author": self._clean((r.get("user") or {}).get("login", ""), 60), "state": r["state"],
                              "text": self._clean(r.get("body"), 800)} for r in reviews]
        result["discussion"] = [{"author": self._clean((c.get("user") or {}).get("login", ""), 60),
                                 "text": self._clean(c.get("body"), 800)} for c in discussion]
        result["checks"] = await self._checks_detail(sha)
        return result

    async def _checks_detail(self, sha: str) -> dict[str, Any]:
        runs = (await self._get(f"/commits/{sha}/check-runs", per_page=100)).get("check_runs", [])
        summary = _ci_state(runs)
        summary["runs"] = [{"name": r["name"], "status": r["status"], "conclusion": r.get("conclusion"),
                            "url": r.get("html_url"),
                            "title": self._clean((r.get("output") or {}).get("title"), 200),
                            "summary": self._clean((r.get("output") or {}).get("summary"), 600)} for r in runs]
        return summary

    # ------------------------------------------------------------------ (3) repo_read
    async def read(self, path: str = "", ref: str | None = None, start_line: int = 1) -> Any:
        path = check_path(path)
        await self.gh.resolve_default_branch()
        ref = check_ref(ref) if ref else self.gh.default_branch
        data = await self._get(f"/contents/{quote(path, safe='/')}" if path else "/contents", ref=ref)
        if isinstance(data, list):
            return {"ref": ref, "path": path or "/", "entries": [
                {"type": "dir" if i["type"] == "dir" else "file", "path": i["path"], "size": i.get("size")}
                for i in data]}
        if data.get("type") != "file":
            return {"ref": ref, "path": path, "note": f"This is a {data.get('type')}, not a file."}
        if data.get("encoding") != "base64" or data.get("content") is None:
            return {"ref": ref, "path": path, "note": "File is too large to read through the API."}
        raw = base64.b64decode(data["content"])
        if b"\x00" in raw[:2000]:
            return {"ref": ref, "path": path, "note": "Binary file - not shown.", "size": len(raw)}
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        text, cut = truncate("\n".join(lines[start - 1:]), MAX_FILE_CHARS)
        return {"notice": UNTRUSTED_NOTICE, "ref": ref, "path": path, "start_line": start,
                "total_lines": len(lines), "truncated": cut,
                "content": redact(text, self._secrets)}

    # ------------------------------------------------------------------ (4) repo_search
    async def search(self, query: str, ref: str | None = None, glob: str = "*") -> dict[str, Any]:
        """Case-insensitive literal text search on any branch/ref. GitHub's own code-search API only covers the
        default branch, so this downloads that ref's tarball and searches it."""
        needle = (query or "").strip().lower()
        if len(needle) < 2:
            raise PRError("Give at least two characters to search for.")
        await self.gh.resolve_default_branch()
        ref = check_ref(ref) if ref else self.gh.default_branch
        import fnmatch

        hits: list[dict[str, Any]] = []
        with tempfile.TemporaryDirectory(prefix="jarvis-search-") as tmp:
            root = await self.gh.download_tree(Path(tmp), ref)
            for p in sorted(root.rglob("*")):
                if len(hits) >= MAX_SEARCH_HITS:
                    break
                if not p.is_file() or p.is_symlink() or ".git" in p.relative_to(root).parts:
                    continue
                rel = p.relative_to(root).as_posix()
                if not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(p.name, glob)):
                    continue
                try:
                    if p.stat().st_size > MAX_SEARCH_FILE_BYTES:
                        continue
                    blob = p.read_bytes()
                except OSError:
                    continue
                if b"\x00" in blob[:2000]:
                    continue
                for n, line in enumerate(blob.decode("utf-8", errors="replace").splitlines(), 1):
                    if needle in line.lower():
                        hits.append({"path": rel, "line": n, "text": redact(line.strip(), self._secrets)[:200]})
                        if len(hits) >= MAX_SEARCH_HITS:
                            break
        return {"notice": UNTRUSTED_NOTICE, "ref": ref, "query": query, "matches": hits,
                "truncated": len(hits) >= MAX_SEARCH_HITS}

    # ------------------------------------------------------------------ (5) pr_comment  [WRITE - approval only]
    async def comment(self, number: int, body: str) -> dict[str, Any]:
        text = redact((body or "").strip(), self._secrets)
        if not text:
            raise PRError("The comment is empty.")
        if len(text) > COMMENT_MAX:
            raise PRError(f"The comment is too long ({len(text)} characters; the limit is {COMMENT_MAX}).")
        data = await self._send("POST", f"/issues/{number}/comments", {"body": text})
        return {"commented": True, "pr": number, "url": data.get("html_url")}

    # ------------------------------------------------------------------ pr_create / pr_close / pr_set_base  [WRITE]
    async def _branch_exists(self, name: str) -> bool:
        try:
            await self._get(f"/branches/{quote(name, safe='/')}")
        except RuntimeError as e:
            if "-> 404" in str(e):
                return False
            raise
        return True

    async def _open_pr(self, number: int) -> dict[str, Any]:
        """The PR, if it is still open - otherwise a PRError saying why it can't be changed."""
        pr = await self._get(f"/pulls/{number}")
        if pr.get("merged"):
            raise PRError(f"PR #{number} is already merged.")
        if pr["state"] != "open":
            raise PRError(f"PR #{number} is already closed.")
        return pr

    async def create_pr(self, head: str, base: str, title: str, body: str = "") -> dict[str, Any]:
        """Open a PR from ``head`` into ``base``, both branches of this repository. ``head`` can never be main/master
        or the default branch (that would mean pushing to main); nothing is pushed or merged by opening a PR."""
        head, base = check_ref(head), check_ref(base)
        await self.gh.resolve_default_branch()
        if head in PROTECTED_BRANCHES or head == self.gh.default_branch:
            raise PRError(f"Refused: '{head}' is the main branch. A pull request has to come from a separate branch - "
                          "Jarvis never pushes to main.")
        if head == base:
            raise PRError("The head and base branch are the same, so there is nothing to open a pull request for.")
        title_text = redact((title or "").strip(), self._secrets)
        body_text = redact((body or "").strip(), self._secrets)
        if not title_text:
            raise PRError("The pull request needs a title.")
        if len(title_text) > PR_TITLE_MAX:
            raise PRError(f"The title is too long ({len(title_text)} characters; the limit is {PR_TITLE_MAX}).")
        if len(body_text) > PR_BODY_MAX:
            raise PRError(f"The description is too long ({len(body_text)} characters; the limit is {PR_BODY_MAX}).")
        for label, branch in (("head", head), ("base", base)):
            if not await self._branch_exists(branch):
                raise PRError(f"The {label} branch '{branch}' doesn't exist in {self.repo}.")
        data = await self._send("POST", "/pulls", {"title": title_text, "head": head, "base": base, "body": body_text})
        return {"created": True, "pr": data.get("number"), "url": data.get("html_url"), "head": head, "base": base}

    async def close_pr(self, number: int, comment: str | None = None) -> dict[str, Any]:
        """Close (never merge) an open PR, then post the optional comment. Branches are left alone."""
        text = redact((comment or "").strip(), self._secrets)
        if len(text) > COMMENT_MAX:
            raise PRError(f"The comment is too long ({len(text)} characters; the limit is {COMMENT_MAX}).")
        await self._open_pr(number)
        data = await self._send("PATCH", f"/pulls/{number}", {"state": "closed"})
        result: dict[str, Any] = {"closed": True, "pr": number, "url": data.get("html_url"), "commented": False}
        if text:
            try:
                await self.comment(number, text)
            except Exception as e:  # noqa: BLE001
                raise PRError(f"PR #{number} was closed, but posting the comment failed: {redact(str(e), self._secrets)}") from e
            result["commented"] = True
        return result

    async def set_base(self, number: int, base: str) -> dict[str, Any]:
        """Point an open PR at a different base branch of this repository."""
        base = check_ref(base)
        pr = await self._open_pr(number)
        old = pr["base"]["ref"]
        if old == base:
            raise PRError(f"PR #{number} already targets {base}.")
        if pr["head"]["ref"] == base:
            raise PRError(f"PR #{number} comes from {base}, so it can't also be its base.")
        if not await self._branch_exists(base):
            raise PRError(f"The branch '{base}' doesn't exist in {self.repo}.")
        data = await self._send("PATCH", f"/pulls/{number}", {"base": base})
        return {"updated": True, "pr": number, "previous_base": old, "base": (data.get("base") or {}).get("ref", base),
                "url": data.get("html_url")}

    # ------------------------------------------------------------------ pr_resolve_conflicts without git  [WRITE]
    async def _check_pr_branch(self, branch: str, base: str) -> tuple[str, str]:
        branch, base = check_ref(branch), check_ref(base)
        await self.gh.resolve_default_branch()
        if branch in PROTECTED_BRANCHES or branch in (self.gh.default_branch, base):
            raise PRError(f"Refused: '{branch}' is main or the base branch - Jarvis never pushes to it.")
        return branch, base

    async def update_branch(self, branch: str, base: str, message: str) -> dict[str, Any]:
        """Merge ``base`` into the PR branch ``branch`` with GitHub's merges API. ``result`` is ``merged`` (with the
        new ``sha``), ``up_to_date`` (nothing to merge) or ``conflict`` (GitHub answered 409: a real conflict)."""
        branch, base = await self._check_pr_branch(branch, base)
        try:
            data = await self._send("POST", "/merges", {"base": branch, "head": base, "commit_message": message})
        except RuntimeError as e:
            if "-> 409" in str(e):
                return {"result": "conflict"}
            raise
        if not data or not data.get("sha"):
            return {"result": "up_to_date"}
        return {"result": "merged", "sha": data["sha"]}

    async def _compare_files(self, basehead: str) -> list[dict[str, Any]]:
        """Files changed by ``A...B`` (since their merge base), renames split into a removal plus an addition."""
        files: list[dict[str, Any]] = []
        for page in range(1, 4):
            chunk = (await self._get(f"/compare/{basehead}", per_page=100, page=page)).get("files") or []
            for f in chunk:
                status = f.get("status")
                if status == "renamed" and f.get("previous_filename"):
                    files.append({"filename": f["previous_filename"], "status": "removed", "sha": None})
                    files.append({"filename": f["filename"], "status": "added", "sha": f.get("sha")})
                else:
                    files.append({"filename": f["filename"], "sha": f.get("sha"),
                                  "status": "removed" if status == "removed" else
                                  "added" if status in ("added", "copied") else "modified"})
            if len(chunk) < 100:
                return files
        raise PRError("Too many files changed (300+) to merge through the GitHub API; a person needs to resolve this.")

    async def merge_plan(self, branch: str, base: str) -> dict[str, Any]:
        """File-level three-way plan for merging ``base`` into ``branch``. ``conflicts`` are files changed on both
        sides to different results (a superset of what git would flag - there is no line-level merge here);
        ``unsupported`` are those where one side deleted the file; ``theirs_only`` are base-side changes that carry
        over unchanged."""
        branch, base = await self._check_pr_branch(branch, base)
        mine = {f["filename"]: f for f in await self._compare_files(f"{quote(base, safe='/')}...{quote(branch, safe='/')}")}
        theirs = {f["filename"]: f for f in await self._compare_files(f"{quote(branch, safe='/')}...{quote(base, safe='/')}")}
        conflicts, unsupported = [], []
        for name in sorted(set(mine) & set(theirs)):
            m, t = mine[name], theirs[name]
            m_gone, t_gone = m["status"] == "removed", t["status"] == "removed"
            if m_gone and t_gone:
                continue
            if not m_gone and not t_gone and m.get("sha") == t.get("sha"):
                continue  # both sides made the same change
            conflicts.append(name)
            if m_gone or t_gone:
                unsupported.append(name)
        return {"conflicts": conflicts, "unsupported": unsupported,
                "theirs_only": {n: f for n, f in sorted(theirs.items()) if n not in mine}}

    async def file_text(self, path: str, ref: str) -> str | None:
        """A file's text at ``ref`` through the contents API; None if it is missing, binary or too big."""
        try:
            data = await self._get(f"/contents/{quote(check_path(path), safe='/')}", ref=check_ref(ref))
        except RuntimeError as e:
            if "-> 404" in str(e):
                return None
            raise
        if not isinstance(data, dict) or data.get("encoding") != "base64" or data.get("content") is None:
            return None
        raw = base64.b64decode(data["content"])
        return None if b"\x00" in raw[:2000] else raw.decode("utf-8", errors="replace")

    async def commit_merge(self, branch: str, base: str, head_sha: str, resolutions: dict[str, str],
                           theirs_only: dict[str, dict[str, Any]], message: str) -> str:
        """Create a two-parent merge commit (``head_sha`` = the PR branch tip, plus the tip of ``base``) through the git
        data API and move the PR branch to it. The tree is the PR branch's tree plus the base-side-only changes plus the
        supplied ``resolutions``. The ref update is never forced, so a branch that moved meanwhile makes it fail."""
        branch, base = await self._check_pr_branch(branch, base)
        head_commit = await self._get(f"/git/commits/{head_sha}")
        base_commit = await self._get(f"/commits/{quote(base, safe='/')}")
        base_tree = await self._get(f"/git/trees/{base_commit['commit']['tree']['sha']}", recursive=1)
        if base_tree.get("truncated"):
            raise PRError(f"{base} has too many files to merge through the GitHub API; a person needs to do this.")
        modes = {e["path"]: e["mode"] for e in base_tree.get("tree", []) if e.get("type") == "blob"}
        entries: list[dict[str, Any]] = []
        for name, f in theirs_only.items():
            if f["status"] == "removed":
                entries.append({"path": name, "mode": "100644", "type": "blob", "sha": None})
            elif name in modes and f.get("sha"):
                entries.append({"path": name, "mode": modes[name], "type": "blob", "sha": f["sha"]})
            else:
                raise PRError(f"{name} changed on {base} in a way that can't be carried over through the GitHub API "
                              "(for example a submodule); a person needs to resolve this.")
        for name, text in sorted(resolutions.items()):
            blob = await self._send("POST", "/git/blobs", {"content": text, "encoding": "utf-8"})
            entries.append({"path": name, "mode": modes.get(name, "100644"), "type": "blob", "sha": blob["sha"]})
        tree = await self._send("POST", "/git/trees", {"base_tree": head_commit["tree"]["sha"], "tree": entries})
        commit = await self._send("POST", "/git/commits",
                                  {"message": message, "tree": tree["sha"], "parents": [head_sha, base_commit["sha"]]})
        await self._send("PATCH", f"{_REF_PREFIX}{branch}", {"sha": commit["sha"], "force": False})
        return commit["sha"]

    # ------------------------------------------------------------------ (7) run_tests (reads CI results)
    async def ci_results(self, ref: str) -> dict[str, Any]:
        ref = check_ref(ref)
        commit = await self._get(f"/commits/{quote(ref, safe='/')}")
        sha = commit["sha"]
        checks = await self._checks_detail(sha)
        workflows = await self.gh.latest_runs(head_sha=sha, limit=10)
        return {"notice": UNTRUSTED_NOTICE, "ref": ref, "sha": sha, "state": checks["state"],
                "failed": checks["failed"], "pending": checks["pending"], "checks": checks["runs"],
                "workflow_runs": [{**w, "name": self._clean(w["name"], 120)} for w in workflows]}

    # ------------------------------------------------------------------ (8) pr_merge  [WRITE - approval only]
    async def merge(self, number: int, expected_head_sha: str | None = None) -> dict[str, Any]:
        """Squash-merge ``number`` - but only if it is open, not a draft, aimed at the default branch, conflict-free
        and green. Raises ``PRError`` listing every reason otherwise."""
        pr = await self._get(f"/pulls/{number}")
        sha = pr["head"]["sha"]
        problems: list[str] = []
        if pr.get("merged"):
            problems.append("it is already merged")
        elif pr["state"] != "open":
            problems.append("it is closed")
        if pr.get("draft"):
            problems.append("it is still a draft")
        if pr["base"]["ref"] != self.gh.default_branch:
            problems.append(f"it targets {pr['base']['ref']}, not {self.gh.default_branch}")
        status = _conflict_state(pr)
        if status == "conflicts":
            problems.append("it has unresolved merge conflicts")
        elif status == "unknown":
            problems.append("GitHub hasn't finished working out whether it has conflicts - try again shortly")
        if expected_head_sha and not sha.startswith(expected_head_sha.strip()):
            problems.append(f"the branch has new commits since it was reviewed (now {sha[:10]})")
        runs = (await self._get(f"/commits/{sha}/check-runs", per_page=100)).get("check_runs", [])
        ci = _ci_state(runs)
        if ci["state"] == "failure":
            problems.append("CI is failing: " + ", ".join(ci["failed"]))
        elif ci["state"] == "pending":
            problems.append("CI is still running: " + ", ".join(ci["pending"]))
        elif ci["state"] != "success":
            problems.append("there are no passing CI results for the latest commit")
        if problems:
            raise PRError(f"Refused to merge PR #{number}: " + "; ".join(problems) + ".")
        title = f"{pr['title']} (#{number})"
        data = await self._send("PUT", f"/pulls/{number}/merge",
                                {"merge_method": "squash", "commit_title": title[:250], "sha": sha})
        return {"merged": True, "pr": number, "merge_sha": data.get("sha"), "head_sha": sha}
