import asyncio
from pathlib import Path

import anyio
import pytest

from fakes import FakeLLMProvider
from minder_harness.core import Message, ModelResponse
from minder_harness.persistence import SQLiteExecutionStore
from support import ProviderGate, open_runtime

pytestmark = pytest.mark.anyio


async def test_provider_gate_release_and_intentional_failure() -> None:
    gate = ProviderGate()

    async def callback(step: int) -> None:
        await gate(step)
        if step == 2:
            raise OSError("intentional provider failure")

    provider = FakeLLMProvider([ModelResponse(content="released")], on_generate=callback)
    messages = [Message("user", "test")]
    with anyio.fail_after(3):
        task = asyncio.create_task(provider.generate(messages))
        try:
            await gate.entered.wait()
            assert not task.done()
        finally:
            gate.release.set()
        assert (await task).content == "released"
        with pytest.raises(OSError, match="intentional provider"):
            await provider.generate(messages)
    assert len(provider.calls) == 2 and not gate.cancelled.is_set()


async def test_cleanup_failure_is_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = SQLiteExecutionStore.aclose

    async def fail_after_close(store: SQLiteExecutionStore) -> None:
        await original(store)
        raise OSError("teardown failure")

    path = tmp_path / "teardown.db"
    with monkeypatch.context() as patch:
        patch.setattr(SQLiteExecutionStore, "aclose", fail_after_close)
        with pytest.raises(OSError, match="teardown failure"):
            async with open_runtime(path):
                pass
    async with SQLiteExecutionStore(path):
        pass
