"""Sweep scheduling: daylight gating, batching, and quota discipline."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.core.config import Settings
from app.domain.models import MeasurementBasis
from app.ingestion.base import (
    AuthenticationError,
    InverterDataSourceInterface,
    PanelReading,
    QuotaExceededError,
    SiteReading,
    TransientVendorError,
    VendorAlarm,
    VendorDevice,
    VendorSite,
)
from app.ingestion.worker import run_detail_sweep, run_site_sweep

BANGKOK = ZoneInfo("Asia/Bangkok")


def at(hour: int, minute: int = 0) -> datetime:
    """A moment on a fixed day, expressed in Bangkok time."""
    return datetime(2026, 8, 27, hour, minute, tzinfo=BANGKOK).astimezone(UTC)


class FakeSource(InverterDataSourceInterface):
    vendor_key = "fake"
    measurement_basis = MeasurementBasis.PANEL
    max_sites_per_call = 100
    supports_panel_data = True

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.batches: list[list[str]] = []
        self.detail_sites: list[str] = []
        self.call_count = 0
        self._fail_with = fail_with
        self.authenticated = False

    async def authenticate(self) -> None:
        if isinstance(self._fail_with, AuthenticationError):
            raise self._fail_with
        self.authenticated = True

    async def list_sites(self) -> list[VendorSite]:
        return []

    async def list_devices(self, vendor_site_id: str) -> list[VendorDevice]:
        return []

    async def fetch_site_readings(self, vendor_site_ids: list[str]) -> list[SiteReading]:
        self.batches.append(list(vendor_site_ids))
        self.call_count += 1
        if self._fail_with and not isinstance(self._fail_with, AuthenticationError):
            raise self._fail_with
        return [
            SiteReading(vendor_site_id=sid, measured_at=datetime.now(UTC))
            for sid in vendor_site_ids
        ]

    async def fetch_device_readings(self, vendor_site_id: str) -> list:
        return []

    async def fetch_panel_readings(self, vendor_site_id: str) -> list[PanelReading]:
        self.detail_sites.append(vendor_site_id)
        self.call_count += 1
        if self._fail_with and not isinstance(self._fail_with, AuthenticationError):
            raise self._fail_with
        return []

    async def fetch_alarms(self, vendor_site_id: str) -> list[VendorAlarm]:
        return []


def settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "ingestion_enabled": True,
        "ingestion_poll_interval_min": 15,
        "ingestion_daylight_start": "06:30",
        "ingestion_daylight_end": "18:00",
        "ingestion_timezone": "Asia/Bangkok",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Daylight gating — the whole point of the schedule
# --------------------------------------------------------------------------- #


async def test_midday_sweep_runs() -> None:
    source = FakeSource()
    result = await run_site_sweep(source, ["1", "2"], settings=settings(), now=at(12))

    assert result.skipped_reason is None
    assert len(result.site_readings) == 2


async def test_night_sweep_is_skipped_without_calling_the_vendor() -> None:
    """PV output at night is zero. Polling through it spends a metered monthly
    budget to confirm the sun is down."""
    source = FakeSource()
    result = await run_site_sweep(source, ["1"], settings=settings(), now=at(2))

    assert result.skipped_reason == "outside daylight window"
    assert source.call_count == 0
    assert source.batches == []


async def test_window_boundaries_are_inclusive() -> None:
    source = FakeSource()
    assert (await run_site_sweep(source, ["1"], settings=settings(), now=at(6, 30))).succeeded
    assert (await run_site_sweep(source, ["1"], settings=settings(), now=at(18, 0))).succeeded

    just_before = await run_site_sweep(source, ["1"], settings=settings(), now=at(6, 29))
    just_after = await run_site_sweep(source, ["1"], settings=settings(), now=at(18, 1))
    assert just_before.skipped_reason == "outside daylight window"
    assert just_after.skipped_reason == "outside daylight window"


async def test_window_is_evaluated_in_bangkok_not_utc() -> None:
    """13:00 Bangkok is 06:00 UTC. Evaluating the window in UTC would put the
    whole Thai fleet outside daylight for most of its actual generating day."""
    source = FakeSource()
    midday_bangkok = datetime(2026, 8, 27, 6, 0, tzinfo=UTC)  # 13:00 in Bangkok

    result = await run_site_sweep(source, ["1"], settings=settings(), now=midday_bangkok)
    assert result.succeeded


# --------------------------------------------------------------------------- #
# Batching — what keeps a 127-site fleet inside a 10,000-call budget
# --------------------------------------------------------------------------- #


async def test_sites_are_batched_to_the_vendor_limit() -> None:
    source = FakeSource()
    site_ids = [str(i) for i in range(127)]

    result = await run_site_sweep(source, site_ids, settings=settings(), now=at(12))

    assert [len(b) for b in source.batches] == [100, 27]
    assert source.call_count == 2, "127 sites must cost 2 calls, not 127"
    assert len(result.site_readings) == 127


async def test_single_site_vendor_makes_one_call_per_site() -> None:
    source = FakeSource()
    source.max_sites_per_call = 1

    await run_site_sweep(source, ["a", "b", "c"], settings=settings(), now=at(12))
    assert [len(b) for b in source.batches] == [1, 1, 1]


# --------------------------------------------------------------------------- #
# Failure handling
# --------------------------------------------------------------------------- #


async def test_quota_error_aborts_the_sweep_immediately() -> None:
    """The budget is monthly and non-renewable. Continuing after a 429 spends
    what is left and buys nothing."""
    source = FakeSource(fail_with=QuotaExceededError("429"))
    site_ids = [str(i) for i in range(250)]

    result = await run_site_sweep(source, site_ids, settings=settings(), now=at(12))

    assert source.call_count == 1, "must stop after the first quota refusal"
    assert not result.succeeded
    assert any("quota" in e for e in result.errors)


async def test_a_failed_batch_does_not_lose_the_other_batches() -> None:
    class FlakyOnce(FakeSource):
        async def fetch_site_readings(self, vendor_site_ids: list[str]) -> list[SiteReading]:
            self.batches.append(list(vendor_site_ids))
            self.call_count += 1
            if len(self.batches) == 1:
                raise TransientVendorError("first batch failed")
            return [
                SiteReading(vendor_site_id=s, measured_at=datetime.now(UTC))
                for s in vendor_site_ids
            ]

    source = FlakyOnce()
    result = await run_site_sweep(
        source, [str(i) for i in range(150)], settings=settings(), now=at(12)
    )

    assert source.call_count == 2
    assert len(result.site_readings) == 50, "second batch must still land"
    assert result.sites_failed == 100


async def test_authentication_failure_is_not_retried() -> None:
    """Repeatedly presenting a rejected credential is how accounts get locked."""
    source = FakeSource(fail_with=AuthenticationError("bad credential"))
    result = await run_site_sweep(source, ["1", "2"], settings=settings(), now=at(12))

    assert source.call_count == 0
    assert result.sites_failed == 2
    assert any("authentication" in e for e in result.errors)


async def test_disabled_ingestion_makes_no_calls() -> None:
    source = FakeSource()
    result = await run_site_sweep(
        source, ["1"], settings=settings(ingestion_enabled=False), now=at(12)
    )

    assert result.skipped_reason == "ingestion disabled"
    assert source.call_count == 0


# --------------------------------------------------------------------------- #
# Detail sweep
# --------------------------------------------------------------------------- #


async def test_detail_sweep_costs_one_call_per_site() -> None:
    source = FakeSource()
    await run_detail_sweep(source, ["a", "b", "c"], settings=settings(), now=at(12))

    assert source.detail_sites == ["a", "b", "c"]
    assert source.call_count == 3


async def test_detail_sweep_can_be_capped_for_a_rehearsal() -> None:
    source = FakeSource()
    await run_detail_sweep(
        source, [str(i) for i in range(50)], settings=settings(), now=at(12), max_sites=5
    )
    assert source.call_count == 5


async def test_detail_sweep_skipped_when_vendor_has_no_panel_data() -> None:
    source = FakeSource()
    source.supports_panel_data = False

    result = await run_detail_sweep(source, ["a"], settings=settings(), now=at(12))
    assert result.skipped_reason == "vendor publishes no panel-level data"
    assert source.call_count == 0


async def test_detail_sweep_skipped_at_night() -> None:
    """Panel comparison needs the array generating; at night every reading is
    near zero and the analytics module discards them anyway."""
    source = FakeSource()
    result = await run_detail_sweep(source, ["a"], settings=settings(), now=at(21))

    assert result.skipped_reason == "outside daylight window"
    assert source.call_count == 0


# --------------------------------------------------------------------------- #
# Quota budget — asserted, not left in a comment
# --------------------------------------------------------------------------- #


def test_default_schedule_fits_the_atmoce_monthly_quota() -> None:
    """127 sites at 100 per call is 2 calls per sweep. The default schedule must
    leave room for the daily detail sweep and for retries."""
    cfg = settings()
    calls_per_month = cfg.estimated_monthly_polls() * 2

    assert calls_per_month == 2760
    assert calls_per_month < 10_000 * 0.35, "site sweeps must not dominate the budget"


def test_hourly_polling_would_break_the_fifteen_minute_offline_rule() -> None:
    """The reason the offline threshold is derived rather than set by hand: a
    site can never look fresher than the rate at which it is polled."""
    hourly = settings(ingestion_poll_interval_min=60)

    assert hourly.effective_offline_after_minutes == 150
    assert hourly.effective_offline_after_minutes > hourly.ingestion_poll_interval_min
