"""Generating the load, and the four habits that keep the numbers honest.

1. Warm up and throw the warm-up away. The first requests of any run pay for
   caches that every later request finds filled.
2. Run the same configuration several times and keep the runs apart. One number
   cannot tell you whether a difference is real.
3. Draw the same sequence of parameters on both sides of a comparison, by
   seeding the random generator per worker.
4. Offer a fixed arrival rate. Comparing per-request latency between a run that
   served 60 requests a second and one that served 500 compares a quiet machine
   with a busy one.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .rpc import RpcError, Session
from .scenario import Scenario
from .stats import Aggregate, RunSummary
from .workload import Operation, Target, weighted


@dataclass
class RunConfig:
    concurrency: int = 8
    warmup_seconds: int = 30
    duration_seconds: int = 60
    runs: int = 3
    rate: float = 0.0  # requests per second across all users; 0 = as fast as possible
    seed: int = 1234
    timeout: int = 300


class Pacer:
    """Holds a fixed arrival rate across all workers, or gets out of the way."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate if rate else 0.0
        self._next: Optional[float] = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            if self._next is None or self._next < now:
                self._next = now
            slot = self._next
            self._next += self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)


@dataclass
class _Collector:
    lock: threading.Lock = field(default_factory=threading.Lock)
    latencies: List[float] = field(default_factory=list)
    buckets: dict = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    def add(self, bucket: Optional[str], milliseconds: float) -> None:
        self.latencies.append(milliseconds)
        if bucket:
            self.buckets.setdefault(bucket, []).append(milliseconds)

    def merge(self, other: "_Collector") -> None:
        with self.lock:
            self.latencies.extend(other.latencies)
            self.errors.extend(other.errors)
            for name, values in other.buckets.items():
                self.buckets.setdefault(name, []).extend(values)


def _worker(
    stop_at: float,
    session_factory: Callable[[], Session],
    operations: List[Operation],
    pacer: Pacer,
    seed: int,
    shared: _Collector,
) -> None:
    local = _Collector()
    rng = random.Random(seed)
    try:
        session = session_factory()
        session.authenticate()
    except RpcError as exc:
        local.errors.append("login: %s" % exc)
        shared.merge(local)
        return

    while time.monotonic() < stop_at:
        pacer.wait()
        if time.monotonic() >= stop_at:
            break
        operation = rng.choice(operations)
        started = time.monotonic()
        try:
            bucket = operation.call(session, rng)
        except RpcError as exc:
            local.errors.append("%s: %s" % (operation.name, exc))
            continue
        local.add(bucket, (time.monotonic() - started) * 1000)

    shared.merge(local)


def _one_run(
    seconds: int,
    session_factory: Callable[[], Session],
    operations: List[Operation],
    config: RunConfig,
    seed_offset: int,
) -> RunSummary:
    shared = _Collector()
    pacer = Pacer(config.rate)
    stop_at = time.monotonic() + seconds
    threads = [
        threading.Thread(
            target=_worker,
            args=(
                stop_at,
                session_factory,
                operations,
                pacer,
                config.seed + seed_offset * 1000 + index,
                shared,
            ),
            daemon=True,
        )
        for index in range(config.concurrency)
    ]

    started = time.monotonic()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started

    return RunSummary(
        seconds=elapsed,
        requests=len(shared.latencies),
        errors=len(shared.errors),
        error_examples=shared.errors[:3],
        latencies_ms=shared.latencies,
        by_bucket_ms=shared.buckets,
    )


def run(
    scenario: Scenario,
    target: Target,
    session_factory: Callable[[], Session],
    config: RunConfig,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Aggregate:
    operations = weighted(scenario.operations(target))
    announce = on_progress or (lambda _message: None)

    if config.warmup_seconds:
        announce("warming up for %d s" % config.warmup_seconds)
        _one_run(config.warmup_seconds, session_factory, operations, config, seed_offset=0)

    runs: List[RunSummary] = []
    for index in range(config.runs):
        announce("run %d of %d, %d s" % (index + 1, config.runs, config.duration_seconds))
        summary = _one_run(
            config.duration_seconds, session_factory, operations, config, seed_offset=index + 1
        )
        announce(
            "  %s requests/s, %s errors" % (summary.rps, summary.errors)
        )
        runs.append(summary)

    return Aggregate(runs=runs)
