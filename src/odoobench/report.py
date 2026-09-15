"""Turning a measurement into something a person can act on."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .scenario import Scenario
from .stats import Aggregate


def envelope(
    label: str,
    scenario: Scenario,
    aggregate: Aggregate,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "label": label,
        "scenario": {
            "name": scenario.name,
            "summary": scenario.summary,
            "reveals": scenario.reveals,
            "blind_to": scenario.blind_to,
        },
        "settings": settings,
        "result": aggregate.as_dict(),
    }


def to_json(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=False)


def to_text(payload: Dict[str, Any]) -> str:
    result = payload["result"]
    scenario = payload["scenario"]
    settings = payload["settings"]
    median = result["median"]
    spread = result["spread"]

    lines: List[str] = []
    lines.append("%s  (scenario: %s)" % (payload["label"], scenario["name"]))
    lines.append("=" * 72)
    lines.append(scenario["summary"])
    lines.append("")
    lines.append(
        "%d users, %d x %d s after %d s warm-up%s"
        % (
            settings["concurrency"],
            settings["runs"],
            settings["duration_seconds"],
            settings["warmup_seconds"],
            ", capped at %s requests/s" % settings["rate"] if settings.get("rate") else "",
        )
    )
    lines.append("")
    lines.append("  requests/s   %8.2f   (runs: %s)" % (
        median["rps"],
        ", ".join("%.2f" % run["rps"] for run in result["runs"]),
    ))
    lines.append("  typical wait %8.1f ms" % median["p50_ms"])
    lines.append("  slowest 5%%   %8.1f ms" % median["p95_ms"])
    lines.append("  requests     %8d" % result["requests"])
    lines.append("  errors       %8d%s" % (
        result["errors"],
        "   <- a run with errors did not serve the load" if result["errors"] else "",
    ))

    if result.get("generator_saturated"):
        lines.append("")
        lines.append("  WARNING: the load generator itself ran out of CPU in at least one run.")
        lines.append("  Those numbers may describe this machine rather than the Odoo server.")
        lines.append("  Run OdooBench on a separate machine, or with fewer users.")

    buckets = result.get("bucket_p50_ms") or {}
    if len(buckets) > 1:
        lines.append("")
        lines.append("  typical wait per parameter:")
        counts = result.get("bucket_requests", {})
        for name, value in buckets.items():
            lines.append("    %-14s %9.1f ms   (%d requests)" % (name, value, counts.get(name, 0)))

    lines.append("")
    lines.append("What a difference here means:")
    lines.extend(_wrap(scenario["reveals"], "  "))
    lines.append("")
    lines.append("What this scenario cannot show:")
    lines.extend(_wrap(scenario["blind_to"], "  "))
    lines.append("")
    lines.append(
        "Spread across runs of this one configuration: %.2f to %.2f requests/s."
        % (spread["rps_min"], spread["rps_max"])
    )
    lines.append(
        "A change smaller than that spread has not been measured, only observed once."
    )
    return "\n".join(lines)


def _wrap(text: str, indent: str, width: int = 70) -> List[str]:
    words = text.split()
    lines, current = [], indent
    for word in words:
        if len(current) + len(word) + 1 > width and current.strip():
            lines.append(current)
            current = indent + word
        else:
            current = current + (" " if current.strip() else "") + word
    if current.strip():
        lines.append(current)
    return lines
