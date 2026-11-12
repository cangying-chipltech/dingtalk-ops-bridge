from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Sequence


LOGGER = logging.getLogger(__name__)


class NotifierError(Exception):
    """Raised when a DingTalk webhook notification cannot be delivered."""


@dataclass(frozen=True)
class WebhookSettings:
    url: str = ""
    secret: str = ""
    keyword: str = ""
    timeout_seconds: float = 10.0
    at_mobiles: Sequence[str] = field(default_factory=tuple)

    @classmethod
    def from_env(cls) -> "WebhookSettings":
        timeout_raw = os.getenv("DINGTALK_WEBHOOK_TIMEOUT_SECONDS", "10")
        try:
            timeout = float(timeout_raw)
        except ValueError as exc:
            raise NotifierError("DINGTALK_WEBHOOK_TIMEOUT_SECONDS must be a number") from exc
        if timeout <= 0:
            raise NotifierError("DINGTALK_WEBHOOK_TIMEOUT_SECONDS must be greater than zero")

        mobiles = tuple(
            item.strip()
            for item in os.getenv("DINGTALK_ALERT_AT_MOBILES", "").split(",")
            if item.strip()
        )
        return cls(
            url=os.getenv("DINGTALK_WEBHOOK_URL", "").strip(),
            secret=os.getenv("DINGTALK_WEBHOOK_SECRET", "").strip(),
            keyword=os.getenv("DINGTALK_WEBHOOK_KEYWORD", "").strip(),
            timeout_seconds=timeout,
            at_mobiles=mobiles,
        )

    @property
    def configured(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class DingTalkWebhook:
    settings: WebhookSettings
    dry_run: bool = False

    @classmethod
    def from_env(cls, *, dry_run: bool = False) -> "DingTalkWebhook":
        return cls(settings=WebhookSettings.from_env(), dry_run=dry_run)

    def send_markdown(self, title: str, text: str) -> None:
        if not self.settings.configured and not self.dry_run:
            raise NotifierError(
                "DINGTALK_WEBHOOK_URL is not configured; anomaly notifications cannot be sent"
            )

        rendered_title, rendered_text = self._apply_keyword(title, text)
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": rendered_title,
                "text": rendered_text,
            },
        }
        if self.settings.at_mobiles:
            payload["at"] = {
                "atMobiles": list(self.settings.at_mobiles),
                "isAtAll": False,
            }

        if self.dry_run:
            LOGGER.info("webhook dry-run title=%s", rendered_title)
            LOGGER.info("webhook dry-run payload=%s", json.dumps(payload, ensure_ascii=False))
            return

        url = signed_webhook_url(self.settings.url, self.settings.secret)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.settings.timeout_seconds) as response:
                response_body = response.read(65536).decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise NotifierError(f"DingTalk webhook HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise NotifierError(f"DingTalk webhook request failed: {exc}") from exc

        try:
            result = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise NotifierError(f"DingTalk webhook returned invalid JSON: {response_body!r}") from exc
        if int(result.get("errcode", -1)) != 0:
            raise NotifierError(
                f"DingTalk webhook rejected the message: errcode={result.get('errcode')} "
                f"errmsg={result.get('errmsg', '')}"
            )

    def _apply_keyword(self, title: str, text: str) -> tuple[str, str]:
        keyword = self.settings.keyword
        if not keyword:
            return title, text
        combined = f"{title}\n{text}"
        if keyword.lower() in combined.lower():
            return title, text
        return f"{title} [{keyword}]", f"{text}\n\n{keyword}"


def signed_webhook_url(url: str, secret: str, timestamp_ms: int | None = None) -> str:
    if not secret:
        return url
    timestamp = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{secret}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), string_to_sign, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode("utf-8")

    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query.extend([("timestamp", timestamp), ("sign", signature)])
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(query), parsed.fragment)
    )
