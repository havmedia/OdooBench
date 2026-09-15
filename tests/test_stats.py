from odoobench.stats import Aggregate, Bucket, Range, RunSummary


def _run(requests, seconds=1.0, errors=0, buckets=None, p50=10.0):
    return RunSummary(
        seconds=seconds, requests=requests, errors=errors,
        p50_ms=p50, p95_ms=p50 * 2, p99_ms=p50 * 3, buckets=buckets or {},
    )


def test_a_run_reports_failed_requests_separately_from_served_ones():
    run = _run(requests=90, seconds=10, errors=10)
    assert run.rps == 9.0
    assert run.error_share == 10.0


def test_the_aggregate_keeps_the_spread_of_its_runs():
    aggregate = Aggregate(runs=[_run(100), _run(120), _run(110)])
    assert aggregate.rps == 110.0
    assert aggregate.rps_range == Range(100.0, 120.0)


def test_ranges_that_touch_count_as_overlapping():
    assert Range(1.0, 2.0).overlaps(Range(2.0, 3.0))
    assert not Range(1.0, 2.0).overlaps(Range(2.1, 3.0))


def test_the_per_request_median_survives_a_mixed_workload():
    # The overall median hides the expensive request when the cheap one is the
    # common case. The per-name view is what keeps it visible.
    runs = [
        _run(100, buckets={"list": Bucket(99, 10.0, 12.0), "filter_1d": Bucket(1, 5000.0, 5000.0)})
        for _ in range(3)
    ]
    aggregate = Aggregate(runs=runs)

    assert aggregate.p(0.50) == 10.0
    assert aggregate.bucket_p50() == {"filter_1d": 5000.0, "list": 10.0}
    assert aggregate.bucket_requests() == {"filter_1d": 3, "list": 297}


def test_a_stretch_without_finished_requests_is_found():
    from odoobench.stats import longest_gap

    per_second = {100: 5, 101: 5, 102: 5, 110: 5, 111: 5, 112: 5, 113: 5, 114: 5}
    # Seconds 103 to 109 finished nothing: seven in a row.
    assert longest_gap(per_second, 99.5, 116.5) == 7


def test_the_edges_of_the_window_are_not_mistaken_for_a_stall():
    from odoobench.stats import longest_gap

    # Traffic in every second from the first full one to the last one counted.
    per_second = {second: 3 for second in range(101, 114)}
    assert longest_gap(per_second, 100.4, 115.9) == 0


def test_a_run_that_stopped_flowing_is_marked_stalled_although_nothing_failed():
    # The MacBook run that started this: five thousand requests in thirteen
    # seconds, then forty-five seconds of hanging connections, reported by the
    # old code as a clean 89.9 requests a second with no errors.
    from odoobench.stats import from_locust

    class Entry:
        num_requests = 5400
        num_failures = 0
        num_reqs_per_sec = {second: 415 for second in range(1000, 1013)}

        def get_response_time_percentile(self, share):
            return 50.0

    class Stats:
        total = Entry()
        entries = {}
        errors = {}

    run = from_locust(Stats(), seconds=60.0, window_start=1000.0, window_end=1060.0)

    assert run.errors == 0
    assert run.longest_gap_seconds >= 40
    assert run.stalled is True
