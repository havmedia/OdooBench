import json

import pytest

from fake_odoo import FakeOdoo
from odoobench import report
from odoobench.cli import build_parser, main
from odoobench.rpc import Session
from odoobench.scenario import SCENARIOS, get
from odoobench.stats import Aggregate, RunSummary
from odoobench.workload import Target, probe


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_builds_and_says_what_it_cannot_see(name):
    item = get(name)
    operations = item.operations(Target())

    assert operations, "a scenario without operations measures nothing"
    # The two sentences are the point of the tool, not decoration.
    assert len(item.reveals) > 40
    assert len(item.blind_to) > 40


def test_an_unknown_scenario_names_the_ones_that_exist():
    with pytest.raises(SystemExit, match="office-day"):
        get("does-not-exist")


def test_the_probe_reads_the_real_date_range_from_the_instance():
    class Fixed(Session):
        def search_read(self, model, domain, fields, limit=80, offset=0, order=""):
            value = "2021-03-04" if order.endswith("asc") else "2025-11-30"
            return [{"create_date": value + " 08:00:00"}]

    target = probe(Fixed("http://x", "d", "u", "p"), Target())

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
    runs = [RunSummary(seconds=1, requests=int(v), errors=0, latencies_ms=[10.0]) for v in (100, 120)]
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
