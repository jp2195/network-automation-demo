"""Tiny NetBox HTTP client shared by the eventing scripts.

Wraps urllib so callers don't have to repeat headers + URL joining. All
methods raise urllib.error.HTTPError on non-2xx (caller decides whether
to swallow); GET returns the parsed JSON body.
"""

import json
import os
import urllib.parse
import urllib.request


class Client:
    def __init__(self, url=None, token=None, timeout=20):
        self.base = (url or os.environ.get("NETBOX_URL", "")).rstrip("/")
        self.token = token or os.environ.get("NETBOX_TOKEN", "")
        self.timeout = timeout
        self._headers = {
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _req(self, method, path, data=None, **params):
        qs = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(
            self.base + path + qs,
            method=method,
            headers=self._headers,
            data=(json.dumps(data).encode() if data is not None else None),
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read()
            return json.loads(body) if body else None

    def get(self, path, **params):
        return self._req("GET", path, **params)

    def post(self, path, data):
        return self._req("POST", path, data=data)

    def patch(self, path, data):
        return self._req("PATCH", path, data=data)

    def find_id(self, path, **filters):
        """Return the first matched row's id, or None."""
        results = (self.get(path, **filters) or {}).get("results", [])
        return results[0]["id"] if results else None
