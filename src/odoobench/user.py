"""The Locust user every run is made of.

One instance is one logged-in person. It picks an operation from the scenario,
sends it, and lets Locust time it. Everything OdooBench knows about Odoo lives in
the scenario it was given; this class only decides who does what and when.
"""

from __future__ import annotations

import itertools
import random
from typing import List

from locust import HttpUser, between, task

from .client import OdooClient, OdooError
from .scenario import Scenario
from .workload import Operation, Target, weighted


class OdooUser(HttpUser):
    """A logged-in Odoo user working through a scenario.

    `scenario`, `target` and `think_time` are set on the class before a run
    starts, by the runner or by a locustfile. Locust instantiates this class
    itself, so there is nowhere else to put them.
    """

    # Abstract so a locustfile importing this class does not try to run it.
    # Locust makes every subclass concrete unless it says otherwise.
    abstract = True
    scenario: Scenario = None  # type: ignore[assignment]
    target: Target = Target()
    login_name: str = "admin"
    password: str = ""
    database: str = ""
    wait_time = between(0.0, 0.0)
    #: Each user seeds its own generator from this and its position, so both
    #: sides of a comparison draw the same pages and the same filters.
    seed: int = 1234
    _counter = itertools.count()

    def on_start(self) -> None:
        self.odoo = OdooClient(
            self.client,
            url=self.host or "",
            db=self.database,
            login=self.login_name,
            password=self.password,
        )
        # Every user logs in for real. A benchmark that reuses one session
        # measures a warm one and misses what a morning looks like.
        self.odoo.authenticate()
        self.rng = random.Random(self.seed * 1000 + next(OdooUser._counter))
        self.operations: List[Operation] = weighted(self.scenario.operations(self.target))

    @task
    def work(self) -> None:
        operation = self.rng.choice(self.operations)
        try:
            operation.call(self.odoo, self.rng)
        except OdooError:
            # Already counted as a failure by Locust, and deliberately not
            # timed: a request that failed is not a fast request.
            pass
