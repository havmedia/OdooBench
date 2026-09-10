import pytest

from fake_odoo import FakeOdoo
from locutus.rpc import RpcError, Session


def test_authenticates_and_keeps_the_session():
    with FakeOdoo() as odoo:
        session = Session(odoo.url, "demo", "admin", "secret")
        assert session.authenticate() == 2
        assert odoo.calls[0]["path"] == "/web/session/authenticate"


def test_a_wrong_password_is_an_error_not_an_empty_result():
    with FakeOdoo() as odoo:
        session = Session(odoo.url, "demo", "admin", "wrong")
        with pytest.raises(RpcError, match="authentication failed"):
            session.authenticate()


def test_search_read_sends_what_the_web_client_sends():
    with FakeOdoo() as odoo:
        session = Session(odoo.url, "demo", "admin", "secret")
        session.authenticate()
        session.search_read(
            "res.partner", [["active", "=", True]], ["display_name"], limit=80, offset=160,
            order="complete_name asc, id desc",
        )

    payload = odoo.call_kw_payloads()[0]
    assert payload["model"] == "res.partner"
    assert payload["method"] == "search_read"
    assert payload["args"] == [[["active", "=", True]], ["display_name"]]
    # The page size, the offset and the model's own order are what make this a
    # list view rather than a query somebody invented for a benchmark.
    assert payload["kwargs"]["limit"] == 80
    assert payload["kwargs"]["offset"] == 160
    assert payload["kwargs"]["order"] == "complete_name asc, id desc"


def test_an_error_inside_a_200_response_is_raised():
    with FakeOdoo(fail_every=1) as odoo:
        session = Session(odoo.url, "demo", "admin", "secret")
        session.authenticate()
        with pytest.raises(RpcError, match="busy"):
            session.search_count("res.partner", [])


def test_an_unreachable_server_is_an_rpc_error_not_a_traceback():
    session = Session("http://127.0.0.1:1", "demo", "admin", "secret", timeout=1)
    with pytest.raises(RpcError):
        session.authenticate()
