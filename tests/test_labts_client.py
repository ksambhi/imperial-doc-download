"""Offline tests for `imperial_doc_download.labts_fetch.client`.

All network interaction is faked with `httpx.MockTransport` -- this suite
must never hit the real LabTS site (there are no credentials in CI, and it
shouldn't be hammered anyway).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from imperial_doc_download.labts_fetch.client import LabtsAuthError, LabtsClient

SIGN_IN_HTML = """<html><body>
<form id="new_user" action="/labts/users/sign_in" method="post">
<input type="hidden" name="authenticity_token" value="test-token"/>
</form>
</body></html>"""

HOME_HTML = """<html><body>
<select id="switch_academic_year"><option value="2324">2324</option></select>
</body></html>"""


def _make_transport(*, login_succeeds: bool = True, session_expired: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if request.method == "GET" and path == "/labts/users/sign_in":
            return httpx.Response(200, text=SIGN_IN_HTML)

        if request.method == "POST" and path == "/labts/users/sign_in":
            if login_succeeds:
                return httpx.Response(302, headers={"Location": "/labts"})
            return httpx.Response(302, headers={"Location": "/labts/users/sign_in"})

        if path == "/labts/expired":
            # Simulates an authenticated-looking URL that silently bounces
            # back to sign-in -- LabTS returns 200 for this, not a 4xx.
            return httpx.Response(302, headers={"Location": "/labts/users/sign_in"})

        if path in ("/labts", "/labts/home/index/2324"):
            return httpx.Response(200, text=HOME_HTML)

        return httpx.Response(404, text="not found")

    return httpx.MockTransport(handler)


def _client(**transport_kwargs) -> LabtsClient:
    return LabtsClient(
        "jbloggs",
        "hunter2",
        delay=0,
        base_url="https://teaching.doc.ic.ac.uk",
        transport=_make_transport(**transport_kwargs),
    )


def test_requires_both_username_and_password() -> None:
    with pytest.raises(ValueError):
        LabtsClient(None, "hunter2")
    with pytest.raises(ValueError):
        LabtsClient("jbloggs", None)


def test_get_logs_in_lazily_then_fetches_the_page() -> None:
    client = _client()
    html = client.get_html("/labts/home/index/2324")

    assert "switch_academic_year" in html


def test_login_failure_raises_auth_error() -> None:
    client = _client(login_succeeds=False)

    with pytest.raises(LabtsAuthError):
        client.get_html("/labts/home/index/2324")


def test_session_expiry_is_detected_via_final_url_not_status_code() -> None:
    client = _client()
    # A first, successful call logs us in.
    client.get_html("/labts")

    with pytest.raises(LabtsAuthError):
        client.get_html("/labts/expired")


def test_login_happens_only_once_across_multiple_get_calls() -> None:
    login_posts = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/labts/users/sign_in":
            return httpx.Response(200, text=SIGN_IN_HTML)
        if request.method == "POST" and path == "/labts/users/sign_in":
            login_posts.append(request)
            return httpx.Response(302, headers={"Location": "/labts"})
        return httpx.Response(200, text=HOME_HTML)

    client = LabtsClient(
        "jbloggs",
        "hunter2",
        delay=0,
        base_url="https://teaching.doc.ic.ac.uk",
        transport=httpx.MockTransport(handler),
    )

    client.get_html("/labts/home/index/2324")
    client.get_html("/labts/home/index/2223")

    assert len(login_posts) == 1


def test_retries_transport_errors_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    import imperial_doc_download.labts_fetch.client as client_module

    sleeps: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: sleeps.append(s))

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/labts/users/sign_in":
            return httpx.Response(200, text=SIGN_IN_HTML)
        if request.method == "POST" and path == "/labts/users/sign_in":
            return httpx.Response(302, headers={"Location": "/labts"})
        if path == "/labts/flaky":
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise httpx.ConnectError("connection reset", request=request)
            return httpx.Response(200, text=HOME_HTML)
        return httpx.Response(200, text=HOME_HTML)

    client = LabtsClient(
        "jbloggs",
        "hunter2",
        delay=0,
        base_url="https://teaching.doc.ic.ac.uk",
        transport=httpx.MockTransport(handler),
    )

    html = client.get_html("/labts/flaky")

    assert "switch_academic_year" in html
    assert attempts["count"] == 3
    # Two failed attempts before success -> two backoff sleeps recorded
    # (plus the configured delay=0 sleeps, which are no-ops here anyway).
    assert sleeps.count(2.0) == 1
    assert sleeps.count(4.0) == 1


def test_exhausting_all_retries_raises_the_underlying_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imperial_doc_download.labts_fetch.client as client_module

    monkeypatch.setattr(client_module.time, "sleep", lambda s: None)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/labts/users/sign_in":
            return httpx.Response(200, text=SIGN_IN_HTML)
        if request.method == "POST" and path == "/labts/users/sign_in":
            return httpx.Response(302, headers={"Location": "/labts"})
        raise httpx.ConnectError("connection reset", request=request)

    client = LabtsClient(
        "jbloggs",
        "hunter2",
        delay=0,
        base_url="https://teaching.doc.ic.ac.uk",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.ConnectError):
        client.get_html("/labts/always-fails")


# --- on-disk page cache ---------------------------------------------------


def _counting_client(tmp_path: Path, calls: list[str]) -> LabtsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/labts/users/sign_in":
            if request.method == "POST":
                return httpx.Response(302, headers={"Location": "/labts"})
            return httpx.Response(200, text=SIGN_IN_HTML)
        # Only count real page fetches, not the post-login redirect to /labts.
        if path.startswith("/labts/home/"):
            calls.append(path)
        return httpx.Response(200, text=HOME_HTML)

    return LabtsClient(
        "jbloggs",
        "hunter2",
        delay=0,
        base_url="https://teaching.doc.ic.ac.uk",
        transport=httpx.MockTransport(handler),
        cache_dir=tmp_path / "cache",
    )


def test_second_fetch_of_the_same_url_is_served_from_cache(tmp_path: Path) -> None:
    calls: list[str] = []
    client = _counting_client(tmp_path, calls)

    first = client.get_html("/labts/home/index/2324")
    second = client.get_html("/labts/home/index/2324")

    assert first == second
    assert calls == ["/labts/home/index/2324"]  # fetched exactly once
    assert client.cache_hits == 1
    assert client.cache_misses == 1


def test_cache_persists_across_clients_and_avoids_login_entirely(tmp_path: Path) -> None:
    calls: list[str] = []
    _counting_client(tmp_path, calls).get_html("/labts/home/index/2324")
    assert calls == ["/labts/home/index/2324"]

    # A fresh client pointed at the same cache must not touch the network at
    # all -- not even to log in.
    def exploding_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    cached_client = LabtsClient(
        "jbloggs",
        "hunter2",
        delay=0,
        base_url="https://teaching.doc.ic.ac.uk",
        transport=httpx.MockTransport(exploding_handler),
        cache_dir=tmp_path / "cache",
    )
    assert "switch_academic_year" in cached_client.get_html("/labts/home/index/2324")
    assert cached_client.cache_hits == 1


def test_no_cache_dir_means_every_fetch_hits_the_network(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/labts/users/sign_in":
            if request.method == "POST":
                return httpx.Response(302, headers={"Location": "/labts"})
            return httpx.Response(200, text=SIGN_IN_HTML)
        if path.startswith("/labts/home/"):
            calls.append(path)
        return httpx.Response(200, text=HOME_HTML)

    client = LabtsClient(
        "jbloggs",
        "hunter2",
        delay=0,
        base_url="https://teaching.doc.ic.ac.uk",
        transport=httpx.MockTransport(handler),
    )
    client.get_html("/labts/home/index/2324")
    client.get_html("/labts/home/index/2324")

    assert len(calls) == 2


def test_relative_and_absolute_urls_share_one_cache_entry(tmp_path: Path) -> None:
    calls: list[str] = []
    client = _counting_client(tmp_path, calls)

    client.get_html("/labts/home/index/2324")
    client.get_html("https://teaching.doc.ic.ac.uk/labts/home/index/2324")

    assert len(calls) == 1
    assert client.cache_hits == 1
