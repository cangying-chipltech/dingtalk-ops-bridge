from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    duration_ms: int

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.exit_code == 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def truncate_text(text: str, limit: int, suffix: str = "\n... [truncated]") -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    if len(suffix) >= limit:
        return text[:limit]
    return text[: limit - len(suffix)] + suffix


def code_block(text: str, limit: int = 3600) -> str:
    content = truncate_text(text.rstrip(), limit)
    if not content:
        return "(no output)"
    content = content.replace("```", "'''")
    return f"```text\n{content}\n```"


def duration_text(milliseconds: int) -> str:
    if milliseconds < 1000:
        return f"{milliseconds}ms"
    return f"{milliseconds / 1000:.2f}s"


def run_process(
    argv: Sequence[str],
    *,
    timeout_seconds: float,
    cwd: str | None = None,
    env: Mapping[str, str] | None = None,
    max_output_chars: int = 4000,
) -> ProcessResult:
    command = [str(part) for part in argv]
    if not command:
        raise ValueError("command must not be empty")

    merged_env = os.environ.copy()
    if env:
        merged_env.update({str(key): str(value) for key, value in env.items()})

    started = time.monotonic()
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as stdout_file:
        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as stderr_file:
            try:
                process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=merged_env,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except OSError as exc:
                elapsed = int((time.monotonic() - started) * 1000)
                return ProcessResult(127, "", str(exc), False, elapsed)

            timed_out = False
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_process_group(process.pid)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass

            elapsed = int((time.monotonic() - started) * 1000)
            return ProcessResult(
                exit_code=process.returncode if process.returncode is not None else -1,
                stdout=_read_output(stdout_file, max_output_chars),
                stderr=_read_output(stderr_file, max_output_chars),
                timed_out=timed_out,
                duration_ms=elapsed,
            )


def _kill_process_group(pid: int) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(pid, signal.SIGKILL)
        else:
            os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        pass


def _read_output(handle: object, limit: int) -> str:
    if not hasattr(handle, "seek") or not hasattr(handle, "read"):
        return ""
    read_bytes = max(4096, limit * 4 + 16)
    handle.seek(0)
    data = handle.read(read_bytes)
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    return truncate_text(text, limit)
