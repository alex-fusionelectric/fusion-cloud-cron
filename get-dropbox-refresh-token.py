"""One-time helper to mint a Dropbox refresh token for the Fusion Cloud
Cron app. Run this locally; the resulting refresh token gets stored as the
DROPBOX_REFRESH_TOKEN GitHub Actions secret and never expires (until the
user revokes the app authorization).

Prereqs:
  1. Create a Dropbox app at https://www.dropbox.com/developers/apps
     - "Scoped access"
     - Access type: "Full Dropbox"
     - Name: e.g. "Fusion Cloud Cron"
  2. On the Permissions tab, check `files.metadata.read` + `files.content.read`,
     then click "Submit".
  3. On the Settings tab, copy the App key and App secret.

Usage:
  python get-dropbox-refresh-token.py YOUR_APP_KEY YOUR_APP_SECRET

The script prints an authorize URL. Open it in any browser, sign in, click
Allow, copy the resulting authorization code, paste it back into the
terminal. The script exchanges the code for a refresh token and prints it.

Refresh tokens are long-lived. Save the printed value into:
  GitHub Secrets -> alex-fusionelectric/fusion-cloud-cron -> DROPBOX_REFRESH_TOKEN
"""

import json
import sys
import urllib.parse
import urllib.request


def main():
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    app_key, app_secret = sys.argv[1], sys.argv[2]

    auth_params = {
        "client_id": app_key,
        "response_type": "code",
        "token_access_type": "offline",  # gives us a refresh token
    }
    authorize_url = "https://www.dropbox.com/oauth2/authorize?" + urllib.parse.urlencode(auth_params)
    print()
    print("=" * 78)
    print("STEP 1: Open this URL in any browser, sign in, click 'Allow':")
    print()
    print("  " + authorize_url)
    print()
    print("=" * 78)
    print()
    code = input("STEP 2: Paste the authorization code Dropbox shows you, then Enter: ").strip()
    if not code:
        sys.exit("no code provided.")

    body = urllib.parse.urlencode({
        "code": code,
        "grant_type": "authorization_code",
        "client_id": app_key,
        "client_secret": app_secret,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.dropboxapi.com/oauth2/token",
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(f"token exchange failed: HTTP {e.code}\n{body}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"token exchange failed: {e}")

    refresh = data.get("refresh_token")
    if not refresh:
        sys.exit(f"no refresh_token in response: {data}")

    print()
    print("=" * 78)
    print("SUCCESS — refresh token below. Save as GitHub secret DROPBOX_REFRESH_TOKEN:")
    print()
    print("  " + refresh)
    print()
    print("=" * 78)
    print()
    print("Also save these as GitHub secrets (so the Actions workflow can refresh access tokens):")
    print(f"  DROPBOX_APP_KEY    = {app_key}")
    print(f"  DROPBOX_APP_SECRET = {app_secret}")
    print()


if __name__ == "__main__":
    main()
