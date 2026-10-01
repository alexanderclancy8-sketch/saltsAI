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
- The merged tree (every tracked file, as a checkout would write it - not ``git archive``, which skips
  export-ignore'd files) is exported WITHOUT its .git folder and its tests run in a scratch directory with a scrubbed
  environment (no Jarvis settings, tokens or API keys) and a hard time limit. If they don't pass - or can't run
  at all - nothing is pushed and what failed is reported.
- The GitHub token is only ever given to git for the clone/fetch/push commands (as an HTTP header on the command
  line, never written to disk or into a remote URL) and all output is redacted before it is returned.

Note: this is a scratch directory and a clean environment, not an operating-system sandbox. That is why the tool
needs the owner's approval on every run.

No git binary on the host? ``_resolve_via_api`` does the same job through the GitHub REST API (merges endpoint, then
contents + git data APIs for conflicts), with the same guard rails (PR branch only, never forced, no workflow files,
no leftover markers). The difference: there is no local test run, so the merge is pushed first and the result of
GitHub Actions CI on the pushed commit is reported. It can't land on main, and pr_merge still needs green CI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from ..integrations.github_pr import PROTECTED_BRANCHES, PRClient, PRError, check_path, check_ref
from ..integrations.redact import redact, truncate

log = logging.getLogger(__name__)

GIT_TIMEOUT = 180
TEST_TIMEOUT = 900
CI_WAIT = 120  # seconds to wait for GitHub Actions after an API push before reporting "still running"
CI_POLL = 15
OUTPUT_TAIL = 4000
HUNK_LIMIT = 3000
MAX_RESOLUTION_CHARS = 400_000
BLOCKED_PREFIXES = (".github/",)
DEFAULT_TEST_COMMANDS: tuple[tuple[str, ...], ...] = ((sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"),)
_CONFIG_YAML = re.compile(r"""ROOT_DIR\s*/\s*["']([\w.\-]+\.ya?ml)["']""")
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


def _missing_config_files(export: Path) -> list[str]:
    """Root *.yaml files that jarvis/config.py reads via ROOT_DIR but which aren't in the exported tree."""
    try:
        text = (export / "jarvis" / "config.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return sorted({n for n in _CONFIG_YAML.findall(text) if not (export / n).is_file()})


def _failure_reason(output: str, rc: int, timeout: int) -> str:
    """One plain sentence on why the tests failed, from the test run's own output."""
    if rc == 124 and "Timed out" in output:
        return f"They ran past the {timeout}s time limit and were stopped."
    failed = [line.strip()[:200] for line in output.splitlines() if line.startswith(("FAILED ", "ERROR "))]
    if failed:
        more = f" (and {len(failed) - 3} more)" if len(failed) > 3 else ""
        return "Failing: " + "; ".join(failed[:3]) + more + "."
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return f"Last line of output: {lines[-1][:200]}" if lines else "They produced no output."


def _report(status: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"status": status, "summary": summary, **extra}


def git_available() -> bool:
    return shutil.which("git") is not None


def _check_resolutions(resolutions: dict[str, str]) -> tuple[dict[str, str], str | None]:
    """The cleaned paths -> text, or (…, reason) when one is unusable (size, path, leftover conflict markers)."""
    clean_res: dict[str, str] = {}
    for name, text in resolutions.items():
        path = check_path(name)
        if len(text) > MAX_RESOLUTION_CHARS:
            raise PRError(f"The resolution for {path} is too large.")
        if _MARKER.search(text):
            return {}, f"The resolution for {path} still contains conflict markers. Nothing was changed."
        clean_res[path] = text
    return clean_res, None


def _ci_sentence(ci: dict[str, Any]) -> str:
    state = ci.get("state")
    if state == "success":
        return "GitHub Actions CI passed on the new commit."
    if state == "failure":
        return f"GitHub Actions CI FAILED on the new commit ({', '.join(ci.get('failed') or ['see the checks'])})."
    if state == "pending":
        return "GitHub Actions CI is still running on the new commit; check it with run_tests."
    if state == "none":
        return "GitHub Actions hasn't reported any CI results on the new commit yet; check it with run_tests."
    return "Couldn't read the CI results for the new commit; check them with run_tests."


async def _await_ci(pc: PRClient, sha: str, wait: float, poll: float) -> dict[str, Any]:
    """CI state of ``sha``, polled for up to ``wait`` seconds. Reading CI never turns a finished push into an error."""
    waited = 0.0
    while True:
        try:
            ci = await pc._checks_detail(sha)  # noqa: SLF001
        except Exception as e:  # noqa: BLE001
            log.warning("Couldn't read CI results for %s: %s", sha[:10], e)
            return {"state": "unknown", "failed": [], "pending": []}
        if ci["state"] in ("success", "failure") or waited >= wait:
            return ci
        await asyncio.sleep(poll)
        waited += poll


async def _resolve_via_api(pc: PRClient, pr: dict[str, Any], number: int, resolutions: dict[str, str] | None, *,
                           ci_wait: float = CI_WAIT, ci_poll: float = CI_POLL) -> dict[str, Any]:
    """The no-git route: (1) GitHub's merges API brings the PR branch up to date; a 409 means real conflicts. (2) For
    conflicts the files changed on both sides are fetched through the contents API and, once ``resolutions`` supply
    their full text, the merge commit (two parents) is built with the git data API and the PR branch ref moved
    forward - never forced, never main. (3) There is no local test run: the result of GitHub Actions CI on the pushed
    commit is reported instead. The caller has already checked the PR (open, same repo, not main)."""
    head_ref, base, head_sha = pr["head"]["ref"], pr["base"]["ref"], pr["head"]["sha"]
    message = f"Merge {base} into {head_ref}"
    step = "merging through the GitHub API"
    try:
        if not resolutions:
            merged = await pc.update_branch(head_ref, base, message)
            if merged["result"] == "up_to_date":
                return _report("up_to_date", f"PR #{number} already contains everything on {base}; nothing to do.")
            if merged["result"] == "merged":
                return await _api_pushed(pc, number, base, head_ref, merged["sha"], [], ci_wait, ci_poll)
        step = "working out which files conflict"
        plan = await pc.merge_plan(head_ref, base)
        conflicted, theirs_only = plan["conflicts"], plan["theirs_only"]
        blocked = [n for n in conflicted if n.startswith(BLOCKED_PREFIXES)]
        extra = sorted(set(resolutions or {}) - set(conflicted))
        unresolved = [n for n in conflicted if n not in (resolutions or {})]
        if blocked:
            return _report("conflicts", f"PR #{number} conflicts with {base} in CI/workflow files "
                           f"({', '.join(blocked)}), which Jarvis never edits. A person needs to resolve this.",
                           conflicted_files=conflicted)
        if plan["unsupported"]:
            return _report("conflicts", f"PR #{number} conflicts with {base} where a file was deleted on one side "
                           f"({', '.join(plan['unsupported'])}). Jarvis can't settle that through the GitHub API; a person "
                           "needs to resolve it.", conflicted_files=conflicted)
        if extra:
            return _report("refused", "Resolutions were supplied for files that don't conflict: " + ", ".join(extra)
                           + ". Nothing was changed.", conflicted_files=conflicted)
        if not conflicted:
            return _report("error", f"GitHub reported a merge conflict between {base} and {head_ref} but no file was "
                                    "changed on both sides, so Jarvis can't tell what to resolve. Nothing was changed; "
                                    "a person needs to look at it.")
        if unresolved:
            step = "reading the conflicted files"
            hunks = {}
            for n in unresolved[:10]:
                mine, theirs = await pc.file_text(n, head_ref), await pc.file_text(n, base)
                hunks[n] = redact(f"=== on {head_ref} ===\n{mine if mine is not None else '(missing or not text)'}\n"
                                  f"=== on {base} ===\n{theirs if theirs is not None else '(missing or not text)'}",
                                  pc._secrets)[:HUNK_LIMIT]  # noqa: SLF001
            return _report("conflicts", f"PR #{number} conflicts with {base} in {len(conflicted)} file(s): "
                           f"{', '.join(conflicted)}. Nothing was pushed. Send the full resolved text of each file "
                           "via `resolutions` to try again.", conflicted_files=conflicted, conflict_hunks=hunks)
        cleaned, bad = _check_resolutions(resolutions or {})
        if bad:
            return _report("refused", bad)
        step = "committing the merge through the GitHub git data API"
        sha = await pc.commit_merge(head_ref, base, head_sha, cleaned, theirs_only, message)
        return await _api_pushed(pc, number, base, head_ref, sha, sorted(cleaned), ci_wait, ci_poll)
    except PRError as e:
        return _report("error", redact(str(e), pc._secrets)[:1000])  # noqa: SLF001
    except RuntimeError as e:  # GitHub answered with an error status
        text = redact(str(e), pc._secrets)[:700]  # noqa: SLF001
        if "-> 422" in text and step.startswith("committing"):
            text += (f" (This usually means {head_ref} moved while Jarvis was working; nothing was overwritten. "
                     "Try again.)")
        return _report("error", f"PR #{number} was not updated: the GitHub API failed while {step}. {text}")
    except Exception as e:  # noqa: BLE001
        log.exception("PR update via the GitHub API failed")
        return _report("error", redact(f"PR #{number} was not updated: {type(e).__name__} while {step}: {e}",
                                       pc._secrets)[:1000])  # noqa: SLF001


async def _api_pushed(pc: PRClient, number: int, base: str, head_ref: str, sha: str, resolved: list[str],
                      ci_wait: float, ci_poll: float) -> dict[str, Any]:
    ci = await _await_ci(pc, sha, ci_wait, ci_poll)
    return _report("pushed", f"Merged {base} into {head_ref} for PR #{number}"
                   + (f" (resolved {', '.join(resolved)})" if resolved else "")
                   + f" through the GitHub API (this host has no git); pushed {sha[:10]}. No local test run: "
                   + _ci_sentence(ci), merge_commit=sha, conflicts_resolved=resolved, method="github-api",
                   ci_state=ci["state"], ci_failed=ci.get("failed", []))


async def resolve_pr(pc: PRClient, number: int, resolutions: dict[str, str] | None = None, *,
                     remote_url: str | None = None, test_commands: Sequence[Sequence[str]] | None = None,
                     test_timeout: int = TEST_TIMEOUT, ci_wait: float = CI_WAIT,
                     ci_poll: float = CI_POLL) -> dict[str, Any]:
    """Merge the base branch into PR ``number``'s branch, run the tests and push if they pass. Never raises for an
    expected outcome - the ``status`` is one of: refused, up_to_date, conflicts, tests_failed, pushed, error.
    With a git binary this is the local clone / scratch-test / push flow; without one it goes through the GitHub API
    (``_resolve_via_api``), where the tests are GitHub Actions CI on the pushed branch."""
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

    if not git_available():  # no git binary on this host: do it all through the GitHub REST API instead
        return await _resolve_via_api(pc, pr, number, resolutions, ci_wait=ci_wait, ci_poll=ci_poll)

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
            # Every file git tracks for the merged commit, written out exactly as a checkout would (NOT `git archive`,
            # which leaves out anything marked export-ignore in .gitattributes), just without a .git folder.
            rc, out = await _run(["git", *ident, "checkout-index", "--all", "--force", f"--prefix={export}/"],
                                 work, genv, GIT_TIMEOUT)
            if rc != 0:
                raise PRError(f"Couldn't write out the merged files to test them: {clean(out, 500)}")
            rc, listing = await _run(["git", *ident, "ls-files", "-z"], work, genv, GIT_TIMEOUT)
            if rc != 0:
                raise PRError(f"git ls-files failed: {clean(listing, 500)}")
            absent = [n for n in listing.decode("utf-8", errors="replace").split("\0")
                      if n and not os.path.lexists(export / n)]
            if absent:
                raise PRError(f"The test copy of the merged branch is missing {len(absent)} file(s) that git tracks "
                              f"({', '.join(absent[:10])}), so the tests were not run. Nothing was pushed.")
            missing = _missing_config_files(export)
            if missing:
                return _report("error", f"The tests were not run: the merged branch has no {', '.join(missing)}, which "
                                        "jarvis/config.py reads from the repository root and the tests need. Check the "
                                        f"file exists on {head_ref} and on {base}. Nothing was pushed.",
                               missing_files=missing)
            (tmp / "testhome").mkdir()
            tenv = _clean_env(tmp / "testhome")
            ran: list[str] = []
            for cmd in (test_commands if test_commands is not None else DEFAULT_TEST_COMMANDS):
                rc, out = await _run(list(cmd), export, tenv, test_timeout)
                ran.append(" ".join(Path(cmd[0]).name if i == 0 else c for i, c in enumerate(cmd)))
                if rc != 0:
                    why = clean(_failure_reason(out.decode("utf-8", errors="replace"), rc, test_timeout), 600)
                    return _report("tests_failed", f"Merged {base} into {head_ref} locally"
                                   + (f" (resolved {', '.join(resolved)})" if resolved else "")
                                   + f", but the tests did not pass ({ran[-1]} exited {rc}). {why} Nothing was pushed.",
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
