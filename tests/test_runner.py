import time

from fake_odoo import FakeOdoo
from odoobench import scenario as scenarios
from odoobench.rpc import Session
from odoobench.runner import Pacer, RunConfig, run
from odoobench.workload import Target


def _factory(odoo, password="secret"):
    return lambda: Session(odoo.url, "demo", "admin", password, timeout=5)


def _config(**overrides):
    base = dict(concurrency=2, warmup_seconds=0, duration_seconds=1, runs=2, seed=7, timeout=5)
    base.update(overrides)
    return RunConfig(**base)


def test_a_run_serves_requests_and_keeps_the_runs_apart():
    with FakeOdoo() as odoo:
        aggregate = run(scenarios.get("browse"), Target(), _factory(odoo), _config())

    assert len(aggregate.runs) == 2
    assert aggregate.requests > 0
    assert aggregate.errors == 0


def test_failed_requests_are_counted_and_not_timed():
    with FakeOdoo(fail_every=2) as odoo:
        aggregate = run(scenarios.get("browse"), Target(), _factory(odoo), _config())

    assert aggregate.errors > 0
    # A failed request must never land in the latency sample: a configuration
    # that drops half the load would otherwise look fast.
    assert aggregate.requests + aggregate.errors > aggregate.requests


def test_a_login_that_fails_does_not_hang_the_run():
    with FakeOdoo() as odoo:
        aggregate = run(
            scenarios.get("browse"), Target(), _factory(odoo, password="wrong"), _config()
        )

    assert aggregate.requests == 0
    assert aggregate.errors == 2 * 2  # one per user per run


def test_the_office_day_mix_files_both_kinds_of_request():
    with FakeOdoo() as odoo:
        aggregate = run(
            scenarios.get("office-day"), Target(), _factory(odoo), _config(duration_seconds=2)
        )

    buckets = aggregate.bucket_requests()
    assert "list" in buckets
    assert any(name.startswith("filter_") for name in buckets)


def test_the_same_seed_draws_the_same_sequence_on_both_sides():
    # Each worker seeds its own generator, so one worker replays exactly. With
    # several workers the parameters are still the same set; only the order in
    # which they arrive at the server is up to the scheduler.
    def offsets():
        with FakeOdoo() as odoo:
            run(scenarios.get("search"), Target(), _factory(odoo), _config(runs=1, concurrency=1))
            return [call["kwargs"]["offset"] for call in odoo.call_kw_payloads()]

    first, second = offsets(), offsets()
    shortest = min(len(first), len(second))

    assert shortest > 0
    assert first[:shortest] == second[:shortest]


def test_the_pacer_holds_a_rate_across_workers():
    pacer = Pacer(rate=50.0)
    started = time.monotonic()
    for _ in range(10):
        pacer.wait()
    elapsed = time.monotonic() - started

    # Ten slots at fifty a second is about 0.2 s. Generous bounds: the point is
    # that it waits at all and does not wait a multiple of what it should.
    assert 0.1 < elapsed < 0.5


def test_no_rate_means_no_waiting():
    pacer = Pacer(rate=0.0)
    started = time.monotonic()
    for _ in range(1000):
        pacer.wait()
    assert time.monotonic() - started < 0.1


def test_a_report_is_fetched_from_the_route_the_print_button_uses():
    with FakeOdoo() as odoo:
        target = Target(document_name="sale.report_saleorder", document_ids=[11, 12, 13, 14])
        run(scenarios.get("document"), target, _factory(odoo), _config(runs=1))

    paths = odoo.report_paths()
    assert paths, "no document was ever requested"
    assert any(path.startswith("/report/pdf/sale.report_saleorder/") for path in paths)
    assert any(path.startswith("/report/html/sale.report_saleorder/") for path in paths)
    # One record per document unless a batch was asked for.
    assert all("," not in path.rsplit("/", 1)[-1] for path in paths)


def test_a_batch_prints_several_records_in_one_request():
    with FakeOdoo() as odoo:
        target = Target(
            document_name="sale.report_saleorder", document_ids=list(range(1, 40)), document_batch=5
        )
        run(scenarios.get("document-batch"), target, _factory(odoo), _config(runs=1))

    last = odoo.report_paths()[-1].rsplit("/", 1)[-1]
    assert len(last.split(",")) == 5


def test_an_empty_document_counts_as_an_error():
    with FakeOdoo(document=b"") as odoo:
        target = Target(document_name="sale.report_saleorder", document_ids=[1, 2, 3])
        aggregate = run(scenarios.get("document-batch"), target, _factory(odoo), _config(runs=1))

    assert aggregate.requests == 0
    assert aggregate.errors > 0


def test_the_pivot_asks_for_a_period_against_a_dimension():
    with FakeOdoo() as odoo:
        target = Target(
            analysis_model="sale.report",
            analysis_date_field="date",
            analysis_dimension="team_id",
            analysis_measures=["price_total"],
        )
        run(scenarios.get("analysis"), target, _factory(odoo), _config(runs=1))

    grouped = [c for c in odoo.call_kw_payloads() if c["method"] == "read_group"]
    assert grouped, "no aggregation was ever asked for"

    pivots = [c for c in grouped if len(c["args"][2]) == 2]
    trends = [c for c in grouped if len(c["args"][2]) == 1]
    assert pivots and trends

    # A pivot wants the full cross product, which is what lazy=False means, and
    # it groups the period by month the way the view does.
    assert pivots[0]["args"][2] == ["date:month", "team_id"]
    assert pivots[0]["args"][1] == ["price_total"]
    assert pivots[0]["kwargs"]["lazy"] is False
    assert trends[0]["kwargs"]["lazy"] is True


def test_the_period_is_limited_to_the_months_asked_for():
    import datetime

    with FakeOdoo() as odoo:
        target = Target(
            analysis_model="sale.report",
            analysis_date_field="date",
            analysis_measures=[],
            analysis_months=6,
            last_date=datetime.date(2026, 1, 1),
        )
        run(scenarios.get("analysis"), target, _factory(odoo), _config(runs=1))

    domain = [c for c in odoo.call_kw_payloads() if c["method"] == "read_group"][0]["args"][0]
    assert domain[0][0] == "date"
    assert domain[0][1] == ">="
    assert domain[0][2].startswith("2025-07")
