"""A sandboxed copy of the Salts FSM source that the engineering agent can read and edit.

Nothing here executes code: the agent can only view, search and edit files. Every
path is resolved and confined to the checkout root. Tests run in the FSM repo's CI
on the pull request.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", "dist", "build", "bin", "obj", ".venv", "venv", "__pycache__", ".next",
             "coverage", ".idea", ".vscode", "vendor", "packages"}
MAX_FILE_BYTES = 1_000_000
MAX_OUTPUT = 30_000


class WorkspaceError(ValueError):
    pass


class Workspace:
    VIRTUAL_ROOT = "/repo"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.originals: dict[str, str | None] = {}
        self._snapshot: dict[str, str] | None = None

    def snapshot(self) -> None:
        """Remember every text file's content so edits made by another tool (Claude Code) can be diffed."""
        snap = {}
        for p in self._files():
            if p.stat().st_size <= MAX_FILE_BYTES:
                try:
                    snap[self.rel(p)] = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
        self._snapshot = snap

    # -- paths ------------------------------------------------------------------
    def resolve(self, path: str) -> Path:
        p = (path or "").strip()
        if p.startswith(self.VIRTUAL_ROOT):
            p = p[len(self.VIRTUAL_ROOT):]
        target = (self.root / p.lstrip("/")).resolve()
        if not target.is_relative_to(self.root):
            raise WorkspaceError(f"Path {path!r} is outside the repository")
        rel_parts = target.relative_to(self.root).parts
        if rel_parts and rel_parts[0] == ".git":
            raise WorkspaceError("The .git directory is off limits")
        return target

    def rel(self, target: Path) -> str:
        return target.relative_to(self.root).as_posix()

    def _remember(self, target: Path) -> None:
        key = self.rel(target)
        if key not in self.originals:
            self.originals[key] = target.read_text(encoding="utf-8") if target.exists() else None

    @staticmethod
    def _clip(text: str) -> str:
        return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + "\n…[output truncated - narrow the view_range]"

    # -- text editor commands -------------------------------------------------------
    def view(self, path: str, view_range: list[int] | None = None) -> str:
        target = self.resolve(path)
        if target.is_dir():
            lines = []
            base_depth = len(target.relative_to(self.root).parts)
            for p in sorted(target.rglob("*")):
                parts = p.relative_to(self.root).parts
                if any(part in SKIP_DIRS for part in parts) or len(parts) - base_depth > 2:
                    continue
                lines.append(f"{self.VIRTUAL_ROOT}/{p.relative_to(self.root).as_posix()}{'/' if p.is_dir() else ''}")
                if len(lines) >= 400:
                    lines.append("…[listing truncated]")
                    break
            return "\n".join(lines) or "(empty directory)"
        if not target.exists():
            raise WorkspaceError(f"{path} does not exist")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise WorkspaceError(f"{path} is too large to view")
        content = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start, end = 1, len(content)
        if view_range:
            start = max(1, int(view_range[0]))
            end = len(content) if len(view_range) < 2 or int(view_range[1]) == -1 else min(len(content), int(view_range[1]))
        return self._clip("\n".join(f"{i:6}\t{content[i - 1]}" for i in range(start, end + 1)))

    def create(self, path: str, file_text: str) -> str:
        target = self.resolve(path)
        self._remember(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(file_text, encoding="utf-8")
        return f"Wrote {self.rel(target)}"

    def str_replace(self, path: str, old_str: str, new_str: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"{path} does not exist")
        text = target.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count == 0:
            raise WorkspaceError("old_str not found in file - view the file and copy the text exactly")
        if count > 1:
            raise WorkspaceError(f"old_str matches {count} places - include more surrounding lines")
        self._remember(target)
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        return f"Edited {self.rel(target)}"

    def insert(self, path: str, insert_line: int, text: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise WorkspaceError(f"{path} does not exist")
        lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
        if not 0 <= insert_line <= len(lines):
            raise WorkspaceError(f"insert_line must be between 0 and {len(lines)}")
        self._remember(target)
        if text and not text.endswith("\n"):
            text += "\n"
        lines.insert(insert_line, text)
        target.write_text("".join(lines), encoding="utf-8")
        return f"Inserted into {self.rel(target)} after line {insert_line}"

    def run_editor_command(self, args: dict) -> str:
        cmd = args.get("command")
        path = args.get("path", "")
        if cmd == "view":
            return self.view(path, args.get("view_range"))
        if cmd == "create":
            return self.create(path, args.get("file_text", ""))
        if cmd == "str_replace":
            return self.str_replace(path, args.get("old_str", ""), args.get("new_str", ""))
        if cmd == "insert":
            return self.insert(path, int(args.get("insert_line", 0)), args.get("insert_text", args.get("new_str", "")))
        raise WorkspaceError(f"Unsupported command {cmd!r}")

    # -- search ---------------------------------------------------------------------------
    def _files(self, glob: str = "*"):
        for p in self.root.rglob("*"):
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(self.root).parts):
                if fnmatch.fnmatch(p.name, glob) or fnmatch.fnmatch(self.rel(p), glob):
                    yield p

    def grep(self, pattern: str, glob: str = "*", max_results: int = 80) -> str:
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise WorkspaceError(f"Bad regex: {e}") from e
        hits = []
        for p in self._files(glob):
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
            try:
                for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{self.VIRTUAL_ROOT}/{self.rel(p)}:{n}: {line.strip()[:200]}")
                        if len(hits) >= max_results:
                            return "\n".join(hits) + "\n…[more matches - refine the pattern]"
            except UnicodeDecodeError:
                continue
        return "\n".join(hits) or "No matches"

    def find(self, glob: str) -> str:
        files = [f"{self.VIRTUAL_ROOT}/{self.rel(p)}" for p in self._files(glob)]
        return "\n".join(sorted(files)[:300]) or "No files match"

    # -- results ----------------------------------------------------------------------------
    def changed_files(self) -> dict[str, str | None]:
        changed: dict[str, str | None] = {}
        if self._snapshot is not None:
            current = {}
            for p in self._files():
                try:
                    current[self.rel(p)] = p.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
            for rel in set(self._snapshot) | set(current):
                if self._snapshot.get(rel) != current.get(rel):
                    changed[rel] = current.get(rel)
                    self.originals.setdefault(rel, self._snapshot.get(rel))
        for rel, original in self.originals.items():
            target = self.root / rel
            current = target.read_text(encoding="utf-8") if target.exists() else None
            if current != original:
                changed[rel] = current
        return changed

    def diff(self) -> str:
        import difflib

        out = []
        for rel, current in self.changed_files().items():
            before = (self.originals.get(rel) or "").splitlines(keepends=True)
            after = (current or "").splitlines(keepends=True)
            out.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
        return "".join(out)
