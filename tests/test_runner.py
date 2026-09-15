import datetime

from fake_odoo import FakeOdoo
from odoobench import scenario as scenarios
from odoobench.runner import Connection, RunConfig, run
from odoobench.workload import Target


def _connection(odoo, password="secret"):
    return Connection(url=odoo.url, db="demo", login="admin", password=password)


def _config(**overrides):
    base = dict(users=2, warmup_seconds=0, duration_seconds=1, runs=2, timeout=5)
    base.update(overrides)
    return RunConfig(**base)


def test_a_run_serves_requests_and_keeps_the_runs_apart():
    with FakeOdoo() as odoo:
        aggregate = run(scenarios.get("browse"), Target(), _connection(odoo), _config())

    assert len(aggregate.runs) == 2
    assert all(r.requests > 0 for r in aggregate.runs)
    assert aggregate.errors == 0
    # Locust files the list view under the name the operation gave it.
    assert "list" in aggregate.bucket_requests()


def test_an_odoo_error_inside_a_200_is_a_failure_not_a_success():
    # Odoo answers "internal error" with status 200. Left to itself Locust would
    # count that as a served request, and a broken server would look healthy.
    with FakeOdoo(fail_every=2) as odoo:
        aggregate = run(scenarios.get("browse"), Target(), _connection(odoo), _config())

    assert aggregate.errors > 0
    assert aggregate.requests > aggregate.errors  # both kinds were seen


def test_a_login_that_fails_does_not_count_as_work():
    with FakeOdoo() as odoo:
        aggregate = run(
            scenarios.get("browse"), Target(), _connection(odoo, password="wrong"), _config()
        )

    assert sum(b for name, b in aggregate.bucket_requests().items() if name != "login") == 0


def test_the_office_day_mix_names_both_kinds_of_request():
    with FakeOdoo() as odoo:
        aggregate = run(
            scenarios.get("office-day"), Target(), _connection(odoo), _config(duration_seconds=2)
        )

    names = aggregate.bucket_requests()
    assert "list" in names
    assert any(name.startswith("filter_") for name in names)


def test_a_rate_caps_the_whole_test_not_each_user():
    with FakeOdoo() as odoo:
        aggregate = run(
            scenarios.get("browse"), Target(), _connection(odoo),
            _config(users=4, rate=8.0, duration_seconds=3, runs=1),
        )

    # Eight a second across four users, not eight each.
    assert 4 <= aggregate.runs[0].rps <= 12


def test_a_document_is_fetched_from_the_route_the_print_button_uses():
    with FakeOdoo() as odoo:
        target = Target(document_name="sale.report_saleorder", document_ids=[11, 12, 13, 14])
        run(scenarios.get("document"), target, _connection(odoo), _config(runs=1))

    paths = odoo.report_paths()
    assert any(p.startswith("/report/pdf/sale.report_saleorder/") for p in paths)
    assert any(p.startswith("/report/html/sale.report_saleorder/") for p in paths)


def test_a_batch_prints_several_records_in_one_request():
    with FakeOdoo() as odoo:
        target = Target(document_name="sale.report_saleorder",
                        document_ids=list(range(1, 40)), document_batch=5)
        run(scenarios.get("document-batch"), target, _connection(odoo), _config(runs=1))

    assert len(odoo.report_paths()[-1].rsplit("/", 1)[-1].split(",")) == 5


def test_an_empty_document_counts_as_a_failure():
    with FakeOdoo(document=b"") as odoo:
        target = Target(document_name="sale.report_saleorder", document_ids=[1, 2, 3])
        aggregate = run(scenarios.get("document-batch"), target, _connection(odoo), _config(runs=1))

    assert aggregate.errors > 0


def test_the_pivot_asks_for_a_period_against_a_dimension():
    with FakeOdoo() as odoo:
        target = Target(analysis_model="sale.report", analysis_date_field="date",
                        analysis_dimension="team_id", analysis_measures=["price_total"],
                        last_date=datetime.date(2026, 1, 1), analysis_months=6)
        run(scenarios.get("analysis"), target, _connection(odoo), _config(runs=1))

    grouped = [c for c in odoo.call_kw_payloads() if c["method"] == "read_group"]
    pivots = [c for c in grouped if len(c["args"][2]) == 2]
    trends = [c for c in grouped if len(c["args"][2]) == 1]
    assert pivots and trends
    assert pivots[0]["args"][2] == ["date:month", "team_id"]
    assert pivots[0]["kwargs"]["lazy"] is False
    assert trends[0]["kwargs"]["lazy"] is True
    assert pivots[0]["args"][0][0][2].startswith("2025-07")


def test_the_same_seed_draws_the_same_work_on_both_sides():
    def offsets():
        with FakeOdoo() as odoo:
            run(scenarios.get("search"), Target(), _connection(odoo),
                _config(users=1, runs=1, seed=99))
            return [c["kwargs"]["offset"] for c in odoo.call_kw_payloads()]

    first, second = offsets(), offsets()
    shortest = min(len(first), len(second))
    assert shortest > 0
    assert first[:shortest] == second[:shortest]


def test_several_generator_processes_share_the_load():
    with FakeOdoo() as odoo:
        aggregate = run(
            scenarios.get("browse"), Target(), _connection(odoo),
            _config(users=4, processes=2, warmup_seconds=2, duration_seconds=4, runs=1),
        )

    assert aggregate.runs[0].requests > 0
    assert "list" in aggregate.bucket_requests()
