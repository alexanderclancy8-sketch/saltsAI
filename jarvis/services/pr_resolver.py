"""Bring a pull request up to date with main: merge main into the PR's own branch, test it, push only if green.

Only ever reached through the approval-gated ``pr_resolve_conflicts`` tool (brain/pr_tools.py), i.e. after the
owner has clicked Approve. Guard rails, all enforced here:
- The PR must be open, from a branch in THIS repository (no forks) and its branch must not be main/master/the
  default branch. The only ref ever pushed to is that PR branch, with a plain (never forced) push, so a moved
  branch makes the push fail instead of being overwritten. Merge only - a rebase would need a force-push.
- Conflicts git can't settle are NOT guessed at. The first pass reports the conflicted files (with their conflict
  hunks) and aborts. To resolve, the complete resolved text of every conflicted file is passed back in
  ``resolutions``; only files that really conflict may be supplied, leftover conflict markers are refused, and
  CI/workflow files are never edited by this tool.
- The merged tree is exported WITHOUT its .git folder and its tests run in a scratch directory with a scrubbed
  environment (no Jarvis settings, tokens or API keys) and a hard time limit. If they don't pass - or can't run
  at all - nothing is pushed and what failed is reported.
- The GitHub token is only ever given to git for the clone/fetch/push commands (as an HTTP header on the command
  line, never written to disk or into a remote URL) and all output is redacted before it is returned.

Note: this is a scratch directory and a clean environment, not an operating-system sandbox. That is why the tool
needs the owner's approval on every run.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import os
import re
import shutil
import signal
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Sequence

from ..integrations.github_pr import PROTECTED_BRANCHES, PRClient, PRError, check_path, check_ref
from ..integrations.redact import redact, truncate

log = logging.getLogger(__name__)

GIT_TIMEOUT = 180
TEST_TIMEOUT = 900
OUTPUT_TAIL = 4000
HUNK_LIMIT = 3000
MAX_RESOLUTION_CHARS = 400_000
BLOCKED_PREFIXES = (".github/",)
DEFAULT_TEST_COMMANDS: tuple[tuple[str, ...], ...] = ((sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"),)
_MARKER = re.compile(r"^(<{7}|>{7})(?: |$)", re.M)  # a lone ======= line can be legitimate text


async def _run(cmd: Sequence[str], cwd: Path, env: dict[str, str], timeout: int) -> tuple[int, bytes]:
    """Run a command (no shell), stdout+stderr combined, killed with its whole process group on timeout."""
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        await proc.wait()
        return 124, f"Timed out after {timeout}s and was stopped.".encode()
    return proc.returncode if proc.returncode is not None else 1, out


def _clean_env(home: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "LC_ALL": "C.UTF-8",
           "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra or {})
    return env


def _git_env(home: Path) -> dict[str, str]:
    return _clean_env(home, {"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})


def _report(status: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "summary": summary, **extra}


async def resolve_pr(pc: PRClient, number: int, resolutions: dict[str, str] | None = None, *,
                     remote_url: str | None = None, test_commands: Sequence[Sequence[str]] | None = None,
                     test_timeout: int = TEST_TIMEOUT) -> dict[str, Any]:
    """Merge the base branch into PR ``number``'s branch, run the tests and push if they pass. Never raises for an
    expected outcome - the ``status`` is one of: refused, up_to_date, conflicts, tests_failed, pushed, error."""
    gh = pc.gh
    resolutions = resolutions or {}
    pr = await pc._get(f"/pulls/{number}")  # noqa: SLF001
    head, base = pr["head"], pr["base"]["ref"]
    head_ref = head["ref"]

    if pr["state"] != "open":
        return _report("refused", f"PR #{number} isn't open.")
    if not head.get("repo") or head["repo"]["full_name"].lower() != gh.repo.lower():
        return _report("refused", f"PR #{number} comes from a fork; Jarvis only updates branches in {gh.repo}.")
    try:
        check_ref(head_ref)
        check_ref(base)
    except PRError as e:
        return _report("refused", str(e))
    if head_ref in PROTECTED_BRANCHES or head_ref in (gh.default_branch, base):
        return _report("refused", f"PR #{number}'s branch is '{head_ref}' - Jarvis never pushes to main or the base branch.")

    if shutil.which("git") is None:
        return _report("error", "git isn't installed on this machine, so the branch can't be updated.")

    token = gh.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    secrets = [token, auth]
    url = remote_url or f"https://github.com/{gh.repo}.git"
    cfg = ["-c", f"http.extraheader=Authorization: Basic {auth}"]  # per command only; never stored in .git/config
    ident = ["-c", "user.name=Jarvis", "-c", "user.email=jarvis@users.noreply.github.com", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=" + os.devnull]

    def clean(data: bytes | str, limit: int = OUTPUT_TAIL) -> str:
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        return truncate(redact(text, secrets), limit)[0]

    with tempfile.TemporaryDirectory(prefix="jarvis-pr-") as tmp_s:
        tmp = Path(tmp_s)
        (tmp / "home").mkdir()
        genv = _git_env(tmp / "home")
        work = tmp / "repo"

        async def git(*args: str, auth_needed: bool = False, allow_fail: bool = False) -> tuple[int, str]:
            cmd = ["git", *(cfg if auth_needed else []), *ident, *args]
            rc, out = await _run(cmd, tmp if not work.exists() else work, genv, GIT_TIMEOUT)
            if rc != 0 and not allow_fail:
                raise PRError(f"git {args[0]} failed: {clean(out, 800)}")
            return rc, clean(out)

        try:
            await git("clone", "--no-tags", "--single-branch", "--branch", head_ref, "--", url, "repo", auth_needed=True)
            await git("fetch", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", auth_needed=True)
            _, local_sha = await git("rev-parse", "HEAD")
            if local_sha.strip() != head["sha"]:
                return _report("error", f"PR #{number}'s branch moved while starting ({head['sha'][:10]} -> "
                                        f"{local_sha.strip()[:10]}). Nothing was changed; try again.")
            rc, merge_out = await git("merge", "--no-ff", "--no-edit", "-m", f"Merge {base} into {head_ref}",
                                      f"origin/{base}", allow_fail=True)
            _, after_sha = await git("rev-parse", "HEAD")
            resolved: list[str] = []
            if rc == 0 and after_sha.strip() == local_sha.strip():
                return _report("up_to_date", f"PR #{number} already contains everything on {base}; nothing to do.")
            if rc != 0:
                _, names = await git("diff", "--name-only", "--diff-filter=U")
                conflicted = [n for n in names.splitlines() if n.strip()]
                if not conflicted:
                    await git("merge", "--abort", allow_fail=True)
                    return _report("error", f"Merging {base} failed for a reason other than conflicts: {clean(merge_out, 800)}")
                blocked = [n for n in conflicted if n.startswith(BLOCKED_PREFIXES)]
                extra = sorted(set(resolutions) - set(conflicted))
                unresolved = [n for n in conflicted if n not in resolutions]
                if blocked or extra or unresolved:
                    hunks = {}
                    for n in unresolved[:10]:
                        try:
                            text = (work / n).read_text(encoding="utf-8", errors="replace")
                        except OSError:
                            text = "(couldn't read)"
                        hunks[n] = clean(text, HUNK_LIMIT)
                    await git("merge", "--abort", allow_fail=True)
                    if blocked:
                        return _report("conflicts", f"PR #{number} conflicts with {base} in CI/workflow files "
                                       f"({', '.join(blocked)}), which Jarvis never edits. A person needs to resolve this.",
                                       conflicted_files=conflicted)
                    if extra:
                        return _report("refused", "Resolutions were supplied for files that don't conflict: "
                                       + ", ".join(extra) + ". Nothing was changed.", conflicted_files=conflicted)
                    return _report("conflicts", f"PR #{number} conflicts with {base} in {len(conflicted)} file(s): "
                                   f"{', '.join(conflicted)}. Nothing was pushed. Send the full resolved text of each file "
                                   "via `resolutions` to try again.", conflicted_files=conflicted, conflict_hunks=hunks)
                for name, text in resolutions.items():
                    path = check_path(name)
                    if len(text) > MAX_RESOLUTION_CHARS:
                        raise PRError(f"The resolution for {path} is too large.")
                    if _MARKER.search(text):
                        await git("merge", "--abort", allow_fail=True)
                        return _report("refused", f"The resolution for {path} still contains conflict markers. "
                                                  "Nothing was changed.")
                for name, text in resolutions.items():
                    path = check_path(name)
                    target = (work / path).resolve()
                    if work.resolve() not in target.parents:
                        raise PRError(f"{path} is outside the repository.")
                    target.write_text(text, encoding="utf-8")
                    await git("add", "--", path)
                await git("commit", "--no-edit")
                resolved = sorted(resolutions)

            _, merged_sha = await git("rev-parse", "HEAD")
            merged_sha = merged_sha.strip()

            # ---- tests, on an export of the merged tree with no .git and no credentials
            export = tmp / "export"
            export.mkdir()
            rc, tar_bytes = await _run(["git", *ident, "archive", "--format=tar", "HEAD"], work, genv, GIT_TIMEOUT)
            if rc != 0:
                raise PRError(f"git archive failed: {clean(tar_bytes, 500)}")
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
                tar.extractall(export, members=[m for m in tar.getmembers() if m.isfile() or m.isdir()], filter="data")
            (tmp / "testhome").mkdir()
            tenv = _clean_env(tmp / "testhome")
            ran: list[str] = []
            for cmd in (test_commands if test_commands is not None else DEFAULT_TEST_COMMANDS):
                rc, out = await _run(list(cmd), export, tenv, test_timeout)
                ran.append(" ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd)))
                if rc != 0:
                    return _report("tests_failed", f"Merged {base} into {head_ref} locally"
                                   + (f" (resolved {', '.join(resolved)})" if resolved else "")
                                   + f", but the tests did not pass ({ran[-1]} exited {rc}). Nothing was pushed.",
                                   output_tail=clean(out), conflicts_resolved=resolved)

            # ---- push to the PR's own branch only: plain push, never forced
            rc, out = await git("push", "origin", f"HEAD:refs/heads/{head_ref}", auth_needed=True, allow_fail=True)
            if rc != 0:
                return _report("error", f"Tests passed but the push to {head_ref} was rejected, so nothing changed: "
                                        f"{clean(out, 800)}", merge_commit=merged_sha)
            return _report("pushed", f"Merged {base} into {head_ref} for PR #{number}"
                           + (f" (resolved {', '.join(resolved)})" if resolved else "")
                           + f"; tests passed; pushed {merged_sha[:10]}.", merge_commit=merged_sha,
                           conflicts_resolved=resolved, tests=ran)
        except PRError as e:
            return _report("error", clean(str(e), 1000))
        except Exception as e:  # noqa: BLE001
            log.exception("PR update failed")
            return _report("error", clean(f"{type(e).__name__}: {e}", 1000))
