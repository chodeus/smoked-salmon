"""A TestClient for salmon web that sends the token and JSON, as the browser does once logged in."""

from typing import Any

from fastapi.testclient import TestClient

from salmon.webui.app import create_app

WEB_TOKEN = "test-web-token-0001"


def web_client(base_url: str = "http://localhost", **app_options: Any) -> TestClient:
    headers = {"Authorization": f"Bearer {WEB_TOKEN}", "Content-Type": "application/json"}
    return TestClient(create_app(auth_token=WEB_TOKEN, **app_options), base_url=base_url, headers=headers)
