import io
import unittest
from unittest.mock import Mock, patch
from urllib.request import ProxyHandler

import adapter


class AdapterTest(unittest.TestCase):
    def test_panel_uses_a_direct_non_redirecting_opener(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'{"response":[]}')
        with patch("adapter.build_opener", return_value=opener) as build, patch("adapter.urlopen") as default:
            output = adapter.http_json("http://127.0.0.1:3000/api/nodes", {"Authorization": "Bearer fixture"})
        self.assertEqual(output, {"response": []})
        proxy, redirects = build.call_args.args
        self.assertIsInstance(proxy, ProxyHandler)
        self.assertEqual(proxy.proxies, {})
        self.assertIsNone(redirects.redirect_request(None, None, 301, None, None, "https://example.invalid"))
        self.assertEqual(opener.open.call_args.args[0].get_header("Authorization"), "Bearer fixture")
        default.assert_not_called()
