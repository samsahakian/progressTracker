#!/usr/bin/env python3
"""Concept2 OAuth helper for the GitHub workflows.

  python3 c2auth.py exchange CODE   one-time: trade the code from the approval redirect for tokens
  python3 c2auth.py refresh         every run: trade the saved refresh token for fresh tokens

Reads C2_CLIENT_ID, C2_CLIENT_SECRET (and C2_REFRESH_TOKEN for refresh) from the environment.
Writes the new access token to $OUT_DIR/access_token and the new refresh token to
$OUT_DIR/refresh_token. Never prints a token.
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOKEN_URL = "https://log.concept2.com/oauth/access_token"
REDIRECT_URI = "http://localhost:8080/callback"
SCOPE = "user:read,results:read"


def post(fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "User-Agent": "c2-progress/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        sys.exit(f"Concept2 refused the token request ({e.code}): {detail}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("exchange", "refresh"):
        sys.exit(__doc__)
    base = {
        "client_id": os.environ["C2_CLIENT_ID"],
        "client_secret": os.environ["C2_CLIENT_SECRET"],
        "scope": SCOPE,
    }
    if sys.argv[1] == "exchange":
        if len(sys.argv) < 3:
            sys.exit("Usage: c2auth.py exchange CODE")
        code = sys.argv[2].strip()
        if "code=" in code:  # accept the whole redirect URL too
            code = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)["code"][0]
        tok = post({**base, "grant_type": "authorization_code", "code": code,
                    "redirect_uri": REDIRECT_URI})
    else:
        rt = os.environ.get("C2_REFRESH_TOKEN")
        if not rt:
            sys.exit("C2_REFRESH_TOKEN is empty. Run the 'Connect Concept2' workflow first.")
        tok = post({**base, "grant_type": "refresh_token", "refresh_token": rt})

    if "access_token" not in tok or "refresh_token" not in tok:
        sys.exit(f"Unexpected token response; keys were {sorted(tok)}")
    out = Path(os.environ.get("OUT_DIR", "."))
    for name in ("access_token", "refresh_token"):
        print(f"::add-mask::{tok[name]}")  # hide from GitHub logs if anything echoes it
        (out / name).write_text(tok[name])
    print(f"Got new tokens (access token valid for {tok.get('expires_in', '?')} s).")


if __name__ == "__main__":
    main()
