"""Sign in to one Google account and save a read-only login for the remote server.

Run once per Google account, on your own machine (a browser window opens):

    uv run python remote/login.py --label personal --client-secrets ~/Downloads/client_secret_XXXX.json

It writes remote/tokens/<label>.json (gitignored), lists the properties that
account can see, and prints the Railway command that stores it as
GSC_TOKEN_<LABEL>.
"""

import argparse
import json
import os
import re
import sys

from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

READONLY_SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--label", required=True, help="Short name for this account, e.g. personal, agency, clientx")
    parser.add_argument("--client-secrets", required=True, help="OAuth client JSON downloaded from Google Cloud")
    args = parser.parse_args()

    label = args.label.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]+", label):
        sys.exit("--label may only contain letters, numbers and underscores.")

    flow = InstalledAppFlow.from_client_secrets_file(os.path.expanduser(args.client_secrets), READONLY_SCOPES)
    # prompt=consent guarantees Google returns a refresh token;
    # select_account lets you pick a different account each run.
    creds = flow.run_local_server(port=0, prompt="consent select_account", access_type="offline")
    if not creds.refresh_token:
        sys.exit("Google didn't return a refresh token. Remove the app at myaccount.google.com/permissions and retry.")

    sites = build("searchconsole", "v1", credentials=creds, cache_discovery=False).sites().list().execute()
    entries = sites.get("siteEntry", [])

    os.makedirs(os.path.join(HERE, "tokens"), exist_ok=True)
    path = os.path.join(HERE, "tokens", f"{label}.json")
    with open(path, "w") as f:
        f.write(creds.to_json())
    os.chmod(path, 0o600)

    print(f"\nSaved login for '{label}' -> {path}")
    print(f"This account can see {len(entries)} Search Console properties:")
    for entry in sorted(entries, key=lambda e: e["siteUrl"]):
        print(f"  {entry['siteUrl']}  ({entry.get('permissionLevel')})")

    var = f"GSC_TOKEN_{label.upper()}"
    compact = json.dumps(json.loads(creds.to_json()), separators=(",", ":"))
    print(f"\nAdd it to Railway (from this repo folder, after `railway link`):")
    print(f"  railway variables --set \"{var}=$(cat {os.path.relpath(path)})\"")
    print(f"\nOr paste this as the value of {var} in the Railway dashboard:\n{compact}")


if __name__ == "__main__":
    main()
