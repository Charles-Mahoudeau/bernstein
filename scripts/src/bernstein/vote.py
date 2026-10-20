"""End-to-end check: a vote cast on Poll shows up in Result's live tally.

The vote travels poll -> Redis -> worker -> PostgreSQL -> result -> Socket.IO,
so this exercises every component, not just the web front ends.
"""

from __future__ import annotations

import json
import random
import shlex
import threading
import time
import uuid
from typing import TYPE_CHECKING

import httpx
import socketio

from . import console, helm
from .errors import BernsteinError
from .runner import state

if TYPE_CHECKING:
    from .config import Settings

OPTIONS = ("a", "b", "c", "d")


class _Scores:
    """The latest ``scores`` event pushed by Result (every second)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._latest: dict[str, int] = {}

    def update(self, payload: object) -> None:
        data = json.loads(payload) if isinstance(payload, str) else payload
        if not isinstance(data, dict):
            return
        with self._lock:
            self._latest = {k: int(v) for k, v in data.items()}
        self._event.set()

    def next(self, timeout: float) -> dict[str, int]:
        """Wait for a fresh event and return it."""
        self._event.clear()
        if not self._event.wait(timeout):
            raise BernsteinError(f"Result sent no 'scores' event within {timeout:g}s")
        with self._lock:
            return dict(self._latest)


def vote_check(settings: Settings, *, timeout: float = 30.0) -> None:
    console.step("Vote check: poll -> redis -> worker -> postgres -> result")
    if state.dry_run:
        console.detail("dry run: no vote is cast")
        return
    urls = helm.app_urls(settings)
    missing = [c for c in ("poll", "result") if c not in urls]
    if missing:
        raise BernsteinError(f"no Ingress URL for: {', '.join(missing)}; deploy the app first")

    scores = _Scores()
    client = socketio.Client(reconnection=False)
    client.on("scores", scores.update)
    console.detail(f"listening to Result's 'scores' events: Socket.IO client on {urls['result']}")
    try:
        client.connect(urls["result"], transports=["polling"], wait_timeout=10)
    except socketio.exceptions.ConnectionError as exc:
        raise BernsteinError(f"cannot connect to Result over Socket.IO: {exc}") from exc
    try:
        before = scores.next(timeout=15)
        console.info(f"current tally: {_format(before)}")
        option = random.choice(OPTIONS)
        voter = uuid.uuid4().hex
        # Echoed as the equivalent curl command; sent with httpx.
        console.command(
            shlex.join(
                [
                    "curl",
                    "-X",
                    "POST",
                    "--cookie",
                    f"voter_id={voter}",
                    "-d",
                    f"vote={option}",
                    f"{urls['poll']}/",
                ]
            ),
            query=False,
        )
        response = httpx.post(
            f"{urls['poll']}/",
            data={"vote": option},
            cookies={"voter_id": voter},
            timeout=10,
        )
        if response.status_code != 200:
            raise BernsteinError(f"Poll answered HTTP {response.status_code} to the vote")
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            now = scores.next(timeout=max(1.0, timeout - (time.monotonic() - start)))
            if now.get(option, 0) > before.get(option, 0):
                elapsed = time.monotonic() - start
                console.success(f"vote for '{option}' counted in Result after {elapsed:.1f}s")
                console.info(f"new tally: {_format(now)}")
                return
        raise BernsteinError(f"vote for '{option}' did not reach Result within {timeout:g}s")
    finally:
        client.disconnect()


def _format(scores: dict[str, int]) -> str:
    return ", ".join(f"{k}={scores.get(k, 0)}" for k in OPTIONS)
