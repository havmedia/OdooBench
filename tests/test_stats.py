from locutus.stats import Aggregate, Range, RunSummary, percentile


def test_percentile_of_an_empty_sample_is_zero_not_a_crash():
    assert percentile([], 0.95) == 0.0


def test_percentile_picks_the_ordered_value():
    assert percentile([5, 1, 4, 2, 3], 0.5) == 3
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10


def test_a_run_reports_failed_requests_separately_from_served_ones():
    run = RunSummary(seconds=10, requests=90, errors=10, latencies_ms=[1.0] * 90)
    assert run.rps == 9.0
    assert run.error_share == 10.0


def _aggregate(*rps_values):
    return Aggregate(
        runs=[
            RunSummary(seconds=1, requests=int(value), errors=0, latencies_ms=[10.0] * int(value))
            for value in rps_values
        ]
    )


def test_the_aggregate_keeps_the_spread_of_its_runs():
    aggregate = _aggregate(100, 120, 110)
    assert aggregate.rps == 110.0
    assert aggregate.rps_range == Range(100.0, 120.0)


def test_ranges_that_touch_count_as_overlapping():
    assert Range(1.0, 2.0).overlaps(Range(2.0, 3.0))
    assert not Range(1.0, 2.0).overlaps(Range(2.1, 3.0))


def test_the_per_bucket_median_survives_a_mixed_workload():
    # The aggregate median hides the expensive parameter when the cheap one is
    # the common case. The per-bucket view is what keeps it visible.
    cheap = [10.0] * 99
    dear = [5000.0]
    runs = [
        RunSummary(
            seconds=1,
            requests=100,
            errors=0,
            latencies_ms=cheap + dear,
            by_bucket_ms={"list": list(cheap), "filter_1d": list(dear)},
        )
        for _ in range(3)
    ]
    aggregate = Aggregate(runs=runs)

    assert aggregate.p(0.50) == 10.0
    assert aggregate.bucket_p50() == {"filter_1d": 5000.0, "list": 10.0}
    assert aggregate.bucket_requests() == {"filter_1d": 3, "list": 297}
