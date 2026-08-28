"""Ingestion scheduler.

Runs two loops at very different cadences, because the vendor calls behind them
cost very different amounts of quota:

  SITE SWEEP — every ``INGESTION_POLL_INTERVAL_MIN`` during daylight.
      One call covers up to 100 sites, so a full fleet sweep is 2 calls. This
      is what keeps the map current.

  DETAIL SWEEP — every ``INGESTION_DETAIL_INTERVAL_HOURS``.
      Per-panel and per-string data costs one call PER SITE. At 127 sites that
      is 127 calls a sweep, so it runs daily rather than continuously. This is
      what feeds the peer-comparison fault detection.

Both are gated on daylight. PV output at night is zero, and polling through it
spends a metered budget to learn that the sun is down.

Cadence is PER VENDOR, not fleet-wide. Atmoce serves 100 sites in one call and
absorbs a 15-minute sweep comfortably. Huawei charges one call per site and
refused a single uninterrupted pass of 51 branches from about the fifth site
onward, so it runs every two hours. The staleness threshold that decides whether
a branch shows as offline is derived from each vendor's own interval — see
``Settings.offline_after_minutes_for``. The two numbers cannot be set
independently: a branch polled every two hours can never look fresher than two
hours old.

QUOTA IS A SHARED, MONTHLY, NON-RENEWABLE RESOURCE. Atmoce allows 10,000 calls
a month per token. Spending it early means flying blind for the rest of the
month, which is why ``QuotaExceededError`` aborts a sweep instead of retrying.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import Settings, get_settings
from app.ingestion.base import (
    AuthenticationError,
    InverterDataSourceInterface,
    PanelReading,
    QuotaExceededError,
    SiteReading,
    TransientVendorError,
)

logger = logging.getLogger(__name__)

__all__ = [
    "SweepResult",
    "WorkerSettings",
    "cron_schedule",
    "poll_vendor",
    "pull_vendor_detail",
    "run_detail_sweep",
    "run_site_sweep",
]

#: Vendors the scheduler polls, with the endpoint each is reached at. Kept in
#: step with ``app.ingestion.check.DEFAULTS`` by the import below rather than
#: duplicated, so adding a vendor in one place cannot leave the worker behind.
SCHEDULED_VENDORS = ("atmoce", "huawei")


@dataclass
class SweepResult:
    """What one sweep achieved, and what it cost."""

    vendor_key: str
    started_at: datetime
    site_readings: list[SiteReading] = field(default_factory=list)
    panel_readings: list[PanelReading] = field(default_factory=list)
    calls_used: int = 0
    sites_attempted: int = 0
    sites_failed: int = 0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.skipped_reason is None and not self.errors

    def summary(self) -> str:
        if self.skipped_reason:
            return f"{self.vendor_key}: skipped ({self.skipped_reason})"
        return (
            f"{self.vendor_key}: {len(self.site_readings)} site readings, "
            f"{len(self.panel_readings)} panel readings, "
            f"{self.calls_used} API calls, "
            f"{self.sites_failed}/{self.sites_attempted} sites failed"
        )


def _batched(items: list[str], size: int) -> list[list[str]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


async def run_site_sweep(
    source: InverterDataSourceInterface,
    vendor_site_ids: list[str],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> SweepResult:
    """Fetch site-level data for the whole fleet, batched to the vendor's limit."""
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    result = SweepResult(vendor_key=source.vendor_key, started_at=moment)

    if not settings.ingestion_enabled:
        result.skipped_reason = "ingestion disabled"
        return result

    if not settings.is_daylight(moment):
        # Not an error, and deliberately not a warning: this is the normal
        # state for more than half of every day.
        result.skipped_reason = "outside daylight window"
        logger.debug(
            "Skipping %s sweep: outside %s-%s %s",
            source.vendor_key,
            settings.ingestion_daylight_start,
            settings.ingestion_daylight_end,
            settings.ingestion_timezone,
        )
        return result

    if not vendor_site_ids:
        result.skipped_reason = "no sites mapped to this vendor"
        return result

    result.sites_attempted = len(vendor_site_ids)
    batches = _batched(vendor_site_ids, source.max_sites_per_call)

    try:
        await source.authenticate()
    except AuthenticationError as exc:
        # Never retried: repeatedly presenting a rejected credential is how
        # vendor accounts get locked out.
        result.errors.append(f"authentication failed: {exc}")
        result.sites_failed = len(vendor_site_ids)
        logger.error("%s authentication failed: %s", source.vendor_key, exc)
        return result

    for batch in batches:
        try:
            result.site_readings.extend(await source.fetch_site_readings(batch))
        except QuotaExceededError as exc:
            # Abort the whole sweep. The budget is monthly; burning what is left
            # on retries costs visibility for weeks.
            result.errors.append(f"quota exceeded: {exc}")
            result.sites_failed += len(batch)
            logger.error("%s quota exceeded — aborting sweep", source.vendor_key)
            break
        except TransientVendorError as exc:
            # One bad batch must not lose the other 100 sites.
            result.errors.append(f"batch failed: {exc}")
            result.sites_failed += len(batch)
            logger.warning("%s batch of %d failed: %s", source.vendor_key, len(batch), exc)
        except Exception as exc:  # noqa: BLE001 - a sweep must never kill the worker
            result.errors.append(f"unexpected: {exc}")
            result.sites_failed += len(batch)
            logger.exception("%s batch raised unexpectedly", source.vendor_key)

    result.calls_used = getattr(source, "call_count", 0)
    logger.info(result.summary())
    return result


async def run_detail_sweep(
    source: InverterDataSourceInterface,
    vendor_site_ids: list[str],
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
    max_sites: int | None = None,
) -> SweepResult:
    """Fetch per-panel / per-string data, one call per site.

    ``max_sites`` caps a single run so an operator can rehearse against a few
    sites without spending the whole detail budget.
    """
    settings = settings or get_settings()
    moment = now or datetime.now(UTC)
    result = SweepResult(vendor_key=source.vendor_key, started_at=moment)

    if not settings.ingestion_enabled:
        result.skipped_reason = "ingestion disabled"
        return result

    if not source.supports_panel_data:
        result.skipped_reason = "vendor publishes no panel-level data"
        return result

    if not settings.is_daylight(moment):
        # Panel comparison needs the array to be generating. Readings taken at
        # night are all near zero, and percentage deviations between near-zero
        # numbers are meaningless — the analytics module discards them anyway.
        result.skipped_reason = "outside daylight window"
        return result

    targets = vendor_site_ids[:max_sites] if max_sites else vendor_site_ids
    result.sites_attempted = len(targets)

    try:
        await source.authenticate()
    except AuthenticationError as exc:
        result.errors.append(f"authentication failed: {exc}")
        result.sites_failed = len(targets)
        return result

    for site_id in targets:
        try:
            result.panel_readings.extend(await source.fetch_panel_readings(site_id))
        except QuotaExceededError as exc:
            result.errors.append(f"quota exceeded: {exc}")
            result.sites_failed += 1
            logger.error("%s quota exceeded during detail sweep — stopping", source.vendor_key)
            break
        except TransientVendorError as exc:
            result.errors.append(f"site {site_id}: {exc}")
            result.sites_failed += 1
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"site {site_id}: {exc}")
            result.sites_failed += 1
            logger.exception("%s detail sweep failed for site %s", source.vendor_key, site_id)

    result.calls_used = getattr(source, "call_count", 0)
    logger.info(result.summary())
    return result


# ===========================================================================
# Scheduler
# ===========================================================================
#
# Everything below is the arq entry point named in docker-compose:
#     command: ["arq", "app.ingestion.worker.WorkerSettings"]
#
# The sweep helpers above fetch and return; these jobs fetch AND PERSIST, via
# ``sync_site_readings``, which commits per site so an interrupted run keeps
# what it already wrote.


def cron_schedule(interval_minutes: int) -> dict[str, set[int]]:
    """Translate a poll interval into arq's cron fields.

    Deliberately does NOT restrict the hours to the daylight window. arq
    evaluates cron against the CONTAINER's clock, which is UTC, while the
    daylight window is site-local (Asia/Bangkok); encoding Bangkok hours into a
    UTC cron would silently shift the whole schedule by seven hours. The jobs
    fire around the clock and ``Settings.is_daylight`` — which converts properly
    — skips the night-time ones. A skipped sweep costs no API calls.
    """
    interval = max(1, interval_minutes)
    if interval < 60:
        return {"minute": set(range(0, 60, interval))}
    return {"hour": set(range(0, 24, max(1, interval // 60))), "minute": {0}}


#: One adapter instance per ACCOUNT, reused across sweeps for the life of the
#: worker, keyed by secrets_ref.
#:
#: Not an optimisation for its own sake. Each adapter caches things that are
#: expensive to rediscover: the session token, and — for Atmoce — which sites
#: its bulk endpoint refuses, which costs a bisection to work out. Rebuilding
#: the adapter every 15 minutes would pay both again on every sweep.
_SOURCES: dict[str, Any] = {}


def _source_for(vendor: str, secrets_ref: str, base_url: str | None) -> Any:
    from app.core.deps import build_data_source
    from app.ingestion.check import DEFAULTS

    source = _SOURCES.get(secrets_ref)
    if source is None:
        source = build_data_source(
            vendor_key=vendor,
            base_url=base_url or DEFAULTS[vendor],
            secrets_ref=secrets_ref,
        )
        _SOURCES[secrets_ref] = source
    return source


async def _sync_vendor(vendor: str, *, include_panels: bool) -> str:
    settings = get_settings()

    if not settings.ingestion_enabled:
        return f"{vendor}: skipped (ingestion disabled)"

    if not settings.is_daylight():
        # The normal state for more than half of every day. Logged at debug so
        # it does not drown the log it shares with real sweeps.
        logger.debug("%s: skipped, outside daylight window", vendor)
        return f"{vendor}: skipped (outside daylight window)"

    accounts = settings.accounts_for(vendor)
    if not accounts:
        return f"{vendor}: skipped (no account configured)"

    # Each account is swept independently. One failing login must not cost the
    # other its sweep — Huawei's two accounts cover different branches, so a
    # shared failure path would hide half the fleet.
    summaries = [
        await _sync_account(vendor, ref, url, include_panels=include_panels)
        for _, ref, url in accounts
    ]
    return " | ".join(summaries)


async def _sync_account(
    vendor: str,
    secrets_ref: str,
    base_url: str | None,
    *,
    include_panels: bool,
) -> str:
    # Imported here, not at module scope: these pull in the database session
    # and the vendor adapters, and importing them eagerly would make this
    # module — which the pure sweep helpers live in — impossible to test
    # without a database.
    from app.infrastructure.db.session import session_scope
    from app.ingestion.sync import sync_site_readings

    label = f"{vendor}/{secrets_ref}"
    source = _source_for(vendor, secrets_ref, base_url)
    before = getattr(source, "call_count", 0)
    try:
        report = await sync_site_readings(
            session_scope,
            source,
            include_panels=include_panels,
            # Inverter-level readings cost one call PER SITE. They belong to the
            # daily detail run, not to a sweep that repeats every 15 minutes —
            # including them there would spend a 10,000-call monthly budget in
            # under two days. The frequent sweep rides on the bulk endpoint,
            # which covers 100 branches per call.
            include_devices=include_panels,
        )
        summary = (
            f"{label}: {report.raw_rows} readings, "
            f"{report.string_rows} string rows, "
            f"{getattr(source, 'call_count', 0) - before} API calls"
        )
        if report.errors:
            # Not raised. A vendor that fails now will be retried at the next
            # tick anyway, and letting the exception escape would have arq
            # retry immediately — which, against a rate-limited API, is the one
            # response guaranteed to make things worse.
            logger.warning("%s finished with %d error(s)", label, len(report.errors))
            for message in report.errors[:5]:
                logger.warning("  %s", message)
        logger.info(summary)
        return summary
    except AuthenticationError as exc:
        # Never retried: repeatedly presenting a rejected credential is how
        # vendor accounts get locked out.
        logger.error("%s authentication failed: %s", label, exc)
        return f"{label}: authentication failed"
    except QuotaExceededError as exc:
        logger.error("%s quota/rate limit hit: %s", label, exc)
        return f"{label}: quota exceeded"
    except Exception as exc:  # noqa: BLE001 — one account must not kill the worker
        logger.exception("%s sweep raised unexpectedly", label)
        return f"{label}: failed ({exc})"


async def poll_vendor(ctx: dict, vendor: str) -> str:
    """Site-level sweep for one vendor. This is what keeps the map current."""
    return await _sync_vendor(vendor, include_panels=False)


async def pull_vendor_detail(ctx: dict, vendor: str) -> str:
    """Panel/string-level sweep for one vendor.

    One API call PER SITE, against 15 minutes' worth of quota for the entire
    fleet at site level — hence once a day, not continuously. This is what feeds
    the peer-comparison fault detection.
    """
    return await _sync_vendor(vendor, include_panels=True)


async def _on_startup(ctx: dict) -> None:
    settings = get_settings()

    # arq configures only its own logger. Without this the application loggers
    # stay at the root default, so every INFO line from the sweep — including
    # what it actually wrote — is discarded and the worker looks like it has
    # hung whenever a run takes more than a moment.
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        force=True,
    )

    logger.info(
        "Ingestion worker up. Daylight %s-%s %s. Cadence: %s",
        settings.ingestion_daylight_start,
        settings.ingestion_daylight_end,
        settings.ingestion_timezone,
        ", ".join(
            f"{vendor} every {settings.poll_interval_for(vendor)}m "
            f"(offline after {settings.offline_after_minutes_for(vendor)}m, "
            f"{len(settings.accounts_for(vendor))} account(s))"
            for vendor in SCHEDULED_VENDORS
        ),
    )


async def _on_shutdown(ctx: dict) -> None:
    from app.infrastructure.db.session import dispose_engine

    for source in _SOURCES.values():
        try:
            await source.close()
        except Exception:  # noqa: BLE001 — shutdown must not raise
            logger.debug("Failed to close %s cleanly", source, exc_info=True)
    _SOURCES.clear()
    await dispose_engine()


def _bound_job(vendor: str, *, include_panels: bool) -> Any:
    """A zero-argument coroutine for one vendor.

    arq calls cron jobs with ``ctx`` and nothing else — there is no way to pass
    the vendor through the schedule — so it is bound into a closure here. The
    generated name is what appears in the worker log, so it is set explicitly
    rather than left as ``<locals>``.
    """
    kind = "detail" if include_panels else "poll"

    async def _job(ctx: dict) -> str:
        return await _sync_vendor(vendor, include_panels=include_panels)

    _job.__name__ = f"{kind}_{vendor}"
    _job.__qualname__ = f"{kind}_{vendor}"
    return _job


def _build_cron_jobs() -> list:
    settings = get_settings()
    jobs = []

    for vendor in SCHEDULED_VENDORS:
        interval = settings.poll_interval_for(vendor)
        jobs.append(
            cron(
                _bound_job(vendor, include_panels=False),
                name=f"poll_{vendor}",
                **cron_schedule(interval),
                # Ticks must not pile up behind a slow sweep. Huawei's paced
                # pass over 51 sites takes minutes; if a run overruns its slot,
                # skipping the next one is correct — two concurrent sweeps of
                # the same vendor would double its request rate and trip the
                # very limit the slow cadence exists to avoid.
                unique=True,
                timeout=interval * 60,
                max_tries=1,
                # Refresh immediately on start for the cheap vendor, so a
                # deploy or restart does not leave the map showing stale data
                # until the next slot. Not for Huawei: one call per site
                # against a rate-limited account means a crash-loop would
                # hammer it, and its 300-minute staleness threshold tolerates
                # waiting for the next scheduled slot anyway.
                run_at_startup=(interval < 60),
            )
        )

    # Detail sweep: once a day, mid-morning site-local, when the array is
    # generating enough for panel-to-panel comparison to mean anything —
    # percentage deviations between near-zero readings are noise.
    #
    # Atmoce only: Huawei's northbound account is refused getDevRealKpi with
    # failCode 20046, so a detail sweep there would spend 51 calls to collect
    # nothing.
    jobs.append(
        cron(
            _bound_job("atmoce", include_panels=True),
            name="detail_atmoce",
            hour={3},  # 10:00 Asia/Bangkok, expressed in the container's UTC
            minute={0},
            unique=True,
            timeout=3600,
            max_tries=1,
        )
    )
    return jobs


class WorkerSettings:
    """arq entry point. Referenced by name from docker-compose."""

    functions = [poll_vendor, pull_vendor_detail]
    cron_jobs = _build_cron_jobs()
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    #: One vendor sweep at a time. These jobs are almost entirely waiting on
    #: vendor APIs that rate-limit per account, so running them in parallel buys
    #: no speed and costs throttling.
    max_jobs = 2
    #: Retained so a failed sweep is still visible in Redis at the next tick.
    keep_result = 3600
