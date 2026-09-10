"""The command line."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from typing import Any, Dict, List, Optional

from . import report, scenario as scenarios, workload
from .compare import bucket_lines, compare
from .rpc import RpcError, Session
from .runner import RunConfig, run
from .workload import Target


def _target_from(args: argparse.Namespace) -> Target:
    target = Target(model=args.model, order=args.order, date_field=args.date_field)
    if args.fields:
        target.fields = [name.strip() for name in args.fields.split(",") if name.strip()]
    if args.first_date:
        target.first_date = date.fromisoformat(args.first_date)
    if args.last_date:
        target.last_date = date.fromisoformat(args.last_date)
    return target


def _session_factory(args: argparse.Namespace):
    def make() -> Session:
        return Session(
            url=args.url,
            db=args.db,
            login=args.login,
            password=args.password,
            timeout=args.timeout,
        )

    return make


def command_run(args: argparse.Namespace) -> int:
    chosen = scenarios.get(args.scenario)
    factory = _session_factory(args)
    target = _target_from(args)

    probe_session = factory()
    try:
        probe_session.authenticate()
    except RpcError as exc:
        print("could not log in: %s" % exc, file=sys.stderr)
        return 2

    if not args.no_probe:
        print("reading the target's date range ...", file=sys.stderr)
        try:
            target = workload.probe(probe_session, target)
            print(
                "  %s runs from %s to %s"
                % (target.model, target.first_date, target.last_date),
                file=sys.stderr,
            )
        except RpcError as exc:
            print("  probe failed (%s), keeping the given range" % exc, file=sys.stderr)

    config = RunConfig(
        concurrency=args.users,
        warmup_seconds=args.warmup,
        duration_seconds=args.duration,
        runs=args.runs,
        rate=args.rate,
        seed=args.seed,
        timeout=args.timeout,
    )

    aggregate = run(
        chosen,
        target,
        factory,
        config,
        on_progress=lambda message: print(message, file=sys.stderr),
    )

    payload = report.envelope(
        label=args.label or "%s on %s" % (chosen.name, args.db),
        scenario=chosen,
        aggregate=aggregate,
        settings={
            "concurrency": config.concurrency,
            "warmup_seconds": config.warmup_seconds,
            "duration_seconds": config.duration_seconds,
            "runs": config.runs,
            "rate": config.rate,
            "seed": config.seed,
            "model": target.model,
            "order": target.order,
            "date_field": target.date_field,
            "first_date": target.first_date.isoformat(),
            "last_date": target.last_date.isoformat(),
        },
    )

    if args.out:
        with open(args.out, "w") as handle:
            handle.write(report.to_json(payload))
        print("written to %s" % args.out, file=sys.stderr)

    print(report.to_json(payload) if args.json else report.to_text(payload))
    return 0


def command_compare(args: argparse.Namespace) -> int:
    before, after = _load(args.before), _load(args.after)
    verdict = compare(before, after)

    print("%s  ->  %s" % (verdict.label_before, verdict.label_after))
    print("=" * 72)
    print(verdict.headline)
    print("")
    print("  median requests/s   %8.2f  ->  %8.2f" % (verdict.before_median, verdict.after_median))
    lines = bucket_lines(before, after)
    if lines:
        print("")
        print("  typical wait per parameter:")
        for line in lines:
            print(line)

    if not verdict.separated:
        print("")
        print("The runs of the two configurations overlap. Whatever changed between")
        print("them, this measurement did not show it. Repeat both sides, alternating,")
        print("before drawing a conclusion.")

    before_blind = before["scenario"]["blind_to"]
    print("")
    print("Remember what this scenario cannot see: %s" % before_blind)
    return 0


def _load(path: str) -> Dict[str, Any]:
    with open(path) as handle:
        return json.load(handle)


def command_scenarios(_args: argparse.Namespace) -> int:
    for name in sorted(scenarios.SCENARIOS):
        item = scenarios.SCENARIOS[name]
        print("%s" % item.name)
        print("  %s" % item.summary)
        print("  shows:      %s" % item.reveals)
        print("  cannot see: %s" % item.blind_to)
        print("")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="locutus",
        description="Measure an Odoo the way its own web client uses it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="generate load and report")
    run_parser.add_argument("--url", required=True, help="http://host:8069")
    run_parser.add_argument("--db", required=True)
    run_parser.add_argument("--login", default="admin")
    run_parser.add_argument(
        "--password",
        default=os.environ.get("LOCUTUS_PASSWORD", ""),
        help="or set LOCUTUS_PASSWORD",
    )
    run_parser.add_argument("--scenario", default="office-day")
    run_parser.add_argument("--users", type=int, default=8, help="concurrent logged-in users")
    run_parser.add_argument("--warmup", type=int, default=30, help="seconds, discarded")
    run_parser.add_argument("--duration", type=int, default=60, help="seconds per run")
    run_parser.add_argument("--runs", type=int, default=3, help="repeats of the same configuration")
    run_parser.add_argument(
        "--rate",
        type=float,
        default=0.0,
        help="cap total requests per second, to compare latency at equal load",
    )
    run_parser.add_argument("--seed", type=int, default=1234)
    run_parser.add_argument("--timeout", type=int, default=300)
    run_parser.add_argument("--model", default="res.partner")
    run_parser.add_argument("--fields", default="", help="comma separated")
    run_parser.add_argument("--order", default="complete_name asc, id desc")
    run_parser.add_argument("--date-field", dest="date_field", default="create_date")
    run_parser.add_argument("--first-date", dest="first_date", default="")
    run_parser.add_argument("--last-date", dest="last_date", default="")
    run_parser.add_argument("--no-probe", action="store_true", help="skip reading the date range")
    run_parser.add_argument("--label", default="", help="what this run is, for the report")
    run_parser.add_argument("--out", default="", help="also write the result as JSON here")
    run_parser.add_argument("--json", action="store_true", help="print JSON instead of text")
    run_parser.set_defaults(func=command_run)

    compare_parser = sub.add_parser("compare", help="compare two result files honestly")
    compare_parser.add_argument("before")
    compare_parser.add_argument("after")
    compare_parser.set_defaults(func=command_compare)

    list_parser = sub.add_parser("scenarios", help="what can be measured, and what it shows")
    list_parser.set_defaults(func=command_scenarios)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", "") == "run" and not args.password:
        parser.error("a password is required: --password or LOCUTUS_PASSWORD")
    return int(args.func(args))
