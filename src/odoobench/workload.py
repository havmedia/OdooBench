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

from .rpc import Session

#: What Odoo's list view asks for. Eighty rows is the web client's page size.
PAGE_SIZE = 80


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

    #: Set by `--report`. Which document to render, on how many records at once,
    #: and the pool of record ids to draw from.
    report_name: str = ""
    report_model: str = ""
    report_ids: List[int] = field(default_factory=list)
    report_batch: int = 1

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


def find_report(session: Session, report_name: str) -> dict:
    """Look the report up the way the interface does, to learn its model."""
    rows = session.search_read(
        "ir.actions.report",
        [["report_name", "=", report_name]],
        ["report_name", "model", "report_type", "name"],
        limit=1,
        order="id asc",
    )
    if not rows:
        raise LookupError("no report named %r on this instance" % report_name)
    return rows[0]


def sample_ids(session: Session, model: str, limit: int = 200) -> List[int]:
    rows = session.search_read(model, [], ["id"], limit=limit, offset=0, order="id asc")
    return [int(row["id"]) for row in rows]


def render_report(target: Target, converter: str) -> Operation:
    """Render a document, the way the print button does.

    Two converters, and the difference between them is the point. `html` runs
    Odoo's own template engine and the queries behind it. `pdf` does all of that
    and then hands the result to wkhtmltopdf, an external program that has to be
    started, fed and waited for. Measuring both says how much of a slow report is
    Odoo and how much is the PDF engine, which are fixed in completely different
    places.
    """
    if not target.report_name:
        raise SystemExit("this scenario needs --report, for example --report account.report_invoice")
    if not target.report_ids:
        raise SystemExit("no records found for report %r" % target.report_name)

    batch = max(1, target.report_batch)

    def call(session: Session, rng: random.Random) -> Optional[str]:
        pool = target.report_ids
        chosen = rng.sample(pool, batch) if len(pool) >= batch else list(pool)
        session.fetch(
            "/report/%s/%s/%s"
            % (converter, target.report_name, ",".join(str(value) for value in chosen))
        )
        return "%s_x%d" % (converter, batch)

    return Operation("render_%s" % converter, 1, call)


def weighted(operations: List[Operation]) -> List[Operation]:
    """Expand weights into a flat list a random choice can draw from."""
    drawn: List[Operation] = []
    for operation in operations:
        drawn.extend([operation] * max(1, operation.weight))
    return drawn
