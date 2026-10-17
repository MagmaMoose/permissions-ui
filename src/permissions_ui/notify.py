"""Telling people something needs them: a card in a Teams channel, through a workflow's webhook.

Best effort. A notification that fails is logged and the request carries on: the request page
and the pull request still show everything.
"""

from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger("permissions_ui")


class Notifier:
    def __init__(self, http: httpx.AsyncClient, webhook: str):
        self._http = http
        self._webhook = webhook

    async def send(self, title: str, facts: dict[str, str], link: str, action: str = "Open") -> None:
        if not self._webhook:
            return
        card = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": [
                {"type": "TextBlock", "text": title, "weight": "Bolder", "wrap": True},
                {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts.items() if v]},
            ],
            "actions": [{"type": "Action.OpenUrl", "title": action, "url": link}],
        }
        payload = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}
        try:
            response = await self._http.post(self._webhook, json=payload, timeout=10)
            if response.status_code >= 300:
                log.info(json.dumps({"msg": "notification refused", "status": response.status_code}))
        except httpx.HTTPError as err:
            log.info(json.dumps({"msg": "notification failed", "reason": err.__class__.__name__}))
