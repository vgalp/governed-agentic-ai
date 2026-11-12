"""Sign in to a FHIR server as a person, once, in the browser (ADR 032). For profiles whose fhir.yaml
uses `auth: smart_user` (clinic-meditech).

    .venv/bin/python scripts/fhir_connect.py --profile clinic-meditech
    .venv/bin/python scripts/fhir_connect.py --profile clinic-meditech --status
    .venv/bin/python scripts/fhir_connect.py --profile clinic-meditech --sign-out

1. Opens the server's sign-in page in your browser (SMART authorization code with PKCE).
2. Waits on this computer for the browser to come back to the redirect URI (localhost only).
3. Trades the one-time code for tokens and keeps them in the token file named in fhir.yaml
   (outside the project, readable by you only). The tool servers refresh the token from there.

Reads the client ID (and secret, if the app has one) from .env. Prints no token and no secret.
"""

import argparse
import http.server
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fhir.client import FhirError, client_from_settings  # noqa: E402
from fhir.smart_user import TokenStore, authorize_url, exchange_code, pkce_pair, smart_configuration  # noqa: E402
from profiles.loader import load_profile  # noqa: E402
from scripts.fhir_check import load_env  # noqa: E402


def wait_for_code(redirect_uri: str, state: str, seconds: int = 300) -> str:
    """Serve the redirect URI on localhost until the browser comes back with a code."""
    u = urllib.parse.urlsplit(redirect_uri)
    if u.hostname not in ("localhost", "127.0.0.1"):
        raise FhirError(f"the redirect URI must be on localhost to sign in from here: {redirect_uri}")
    got: dict = {}

    class Back(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = urllib.parse.urlsplit(self.path)
            if p.path != u.path:
                self.send_response(404)
                self.end_headers()
                return
            q = dict(urllib.parse.parse_qsl(p.query))
            got.update(q)
            ok = "code" in q and q.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(("Signed in. You can close this tab." if ok else
                              "Sign-in did not finish: " + q.get("error_description", q.get("error", "no code"))).encode())

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer((u.hostname, u.port or 80), Back)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        end = time.time() + seconds
        while time.time() < end and not got:
            time.sleep(0.2)
    finally:
        srv.shutdown()
    if not got:
        raise FhirError("no answer from the browser in time")
    if got.get("state") != state:
        raise FhirError("the browser came back with the wrong state; sign-in stopped")
    if "code" not in got:
        raise FhirError(f"sign-in refused: {got.get('error_description') or got.get('error', 'no code')}")
    return got["code"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--profile", default="clinic-meditech")
    ap.add_argument("--status", action="store_true", help="say whether you are signed in, and until when")
    ap.add_argument("--sign-out", action="store_true", help="delete the token file")
    ap.add_argument("--no-browser", action="store_true", help="print the sign-in link instead of opening it")
    args = ap.parse_args()
    load_env(ROOT / ".env")
    m = load_profile(args.profile).fhir_map
    if m is None or m.server.get("auth") != "smart_user":
        print(f"Profile {args.profile} does not sign in as a person (fhir.yaml server.auth is not smart_user)")
        return 1
    try:
        client = client_from_settings(m.server)
    except FhirError as e:
        print(f"✗ {e}")
        return 1
    s, store = client.auth.s, TokenStore(client.auth.s.token_store)

    if args.sign_out:
        store.clear()
        print(f"Signed out: {store.path} deleted.")
        return 0
    if args.status:
        data = store.load()
        if not data:
            print("Not signed in.")
            return 1
        left = data.get("expires_at", 0) - time.time()
        print(f"Signed in. Access token {'valid for %d min' % (left // 60) if left > 0 else 'ended (refreshed on next use)'}; "
              f"refresh token {'kept' if data.get('refresh_token') else 'none'}; "
              f"patient {'named' if data.get('patient') else 'not named'}; scope: {data.get('scope') or 'not stated'}")
        return 0

    try:
        endpoint = s.authorize_url or smart_configuration(m.server["base_url"])["authorization_endpoint"]
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(24)
        link = authorize_url(endpoint, s.client_id, s.redirect_uri, s.scope, m.server["base_url"], state, challenge)
        print("1. Sign in with the account registered for this app (a sandbox account, not a real patient).")
        if args.no_browser or not webbrowser.open(link):
            print("   Open this link in your browser:\n   " + link)
        print(f"2. Waiting for the browser to come back to {s.redirect_uri} …")
        code = wait_for_code(s.redirect_uri, state)
        kept = exchange_code(s, code, verifier)
    except (FhirError, KeyError) as e:
        print(f"   ✗ {e}")
        return 1
    print(f"3. ✓ signed in; tokens kept in {store.path} (owner-only)."
          f" Patient {'named by the server' if kept.get('patient') else 'not named'};"
          f" refresh token {'received' if kept.get('refresh_token') else 'not received (add offline_access)'}.")
    print(f"Next: .venv/bin/python scripts/fhir_check.py --profile {args.profile}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
