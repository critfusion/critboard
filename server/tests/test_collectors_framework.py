import asyncio

import pytest

from critdash.collectors import BaseCollector, Scheduler


class GoodCollector(BaseCollector):
    name = "good"
    interval_s = 0.01

    async def collect(self) -> dict:
        return {"good": {"ok": True}}


class BadCollector(BaseCollector):
    name = "bad"
    interval_s = 0.01

    async def collect(self) -> dict:
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_raising_collector_marked_not_ok_without_killing_scheduler():
    results = []
    scheduler = Scheduler(on_result=lambda name, data: results.append((name, data)))
    scheduler.register(GoodCollector())
    scheduler.register(BadCollector())

    await scheduler.run_once("good")
    await scheduler.run_once("bad")

    health = scheduler.health_snapshot()
    assert health["good"]["ok"] is True
    assert health["good"]["error"] is None
    assert health["bad"]["ok"] is False
    assert "boom" in health["bad"]["error"]

    # good collector still ran and published its result despite bad's failure
    assert ("good", {"good": {"ok": True}}) in results


@pytest.mark.asyncio
async def test_scheduler_loop_survives_repeated_failures():
    scheduler = Scheduler(on_result=lambda name, data: None)
    scheduler.register(BadCollector())
    scheduler.start()
    await asyncio.sleep(0.05)
    await scheduler.stop()
    health = scheduler.health_snapshot()
    assert health["bad"]["ok"] is False
    assert health["bad"]["stale"] is True


def test_stale_flag_threshold():
    from critdash.collectors import SourceHealth

    h = SourceHealth(ok=True, last_ok="x", stale=False)
    assert h.to_dict()["stale"] is False
