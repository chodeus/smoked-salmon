"""What this fork's salmon web adds to upstream's: each thread's stack with --dev."""

import threading

from aiohttp.test_utils import TestClient
from test_webui_server import TOKEN, _with_client  # pyright: ignore[reportMissingImports]

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_dev_mode_lists_each_threads_stack() -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/debug/threads", headers=AUTH)
        assert response.status == 200
        stacks = await response.json()
        assert threading.current_thread().name in stacks
        assert any("test_dev_mode_lists_each_threads_stack" in line for line in stacks[threading.current_thread().name])

    _with_client(test, dev=True)


def test_the_thread_stacks_are_not_served_without_dev_mode() -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/debug/threads", headers=AUTH)
        assert response.status == 404

    _with_client(test)


def test_the_thread_stacks_need_the_token() -> None:
    async def test(client: TestClient) -> None:
        response = await client.get("/api/debug/threads")
        assert response.status == 401

    _with_client(test, dev=True)
