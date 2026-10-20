"""Availability monitor: proves that an operation does not interrupt the service.

While an operation runs inside ``with Monitor(...)``:

- one thread per target sends HTTP requests at a fixed rate through the public
  Traefik IP, as a user would, and records status, latency and errors;
- one thread samples the ready/desired replicas of every workload of the release;
- a status line is printed every few seconds, and each failure streak is printed
  when it starts and when it ends.

On exit a summary is printed and saved to ``reports/<timestamp>-<name>.json``
(with every request in a ``.csv`` next to it). If any target is below
``min_availability`` percent, ``AvailabilityError`` is raised.
"""

from __future__ import annotations

import csv
import itertools
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

import httpx

from . import console, helm, kube
from .config import REPORTS_DIR, shown_path
from .errors import AvailabilityError, BernsteinError
from .runner import state

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from types import TracebackType

    from .config import Settings

STATUS_EVERY = 5.0
REPLICA_EVERY = 2.0


@dataclass(frozen=True)
class Target:
    name: str
    url: str


@dataclass(frozen=True)
class Sample:
    t: float  # seconds since the monitor started
    target: str
    status: int | None
    latency_ms: float
    error: str

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400


@dataclass(frozen=True)
class FailureWindow:
    start: float
    end: float
    failures: int


@dataclass(frozen=True)
class TargetStats:
    target: str
    url: str
    requests: int
    ok: int
    failed: int
    availability: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    errors: dict[str, int]
    windows: list[FailureWindow]


@dataclass(frozen=True)
class WorkloadStats:
    component: str
    desired: int
    min_ready: int
    zero_ready_seconds: float


@dataclass
class Report:
    name: str
    started: str
    duration: float
    min_availability: float
    targets: list[TargetStats]
    workloads: list[WorkloadStats]
    events: list[tuple[float, str]]
    passed: bool = False
    files: list[str] = field(default_factory=list)

    @property
    def availability(self) -> float:
        total = sum(t.requests for t in self.targets)
        return 100.0 * sum(t.ok for t in self.targets) / total if total else 0.0


def default_targets(settings: Settings) -> list[Target]:
    urls = helm.app_urls(settings)
    targets: list[Target] = []
    if "poll" in urls:
        targets.append(Target("poll", f"{urls['poll']}/"))
    if "result" in urls:
        targets.append(Target("result", f"{urls['result']}/"))
        # The Socket.IO handshake reaches the Node process, not only static files.
        targets.append(Target("socket.io", f"{urls['result']}/socket.io/?EIO=4&transport=polling"))
    return targets


class Monitor:
    def __init__(
        self,
        settings: Settings,
        name: str,
        *,
        min_availability: float = 100.0,
        rate: float | None = None,
        timeout: float | None = None,
        targets: Sequence[Target] | None = None,
    ) -> None:
        self.settings = settings
        self.name = name
        self.min_availability = min_availability
        self.rate = rate or settings.probe_rate
        self.timeout = timeout or settings.probe_timeout
        if self.rate <= 0 or self.timeout <= 0:
            raise BernsteinError("the probe rate and timeout must be positive")
        self.targets = list(targets) if targets is not None else []
        self.samples: list[Sample] = []
        self.replicas: list[tuple[float, dict[str, tuple[int, int]]]] = []
        self.events: list[tuple[float, str]] = []
        self.report: Report | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._start = 0.0
        self._started_at = ""
        self._enabled = not state.dry_run

    # --- lifecycle ---------------------------------------------------------

    def __enter__(self) -> Self:
        console.step(f"Availability monitor: {self.name}")
        if not self._enabled:
            console.detail("dry run: the availability monitor is not started")
            return self
        if not self.targets:
            self.targets = default_targets(self.settings)
        if not self.targets:
            raise BernsteinError("no Ingress URL found for the release; deploy the app first")
        for target in self.targets:
            console.info(f"probing {target.name:<9} GET {target.url}")
        console.info(
            f"{self.rate:g} req/s per target, timeout {self.timeout:g}s, "
            f"pass if every target >= {self.min_availability:g}%"
        )
        self._start = time.monotonic()
        self._started_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._threads = [
            threading.Thread(target=self._probe, args=(t,), daemon=True) for t in self.targets
        ]
        self._threads.append(threading.Thread(target=self._sample_replicas, daemon=True))
        self._threads.append(threading.Thread(target=self._print_status, daemon=True))
        for thread in self._threads:
            thread.start()
        # A baseline before the operation starts, so a broken app is not blamed on it.
        time.sleep(2.0)
        with self._lock:
            baseline_failures = sum(not sample.ok for sample in self.samples)
        if baseline_failures:
            console.warn(
                f"{baseline_failures} request(s) failed before the operation started: "
                "the app is not healthy (see 'bernstein status')"
            )
        self.event("operation starts")
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self._enabled:
            return
        self.event("operation ends" if exc is None else f"operation interrupted: {exc_type}")
        time.sleep(2.0)  # a short tail to catch failures right after the operation
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=self.timeout + 5)
        self.report = self._summarize()
        self._print_report(self.report)
        self._write_report(self.report)
        if exc is None and not self.report.passed:
            raise AvailabilityError(
                f"{self.name}: availability below {self.min_availability:g}% "
                f"(overall {self.report.availability:.2f}%)",
                self.report,
            )

    def event(self, message: str) -> None:
        """Record a timeline event (shown in the output and the report)."""
        if not self._enabled:
            return
        t = time.monotonic() - self._start
        with self._lock:
            self.events.append((round(t, 2), message))
        console.progress(f"event: {message}", start=self._start)

    def hold(self, seconds: float | None) -> None:
        """Keep probing for ``seconds`` (forever if None) or until Ctrl-C."""
        if not self._enabled:
            return
        deadline = None if seconds is None else time.monotonic() + seconds
        try:
            while deadline is None or time.monotonic() < deadline:
                time.sleep(0.2)
        except KeyboardInterrupt:
            console.info("stopped by Ctrl-C")

    # --- threads -----------------------------------------------------------

    def _probe(self, target: Target) -> None:
        interval = 1.0 / self.rate
        streak_start: float | None = None
        streak = 0
        next_at = time.monotonic()
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            while not self._stop.is_set():
                sample = self._request(client, target)
                with self._lock:
                    self.samples.append(sample)
                if not sample.ok:
                    if streak == 0:
                        streak_start = sample.t
                        console.progress(
                            f"FAIL {target.name}: {sample.error or sample.status}",
                            start=self._start,
                        )
                    streak += 1
                elif streak:
                    lasted = sample.t - (streak_start or sample.t)
                    console.progress(
                        f"recovered {target.name} after {streak} failure(s) in {lasted:.1f}s",
                        start=self._start,
                    )
                    streak = 0
                next_at = max(next_at + interval, time.monotonic())
                self._stop.wait(next_at - time.monotonic())

    def _request(self, client: httpx.Client, target: Target) -> Sample:
        started = time.monotonic()
        status: int | None = None
        error = ""
        try:
            response = client.get(target.url)
            status = response.status_code
            if status >= 400:
                error = f"HTTP {status}"
        except httpx.TimeoutException:
            error = "timeout"
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        latency = (time.monotonic() - started) * 1000
        return Sample(
            round(started - self._start, 3), target.name, status, round(latency, 1), error
        )

    def _sample_replicas(self) -> None:
        while not self._stop.is_set():
            items = kube.workloads(self.settings, echo=False)
            if items:
                snapshot = {w.component: (w.ready, w.desired) for w in items}
                with self._lock:
                    self.replicas.append((round(time.monotonic() - self._start, 2), snapshot))
            self._stop.wait(REPLICA_EVERY)

    def _print_status(self) -> None:
        while not self._stop.wait(STATUS_EVERY):
            with self._lock:
                samples = list(self.samples)
                last_replicas = self.replicas[-1][1] if self.replicas else {}
            parts = []
            for target in self.targets:
                mine = [s for s in samples if s.target == target.name]
                parts.append(f"{target.name} {sum(s.ok for s in mine)}/{len(mine)}")
            ready = " ".join(f"{c} {r}/{d}" for c, (r, d) in sorted(last_replicas.items()))
            console.progress(f"ok {' · '.join(parts)} | ready {ready or '?'}", start=self._start)

    # --- results -----------------------------------------------------------

    def _summarize(self) -> Report:
        duration = time.monotonic() - self._start
        targets = [self._target_stats(t) for t in self.targets]
        workloads = self._workload_stats()
        report = Report(
            name=self.name,
            started=self._started_at,
            duration=round(duration, 1),
            min_availability=self.min_availability,
            targets=targets,
            workloads=workloads,
            events=list(self.events),
        )
        report.passed = all(t.availability >= self.min_availability for t in targets)
        return report

    def _target_stats(self, target: Target) -> TargetStats:
        mine = [s for s in self.samples if s.target == target.name]
        ok = [s for s in mine if s.ok]
        latencies = sorted(s.latency_ms for s in ok)
        errors: dict[str, int] = {}
        windows: list[FailureWindow] = []
        current: list[Sample] = []
        for sample in mine:
            if sample.ok:
                if current:
                    windows.append(FailureWindow(current[0].t, current[-1].t, len(current)))
                    current = []
                continue
            key = sample.error or str(sample.status)
            errors[key] = errors.get(key, 0) + 1
            current.append(sample)
        if current:
            windows.append(FailureWindow(current[0].t, current[-1].t, len(current)))
        return TargetStats(
            target=target.name,
            url=target.url,
            requests=len(mine),
            ok=len(ok),
            failed=len(mine) - len(ok),
            availability=round(100.0 * len(ok) / len(mine), 3) if mine else 0.0,
            p50_ms=_percentile(latencies, 50),
            p95_ms=_percentile(latencies, 95),
            max_ms=latencies[-1] if latencies else 0.0,
            errors=errors,
            windows=windows,
        )

    def _workload_stats(self) -> list[WorkloadStats]:
        components = sorted({c for _, snap in self.replicas for c in snap})
        stats = []
        for component in components:
            points = [(t, snap[component]) for t, snap in self.replicas if component in snap]
            zero = 0.0
            for (t, (ready, _)), (next_t, _) in itertools.pairwise(points):
                if ready == 0:
                    zero += next_t - t
            stats.append(
                WorkloadStats(
                    component=component,
                    desired=max(d for _, (_, d) in points),
                    min_ready=min(r for _, (r, _) in points),
                    zero_ready_seconds=round(zero, 1),
                )
            )
        return stats

    def _print_report(self, report: Report) -> None:
        console.step(f"Availability report: {report.name} ({report.duration:.0f}s)")
        rows = [
            [
                t.target,
                str(t.requests),
                str(t.failed),
                f"{t.availability:.2f}%",
                f"{t.p50_ms:.0f} / {t.p95_ms:.0f} / {t.max_ms:.0f}",
                ", ".join(f"{k} x{v}" for k, v in t.errors.items()) or "-",
            ]
            for t in report.targets
        ]
        console.table(
            "HTTP probes",
            ["target", "requests", "failed", "availability", "p50/p95/max ms", "errors"],
            rows,
        )
        for t in report.targets:
            for w in t.windows:
                console.info(
                    f"{t.target}: {w.failures} failure(s) from +{w.start:.1f}s to +{w.end:.1f}s"
                )
        if report.workloads:
            console.table(
                "Ready replicas during the run",
                ["component", "desired", "min ready", "time at 0 ready"],
                [
                    [w.component, str(w.desired), str(w.min_ready), f"{w.zero_ready_seconds:.1f}s"]
                    for w in report.workloads
                ],
            )
        verdict = f"overall {report.availability:.2f}%, threshold {report.min_availability:g}%"
        if report.passed:
            console.success(f"PASS {report.name}: {verdict}")
        else:
            console.failure(f"{report.name}: {verdict}")

    def _write_report(self, report: Report) -> None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = REPORTS_DIR / f"{stamp}-{_slug(report.name)}"
        json_path, csv_path = base.with_suffix(".json"), base.with_suffix(".csv")
        report.files = [shown_path(json_path), shown_path(csv_path)]
        document = asdict(report)
        document["availability"] = round(report.availability, 3)
        document["replicas"] = [
            {"t": t, **{c: {"ready": r, "desired": d} for c, (r, d) in snap.items()}}
            for t, snap in self.replicas
        ]
        json_path.write_text(json.dumps(document, indent=2) + "\n")
        _write_samples(csv_path, self.samples)
        console.detail(f"report saved: {report.files[0]}")
        console.detail(f"requests saved: {report.files[1]}")


def _percentile(sorted_values: list[float], percent: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, round(percent / 100 * (len(sorted_values) - 1)))
    return sorted_values[index]


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.lower()).strip("-")


def _write_samples(path: Path, samples: list[Sample]) -> None:
    with path.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["t", "target", "status", "latency_ms", "error"])
        for s in sorted(samples, key=lambda s: s.t):
            writer.writerow([s.t, s.target, s.status or "", s.latency_ms, s.error])
