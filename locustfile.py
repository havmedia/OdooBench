"""Run OdooBench's scenarios in Locust itself, with its web interface.

    locust -f locustfile.py --host http://10.0.0.5:8069 \
           --odoo-db production --odoo-password secret --scenario office-day

Then open http://localhost:8089 and drive it by hand: ramp users up until it
bends, watch the chart, keep it running while somebody else does something to
the server. That is what Locust is good at.

`odoobench run` uses the same scenarios and the same user class, and adds what
Locust does not do: it repeats each configuration, keeps the runs apart, and
refuses to call a difference real when the runs overlap. Use the web interface to
explore and the command to conclude.
"""

from locust import events

from odoobench.client import OdooClient
from odoobench.scenario import get as get_scenario
from odoobench.user import OdooUser
from odoobench.workload import Target, find_document, inspect_analysis, probe, sample_ids


@events.init_command_line_parser.add_listener
def _(parser):
    group = parser.add_argument_group("OdooBench")
    group.add_argument("--odoo-db", required=True, help="database name")
    group.add_argument("--odoo-login", default="admin")
    group.add_argument("--odoo-password", default="", env_var="ODOOBENCH_PASSWORD")
    group.add_argument("--scenario", default="office-day", help="see `odoobench scenarios`")
    group.add_argument("--model", default="res.partner")
    group.add_argument("--analysis", default="", help="analysis model, e.g. sale.report")
    group.add_argument("--document", default="", help="printable document, e.g. account.report_invoice")
    group.add_argument("--months", type=int, default=12)


@events.init.add_listener
def _(environment, **_kwargs):
    """Prepare the scenario once, before any user starts."""
    options = environment.parsed_options
    if not options:
        return

    scenario = get_scenario(options.scenario)
    target = Target(model=options.model)

    lookup = OdooClient.standalone(
        url=environment.host, db=options.odoo_db,
        login=options.odoo_login, password=options.odoo_password,
    )
    lookup.authenticate()

    if options.analysis:
        found = inspect_analysis(lookup, options.analysis)
        target.analysis_model = options.analysis
        target.analysis_date_field = found["date_field"]
        target.analysis_dimension = found["dimension"]
        target.analysis_measures = found["measures"]
        target.analysis_months = options.months
    elif options.document:
        found = find_document(lookup, options.document)
        target.document_name = options.document
        target.document_model = found["model"]
        target.document_ids = sample_ids(lookup, found["model"], limit=200)
    else:
        # Ask the instance which dates its records actually cover, so the
        # filters hit real data instead of an empty window.
        probe(lookup, target)

    ScenarioUser.scenario = scenario
    ScenarioUser.target = target
    ScenarioUser.database = options.odoo_db
    ScenarioUser.login_name = options.odoo_login
    ScenarioUser.password = options.odoo_password

    print(
        "OdooBench scenario %r: %s\n  shows:      %s\n  cannot see: %s"
        % (scenario.name, scenario.summary, scenario.reveals, scenario.blind_to)
    )


class ScenarioUser(OdooUser):
    """Configured in the init listener above, instantiated by Locust."""
