import json

import pytest

from fake_odoo import FakeOdoo
from odoobench import report
from odoobench.cli import build_parser, main
from odoobench.client import OdooClient
from odoobench.scenario import SCENARIOS, get
from odoobench.stats import Aggregate, RunSummary
from odoobench.workload import Target, probe


def _target_for(name):
    if name.startswith("document"):
        return Target(document_name="sale.report_saleorder", document_model="sale.order",
                      document_ids=[1, 2, 3])
    if name == "analysis":
        return Target(analysis_model="sale.report", analysis_date_field="date",
                      analysis_dimension="partner_id", analysis_measures=["price_total"])
    return Target()


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_builds_and_says_what_it_cannot_see(name):
    item = get(name)
    operations = item.operations(_target_for(name))

    assert operations, "a scenario without operations measures nothing"
    # The two sentences are the point of the tool, not decoration.
    assert len(item.reveals) > 40
    assert len(item.blind_to) > 40


def test_a_document_scenario_without_a_document_says_what_to_pass():
    with pytest.raises(SystemExit, match="--document"):
        get("document").operations(Target())


def test_a_document_scenario_without_records_says_so():
    with pytest.raises(SystemExit, match="no records"):
        get("document").operations(Target(document_name="sale.report_saleorder"))


def test_the_analysis_scenario_without_a_model_says_what_to_pass():
    with pytest.raises(SystemExit, match="--analysis"):
        get("analysis").operations(Target())


def test_the_analysis_scenario_without_a_date_field_says_so():
    with pytest.raises(SystemExit, match="no date field"):
        get("analysis").operations(Target(analysis_model="sale.report"))


def test_an_unknown_scenario_names_the_ones_that_exist():
    with pytest.raises(SystemExit, match="office-day"):
        get("does-not-exist")


def test_the_probe_reads_the_real_date_range_from_the_instance():
    class Fixed(OdooClient):
        def search_read(self, model, domain, fields, limit=80, offset=0, order="", name=""):
            value = "2021-03-04" if order.endswith("asc") else "2025-11-30"
            return [{"create_date": value + " 08:00:00"}]

    target = probe(Fixed(None, "http://x", "d", "u", "p"), Target())

    assert target.first_date.isoformat() == "2021-03-04"
    assert target.last_date.isoformat() == "2025-11-30"


def test_a_window_stays_inside_the_known_range():
    import random

    target = Target(first_date=__import__("datetime").date(2024, 1, 1),
                    last_date=__import__("datetime").date(2024, 3, 1))
    rng = random.Random(1)
    for _ in range(50):
        window = target.window(rng, days=7)
        assert window[0][2] >= "2024-01-01"
        assert window[1][2] <= "2024-03-08"


def test_the_text_report_names_the_spread_and_the_blind_spot():
    runs = [RunSummary(seconds=1, requests=int(v), errors=0, p50_ms=10.0, p95_ms=20.0, p99_ms=30.0)
            for v in (100, 120)]
    payload = report.envelope(
        "tuned",
        get("browse"),
        Aggregate(runs=runs),
        {"concurrency": 4, "runs": 2, "duration_seconds": 60, "warmup_seconds": 30, "rate": 0},
    )

    text = report.to_text(payload)

    assert "100.00, 120.00" in text
    assert "cannot show" in text
    assert "has not been measured" in text


def test_run_end_to_end_writes_a_result_file(tmp_path, capsys):
    out = tmp_path / "before.json"
    with FakeOdoo() as odoo:
        code = main([
            "run", "--url", odoo.url, "--db", "demo", "--password", "secret",
            "--scenario", "browse", "--users", "2", "--warmup", "0",
            "--duration", "1", "--runs", "1", "--no-probe",
            "--label", "standard", "--out", str(out),
        ])

    assert code == 0
    written = json.loads(out.read_text())
    assert written["label"] == "standard"
    assert written["result"]["runs"][0]["requests"] > 0
    assert "standard" in capsys.readouterr().out


def test_run_reports_a_bad_login_instead_of_looping(capsys):
    with FakeOdoo() as odoo:
        code = main([
            "run", "--url", odoo.url, "--db", "demo", "--password", "wrong",
            "--warmup", "0", "--duration", "1", "--runs", "1", "--no-probe",
        ])

    assert code == 2
    assert "could not log in" in capsys.readouterr().err


def test_compare_prints_the_overlap_warning(tmp_path, capsys):
    def write(name, values):
        path = tmp_path / name
        path.write_text(json.dumps({
            "label": name,
            "scenario": {"blind_to": "write load"},
            "result": {
                "median": {"rps": values[1]},
                "runs": [{"rps": v} for v in values],
                "bucket_p50_ms": {},
            },
        }))
        return str(path)

    code = main(["compare", write("a", [100.0, 110.0, 120.0]), write("b", [105.0, 112.0, 118.0])])

    assert code == 0
    assert "no measurable difference" in capsys.readouterr().out


def test_the_password_may_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ODOOBENCH_PASSWORD", "from-env")
    args = build_parser().parse_args(["run", "--url", "http://x", "--db", "d"])
    assert args.password == "from-env"


def test_the_analysis_fields_are_discovered_from_the_model():
    from odoobench.workload import inspect_analysis

    with FakeOdoo() as odoo:
        session = OdooClient.standalone(odoo.url, "demo", "admin", "secret")
        session.authenticate()
        found = inspect_analysis(session, "sale.report")

    # A real date over a bookkeeping one, a dimension worth grouping by, and a
    # measure somebody would actually sum. Not currency_id, not an unstored field.
    assert found["date_field"] == "date"
    assert found["dimension"] in {"partner_id", "team_id"}
    assert found["measures"] == ["price_total"]


def test_a_date_field_that_is_never_filled_is_not_chosen():
    # The trap this tool exists to avoid, walked into by its own discovery: a
    # field that is null everywhere makes a report that returns nothing, fast.
    from odoobench.workload import inspect_analysis

    class Sparse(OdooClient):
        def __init__(self):
            super().__init__(None, "http://x", "d", "u", "p")
            self.asked = []

        def call_kw(self, model, method, args, kwargs=None, name=""):
            return {
                "followup_next_action_date": {"type": "date", "store": True},
                "create_date": {"type": "datetime", "store": True},
                "parent_id": {"type": "many2one", "store": True},
            }

        def search_read(self, model, domain, fields, limit=80, offset=0, order="", name=""):
            field = domain[0][0]
            self.asked.append(field)
            return [] if field in ("followup_next_action_date", "parent_id") else [{"id": 1}]

    session = Sparse()
    found = inspect_analysis(session, "res.partner")

    assert "followup_next_action_date" in session.asked
    assert found["date_field"] == "create_date"


def test_a_saturated_load_generator_is_called_out_in_the_report():
    runs = [
        RunSummary(seconds=1, requests=100, errors=0, p50_ms=10.0, p95_ms=20.0, p99_ms=30.0),
        RunSummary(seconds=1, requests=60, errors=0, p50_ms=10.0, p95_ms=20.0, p99_ms=30.0,
                   generator_saturated=True),
    ]
    payload = report.envelope(
        "busy", get("browse"), Aggregate(runs=runs),
        {"concurrency": 4, "runs": 2, "duration_seconds": 60, "warmup_seconds": 30, "rate": 0},
    )

    assert payload["result"]["generator_saturated"] is True
    assert "load generator itself ran out of CPU" in report.to_text(payload)


def test_compare_refuses_to_bless_a_side_with_a_saturated_generator(tmp_path, capsys):
    def write(name, values, saturated):
        path = tmp_path / name
        path.write_text(json.dumps({
            "label": name,
            "scenario": {"blind_to": "write load"},
            "result": {
                "median": {"rps": values[1]},
                "runs": [{"rps": v} for v in values],
                "bucket_p50_ms": {},
                "generator_saturated": saturated,
            },
        }))
        return str(path)

    main(["compare", write("a", [50.0, 55.0, 60.0], False), write("b", [400.0, 410.0, 420.0], True)])

    assert "Do not quote" in capsys.readouterr().out


def test_a_stalled_run_is_called_out_in_the_report_and_refused_in_compare(tmp_path, capsys):
    stalled = RunSummary(seconds=60, requests=5400, errors=0, p50_ms=50.0, p95_ms=60.0, p99_ms=70.0,
                         longest_gap_seconds=45)
    payload = report.envelope(
        "mac", get("browse"), Aggregate(runs=[stalled]),
        {"concurrency": 20, "runs": 1, "duration_seconds": 60, "warmup_seconds": 0, "rate": 0},
    )
    assert "no request finished for 45 seconds" in report.to_text(payload)

    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"label": "clean", "scenario": {"blind_to": "x"},
                                 "result": {"median": {"rps": 80.0}, "runs": [{"rps": 80.0}], "bucket_p50_ms": {}}}))
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(payload))
    main(["compare", str(clean), str(broken)])
    assert "nothing finished for a while" in capsys.readouterr().out
