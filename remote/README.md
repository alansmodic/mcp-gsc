# Remote multi-account deployment (Railway)

This fork adds a hosted, read-only, multi-account mode on top of upstream
[AminForou/mcp-gsc](https://github.com/AminForou/mcp-gsc). Upstream's
`gsc_server.py` is untouched; everything here lives in `remote/`, `railway.json`
and `.github/workflows/sync-upstream.yml`, so upstream releases merge cleanly.

| Piece | What it does |
|---|---|
| `remote/server.py` | Loads one Google login per account, routes each call to the account that can see the property, drops write tools, serves Streamable HTTP behind a shared secret |
| `remote/login.py` | Run locally once per Google account to create a read-only login |
| `remote/Dockerfile` + `railway.json` | Railway build/deploy config (health check at `/health`) |
| `sync-upstream.yml` | Daily: merge upstream, run all tests, push to `main` only if green (Railway redeploys); opens an issue on failure |

## 1. Google Cloud (once)

1. <https://console.cloud.google.com/projectcreate> → name it `gsc-mcp` → Create.
2. <https://console.cloud.google.com/apis/library/searchconsole.googleapis.com> → **Enable**.
3. <https://console.cloud.google.com/auth/branding> → **Get started**: app name `GSC MCP`,
   support email = you, audience **External**, contact email = you → Create.
4. <https://console.cloud.google.com/auth/audience> → **Publish app** → Confirm.
   (Skipping this makes Google expire every login after 7 days.)
5. <https://console.cloud.google.com/auth/clients> → **Create client** → type **Desktop app**
   → name `gsc-mcp-login` → Create → **Download JSON**.

## 2. Sign in each Google account (once per account)

```bash
uv run python remote/login.py --label personal --client-secrets ~/Downloads/client_secret_XXXX.json
uv run python remote/login.py --label agency   --client-secrets ~/Downloads/client_secret_XXXX.json
uv run python remote/login.py --label clientx  --client-secrets ~/Downloads/client_secret_XXXX.json
```

Pick the right account in the browser, click through "Google hasn't verified this app"
(Advanced → Go to GSC MCP), and allow read-only Search Console access. Each run
saves `remote/tokens/<label>.json` (gitignored) and prints the properties it can see.

## 3. Railway variables

| Variable | Value |
|---|---|
| `MCP_AUTH_TOKEN` | Random secret, ≥32 chars (`python3 -c "import secrets;print(secrets.token_urlsafe(32))"`) |
| `GSC_TOKEN_<LABEL>` | Contents of `remote/tokens/<label>.json`, one per account |
| `MCP_ALLOW_URL_TOKEN` | Optional, default `true`. Set `false` to require the header and disable `/<token>/mcp` |

```bash
railway variables --set "GSC_TOKEN_PERSONAL=$(cat remote/tokens/personal.json)"
```

## 4. Connect clients

Endpoint: `https://<your-app>.up.railway.app/mcp`

- **Header-capable clients** (Craft, Claude Code, Cursor, Codex, Grok/xAI API):
  `Authorization: Bearer <MCP_AUTH_TOKEN>`
- **Clients without custom headers** (ChatGPT connectors, etc.): use
  `https://<your-app>.up.railway.app/<MCP_AUTH_TOKEN>/mcp` with "No authentication".
  The secret appears in the URL, so treat that URL like a password.

```bash
# Claude Code
claude mcp add --transport http gsc https://<your-app>.up.railway.app/mcp \
  --header "Authorization: Bearer $MCP_AUTH_TOKEN"
```

## Operations

- **Add/replace an account:** rerun `login.py` with a label, set its variable. Railway redeploys.
- **Rotate the secret:** change `MCP_AUTH_TOKEN`, update clients.
- **Revoke a login:** <https://myaccount.google.com/permissions> → GSC MCP → Remove access.
- **Tests:** `uv run --with pytest pytest remote/test_server.py -q`
