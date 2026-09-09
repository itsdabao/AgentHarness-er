import json
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from fakes import FakeLLMProvider
from minder_harness.core import ModelResponse, Session
from minder_harness.core.models import as_jsonable
from minder_harness.persistence import SQLiteExecutionStore
from minder_harness.persistence.schema import MIGRATIONS
from minder_harness.rpc import create_app
from test_rpc import rpc, service_for, terminal

pytestmark = pytest.mark.anyio


async def test_session_picker_pages_summary_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "sessions.db"
    provider = FakeLLMProvider([ModelResponse(content="done"), ModelResponse(content="again")])
    app = create_app(lambda: service_for(path, provider))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as http:
            empty = (await rpc(http, "list_sessions"))["result"]["data"]
            assert empty == {"sessions": [], "has_more": False, "next_before": None}
            ids = []
            for _ in range(5):
                ids.append((await rpc(http, "create_session"))["result"]["data"]["session_id"])
            first = (await rpc(http, "list_sessions", limit=2))["result"]["data"]
            assert [s["session_id"] for s in first["sessions"]] == ids[::-1][:2]
            assert first["has_more"]
            assert first["sessions"][0]["latest_run_id"] is None
            # Insertion and activity must not shift the older-page cursor.
            await rpc(http, "create_session")
            task = "Check   CNC-04\nstatus"
            run = (await rpc(http, "submit_task", session_id=ids[2], content=task))["result"][
                "data"
            ]
            await terminal(http, run["run_id"])
            run = (await rpc(http, "submit_task", session_id=ids[2], content="follow-up"))[
                "result"
            ]["data"]
            await terminal(http, run["run_id"])
            second = (await rpc(http, "list_sessions", before=first["next_before"], limit=2))[
                "result"
            ]["data"]
            assert [s["session_id"] for s in second["sessions"]] == [ids[2], ids[1]]
            summary = second["sessions"][0]
            assert summary["title"] == "Check CNC-04 status"
            assert summary["latest_run_id"] == run["run_id"]
            assert summary["latest_run_status"] == "completed"
            assert set(summary) == {
                "session_id",
                "title",
                "created_at",
                "updated_at",
                "latest_run_id",
                "latest_run_status",
            }
            third = (await rpc(http, "list_sessions", before=second["next_before"], limit=2))[
                "result"
            ]["data"]
            assert [s["session_id"] for s in third["sessions"]] == [ids[0]]
            assert not third["has_more"]
    async with SQLiteExecutionStore(path) as store:
        restored = await store.list_sessions()
        items = restored["sessions"]
        assert isinstance(items, list) and len(items) == 6
        assert summary in items


@pytest.mark.parametrize(
    "params", [{"limit": 0}, {"limit": 101}, {"before": 0}, {"before": 2**53}, {"limit": "20"}]
)
async def test_session_page_rejects_invalid_params(tmp_path: Path, params: dict[str, Any]) -> None:
    app = create_app(lambda: service_for(tmp_path / "invalid.db", FakeLLMProvider()))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://local"
        ) as http:
            assert (await rpc(http, "list_sessions", **params))["error"]["code"] == -32602


async def test_session_indexes_upgrade_v2_without_losing_history(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    with sqlite3.connect(path) as db:
        for migration in MIGRATIONS[:2]:
            for statement in migration:
                db.execute(statement)
        db.execute("PRAGMA user_version = 2")
        db.execute(
            "INSERT INTO sessions VALUES (?, ?)", ("old", json.dumps(as_jsonable(Session("old"))))
        )
    async with SQLiteExecutionStore(path) as store:
        page = await store.list_sessions()
        items = page["sessions"]
        assert isinstance(items, list) and len(items) == 1
        assert isinstance(items[0], dict) and items[0]["session_id"] == "old"
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
        indexes = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {"messages_by_session", "runs_by_session"} <= indexes
