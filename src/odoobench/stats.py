"""Numbers, and how much of them to believe.

The one thing this module exists for: a single median is not a measurement. Two
configurations that differ by less than the spread between repeated runs of the
same configuration have not been shown to differ at all. Every summary here
therefore carries its runs with it.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Sequence


def percentile(values: Sequence[float], share: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(share * (len(ordered) - 1))))
    return ordered[index]


@dataclass
class RunSummary:
    """One timed run: what got through, how fast, and what did not."""

    seconds: float
    requests: int
    errors: int
    error_examples: List[str] = field(default_factory=list)
    latencies_ms: List[float] = field(default_factory=list)
    by_bucket_ms: Dict[str, List[float]] = field(default_factory=dict)

    @property
    def rps(self) -> float:
        return round(self.requests / self.seconds, 2) if self.seconds else 0.0

    @property
    def error_share(self) -> float:
        total = self.requests + self.errors
        return round(100.0 * self.errors / total, 3) if total else 0.0

    def p(self, share: float) -> float:
        return round(percentile(self.latencies_ms, share), 1)

    def as_dict(self) -> Dict[str, object]:
        return {
            "seconds": round(self.seconds, 1),
            "requests": self.requests,
            "errors": self.errors,
            "error_share_percent": self.error_share,
            "error_examples": self.error_examples[:3],
            "rps": self.rps,
            "p50_ms": self.p(0.50),
            "p95_ms": self.p(0.95),
            "p99_ms": self.p(0.99),
            "mean_ms": round(statistics.fmean(self.latencies_ms), 1) if self.latencies_ms else 0.0,
            "buckets": {
                name: {
                    "requests": len(values),
                    "p50_ms": round(percentile(values, 0.50), 1),
                    "p95_ms": round(percentile(values, 0.95), 1),
                }
                for name, values in sorted(self.by_bucket_ms.items())
            },
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
        return round(statistics.median([run.p(share) for run in self.runs]), 1)

    @property
    def errors(self) -> int:
        return sum(run.errors for run in self.runs)

    @property
    def requests(self) -> int:
        return sum(run.requests for run in self.runs)

    def bucket_p50(self) -> Dict[str, float]:
        """Median p50 per bucket, so a per-parameter effect stays visible.

        An aggregate hides the very thing worth seeing when a workload mixes
        cheap and expensive parameters: the cheap ones dominate the median and
        the expensive ones, where a setting actually bites, disappear.
        """
        names: Dict[str, List[float]] = {}
        for run in self.runs:
            for name, values in run.by_bucket_ms.items():
                names.setdefault(name, []).append(percentile(values, 0.50))
        return {name: round(statistics.median(v), 1) for name, v in sorted(names.items())}

    def bucket_requests(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for run in self.runs:
            for name, values in run.by_bucket_ms.items():
                counts[name] = counts.get(name, 0) + len(values)
        return dict(sorted(counts.items()))

    def as_dict(self) -> Dict[str, object]:
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
