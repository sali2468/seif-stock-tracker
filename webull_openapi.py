"""webull_openapi.py — minimal, self-contained client for the Webull OpenAPI.

Webull's official `webull-python-sdk` vendors an ancient requests/urllib3 that is
broken on Python 3.12 (its `six.moves` shim fails to import) and pins a grpcio with
no 3.12 wheel. Rather than depend on it, we implement Webull's documented HMAC-SHA1
request signing here against the standard `requests` library.

The signing is byte-for-byte identical to the SDK's
`webullsdkcore.auth.composer.default_signature_composer.calc_signature`
(validated in tests). Only the handful of v1 trade endpoints the app needs are wired.
"""

import base64
import hashlib
import hmac
import json
import socket
import uuid
from datetime import datetime
from urllib.parse import quote

import requests

# Region → trade API host (from the SDK's data/endpoints.json).
HOSTS = {"us": "api.webull.com", "hk": "api.webull.hk", "jp": "api.webull.co.jp"}


def _iso_now() -> str:
    # matches common.get_iso_8601_date()
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _nonce() -> str:
    # matches common.get_uuid()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, socket.gethostname() + str(uuid.uuid1())))


def _md5_hex_upper(raw: str) -> str:
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def _signature(app_secret: str, string_to_sign: str) -> str:
    # HMAC-SHA1 with key = app_secret + "&", base64-encoded (sha_hmac1.get_sign_string)
    h = hmac.new((app_secret + "&").encode("utf-8"),
                 string_to_sign.encode("utf-8"), hashlib.sha1)
    return base64.standard_b64encode(h.digest()).decode("utf-8").strip()


def _sign_headers(app_key: str, app_secret: str, host: str,
                  uri: str, params: dict, body_params=None) -> dict:
    """Reproduce default_signature_composer.calc_signature and return the request
    headers to send (the signature is computed over the sign-headers + query params
    + optional body md5, exactly as the SDK does)."""
    sign_headers = {
        "x-app-key":             app_key,
        "x-timestamp":           _iso_now(),
        "x-signature-version":   "1.0",
        "x-signature-algorithm": "HMAC-SHA1",
        "x-signature-nonce":     _nonce(),
    }
    # These five go on the wire; "Host" participates in the signature only (requests
    # sets the Host header itself).
    req_headers = dict(sign_headers)

    sign_params = {k.lower(): v for k, v in sign_headers.items()}
    sign_params["host"] = host
    for k, v in (params or {}).items():
        if k in sign_params:
            sign_params[k] = str(sign_params[k]) + "&" + str(v)
        else:
            sign_params[k] = str(v)

    body_string = None
    if body_params is not None:
        raw = json.dumps(body_params, ensure_ascii=False, separators=(",", ":"))
        body_string = _md5_hex_upper(raw)

    sorted_arr = [f"{k}={sign_params[k]}" for k in sorted(sign_params)]
    # matches _build_sign_string: with a uri, join params by '&'; the no-uri
    # fallback (never hit here — every endpoint has a path) joins by '='.
    string_to_sign = uri + "&" + "&".join(sorted_arr) if uri else "=".join(sorted_arr)
    if body_string:
        string_to_sign += "&" + body_string
    string_to_sign = quote(string_to_sign, safe="")

    req_headers["x-signature"] = _signature(app_secret, string_to_sign)
    return req_headers


class WebullOpenAPI:
    """Signed client for the Webull OpenAPI trade endpoints (v1)."""

    def __init__(self, app_key: str, app_secret: str, region: str = "us"):
        self.app_key = app_key
        self.app_secret = app_secret
        self.host = HOSTS.get((region or "us").lower(), HOSTS["us"])

    def _get(self, uri: str, params: dict = None, timeout: int = 10) -> requests.Response:
        params = {k: str(v) for k, v in (params or {}).items()}
        headers = _sign_headers(self.app_key, self.app_secret, self.host, uri, params)
        headers["x-version"] = "v1"
        headers["Accept-Encoding"] = "gzip"
        return requests.get(f"https://{self.host}{uri}", params=params,
                            headers=headers, timeout=timeout)

    # ── Endpoints ─────────────────────────────────────────────────────────────
    def list_accounts(self) -> requests.Response:
        return self._get("/app/subscriptions/list")

    def get_balance(self, account_id, currency: str = "USD") -> requests.Response:
        return self._get("/account/balance",
                         {"account_id": account_id, "total_asset_currency": currency})

    def get_positions(self, account_id, page_size: int = 100) -> requests.Response:
        return self._get("/account/positions",
                         {"account_id": account_id, "page_size": page_size})

    def ping(self):
        """Return (ok, message). Validates credentials via the subscriptions list."""
        try:
            r = self.list_accounts()
        except Exception as e:
            return False, str(e)
        if r.status_code == 200:
            return True, "Connected"
        try:
            j = r.json()
            msg = j.get("msg") or j.get("message") or (r.text or "")[:120]
        except Exception:
            msg = (r.text or "")[:120]
        return False, f"{r.status_code}: {msg}"
