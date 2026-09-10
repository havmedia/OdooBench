"""OdooBench: measure an Odoo the way its own web client uses it."""

__version__ = "0.1.0"

from .rpc import RpcError, Session
from .runner import RunConfig, run
from .scenario import SCENARIOS, Scenario
from .workload import Target

__all__ = ["RpcError", "Session", "RunConfig", "run", "SCENARIOS", "Scenario", "Target"]
