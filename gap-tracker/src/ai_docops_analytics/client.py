# Copyright Hewlett Packard Enterprise Development LP.
"""Small OpenWebUI client for exporting answer feedback."""

from __future__ import annotations

import requests


class OpenWebUIClient:
    """Authenticate once and export thumbs-up/down feedback."""

    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
        timeout: float = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self._signin(email, password)

    def _signin(self, email: str, password: str) -> None:
        resp = self.session.post(
            f"{self.base_url}/api/v1/auths/signin",
            json={"email": email, "password": password},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        token = resp.json()["token"]
        self.session.headers["Authorization"] = f"Bearer {token}"

    def get_feedbacks_export(self) -> list[dict]:
        resp = self.session.get(
            f"{self.base_url}/api/v1/evaluations/feedbacks/all/export",
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json() or []
