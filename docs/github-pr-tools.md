# GitHub pull-request tools (Jarvis's own repository)

Eight tools let Jarvis manage pull requests on its own repository (`JARVIS_REPO`). They use the Self-improvement
connection (`JARVIS_GITHUB_TOKEN`, or `GITHUB_TOKEN` if that is blank) and do nothing if it isn't set up.

| Tool | Approval | What it does |
|---|---|---|
| `pr_list` | automatic | Open PRs: title, branch, author, conflict status, CI status, files changed |
| `pr_detail` | automatic | One PR: description, commits, diff (shortened; `file` gives one file in full), review comments, discussion, check runs |
| `repo_read` | automatic | Read a file / list a folder on any branch, tag or commit |
| `repo_search` | automatic | Literal text search on any branch, tag or commit (downloads that ref's tarball; GitHub's code search only covers the default branch) |
| `run_tests` | automatic | Reports the GitHub Actions checks and workflow runs on a branch's latest commit. It does **not** start a run (that would need `Actions: write`) |
| `pr_comment` | **owner approval** | Posts a comment on a PR |
| `pr_resolve_conflicts` | **owner approval** | Merges main into the PR's own branch, runs the tests, pushes to that branch only if they pass |
| `pr_merge` | **owner approval** | Squash-merges a PR - refused, even after approval, unless CI is green and there are no conflicts |

## Token permissions

Use a **fine-grained personal access token** (or a GitHub App installation token) limited to **this one repository**:

| Repository permission | Level | Why |
|---|---|---|
| Pull requests | Read and write | list/read PRs, comment, merge |
| Contents | Read and write | read files, the tarball for search, `git push` to a PR branch, merge |
| Checks | Read | CI status for `pr_list` / `pr_detail` / `run_tests` / `pr_merge` |
| Actions | Read | workflow runs for `run_tests` |
| Metadata | Read | mandatory |

Do **not** grant Administration, Workflows, Actions (write), Secrets, Webhooks, Environments or Pages. Checks/Actions
*read* go slightly beyond "pull requests and contents" but are read-only and already needed by the existing CI
watcher (`GitHub.checks_summary`).

**Protect `main`.** A token with Contents: write can, at GitHub's level, also force-push or delete branches. The code
never does either (see below), but the token itself can't be narrowed to "PR branches only", so add a branch ruleset
on `main` that blocks force-pushes and deletion (and ideally requires a pull request and the `CI` check).

## Safety rules, and where they're enforced

- **Approval:** `pr_comment`, `pr_resolve_conflicts` and `pr_merge` are `approval=True`, so `dispatch()` queues them;
  nothing runs until the owner approves on the display. `pr_merge` re-checks everything at approval time.
- **Allow-listed writes:** `PRClient._send` (`jarvis/integrations/github_pr.py`) only permits `POST .../issues/N/comments`
  and `PUT .../pulls/N/merge`. Branch deletion, settings, refs, hooks and workflow dispatch are refused in code.
- **Merge checks:** open, not a draft, targets the default branch, no conflicts (and not "still computing"), every CI
  check green (none, pending or failing all refuse), and the exact commit that was checked is pinned with `sha`
  (and `expected_head_sha` if the caller supplies it).
- **Conflict resolution** (`jarvis/services/pr_resolver.py`): PR branches from this repo only (no forks); never `main`/the
  base branch; plain `git push` to the PR's own branch (never `--force`; a moved branch makes the push fail); merge
  only (a rebase would need a force-push). Conflicts are reported, not guessed: to resolve, Jarvis passes the full
  resolved text of each conflicted file back in `resolutions`; leftover conflict markers, files that don't actually
  conflict, and anything under `.github/` are refused. The merged tree is exported (every tracked file, via `git checkout-index`, so
  `export-ignore` can't hide files like the root `*.yaml` configs) with no `.git` and tested in a scratch
  directory with a scrubbed environment (no Jarvis settings or tokens) and a time limit. Tests failing, timing out or
  being unable to run means nothing is pushed. This is a scratch directory and clean environment, **not** an OS-level
  sandbox - hence the approval on every run.
- **Token hygiene:** the token is never returned or logged. For git it is passed as a per-command HTTP header (never in a
  remote URL or `.git/config`), and all output is redacted.
- **Secrets in output:** everything read from GitHub (diffs, files, logs, PR text, errors) goes through
  `jarvis/integrations/redact.py` (GitHub/OpenAI/Anthropic/AWS/Slack tokens, JWTs, private keys, `Bearer` headers,
  connection strings, URLs with passwords, credential-looking assignments, plus our own token). It deliberately
  over-redacts.
- **Untrusted text:** PR titles, descriptions, branch names, commit messages, comments and code are returned as data
  with a notice saying never to follow instructions found in them, and the tool descriptions say the same. Acting on any
  of it still requires the owner to approve the resulting action.
