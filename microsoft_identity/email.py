from __future__ import annotations

import csv
import html
import io
import json

import requests

from .service import MicrosoftAuthenticator
from .storage import EmailStore

GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"


def _parse_recipients(value: str) -> list[tuple[str, str]]:
    result = []
    for row in csv.reader(io.StringIO(value)):
        if not row or not any(x.strip() for x in row): continue
        name, address = ("", row[0].strip()) if len(row) == 1 else (row[0].strip(), row[1].strip())
        if "@" not in address or any(x in address for x in "\r\n"): raise ValueError(f"Invalid recipient: {address}")
        result.append((name, address))
    if not 1 <= len(result) <= 100: raise ValueError("Enter between 1 and 100 recipients.")
    return result


def send_bulk(store: EmailStore, auth: MicrosoftAuthenticator, account_id: int, template: dict[str, object], recipients: list[tuple[str, str]]) -> tuple[int, int]:
    account = store.account(account_id)
    if not account: raise RuntimeError("Sender account was not found.")
    token, sent, failed = auth.access_token(account), 0, 0
    for name, address in recipients:
        subject = str(template["subject"]).replace("{{name}}", name).replace("{{email}}", address)
        body = str(template["body"]).replace("{{name}}", name).replace("{{email}}", address)
        payload = {"message": {"subject": subject, "body": {"contentType": "HTML", "content": html.escape(body).replace("\n", "<br>")}, "toRecipients": [{"emailAddress": {"address": address}}]}, "saveToSentItems": True}
        try:
            response = requests.post(GRAPH_SEND_URL, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, data=json.dumps(payload), timeout=30)
            response.raise_for_status(); store.delivery(account_id, address, subject, "sent"); sent += 1
        except requests.RequestException as exc:
            store.delivery(account_id, address, subject, "failed", str(exc)); failed += 1
    return sent, failed



