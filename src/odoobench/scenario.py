"""What to measure, and what each measurement can and cannot show.

Every scenario carries two sentences that matter more than its code: what a
difference in its numbers means, and what it is blind to. Both are printed in
the report. A benchmark that does not say what it cannot see invites its reader
to conclude the opposite of the truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List

from . import workload
from .workload import Operation, Target


@dataclass
class Scenario:
    name: str
    summary: str
    reveals: str
    blind_to: str
    build: Callable[[Target], List[Operation]] = field(repr=False)

    def operations(self, target: Target) -> List[Operation]:
        return self.build(target)


def _browse(target: Target) -> List[Operation]:
    return [workload.open_list(target)]


def _search(target: Target) -> List[Operation]:
    return [workload.filtered_list(target, widths=[1, 3, 7, 30])]


def _office_day(target: Target) -> List[Operation]:
    listing = workload.open_list(target)
    listing.weight = 99
    searching = workload.filtered_list(target, widths=[1, 3])
    searching.weight = 1
    return [listing, searching]


def _month_end(target: Target) -> List[Operation]:
    grouped = Target(**{**target.__dict__})
    grouped.analysis_model = target.model
    grouped.analysis_date_field = target.date_field
    grouped.analysis_dimension = ""
    grouped.analysis_measures = []
    grouped.analysis_months = 1
    return [workload.trend(grouped)]


def _flat(target: Target) -> List[Operation]:
    return [workload.unsorted_scan(target)]


def _document(target: Target) -> List[Operation]:
    return [
        workload.print_document(target, converter="html"),
        workload.print_document(target, converter="pdf"),
    ]


def _document_batch(target: Target) -> List[Operation]:
    return [workload.print_document(target, converter="pdf")]


def _analysis(target: Target) -> List[Operation]:
    if not target.analysis_model:
        raise SystemExit(
            "this scenario needs --analysis, for example --analysis sale.report"
        )
    if not target.analysis_date_field:
        raise SystemExit(
            "%s has no date field to group a period by" % target.analysis_model
        )
    return [workload.pivot(target), workload.trend(target)]


SCENARIOS: Dict[str, Scenario] = {
    "browse": Scenario(
        name="browse",
        summary="Open lists and page through them, the way people do all day.",
        reveals=(
            "How many requests an instance can serve at once. This is Odoo's own "
            "Python at work, so it moves with the number of worker processes and "
            "the memory limits they run under."
        ),
        blind_to=(
            "Database settings. These queries use the model's indexed default "
            "order and return one small page, so the database does the same "
            "little work whatever it has been told about its hardware."
        ),
        build=_browse,
    ),
    "search": Scenario(
        name="search",
        summary="Filter a list by a date range and page through the result.",
        reveals=(
            "Whether the database picks the index or reads the whole table. The "
            "narrower the filter, the rarer a hit, and the further it has to walk "
            "before it has a page together. That is where its cost settings decide "
            "the outcome, and the two answers differ by orders of magnitude."
        ),
        blind_to=(
            "Worker counts, mostly. At a low concurrency the database is the "
            "bottleneck, so adding Odoo processes moves little."
        ),
        build=_search,
    ),
    "office-day": Scenario(
        name="office-day",
        summary="Ninety-nine list views for every filtered search. A normal day.",
        reveals=(
            "Both bottlenecks in one number, in the order they appear: first the "
            "worker count, then the database. Tuning only one of them moves the "
            "queue to the other."
        ),
        blind_to=(
            "Write load. Nothing here inserts or updates, so vacuum, checkpoint "
            "and WAL settings are untouched by it."
        ),
        build=_office_day,
    ),
    "month-end": Scenario(
        name="month-end",
        summary="Group a month of records, the way a report does.",
        reveals=(
            "Whether an aggregation fits in the memory the database may use for "
            "one. Half measures do not help here: a sort that still spills, only "
            "in fewer batches, is no faster, and can be slower."
        ),
        blind_to=(
            "Everyday latency. Few people run reports, and one running report "
            "does not slow a server the way a hundred list views do."
        ),
        build=_month_end,
    ),
    "analysis": Scenario(
        name="analysis",
        summary="Open a pivot and a graph on an analysis model, the way a manager does.",
        reveals=(
            "What a report costs when nobody is looking at a list. Grouping a "
            "period against a dimension is the one everyday query that can need "
            "more memory than the database allows a single query, and the "
            "difference between an aggregation that fits and one that spills to "
            "disk is a third of its runtime. It also runs on spare cores, so it "
            "moves with how many the database may use at once."
        ),
        blind_to=(
            "Everyday latency under load. One manager opening a pivot is not a "
            "hundred people clicking, and a server saturated by list views has no "
            "spare core left for this query to use anyway. Run it at a low "
            "concurrency, alongside a busy office rather than instead of one."
        ),
        build=_analysis,
    ),
    "document": Scenario(
        name="document",
        summary="Print a document, once as HTML and once as PDF.",
        reveals=(
            "How long a document takes, and how much of that is Odoo against how "
            "much is the PDF engine. The HTML pass is the template and its "
            "queries; the PDF pass is the same work plus starting wkhtmltopdf, "
            "feeding it and waiting. The gap between the two buckets tells you "
            "which of the two to go and fix."
        ),
        blind_to=(
            "Everything list views show. A report is one long request, not many "
            "short ones, so it says nothing about how many people can click at "
            "once. It is also the request most likely to hit a worker's time and "
            "memory limits, so a failure here is a finding, not noise."
        ),
        build=_document,
    ),
    "document-batch": Scenario(
        name="document-batch",
        summary="Print documents as PDF, in batches if you ask for them.",
        reveals=(
            "What a printing run costs. With --document-batch this is the month-end "
            "print job rather than one invoice, which is where the PDF engine "
            "stops being an afterthought and starts being the whole request."
        ),
        blind_to=(
            "Where the time went inside the request. Run the `document` scenario "
            "for that; it splits the template from the PDF engine."
        ),
        build=_document_batch,
    ),
    "flat": Scenario(
        name="flat",
        summary="Sort by a column with no index. The control, not a benchmark.",
        reveals=(
            "Nothing about the database, on purpose. There is one possible plan, "
            "so its settings cannot change the result. Run it to see for yourself "
            "that a difference elsewhere was real."
        ),
        blind_to=(
            "Everything a database setting does. If this scenario shows a "
            "difference between two configurations, something else changed too."
        ),
        build=_flat,
    ),
}


def get(name: str) -> Scenario:
    try:
        return SCENARIOS[name]
    except KeyError:
        raise SystemExit(
            "unknown scenario %r. Available: %s" % (name, ", ".join(sorted(SCENARIOS)))
        )
