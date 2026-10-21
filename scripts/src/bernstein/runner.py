"""Run external commands, echoing each one before it runs.

Commands come in two kinds:

- *queries* only read state (``az ... show``, ``kubectl get``). Their output is
  captured, they are echoed dimmed, hidden with ``--quiet`` and still run with
  ``--dry-run`` so the rest of the plan is accurate.
- *actions* change something. They are highlighted, their output is streamed
  live and indented, and ``--dry-run`` prints them without running them.

Secret values passed in ``secrets`` are replaced by ``***`` in every echoed
command and every printed output line.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import console
from .errors import BernsteinError, CommandFailedError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

MASK = "***"
_SLOW_COMMAND_SECONDS = 5.0
_TAIL_LINES = 40


class _State:
    dry_run = False


state = _State()


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str = ""
    skipped: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def json(self) -> Any:
        return json.loads(self.stdout) if self.stdout.strip() else None


def mask(text: str, secrets: Iterable[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, MASK)
    return text


def display(args: Sequence[str], secrets: Iterable[str] = ()) -> str:
    return mask(shlex.join(args), secrets)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise BernsteinError(f"'{name}' is not installed or not on PATH")


def run(
    args: Sequence[str],
    *,
    query: bool = False,
    check: bool = True,
    secrets: Sequence[str] = (),
    stdin: str | None = None,
    cwd: Path | None = None,
    timeout: float | None = None,
    echo: bool = True,
) -> Result:
    """Run ``args``; see the module docstring for query vs action behaviour."""
    shown = display(args, secrets)
    dry = state.dry_run and not query
    if echo and not (query and console.state.quiet):
        console.command(shown, query=query, dry_run=dry)
    if dry:
        return Result(returncode=0, stdout="", skipped=True)
    require_tool(args[0])
    if query:
        return _run_captured(
            args, shown=shown, check=check, secrets=secrets, stdin=stdin, cwd=cwd, timeout=timeout
        )
    return _run_streamed(
        args, shown=shown, check=check, secrets=secrets, stdin=stdin, cwd=cwd, timeout=timeout
    )


def run_json(args: Sequence[str], *, check: bool = True) -> Any:
    """Run a query whose output is JSON and return it parsed (None on failure)."""
    result = run(args, query=True, check=check)
    if not result.ok:
        return None
    try:
        return result.json()
    except json.JSONDecodeError as exc:
        raise BernsteinError(f"unexpected non-JSON output from: {shlex.join(args)}") from exc


def _run_captured(
    args: Sequence[str],
    *,
    shown: str,
    check: bool,
    secrets: Sequence[str],
    stdin: str | None,
    cwd: Path | None,
    timeout: float | None,
) -> Result:
    try:
        proc = subprocess.run(
            list(args),
            input=stdin,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BernsteinError(f"timed out after {timeout}s: {shown}") from exc
    if console.state.verbose:
        for line in mask(proc.stdout, secrets).splitlines():
            console.output(line)
    if check and proc.returncode != 0:
        _print_tail(mask(proc.stderr or proc.stdout, secrets).splitlines())
        raise CommandFailedError(shown, proc.returncode, mask(proc.stderr, secrets))
    return Result(proc.returncode, proc.stdout, proc.stderr)


def _run_streamed(
    args: Sequence[str],
    *,
    shown: str,
    check: bool,
    secrets: Sequence[str],
    stdin: str | None,
    cwd: Path | None,
    timeout: float | None,
) -> Result:
    start = time.monotonic()
    tail: deque[str] = deque(maxlen=_TAIL_LINES)
    lines: list[str] = []
    with subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        cwd=cwd,
    ) as proc:
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            proc.stdin.close()
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = mask(raw.rstrip("\n"), secrets)
            lines.append(line)
            tail.append(line)
            console.output(line)
            if timeout is not None and time.monotonic() - start > timeout:
                proc.kill()
                raise BernsteinError(f"timed out after {timeout}s: {shown}")
        returncode = proc.wait()
    elapsed = time.monotonic() - start
    if elapsed >= _SLOW_COMMAND_SECONDS:
        console.detail(f"took {console.format_duration(elapsed)}")
    if check and returncode != 0:
        raise CommandFailedError(shown, returncode, "\n".join(tail))
    return Result(returncode, "\n".join(lines))


def _print_tail(lines: list[str]) -> None:
    for line in lines[-_TAIL_LINES:]:
        console.output(line, failed=True)
