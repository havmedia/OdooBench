"""What a request is made of.

Every operation here is one call the Odoo web client makes when a person does
something ordinary. The bucket an operation returns is the label its timing is
filed under, which is how a per-parameter effect stays visible in the report
instead of drowning in the aggregate.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, List, Optional

from .rpc import RpcError, Session

#: What Odoo's list view asks for. Eighty rows is the web client's page size.
PAGE_SIZE = 80

#: How many groups a pivot brings back. The database still aggregates
#: everything; this only stops the result itself from becoming the bottleneck.
GROUP_LIMIT = 500


@dataclass
class Target:
    """The model a scenario works on, and the columns it sorts and filters by.

    The defaults describe `res.partner`, which every Odoo has and which is large
    in every Odoo that has been in use for a while. The `order` is the model's
    own `_order`, because that is what the web client sends when the user has
    not clicked a column header.
    """

    model: str = "res.partner"
    fields: List[str] = field(default_factory=lambda: ["display_name", "email", "create_date"])
    order: str = "complete_name asc, id desc"
    date_field: str = "create_date"
    first_date: date = date(2020, 1, 1)
    last_date: date = date(2026, 1, 1)
    unsorted_field: str = "email"

    #: Set by `--document`. Which document to print, on how many records at
    #: once, and the pool of record ids to draw from.
    document_name: str = ""
    document_model: str = ""
    document_ids: List[int] = field(default_factory=list)
    document_batch: int = 1

    #: Set by `--analysis`. Which analysis model to pivot, over how long, by
    #: what, measuring what. Empty entries are discovered from the model.
    analysis_model: str = ""
    analysis_date_field: str = ""
    analysis_dimension: str = ""
    analysis_measures: List[str] = field(default_factory=list)
    analysis_months: int = 12

    @property
    def span_days(self) -> int:
        return max(1, (self.last_date - self.first_date).days)

    def window(self, rng: random.Random, days: int) -> List[list]:
        """A date filter of the given width, placed somewhere in the real data."""
        start_at = self.first_date + timedelta(days=rng.randrange(0, max(1, self.span_days - days)))
        return [
            [self.date_field, ">=", start_at.isoformat()],
            [self.date_field, "<", (start_at + timedelta(days=days)).isoformat()],
        ]


@dataclass
class Operation:
    """One call, its share of the traffic, and how its timing gets filed."""

    name: str
    weight: int
    call: Callable[[Session, random.Random], Optional[str]]


def probe(session: Session, target: Target) -> Target:
    """Read the target's real size and date range before generating any load.

    A window picked from a range the data does not cover measures an empty
    result set very quickly and says nothing. Ask the instance first.
    """
    bounds = []
    for direction in ("asc", "desc"):
        rows = session.search_read(
            target.model,
            [[target.date_field, "!=", False]],
            [target.date_field],
            limit=1,
            order="%s %s" % (target.date_field, direction),
        )
        if not rows:
            return target
        bounds.append(str(rows[0][target.date_field])[:10])

    first, last = sorted(bounds)
    return Target(
        model=target.model,
        fields=list(target.fields),
        order=target.order,
        date_field=target.date_field,
        first_date=date.fromisoformat(first),
        last_date=date.fromisoformat(last),
        unsorted_field=target.unsorted_field,
    )


def count(session: Session, target: Target) -> int:
    return int(session.search_count(target.model, []) or 0)


# -- the operations ------------------------------------------------------


def open_list(target: Target, pages: int = 6) -> Operation:
    """Open a list and page through it. The everyday click.

    Cheap for the database, because the model's default order is indexed and the
    page is small. What this costs is Odoo's Python, which is why it is the
    operation that shows how many worker processes an instance has.
    """

    def call(session: Session, rng: random.Random) -> Optional[str]:
        session.search_read(
            target.model,
            [["active", "=", True]],
            target.fields,
            limit=PAGE_SIZE,
            offset=rng.randrange(0, pages) * PAGE_SIZE,
            order=target.order,
        )
        return "list"

    return Operation("open_list", 1, call)


def filtered_list(target: Target, widths: List[int], pages: int = 6) -> Operation:
    """Filter a list by a date range, keep the model's order, page through it.

    The database has a real choice here: walk the index that satisfies the order
    and check each entry against the filter, or read the table and sort. Which
    one it picks depends on its cost settings, and the two differ by orders of
    magnitude on a large table. The width of the filter decides how rare a hit
    is, so it is filed as the bucket.
    """

    def call(session: Session, rng: random.Random) -> Optional[str]:
        days = rng.choice(widths)
        session.search_read(
            target.model,
            [["active", "=", True]] + target.window(rng, days),
            target.fields,
            limit=PAGE_SIZE,
            offset=rng.randrange(0, pages) * PAGE_SIZE,
            order=target.order,
        )
        return "filter_%dd" % days

    return Operation("filtered_list", 1, call)


def grouped_report(target: Target, groupby: str, days: int = 30) -> Operation:
    """Group a large window. What a manager does at month end.

    Aggregation is the one everyday operation that can need more memory than the
    database is allowed to use, and it is where a sort that spills to disk costs
    real time.
    """

    def call(session: Session, rng: random.Random) -> Optional[str]:
        session.read_group(
            target.model,
            [["active", "=", True]] + target.window(rng, days),
            ["id"],
            [groupby],
        )
        return "group_%dd" % days

    return Operation("grouped_report", 1, call)


def unsorted_scan(target: Target, days: int = 30) -> Operation:
    """Sort a window by a column with no index.

    Kept because it is the trap: there is exactly one possible plan, so no
    database setting can change the outcome. A benchmark built out of this
    operation will report that database tuning does nothing, and will be wrong
    about everything except itself.
    """

    def call(session: Session, rng: random.Random) -> Optional[str]:
        session.search_read(
            target.model,
            [["active", "=", True]] + target.window(rng, days),
            target.fields,
            limit=PAGE_SIZE,
            offset=0,
            order="%s asc" % target.unsorted_field,
        )
        return "unsorted"

    return Operation("unsorted_scan", 1, call)


def find_document(session: Session, name: str) -> dict:
    """Look the printable document up, to learn which model it prints."""
    rows = session.search_read(
        "ir.actions.report",
        [["report_name", "=", name]],
        ["report_name", "model", "report_type", "name"],
        limit=1,
        order="id asc",
    )
    if not rows:
        raise LookupError("no printable document named %r on this instance" % name)
    return rows[0]


def sample_ids(session: Session, model: str, limit: int = 200) -> List[int]:
    rows = session.search_read(model, [], ["id"], limit=limit, offset=0, order="id asc")
    return [int(row["id"]) for row in rows]


def print_document(target: Target, converter: str) -> Operation:
    """Render a document, the way the print button does.

    Two converters, and the difference between them is the point. `html` runs
    Odoo's own template engine and the queries behind it. `pdf` does all of that
    and then hands the result to wkhtmltopdf, an external program that has to be
    started, fed and waited for. Measuring both says how much of a slow report is
    Odoo and how much is the PDF engine, which are fixed in completely different
    places.
    """
    if not target.document_name:
        raise SystemExit(
            "this scenario needs --document, for example --document account.report_invoice"
        )
    if not target.document_ids:
        raise SystemExit("no records found for document %r" % target.document_name)

    batch = max(1, target.document_batch)

    def call(session: Session, rng: random.Random) -> Optional[str]:
        pool = target.document_ids
        chosen = rng.sample(pool, batch) if len(pool) >= batch else list(pool)
        session.fetch(
            "/report/%s/%s/%s"
            % (converter, target.document_name, ",".join(str(value) for value in chosen))
        )
        return "%s_x%d" % (converter, batch)

    return Operation("render_%s" % converter, 1, call)


#: Fields no pivot is ever grouped by, however tempting their type.
_NEVER_GROUP = {
    "create_uid", "write_uid", "id", "message_main_attachment_id", "currency_id",
}

#: Field names that make a good measure, best first.
_MEASURE_HINTS = ("price_total", "price_subtotal", "amount_total", "amount_untaxed",
                  "product_uom_qty", "product_qty", "qty_delivered", "quantity", "nbr")


def _has_values(session: Session, model: str, name: str) -> bool:
    """Does this field hold anything at all?

    Asked because a field that is null everywhere makes a report that returns
    nothing and returns it very quickly. A benchmark that picks one measures an
    empty result set and calls the server fast.
    """
    try:
        return bool(session.search_read(model, [[name, "!=", False]], ["id"], limit=1, order=""))
    except RpcError:
        return False


def inspect_analysis(session: Session, model: str) -> dict:
    """Work out a realistic pivot for a model by reading its fields and its data.

    Odoo's analysis models (`sale.report`, `account.invoice.report` and their
    kin) are read-only SQL views built for exactly this: group by a period and a
    dimension, sum a measure. Rather than make the operator spell that out, ask
    the model what it has, then check that what it has is filled in.
    """
    described = session.call_kw(model, "fields_get", [[], ["type", "string", "store"]])

    def stored(name: str) -> bool:
        return bool(described[name].get("store", True))

    dates = [
        name for name, spec in described.items()
        if spec.get("type") in ("date", "datetime") and stored(name)
        and name not in ("create_date", "write_date")
    ]
    dates.sort(key=lambda name: (0 if name in ("date", "date_order", "invoice_date") else 1, name))
    # The bookkeeping date is a poor choice on a model built for reporting and
    # the only choice on a model that is not. It goes last, never missing.
    if "create_date" in described:
        dates.append("create_date")

    dimensions = [
        name for name, spec in described.items()
        if spec.get("type") == "many2one" and stored(name) and name not in _NEVER_GROUP
    ]
    dimensions.sort(key=lambda name: (0 if "partner" in name else 1, name))

    measures = [name for name in _MEASURE_HINTS if name in described]
    if not measures:
        measures = [
            name for name, spec in described.items()
            if spec.get("type") in ("float", "monetary", "integer") and stored(name)
            and name not in _NEVER_GROUP
        ][:1]

    return {
        "date_field": _first_filled(session, model, dates),
        "dimension": _first_filled(session, model, dimensions),
        "measures": measures[:2],
    }


def _first_filled(session: Session, model: str, candidates: List[str]) -> str:
    for name in candidates[:6]:
        if _has_values(session, model, name):
            return name
    return candidates[0] if candidates else ""


def _analysis_domain(target: Target) -> List[list]:
    if not target.analysis_date_field or not target.analysis_months:
        return []
    days = int(target.analysis_months * 30.4)
    start_at = target.last_date - timedelta(days=days)
    return [[target.analysis_date_field, ">=", start_at.isoformat()]]


def pivot(target: Target) -> Operation:
    """Two dimensions at once, the way a pivot table is opened.

    This is the query an analysis model exists for, and the one that can need
    more memory than the database is allowed to give a single query. It groups a
    period against a dimension and sums a measure, over however many rows the
    period covers.
    """

    def call(session: Session, rng: random.Random) -> Optional[str]:
        groupby = ["%s:month" % target.analysis_date_field]
        if target.analysis_dimension:
            groupby.append(target.analysis_dimension)
        session.call_kw(
            target.analysis_model,
            "read_group",
            [_analysis_domain(target), list(target.analysis_measures), groupby],
            # A pivot view shows a bounded number of rows. Without the limit a
            # dimension with millions of values would drag all of them over the
            # wire, and the measurement would become one of the network.
            {"lazy": False, "limit": GROUP_LIMIT, "context": {}},
        )
        return "pivot"

    return Operation("pivot", 1, call)


def trend(target: Target) -> Operation:
    """One dimension, the way a graph view is opened. The cheaper sibling."""

    def call(session: Session, rng: random.Random) -> Optional[str]:
        session.call_kw(
            target.analysis_model,
            "read_group",
            [
                _analysis_domain(target),
                list(target.analysis_measures),
                ["%s:month" % target.analysis_date_field],
            ],
            {"lazy": True, "limit": GROUP_LIMIT, "context": {}},
        )
        return "trend"

    return Operation("trend", 1, call)


def weighted(operations: List[Operation]) -> List[Operation]:
    """Expand weights into a flat list a random choice can draw from."""
    drawn: List[Operation] = []
    for operation in operations:
        drawn.extend([operation] * max(1, operation.weight))
    return drawn
