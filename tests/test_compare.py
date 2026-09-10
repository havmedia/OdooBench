from odoobench.compare import bucket_lines, compare


def _result(label, rps_values, buckets=None):
    return {
        "label": label,
        "scenario": {"blind_to": "write load"},
        "result": {
            "median": {"rps": sorted(rps_values)[len(rps_values) // 2]},
            "runs": [{"rps": value} for value in rps_values],
            "bucket_p50_ms": buckets or {},
        },
    }


def test_overlapping_runs_are_reported_as_no_difference():
    # 686.79 against 708.54 was the real pair that started this rule: the runs
    # of each side spread further than the gap between the two medians.
    before = _result("odoo only", [751.48, 664.62, 686.79])
    after = _result("odoo and database", [745.28, 671.14, 708.54])

    verdict = compare(before, after)

    assert not verdict.separated
    assert "no measurable difference" in verdict.headline


def test_clearly_separated_runs_report_a_factor():
    before = _result("standard", [60.15, 57.63, 59.93])
    after = _result("tuned", [496.97, 534.77, 508.15])

    verdict = compare(before, after)

    assert verdict.separated
    assert verdict.factor == 8.48
    assert "faster" in verdict.headline


def test_a_slower_after_is_named_as_slower():
    verdict = compare(_result("a", [100.0, 101.0]), _result("b", [50.0, 51.0]))

    assert verdict.separated
    assert "slower" in verdict.headline


def test_buckets_are_lined_up_even_when_one_side_lacks_one():
    lines = bucket_lines(
        _result("a", [1.0], {"filter_1d": 5251.9, "list": 40.0}),
        _result("b", [1.0], {"filter_1d": 782.2}),
    )

    assert any("filter_1d" in line and "5251.9" in line and "782.2" in line for line in lines)
    assert any("list" in line and "-" in line for line in lines)
