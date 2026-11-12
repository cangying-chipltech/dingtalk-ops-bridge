from __future__ import annotations

import base64
import hashlib
import hmac
import unittest
import urllib.parse

from dingtalk_ops_bridge.notifier import DingTalkWebhook, WebhookSettings, signed_webhook_url


class NotifierTests(unittest.TestCase):
    def test_signed_url(self):
        timestamp = 1700000000000
        secret = "SEC-test-secret"
        url = signed_webhook_url(
            "https://oapi.dingtalk.com/robot/send?access_token=token",
            secret,
            timestamp_ms=timestamp,
        )
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertEqual(query["access_token"], "token")
        self.assertEqual(query["timestamp"], str(timestamp))
        expected = base64.b64encode(
            hmac.new(
                secret.encode("utf-8"),
                f"{timestamp}\n{secret}".encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")
        self.assertEqual(query["sign"], expected)

    def test_keyword_is_added_once(self):
        webhook = DingTalkWebhook(
            settings=WebhookSettings(url="https://example.com", keyword="OPS"),
        )
        title, text = webhook._apply_keyword("Alert", "body")
        self.assertIn("OPS", title)
        self.assertIn("OPS", text)
        second_title, second_text = webhook._apply_keyword(title, text)
        self.assertEqual((second_title, second_text), (title, text))

    def test_dry_run_allows_missing_webhook_url(self):
        webhook = DingTalkWebhook(settings=WebhookSettings(), dry_run=True)
        webhook.send_markdown("title", "text")


if __name__ == "__main__":
    unittest.main()
