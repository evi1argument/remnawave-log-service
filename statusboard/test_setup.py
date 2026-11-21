import unittest
from unittest.mock import patch

import setup_config


class SetupConfigTest(unittest.TestCase):
    def test_uses_loopback_and_detects_enabled_countries(self):
        data = {"response": [
            {"countryCode": "NL", "isDisabled": False, "isConnected": True},
            {"countryCode": "DE", "isDisabled": False, "isConnected": False},
            {"countryCode": "FI", "isDisabled": True, "isConnected": False},
        ]}
        with patch("adapter.panel_token", return_value="fixture"), patch(
            "adapter.http_json", return_value=data
        ) as request:
            result = setup_config.configure("http://127.0.0.1:3000/api/nodes")
        self.assertEqual(result["locations"], {"DE": "DE", "NL": "NL"})
        self.assertIn("/var/lib/", result["public_json"])
        self.assertNotIn("fixture", str(result))
        self.assertEqual(request.call_args.kwargs["headers"]["Authorization"], "Bearer fixture")

    def test_rejects_external_target_before_reading_token(self):
        with patch("adapter.panel_token") as token:
            with self.assertRaises(ValueError):
                setup_config.configure("https://example.invalid/api/nodes")
        token.assert_not_called()

    def test_refuses_empty_or_unusable_panel_response(self):
        with patch("adapter.panel_token", return_value="fixture"):
            for reply in ({"response": []}, {"response": [{"countryCode": "NL", "isDisabled": False}]}):
                with self.subTest(reply=reply), patch("adapter.http_json", return_value=reply):
                    with self.assertRaises(ValueError):
                        setup_config.configure("http://127.0.0.1:3000/api/nodes")
