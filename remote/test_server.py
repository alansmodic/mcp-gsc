"""Tests for the remote wrapper. No credentials or network needed.

Run: uv run --with pytest pytest remote/test_server.py -q
"""

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import server  # noqa: E402

TOKEN = "t" * 40


def fake_token(n):
    return json.dumps({"client_id": f"id{n}", "client_secret": "s", "refresh_token": f"r{n}"})


ENV = {
    "MCP_AUTH_TOKEN": TOKEN,
    "GSC_TOKEN_PERSONAL": fake_token(1),
    "GSC_TOKEN_AGENCY": fake_token(2),
    "UNRELATED": "x",
}


def fake_services():
    """Two accounts: both see shared.com (agency as owner), each has a private site."""
    personal, agency = MagicMock(name="personal"), MagicMock(name="agency")
    personal.sites().list().execute.return_value = {"siteEntry": [
        {"siteUrl": "sc-domain:mine.com", "permissionLevel": "siteOwner"},
        {"siteUrl": "https://shared.com/", "permissionLevel": "siteRestrictedUser"},
    ]}
    agency.sites().list().execute.return_value = {"siteEntry": [
        {"siteUrl": "sc-domain:client.com", "permissionLevel": "siteFullUser"},
        {"siteUrl": "https://shared.com/", "permissionLevel": "siteOwner"},
    ]}
    for svc in (personal, agency):
        svc.searchanalytics().query().execute.return_value = {"rows": [{"keys": [svc._mock_name], "clicks": 1}]}
        svc.urlInspection().index().inspect().execute.return_value = {"inspectionResult": {"owner": svc._mock_name}}
    return {"personal": personal, "agency": agency}


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.router = server.AccountRouter(server.load_accounts(ENV))
        self.services = fake_services()
        patcher = patch.object(server.AccountRouter, "service_for_account", lambda _, label: self.services[label])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_loads_only_token_vars(self):
        self.assertEqual(sorted(self.router.accounts), ["agency", "personal"])
        self.assertEqual(self.router.accounts["agency"].scopes, server.READONLY_SCOPES)

    def test_sites_list_merges_and_keeps_best_permission(self):
        result = self.router.service().sites().list().execute()
        by_url = {e["siteUrl"]: e["permissionLevel"] for e in result["siteEntry"]}
        self.assertEqual(by_url, {
            "sc-domain:mine.com": "siteOwner",
            "sc-domain:client.com": "siteFullUser",
            "https://shared.com/": "siteOwner",
        })

    def test_routes_by_site_url_kwarg(self):
        svc = self.router.service()
        r = svc.searchanalytics().query(siteUrl="sc-domain:client.com", body={}).execute()
        self.assertEqual(r["rows"][0]["keys"], ["agency"])
        r = svc.searchanalytics().query(siteUrl="sc-domain:mine.com", body={}).execute()
        self.assertEqual(r["rows"][0]["keys"], ["personal"])

    def test_shared_site_uses_highest_permission_account(self):
        r = self.router.service().searchanalytics().query(siteUrl="https://shared.com/", body={}).execute()
        self.assertEqual(r["rows"][0]["keys"], ["agency"])

    def test_routes_by_body_site_url(self):
        body = {"siteUrl": "sc-domain:mine.com", "inspectionUrl": "https://mine.com/a"}
        r = self.router.service().urlInspection().index().inspect(body=body).execute()
        self.assertEqual(r["inspectionResult"]["owner"], "personal")

    def test_one_broken_account_does_not_hide_others(self):
        self.services["personal"].sites().list().execute.side_effect = Exception("invalid_grant")
        result = self.router.service().sites().list().execute()
        self.assertEqual({e["siteUrl"] for e in result["siteEntry"]},
                         {"sc-domain:client.com", "https://shared.com/"})

    def test_no_accounts_gives_clear_error(self):
        with self.assertRaises(FileNotFoundError):
            server.AccountRouter({}).service()


class UpstreamIntegrationTests(unittest.TestCase):
    """Upstream tools run unchanged on top of the router."""

    def setUp(self):
        self.services = fake_services()
        patcher = patch.object(server.AccountRouter, "service_for_account", lambda _, label: self.services[label])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.original = server.gsc_server.get_gsc_service
        self.addCleanup(setattr, server.gsc_server, "get_gsc_service", self.original)
        server.configure(ENV)

    def test_write_tools_removed(self):
        names = {t.name for t in asyncio.run(server.gsc_server.mcp.list_tools())}
        for removed in server.REMOVED_TOOLS:
            self.assertNotIn(removed, names)
        self.assertIn("get_search_analytics", names)
        self.assertIn("list_properties", names)

    def test_list_properties_spans_accounts(self):
        out = asyncio.run(server.gsc_server.list_properties())
        for url in ("sc-domain:mine.com", "sc-domain:client.com", "https://shared.com/"):
            self.assertIn(url, out)

    def test_search_analytics_hits_owning_account(self):
        asyncio.run(server.gsc_server.get_search_analytics("sc-domain:client.com", days=7))
        self.assertTrue(self.services["agency"].searchanalytics().query.called)


class TokenGateTests(unittest.TestCase):
    def setUp(self):
        from starlette.testclient import TestClient

        # FastMCP's session manager can only run once; give each test a fresh one.
        server.gsc_server.mcp._session_manager = None
        self.addCleanup(setattr, server.gsc_server.mcp, "_session_manager", None)

        self.original = server.gsc_server.get_gsc_service
        self.addCleanup(setattr, server.gsc_server, "get_gsc_service", self.original)
        self.client_cm = TestClient(server.build_app(ENV))
        self.client = self.client_cm.__enter__()
        self.addCleanup(self.client_cm.__exit__, None, None, None)

    def mcp_post(self, path, headers=None):
        payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}}
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        h.update(headers or {})
        return self.client.post(path, json=payload, headers=h)

    def test_health_is_open(self):
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_oauth_discovery_is_404_not_401(self):
        self.assertEqual(self.client.get("/.well-known/oauth-protected-resource").status_code, 404)
        self.assertEqual(self.client.get("/.well-known/oauth-authorization-server").status_code, 404)

    def test_rejects_missing_or_wrong_token(self):
        self.assertEqual(self.mcp_post("/mcp").status_code, 401)
        self.assertEqual(self.mcp_post("/mcp", {"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.mcp_post("/wrong/mcp").status_code, 401)

    def test_bearer_header(self):
        r = self.mcp_post("/mcp", {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("gsc-server", r.text)

    def test_url_token(self):
        r = self.mcp_post(f"/{TOKEN}/mcp")
        self.assertEqual(r.status_code, 200, r.text)

    def test_url_token_can_be_disabled(self):
        server.gsc_server.mcp._session_manager = None
        app = server.build_app({**ENV, "MCP_ALLOW_URL_TOKEN": "false"})
        from starlette.testclient import TestClient
        with TestClient(app) as c:
            r = c.post(f"/{TOKEN}/mcp", json={}, headers={"Accept": "application/json, text/event-stream"})
        self.assertEqual(r.status_code, 401)

    def test_short_token_refused(self):
        with self.assertRaises(SystemExit):
            server.build_app({**ENV, "MCP_AUTH_TOKEN": "short"})


if __name__ == "__main__":
    unittest.main()
