"""Numbers, and how much of them to believe.

The one thing this module exists for: a single median is not a measurement. Two
configurations that differ by less than the spread between repeated runs of the
same configuration have not been shown to differ at all. Every summary here
therefore carries its runs with it.

Locust produces the numbers; this decides what may be said about them.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

#: A stretch this long without a single finished request means the load stopped
#: flowing. Requests that hang are neither successes nor failures to Locust, so
#: without this a stalled run reports a tidy average and no errors.
STALL_SECONDS = 5


@dataclass
class Bucket:
    """One named request within a run. Locust's request name is the name here."""

    requests: int
    p50_ms: float
    p95_ms: float

    def as_dict(self) -> Dict[str, Any]:
        return {"requests": self.requests, "p50_ms": self.p50_ms, "p95_ms": self.p95_ms}


@dataclass
class RunSummary:
    """One timed run: what got through, how fast, and what did not."""

    seconds: float
    requests: int
    errors: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    buckets: Dict[str, Bucket] = field(default_factory=dict)
    error_examples: List[str] = field(default_factory=list)
    #: Locust's own CPU ran above 90% during this run. The generator, not the
    #: server, may have set the pace, and then the number measures the wrong box.
    generator_saturated: bool = False
    #: The longest stretch, in whole seconds, in which no request finished.
    longest_gap_seconds: int = 0

    @property
    def stalled(self) -> bool:
        return self.longest_gap_seconds >= STALL_SECONDS

    @property
    def rps(self) -> float:
        return round(self.requests / self.seconds, 2) if self.seconds else 0.0

    @property
    def error_share(self) -> float:
        total = self.requests + self.errors
        return round(100.0 * self.errors / total, 3) if total else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "seconds": round(self.seconds, 1),
            "requests": self.requests,
            "errors": self.errors,
            "error_share_percent": self.error_share,
            "error_examples": self.error_examples[:3],
            "rps": self.rps,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "generator_saturated": self.generator_saturated,
            "longest_gap_seconds": self.longest_gap_seconds,
            "stalled": self.stalled,
            "buckets": {name: bucket.as_dict() for name, bucket in sorted(self.buckets.items())},
        }


@dataclass
class Aggregate:
    """Several runs of the same configuration, kept apart on purpose."""

    runs: List[RunSummary]

    @property
    def rps_values(self) -> List[float]:
        return [run.rps for run in self.runs]

    @property
    def rps(self) -> float:
        return round(statistics.median(self.rps_values), 2)

    @property
    def rps_range(self) -> "Range":
        return Range(min(self.rps_values), max(self.rps_values))

    def p(self, share: float) -> float:
        key = {0.50: "p50_ms", 0.95: "p95_ms", 0.99: "p99_ms"}[share]
        return round(statistics.median([getattr(run, key) for run in self.runs]), 1)

    @property
    def errors(self) -> int:
        return sum(run.errors for run in self.runs)

    @property
    def requests(self) -> int:
        return sum(run.requests for run in self.runs)

    def bucket_p50(self) -> Dict[str, float]:
        """Median p50 per named request, so a per-parameter effect stays visible.

        An aggregate hides the very thing worth seeing when a workload mixes
        cheap and expensive parameters: the cheap ones dominate the median and
        the expensive ones, where a setting actually bites, disappear.
        """
        collected: Dict[str, List[float]] = {}
        for run in self.runs:
            for name, bucket in run.buckets.items():
                collected.setdefault(name, []).append(bucket.p50_ms)
        return {name: round(statistics.median(v), 1) for name, v in sorted(collected.items())}

    def bucket_requests(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for run in self.runs:
            for name, bucket in run.buckets.items():
                counts[name] = counts.get(name, 0) + bucket.requests
        return dict(sorted(counts.items()))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "median": {
                "rps": self.rps,
                "p50_ms": self.p(0.50),
                "p95_ms": self.p(0.95),
                "p99_ms": self.p(0.99),
            },
            "spread": {"rps_min": self.rps_range.low, "rps_max": self.rps_range.high},
            "requests": self.requests,
            "errors": self.errors,
            "generator_saturated": any(run.generator_saturated for run in self.runs),
            "stalled": any(run.stalled for run in self.runs),
            "bucket_p50_ms": self.bucket_p50(),
            "bucket_requests": self.bucket_requests(),
            "runs": [run.as_dict() for run in self.runs],
        }


@dataclass(frozen=True)
class Range:
    low: float
    high: float

    def overlaps(self, other: "Range") -> bool:
        return self.low <= other.high and other.low <= self.high

    def __str__(self) -> str:
        return "%.2f..%.2f" % (self.low, self.high)


def longest_gap(per_second: Dict[int, int], window_start: float, window_end: float) -> int:
    """Longest run of seconds inside the window in which nothing finished.

    The first second and the last two are left out: the window rarely starts on
    a second boundary, and worker processes report up to a second late.
    """
    first, last = int(window_start) + 1, int(window_end) - 2
    longest = current = 0
    for second in range(first, last + 1):
        if per_second.get(second, 0):
            current = 0
        else:
            current += 1
            longest = max(longest, current)
    return longest


def from_locust(
    stats: Any,
    seconds: float,
    window_start: Optional[float] = None,
    window_end: Optional[float] = None,
) -> RunSummary:
    """Turn Locust's statistics for the window just finished into one run.

    Requests per second is counted here rather than taken from Locust, because
    Locust's own rate covers the whole time its statistics have been collecting
    and OdooBench resets them between runs.
    """
    total = stats.total
    buckets: Dict[str, Bucket] = {}
    for (name, _method), entry in stats.entries.items():
        if not entry.num_requests:
            continue
        buckets[name] = Bucket(
            requests=entry.num_requests,
            p50_ms=round(entry.get_response_time_percentile(0.5) or 0.0, 1),
            p95_ms=round(entry.get_response_time_percentile(0.95) or 0.0, 1),
        )

    return RunSummary(
        seconds=seconds,
        requests=total.num_requests,
        errors=total.num_failures,
        p50_ms=round(total.get_response_time_percentile(0.5) or 0.0, 1),
        p95_ms=round(total.get_response_time_percentile(0.95) or 0.0, 1),
        p99_ms=round(total.get_response_time_percentile(0.99) or 0.0, 1),
        buckets=buckets,
        error_examples=[str(error.error)[:160] for error in list(stats.errors.values())[:3]],
        longest_gap_seconds=(
            longest_gap(dict(total.num_reqs_per_sec), window_start, window_end)
            if window_start is not None and window_end is not None
            else 0
        ),
    )
