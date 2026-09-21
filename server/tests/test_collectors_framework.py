import asyncio

import pytest

from critdash.collectors import BaseCollector, CollectorIssue, Scheduler


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


class OptionalDepMissingCollector(BaseCollector):
    name = "optdep"
    interval_s = 0.01

    async def collect(self) -> dict:
        raise CollectorIssue(
            "dependency_missing", "foo is not installed", remedy="Install foo.", optional=True
        )


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


@pytest.mark.asyncio
async def test_collector_issue_populates_structured_health_fields():
    scheduler = Scheduler(on_result=lambda name, data: None)
    scheduler.register(OptionalDepMissingCollector())
    await scheduler.run_once("optdep")

    health = scheduler.health_snapshot()["optdep"]
    assert health["ok"] is False
    assert health["reason_code"] == "dependency_missing"
    assert health["detail"] == "foo is not installed"
    assert health["remedy"] == "Install foo."
    assert health["optional"] is True
    # error stays populated too, for anything that still just reads that field
    assert "foo is not installed" in health["error"]


@pytest.mark.asyncio
async def test_unclassified_exception_leaves_structured_fields_none():
    scheduler = Scheduler(on_result=lambda name, data: None)
    scheduler.register(BadCollector())
    await scheduler.run_once("bad")

    health = scheduler.health_snapshot()["bad"]
    assert health["ok"] is False
    assert health["reason_code"] is None
    assert health["detail"] is None
    assert health["remedy"] is None
    assert health["optional"] is False
    assert "boom" in health["error"]


@pytest.mark.asyncio
async def test_success_clears_previous_failure_reason_code():
    calls = {"n": 0}

    class FlakyThenGood(BaseCollector):
        name = "flaky"
        interval_s = 0.01

        async def collect(self) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                raise CollectorIssue("config_missing", "no config", optional=True)
            return {"flaky": {}}

    scheduler = Scheduler(on_result=lambda name, data: None)
    scheduler.register(FlakyThenGood())
    await scheduler.run_once("flaky")
    assert scheduler.health_snapshot()["flaky"]["reason_code"] == "config_missing"

    await scheduler.run_once("flaky")
    health = scheduler.health_snapshot()["flaky"]
    assert health["ok"] is True
    assert health["reason_code"] is None
    assert health["remedy"] is None
    assert health["optional"] is False


@pytest.mark.asyncio
async def test_start_one_starts_a_single_collector_after_start_already_ran():
    """Bug 2's periodic re-detection: a collector that was inactive at
    startup (registered later, once its dependency shows up) must be able to
    join the running loop without restarting every other collector's task."""
    scheduler = Scheduler(on_result=lambda name, data: None)
    scheduler.register(GoodCollector())
    scheduler.start()
    try:
        late = GoodCollector()
        late.name = "late"
        scheduler.register(late)
        scheduler.start_one("late")
        await asyncio.sleep(0.05)
        health = scheduler.health_snapshot()
        assert health["late"]["ok"] is True
        assert health["good"]["ok"] is True
    finally:
        await scheduler.stop()
