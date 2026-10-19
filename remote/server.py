"""Remote, multi-account, read-only wrapper around upstream gsc_server.py.

Upstream's gsc_server.py is imported untouched so upstream releases merge cleanly.
This module:

* loads one Google login per account from ``GSC_TOKEN_<LABEL>`` env vars
  (authorized-user JSON produced by ``remote/login.py``),
* replaces ``gsc_server.get_gsc_service`` with a router that sends each API call
  to whichever account can see the requested property (``sites.list`` is merged
  across all accounts),
* drops the write/interactive tools, since the logins are read-only,
* serves MCP over Streamable HTTP behind a shared secret, sent either as
  ``Authorization: Bearer <token>`` or as a URL prefix ``/<token>/mcp`` for
  clients that can't send headers.
"""

import hmac
import json
import logging
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gsc_server  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

log = logging.getLogger("gsc-remote")

READONLY_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
TOKEN_ENV_PREFIX = "GSC_TOKEN_"
SITE_MAP_TTL = int(os.environ.get("GSC_SITE_MAP_TTL", "600"))

# Tools that write to GSC or need a local browser. They can't work with
# read-only server-side logins, so they're removed rather than left to fail.
REMOVED_TOOLS = [
    "add_site",
    "delete_site",
    "submit_sitemap",
    "delete_sitemap",
    "manage_sitemaps",
    "reauthenticate",
]

# Resource accessors in the searchconsole v1 client; any other call in the
# chain is the API method itself.
RESOURCES = {"sites", "searchanalytics", "sitemaps", "urlInspection", "index"}

PERMISSION_RANK = {
    "siteOwner": 4,
    "siteFullUser": 3,
    "siteRestrictedUser": 2,
    "siteUnverifiedUser": 1,
}


def load_accounts(environ=None):
    """Return {label: Credentials} from GSC_TOKEN_<LABEL> env vars."""
    environ = os.environ if environ is None else environ
    accounts = {}
    for key in sorted(environ):
        if not key.startswith(TOKEN_ENV_PREFIX) or not environ[key].strip():
            continue
        label = key[len(TOKEN_ENV_PREFIX):].lower()
        try:
            info = json.loads(environ[key])
        except json.JSONDecodeError as e:
            raise ValueError(f"{key} is not valid JSON: {e}") from e
        accounts[label] = Credentials.from_authorized_user_info(info, READONLY_SCOPES)
    return accounts


class AccountRouter:
    """Builds per-account services and maps each property to an account."""

    def __init__(self, accounts):
        self.accounts = accounts
        self._local = threading.local()
        self._lock = threading.Lock()
        self._site_map = {}
        self._site_map_at = 0.0

    def service(self):
        """Drop-in replacement for gsc_server.get_gsc_service()."""
        if not self.accounts:
            raise FileNotFoundError(
                "No Google accounts configured on the server. Run remote/login.py for "
                "each account and set the resulting GSC_TOKEN_<LABEL> variables."
            )
        return _Chain(self, ())

    def service_for_account(self, label):
        # googleapiclient/httplib2 objects aren't thread-safe; upstream fans out
        # URL inspections with asyncio.to_thread, so cache one client per thread.
        cache = self._local.__dict__.setdefault("services", {})
        if label not in cache:
            cache[label] = build(
                "searchconsole", "v1", credentials=self.accounts[label], cache_discovery=False
            )
        return cache[label]

    def list_sites(self, label):
        resp = self.service_for_account(label).sites().list().execute()
        return resp.get("siteEntry", [])

    def merged_sites(self):
        """All properties across accounts, deduped, keeping the best permission."""
        best = {}
        errors = []
        for label in self.accounts:
            try:
                entries = self.list_sites(label)
            except Exception as e:  # one broken login shouldn't hide the others
                log.warning("sites.list failed for account %s: %s", label, e)
                errors.append((label, e))
                continue
            for entry in entries:
                url = entry.get("siteUrl")
                rank = PERMISSION_RANK.get(entry.get("permissionLevel"), 0)
                if url not in best or rank > best[url][0]:
                    best[url] = (rank, label, entry)
        if errors and not best:
            raise errors[0][1]
        with self._lock:
            self._site_map = {url: label for url, (_, label, _) in best.items()}
            self._site_map_at = time.monotonic()
        return [entry for _, _, entry in best.values()]

    def account_for_site(self, site_url):
        with self._lock:
            fresh = time.monotonic() - self._site_map_at < SITE_MAP_TTL
            label = self._site_map.get(site_url)
        if label and fresh:
            return label
        self.merged_sites()
        with self._lock:
            label = self._site_map.get(site_url)
        # Unknown property: let the first account make the call so upstream's
        # normal "site not found" handling kicks in.
        return label or next(iter(self.accounts))


class _Chain:
    """Records service.<resource>().<resource>().<method>(**kwargs)."""

    def __init__(self, router, path):
        self._router = router
        self._path = path

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return _Step(self._router, self._path, name)


class _Step:
    def __init__(self, router, path, name):
        self._router = router
        self._path = path
        self._name = name

    def __call__(self, *args, **kwargs):
        if self._name in RESOURCES and not args and not kwargs:
            return _Chain(self._router, self._path + (self._name,))
        return _Request(self._router, self._path, self._name, kwargs)


class _Request:
    def __init__(self, router, path, method, kwargs):
        self._router = router
        self._path = path
        self._method = method
        self._kwargs = kwargs

    def _site_url(self):
        if "siteUrl" in self._kwargs:
            return self._kwargs["siteUrl"]
        body = self._kwargs.get("body")
        if isinstance(body, dict):
            return body.get("siteUrl")
        return None

    def execute(self, **execute_kwargs):
        site_url = self._site_url()
        if site_url is None:
            if self._path == ("sites",) and self._method == "list":
                return {"siteEntry": self._router.merged_sites()}
            raise ValueError(f"Can't route {'.'.join(self._path)}.{self._method}: no siteUrl")
        label = self._router.account_for_site(site_url)
        target = self._router.service_for_account(label)
        for resource in self._path:
            target = getattr(target, resource)()
        return getattr(target, self._method)(**self._kwargs).execute(**execute_kwargs)


class TokenGate:
    """ASGI middleware: require the shared secret on every request but /health."""

    def __init__(self, app, token, allow_url_token=True):
        self.app = app
        self.token = token.encode()
        self.allow_url_token = allow_url_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        path = scope["path"]
        if path == "/health":
            return await _respond(send, 200, {"status": "ok"})
        # Clients probing for OAuth metadata (e.g. Grok Bot, ChatGPT) should see
        # "no OAuth here" rather than a 401 that implies an OAuth flow exists.
        if path.startswith("/.well-known/"):
            return await _respond(send, 404, {"error": "not_found"})

        if self.allow_url_token:
            first, sep, rest = path.lstrip("/").partition("/")
            if first and hmac.compare_digest(first.encode(), self.token):
                scope = dict(scope)
                scope["path"] = "/" + rest if sep else "/"
                scope["raw_path"] = scope["path"].encode()
                return await self.app(scope, receive, send)

        for name, value in scope.get("headers", []):
            if name == b"authorization":
                scheme, _, presented = value.partition(b" ")
                if scheme.lower() == b"bearer" and hmac.compare_digest(presented.strip(), self.token):
                    return await self.app(scope, receive, send)

        return await _respond(send, 401, {"error": "unauthorized"})


async def _respond(send, status, payload):
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})


def configure(environ=None):
    """Patch upstream for remote use and return the router."""
    router = AccountRouter(load_accounts(environ))
    gsc_server.get_gsc_service = router.service
    for name in REMOVED_TOOLS:
        try:
            gsc_server.mcp.remove_tool(name)
        except Exception:
            pass  # tool renamed/removed upstream
    return router


def build_app(environ=None):
    environ = os.environ if environ is None else environ
    token = environ.get("MCP_AUTH_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("MCP_AUTH_TOKEN must be set to a random string of at least 32 characters.")

    router = configure(environ)
    log.info("Loaded %d Google account(s): %s", len(router.accounts), ", ".join(router.accounts) or "none")

    settings = gsc_server.mcp.settings
    settings.stateless_http = True
    # The token gate replaces the SDK's localhost-only Host check.
    settings.transport_security.enable_dns_rebinding_protection = False

    allow_url_token = environ.get("MCP_ALLOW_URL_TOKEN", "true").lower() in ("true", "1", "yes")
    return TokenGate(gsc_server.mcp.streamable_http_app(), token, allow_url_token)


def main():
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = build_app()
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )


if __name__ == "__main__":
    main()
