"""Deciding whether two measurements actually differ.

The rule is deliberately blunt: if the runs of one configuration overlap the
runs of the other, the measurement did not separate them. Saying "three percent
faster" when repeated runs of the same configuration spread by six is how a
benchmark ends up arguing for a change that does nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from .stats import Range


@dataclass
class Verdict:
    label_before: str
    label_after: str
    before: Range
    after: Range
    before_median: float
    after_median: float
    separated: bool

    @property
    def factor(self) -> float:
        """How much more the second configuration served, as a plain multiple."""
        return round(self.after_median / self.before_median, 2) if self.before_median else 0.0

    @property
    def headline(self) -> str:
        if not self.separated:
            return (
                "no measurable difference: the runs overlap (%s against %s requests/s)"
                % (self.before, self.after)
            )
        faster = self.after_median > self.before_median
        multiple = self.factor if faster else round(1 / self.factor, 2) if self.factor else 0.0
        return "%.2f x %s (%s against %s requests/s across runs)" % (
            multiple,
            "faster" if faster else "slower",
            self.before,
            self.after,
        )


def _range(result: Dict[str, Any]) -> Range:
    runs: List[Dict[str, Any]] = result["result"]["runs"]
    values = [float(run["rps"]) for run in runs]
    return Range(min(values), max(values))


def compare(before: Dict[str, Any], after: Dict[str, Any]) -> Verdict:
    before_range, after_range = _range(before), _range(after)
    return Verdict(
        label_before=before.get("label", "before"),
        label_after=after.get("label", "after"),
        before=before_range,
        after=after_range,
        before_median=float(before["result"]["median"]["rps"]),
        after_median=float(after["result"]["median"]["rps"]),
        separated=not before_range.overlaps(after_range),
    )


def bucket_lines(before: Dict[str, Any], after: Dict[str, Any]) -> List[str]:
    """Per-parameter comparison, because the aggregate hides where it bit."""
    left = before["result"].get("bucket_p50_ms", {})
    right = after["result"].get("bucket_p50_ms", {})
    lines = []
    for name in sorted(set(left) | set(right)):
        lines.append(
            "  %-14s %10s ms -> %10s ms"
            % (name, left.get(name, "-"), right.get(name, "-"))
        )
    return lines
