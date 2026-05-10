"""Step-based progress board for backup/restore Telegram messages.

The board is a single Telegram message that mc-guard edits as backup.sh or
restore.sh emit `PROGRESS\\tstep\\tstatus\\tlabel\\tdetail` lines into a shared
file. Each step has fixed name and order; status transitions are
pending → running → ok (or fail).

The script side never imports this module — it just appends TSV lines to the
file path passed via env (`BACKUP_PROGRESS_FILE` / `RESTORE_PROGRESS_FILE`).
That decoupling means /bin/sh buffering can't stall the UI: the file is
re-read by polling.
"""
from __future__ import annotations

import logging
import pathlib
import re
import threading
import time

log = logging.getLogger(__name__)

BACKUP_STEPS: tuple[str, ...] = (
    "Preflight",
    "Save-all flush",
    "Save-off (pause writes)",
    "Compress world",
    "Save-on (resume writes)",
    "Verify archive",
    "R2 upload",
    "Local retention",
)

RESTORE_STEPS: tuple[str, ...] = (
    "Safety snapshot of current world",
    "Stop Minecraft",
    "Wipe volume and extract backup",
    "Start Minecraft",
)

PROGRESS_LINE_RE = re.compile(r"^PROGRESS\t(\d+)\t(\w+)\t([^\t]*)\t([^\t]*)$")

# Telegram edit rate limits are generous (1 edit/sec is safe in DM) but we
# also avoid no-op edits when nothing has changed.
MIN_EDIT_INTERVAL_SEC = 1.2


class ProgressBoard:
    """Mutable per-task state with a single-message render.

    Thread-safe: scripts write events into a file; the poller mutates state
    while the renderer reads it. All public methods grab the same lock.
    """

    def __init__(self, kind: str):
        if kind == "backup":
            self._title_emoji = "📦"
            self._kind_label = "Minecraft backup"
            self._steps = BACKUP_STEPS
        elif kind == "restore":
            self._title_emoji = "♻️"
            self._kind_label = "Minecraft restore"
            self._steps = RESTORE_STEPS
        else:
            raise ValueError(f"unknown progress kind: {kind!r}")

        self.kind = kind
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._status: dict[int, str] = {i: "pending" for i in range(1, len(self._steps) + 1)}
        self._label: dict[int, str] = {i: self._steps[i - 1] for i in range(1, len(self._steps) + 1)}
        self._detail: dict[int, str] = {i: "" for i in range(1, len(self._steps) + 1)}
        self._current: int = 0
        # Final state set when the subprocess exits: "ok" | "failed" | None.
        self._final: str | None = None
        self._fail_step: int | None = None

    # ── mutation ────────────────────────────────────────────────────────────
    def update(self, step: int, status: str, label: str = "", detail: str = "") -> None:
        if not 1 <= step <= len(self._steps):
            return
        if status not in ("pending", "running", "ok", "fail", "skip"):
            return
        with self._lock:
            self._status[step] = status
            if label:
                self._label[step] = label
            if detail:
                self._detail[step] = detail
            if status in ("running", "ok", "fail", "skip"):
                if status == "fail":
                    self._fail_step = step
                self._current = max(self._current, step)

    def mark_done(self, success: bool) -> None:
        with self._lock:
            self._final = "ok" if success else "failed"

    # ── render ──────────────────────────────────────────────────────────────
    def render(self) -> str:
        with self._lock:
            total = len(self._steps)
            n_ok = sum(1 for s in self._status.values() if s in ("ok", "skip"))
            pct = int(round(n_ok * 100 / total)) if total else 0
            bar_cells = 10
            filled = int(round(n_ok * bar_cells / total)) if total else 0
            bar = "▰" * filled + "▱" * (bar_cells - filled)

            elapsed = int(time.monotonic() - self._t0)
            mm, ss = divmod(elapsed, 60)
            elapsed_str = f"{mm}m {ss:02d}s" if mm else f"{ss}s"

            if self._final == "ok":
                header = f"{self._title_emoji} *{self._kind_label}* — ✅ done in {elapsed_str}"
            elif self._final == "failed":
                step_n = self._fail_step or self._current or 0
                step_lbl = self._steps[step_n - 1] if 1 <= step_n <= total else "?"
                header = (
                    f"{self._title_emoji} *{self._kind_label}* — ❌ failed at step {step_n} / {total}"
                    f" ({step_lbl}) · {elapsed_str}"
                )
            else:
                cur = self._current if self._current else 1
                header = f"{self._title_emoji} *{self._kind_label}* — step {cur} / {total} · {elapsed_str}"

            lines = [header, "", f"`{bar}`  {pct}%", ""]
            for i in range(1, total + 1):
                st = self._status[i]
                icon = {
                    "pending": "⏳",
                    "running": "🔄",
                    "ok":      "✅",
                    "skip":    "⏭️",
                    "fail":    "❌",
                }.get(st, "•")
                label = self._label[i]
                detail = self._detail[i]
                if detail:
                    lines.append(f"{icon} {label} — `{detail}`")
                else:
                    lines.append(f"{icon} {label}")
            return "\n".join(lines)


class ProgressFileTail:
    """Tail a PROGRESS event file, applying updates to a board.

    Robust to the file not existing yet, partial writes (we re-read on the
    next tick), and concurrent shell appends (we only seek to the last
    consumed offset, never truncate).
    """

    def __init__(self, path: pathlib.Path, board: ProgressBoard):
        self.path = path
        self.board = board
        self._pos = 0
        self._buf = ""

    def poll(self) -> bool:
        """Read any new content. Returns True if at least one event was applied."""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as fh:
                fh.seek(self._pos)
                chunk = fh.read()
                self._pos = fh.tell()
        except FileNotFoundError:
            return False
        except OSError as e:
            log.warning("progress tail %s: %s", self.path, e)
            return False

        if not chunk:
            return False

        self._buf += chunk
        applied = False
        # Only complete lines are events; keep any trailing partial in the
        # buffer for next poll.
        if "\n" not in self._buf:
            return False
        *lines, tail = self._buf.split("\n")
        self._buf = tail
        for line in lines:
            line = line.rstrip("\r")
            m = PROGRESS_LINE_RE.match(line)
            if not m:
                continue
            step = int(m.group(1))
            status = m.group(2)
            label = m.group(3)
            detail = m.group(4)
            self.board.update(step, status, label, detail)
            applied = True
        return applied


def _md_safe(text: str) -> str:
    """Trim/strip characters that confuse Telegram Markdown v1 parser inside `text`.

    Backticks fence code spans and must not appear unmatched. Asterisks and
    underscores can also break parsing; we replace them in dynamic detail
    fields (file names from PROGRESS lines are already tame, but be paranoid).
    """
    return text.replace("`", "ʼ").replace("*", "·").replace("_", "-")
