"""The tests' network guard (conftest, #53) lets through loopback and the test's own services: the database
TEST_ADMIN_URL names and VALKEY_URL's host, which CI or a developer may run on another machine."""
import socket
import sys

import pytest

conftest = sys.modules["conftest"]  # the one pytest loaded, whose guard is the live one


def test_the_service_hosts_come_from_the_admin_and_valkey_urls():
    urls = ["postgresql+asyncpg://jobfinder:pw@db.ci.internal:5432/postgres", "redis://valkey:6379/0",
            "redis://localhost:6380", None]
    assert conftest._service_hosts(urls) == {"db.ci.internal", "valkey"}


def test_a_service_host_is_let_through_by_name_and_by_address():
    def resolve(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 0))]

    allowed = conftest._addresses({"db.ci.internal"}, resolve)
    assert allowed == {"db.ci.internal", "10.0.0.7"}
    assert conftest._permitted("db.ci.internal", allowed) and conftest._permitted("10.0.0.7", allowed)
    assert conftest._permitted("127.0.0.1", allowed) and conftest._permitted(b"localhost", allowed)
    assert not conftest._permitted("10.0.0.8", allowed) and not conftest._permitted("example.com", allowed)


def test_an_unresolvable_service_host_is_kept_by_name():
    def resolve(host, port):
        raise socket.gaierror("no such host")

    assert conftest._addresses({"db.ci.internal"}, resolve) == {"db.ci.internal"}


def test_any_other_host_is_still_refused():
    with pytest.raises(RuntimeError, match="must not reach the network") as refused:
        socket.getaddrinfo("example.com", 443)
    assert refused.type is conftest.RealNetworkCall
    conftest._blocked.clear()  # expected here: don't fail the test for it
