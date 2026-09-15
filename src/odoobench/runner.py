"""Driving Locust, and the habits that keep its numbers honest.

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
4. Notice when the generator, not the server, set the pace. One Locust process
   uses one core; past a few hundred requests a second that core is full. Each
   run records whether any generator process went over 90% CPU, and
   `--processes` spreads the users over several of them.
5. Count a request that failed as a failure, never as a fast success. That one
   happens in `client.py`, because Odoo reports its errors inside a 200.
"""

from __future__ import annotations

import itertools
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Type

import gevent
from locust import between, constant_throughput
from locust import runners as locust_runners
from locust.env import Environment

from .scenario import Scenario
from .stats import Aggregate, RunSummary, from_locust
from .user import OdooUser
from .workload import Target

#: How often worker processes send their numbers to the master. Locust's default
#: of three seconds would smear a run's edges across its neighbours.
REPORT_INTERVAL = 1.0


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
    processes: int = 1  # Locust processes generating the load


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


def target_to_dict(target: Target) -> Dict[str, Any]:
    data = asdict(target)
    data["first_date"] = target.first_date.isoformat()
    data["last_date"] = target.last_date.isoformat()
    return data


def target_from_dict(data: Dict[str, Any]) -> Target:
    data = dict(data)
    data["first_date"] = date.fromisoformat(data["first_date"])
    data["last_date"] = date.fromisoformat(data["last_date"])
    return Target(**data)


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

    if config.processes > 1:
        return _run_distributed(scenario, target, connection, config, user_class, announce)

    environment = Environment(user_classes=[user_class], host=connection.url)
    runner = environment.create_local_runner()

    def saturated() -> bool:
        flagged = bool(getattr(runner, "cpu_warning_emitted", False))
        runner.cpu_warning_emitted = False
        return flagged

    runner.start(config.users, spawn_rate=config.spawn_rate or config.users)
    try:
        return _measure(environment, config, announce, saturated)
    finally:
        runner.quit()


def _measure(
    environment: Environment,
    config: RunConfig,
    announce: Callable[[str], None],
    saturated: Callable[[], bool],
) -> Aggregate:
    if config.warmup_seconds:
        announce("warming up for %d s" % config.warmup_seconds)
        gevent.sleep(config.warmup_seconds)
        environment.stats.reset_all()
        saturated()  # a warm-up that strained the generator says nothing about the runs

    runs: List[RunSummary] = []
    for index in range(config.runs):
        announce("run %d of %d, %d s" % (index + 1, config.runs, config.duration_seconds))
        started = time.monotonic()
        gevent.sleep(config.duration_seconds)
        summary = from_locust(environment.stats, time.monotonic() - started)
        summary.generator_saturated = saturated()
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

    return Aggregate(runs=runs)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _run_distributed(
    scenario: Scenario,
    target: Target,
    connection: Connection,
    config: RunConfig,
    user_class: Type[OdooUser],
    announce: Callable[[str], None],
) -> Aggregate:
    """Spread the users over several Locust processes on this machine.

    The master process only collects numbers; each worker process runs users.
    Workers report every second, so a run's window is shifted by up to that
    much, equally for every run and both sides of a comparison.
    """
    locust_runners.WORKER_REPORT_INTERVAL = REPORT_INTERVAL
    environment = Environment(user_classes=[user_class], host=connection.url)
    port = _free_port()
    master = environment.create_master_runner("127.0.0.1", port)

    payload = json.dumps(
        {
            "scenario": scenario.name,
            "target": target_to_dict(target),
            "connection": asdict(connection),
            "config": asdict(config),
        }
    )
    workers = [
        subprocess.Popen(
            [sys.executable, "-m", "odoobench._worker", "127.0.0.1", str(port)],
            env={**os.environ, "ODOOBENCH_WORKER_PAYLOAD": payload, "ODOOBENCH_WORKER_INDEX": str(i)},
        )
        for i in range(config.processes)
    ]

    try:
        announce("starting %d load generator processes" % config.processes)
        deadline = time.monotonic() + 60
        while master.worker_count < config.processes:
            if time.monotonic() > deadline:
                raise SystemExit(
                    "only %d of %d worker processes came up" % (master.worker_count, config.processes)
                )
            gevent.sleep(0.2)

        def saturated() -> bool:
            nodes = list(getattr(master, "clients", {}).values())
            flagged = bool(getattr(master, "worker_cpu_warning_emitted", False)) or any(
                getattr(node, "cpu_warning_emitted", False) for node in nodes
            )
            master.worker_cpu_warning_emitted = False
            for node in nodes:
                node.cpu_warning_emitted = False
            return flagged

        master.start(config.users, spawn_rate=config.spawn_rate or config.users)
        return _measure(environment, config, announce, saturated)
    finally:
        master.quit()
        for process in workers:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
