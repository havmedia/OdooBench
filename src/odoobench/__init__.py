"""OdooBench: measure an Odoo the way its own web client uses it.

Locust imports first and on purpose: it patches the standard library for
cooperative networking, and anything imported before it would keep the blocking
version and quietly serialise the whole test.
"""

import locust as _locust  # noqa: F401  isort:skip

__version__ = "0.2.0"

from .client import OdooClient, OdooError  # noqa: E402
from .runner import Connection, RunConfig, run  # noqa: E402
from .scenario import SCENARIOS, Scenario  # noqa: E402
from .user import OdooUser  # noqa: E402
from .workload import Target  # noqa: E402

__all__ = [
    "OdooClient",
    "OdooError",
    "Connection",
    "RunConfig",
    "run",
    "SCENARIOS",
    "Scenario",
    "OdooUser",
    "Target",
]
