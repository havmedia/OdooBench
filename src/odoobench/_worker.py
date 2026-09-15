"""A Locust worker process, started by `odoobench run --processes`.

It rebuilds exactly the user class the master built, from a description passed
in the environment, connects to the master and runs users until told to stop.
"""

import itertools
import json
import os
import sys

from locust import runners as locust_runners
from locust.env import Environment

from .runner import REPORT_INTERVAL, Connection, RunConfig, build_user_class, target_from_dict
from .scenario import get as get_scenario
from .user import OdooUser


def main() -> None:
    host, port = sys.argv[1], int(sys.argv[2])
    payload = json.loads(os.environ["ODOOBENCH_WORKER_PAYLOAD"])
    index = int(os.environ.get("ODOOBENCH_WORKER_INDEX", "0"))

    locust_runners.WORKER_REPORT_INTERVAL = REPORT_INTERVAL
    config = RunConfig(**payload["config"])
    user_class = build_user_class(
        get_scenario(payload["scenario"]),
        target_from_dict(payload["target"]),
        Connection(**payload["connection"]),
        config,
    )
    # Every worker draws its users' seeds from its own range, so two workers
    # never replay the same sequence and a run stays repeatable.
    OdooUser._counter = itertools.count((index + 1) * 10000)

    environment = Environment(user_classes=[user_class], host=payload["connection"]["url"])
    worker = environment.create_worker_runner(host, port)
    worker.greenlet.join()


if __name__ == "__main__":
    main()
