"""Local Remnawave adapter for the optional status board."""

import json
import os
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def panel_token():
    return os.environ["REMNAWAVE_API_TOKEN"]


def http_json(url, headers=None, data=None, timeout=12):
    request = Request(url, headers=headers or {}, data=data)
    if urlsplit(url).hostname in ("127.0.0.1", "localhost", "::1"):
        open_request = build_opener(ProxyHandler({}), NoRedirect()).open
    else:
        open_request = urlopen
    with open_request(request, timeout=timeout) as response:
        return json.load(response)

