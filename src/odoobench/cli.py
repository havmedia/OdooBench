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

    if args.scenario.startswith("document"):
        target = _prepare_document(probe_session, target, args)
    if args.scenario == "analysis":
        target = _prepare_analysis(probe_session, target, args)

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


def _prepare_document(session: Session, target: Target, args: argparse.Namespace) -> Target:
    """Find the report, its model and enough records to draw from."""
    if not args.document:
        raise SystemExit(
            "the %s scenario needs --document. Run `odoobench documents` to see "
            "what this instance can print." % args.scenario
        )
    try:
        found = workload.find_document(session, args.document)
    except (LookupError, RpcError) as exc:
        raise SystemExit(str(exc))

    model = found.get("model") or ""
    if not model:
        raise SystemExit("document %r has no model to print" % args.document)

    ids = workload.sample_ids(session, model, limit=args.document_sample)
    if not ids:
        raise SystemExit(
            "document %r prints %s, and this instance has no %s records"
            % (args.document, model, model)
        )

    print(
        "  document %s prints %s, drawing from %d records"
        % (args.document, model, len(ids)),
        file=sys.stderr,
    )
    target.document_name = args.document
    target.document_model = model
    target.document_ids = ids
    target.document_batch = args.document_batch
    return target


def _prepare_analysis(session: Session, target: Target, args: argparse.Namespace) -> Target:
    """Ask the analysis model what it can be grouped by, unless told."""
    if not args.analysis:
        raise SystemExit(
            "the analysis scenario needs --analysis, for example --analysis sale.report. "
            "Run `odoobench analyses` to see what this instance has."
        )
    try:
        found = workload.inspect_analysis(session, args.analysis)
    except RpcError as exc:
        raise SystemExit("cannot read the fields of %r: %s" % (args.analysis, exc))

    target.analysis_model = args.analysis
    target.analysis_date_field = args.group_period or found["date_field"]
    target.analysis_dimension = (
        "" if args.group_by == "none" else (args.group_by or found["dimension"])
    )
    target.analysis_measures = (
        [name.strip() for name in args.measure.split(",") if name.strip()]
        if args.measure
        else found["measures"]
    )
    target.analysis_months = args.months

    print(
        "  %s grouped by %s%s over %d months, measuring %s"
        % (
            target.analysis_model,
            target.analysis_date_field + ":month",
            " and " + target.analysis_dimension if target.analysis_dimension else "",
            target.analysis_months,
            ", ".join(target.analysis_measures) or "record count",
        ),
        file=sys.stderr,
    )
    return target


def command_analyses(args: argparse.Namespace) -> int:
    """List the analysis models this instance has, and whether they hold data."""
    session = _session_factory(args)()
    try:
        session.authenticate()
    except RpcError as exc:
        print("could not log in: %s" % exc, file=sys.stderr)
        return 2

    rows = session.search_read(
        "ir.model",
        [["model", "like", "%report%"], ["transient", "=", False]],
        ["model", "name"],
        limit=args.limit,
        order="model asc",
    )

    print("%-42s %-30s %s" % ("model", "name", "has data"))
    print("-" * 86)
    for row in rows:
        model = row["model"]
        # A limit-one read answers "is there anything here" without counting
        # millions of rows, which on a SQL view is not a cheap question.
        try:
            sample = session.search_read(model, [], ["id"], limit=1, order="")
            state = "yes" if sample else "no"
        except RpcError:
            state = "-"
        print("%-42s %-30s %s" % (model, (row.get("name") or "")[:30], state))
    print("")
    print("Pick one with data and pass it as --analysis. Any model works, not just these.")
    return 0


def command_documents(args: argparse.Namespace) -> int:
    """List what this instance can print, and whether it has anything to print."""
    session = _session_factory(args)()
    try:
        session.authenticate()
    except RpcError as exc:
        print("could not log in: %s" % exc, file=sys.stderr)
        return 2

    rows = session.search_read(
        "ir.actions.report",
        [["report_type", "in", ["qweb-pdf", "qweb-html", "qweb-text"]]],
        ["report_name", "model", "report_type", "name"],
        limit=args.limit,
        order="model asc, report_name asc",
    )

    counts: Dict[str, int] = {}
    for row in rows:
        model = row.get("model") or ""
        if model and model not in counts:
            try:
                counts[model] = int(session.search_count(model, []) or 0)
            except RpcError:
                counts[model] = -1

    print("%-46s %-24s %10s" % ("report", "prints", "records"))
    print("-" * 82)
    for row in rows:
        model = row.get("model") or ""
        total = counts.get(model, -1)
        print(
            "%-46s %-24s %10s"
            % (row["report_name"], model, "?" if total < 0 else total)
        )
    print("")
    print("Pick one with records and pass it as --document.")
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
        prog="odoobench",
        description="Measure an Odoo the way its own web client uses it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="generate load and report")
    run_parser.add_argument("--url", required=True, help="http://host:8069")
    run_parser.add_argument("--db", required=True)
    run_parser.add_argument("--login", default="admin")
    run_parser.add_argument(
        "--password",
        default=os.environ.get("ODOOBENCH_PASSWORD", ""),
        help="or set ODOOBENCH_PASSWORD",
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
    run_parser.add_argument(
        "--document", default="", help="printable document, e.g. account.report_invoice"
    )
    run_parser.add_argument(
        "--document-batch",
        dest="document_batch",
        type=int,
        default=1,
        help="records per printed document; raise it for a month-end print run",
    )
    run_parser.add_argument(
        "--analysis", default="", help="analysis model to pivot, e.g. sale.report"
    )
    run_parser.add_argument(
        "--group-by",
        dest="group_by",
        default="",
        help="dimension to group by, or 'none'; discovered from the model if unset",
    )
    run_parser.add_argument(
        "--group-period",
        dest="group_period",
        default="",
        help="date field to group by month; discovered from the model if unset",
    )
    run_parser.add_argument(
        "--measure", default="", help="comma separated measures; discovered if unset"
    )
    run_parser.add_argument(
        "--months", type=int, default=12, help="how far back the report reaches"
    )
    run_parser.add_argument(
        "--document-sample",
        dest="document_sample",
        type=int,
        default=200,
        help="how many record ids to draw from",
    )
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

    documents_parser = sub.add_parser("documents", help="what this instance can print")
    documents_parser.add_argument("--url", required=True)
    documents_parser.add_argument("--db", required=True)
    documents_parser.add_argument("--login", default="admin")
    documents_parser.add_argument(
        "--password", default=os.environ.get("ODOOBENCH_PASSWORD", "")
    )
    documents_parser.add_argument("--timeout", type=int, default=60)
    documents_parser.add_argument("--limit", type=int, default=60)
    documents_parser.set_defaults(func=command_documents)

    analyses_parser = sub.add_parser("analyses", help="what this instance can report on")
    analyses_parser.add_argument("--url", required=True)
    analyses_parser.add_argument("--db", required=True)
    analyses_parser.add_argument("--login", default="admin")
    analyses_parser.add_argument(
        "--password", default=os.environ.get("ODOOBENCH_PASSWORD", "")
    )
    analyses_parser.add_argument("--timeout", type=int, default=60)
    analyses_parser.add_argument("--limit", type=int, default=60)
    analyses_parser.set_defaults(func=command_analyses)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "command", "") in {"run", "documents", "analyses"} and not args.password:
        parser.error("a password is required: --password or ODOOBENCH_PASSWORD")
    return int(args.func(args))
