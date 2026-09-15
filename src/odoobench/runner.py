"""Driving Locust, and the four habits that keep its numbers honest.

Locust generates the load and counts it. What it does not do is tell you whether
a difference between two of its runs is real, because it reports one number per
test. That is what this adds:

1. Warm up and throw the warm-up away. The first requests of any run pay for
   caches every later request finds filled.
2. Run the same configuration several times and keep the runs apart. The users
   stay logged in across runs; only the statistics are reset.
3. Offer a fixed arrival rate. Comparing per-request latency between a run that
   served 60 requests a second and one that served 500 compares a quiet machine
   with a busy one.
4. Count a request that failed as a failure, never as a fast success. That one
   happens in `client.py`, because Odoo reports its errors inside a 200.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Type

import gevent
from locust import between, constant_throughput
from locust.env import Environment

from .scenario import Scenario
from .stats import Aggregate, RunSummary, from_locust
from .user import OdooUser
from .workload import Target


@dataclass
class RunConfig:
    users: int = 8
    spawn_rate: float = 0.0  # 0 = all at once
    warmup_seconds: int = 30
    duration_seconds: int = 60
    runs: int = 3
    rate: float = 0.0  # requests per second across all users; 0 = as fast as possible
    seed: int = 1234
    timeout: int = 300


@dataclass
class Connection:
    url: str
    db: str
    login: str
    password: str


def build_user_class(
    scenario: Scenario, target: Target, connection: Connection, config: RunConfig
) -> Type[OdooUser]:
    """One user class per run, because Locust configures users on the class."""

    # `--rate` is a rate for the whole test, which is how a person thinks about
    # it. Locust paces each user separately, so it gets its share.
    wait = (
        constant_throughput(config.rate / max(1, config.users))
        if config.rate
        else between(0.0, 0.0)
    )

    return type(
        "ScenarioUser",
        (OdooUser,),
        {
            "scenario": scenario,
            "target": target,
            "database": connection.db,
            "login_name": connection.login,
            "password": connection.password,
            "wait_time": wait,
            "seed": config.seed,
            "host": connection.url,
        },
    )


def run(
    scenario: Scenario,
    target: Target,
    connection: Connection,
    config: RunConfig,
    on_progress: Optional[Callable[[str], None]] = None,
) -> Aggregate:
    announce = on_progress or (lambda _message: None)
    user_class = build_user_class(scenario, target, connection, config)
    OdooUser._counter = itertools.count()

    environment = Environment(user_classes=[user_class], host=connection.url)
    runner = environment.create_local_runner()
    runner.start(config.users, spawn_rate=config.spawn_rate or config.users)

    try:
        if config.warmup_seconds:
            announce("warming up for %d s" % config.warmup_seconds)
            gevent.sleep(config.warmup_seconds)
            environment.stats.reset_all()

        runs: List[RunSummary] = []
        for index in range(config.runs):
            announce("run %d of %d, %d s" % (index + 1, config.runs, config.duration_seconds))
            started = time.monotonic()
            gevent.sleep(config.duration_seconds)
            summary = from_locust(environment.stats, time.monotonic() - started)
            summary.generator_saturated = bool(getattr(runner, "cpu_warning_emitted", False))
            runner.cpu_warning_emitted = False
            environment.stats.reset_all()
            announce(
                "  %s requests/s, %s failed%s"
                % (
                    summary.rps,
                    summary.errors,
                    "  <- load generator was CPU-bound" if summary.generator_saturated else "",
                )
            )
            runs.append(summary)
    finally:
        runner.quit()

    return Aggregate(runs=runs)
