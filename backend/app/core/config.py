"""Application settings, loaded from environment / .env.

Every tunable lives here. Nothing in the codebase reads ``os.environ`` directly,
so what the system can be configured with is answerable by reading one file —
which matters when corporate IT takes this over.
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["AuthMode", "SecretsProviderKind", "Settings", "get_settings"]


class AuthMode(str, Enum):
    MOCK = "mock"
    ENTERPRISE_SSO = "enterprise_sso"


class SecretsProviderKind(str, Enum):
    ENV = "env"
    VAULT = "vault"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- Application ---------------------------------------------------------
    app_env: str = "development"
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:3000"

    # -- Database ------------------------------------------------------------
    # Phase 2: corporate IT repoints this at the enterprise SQL server and
    # selects the matching repository implementation in app.core.deps.
    database_url: str = Field(
        default="postgresql+asyncpg://solarcell:solarcell@db:5432/solarcell",
    )
    sql_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_recycle_seconds: int = 1800

    # -- Redis ---------------------------------------------------------------
    redis_url: str = "redis://redis:6379/0"

    # -- Authentication ------------------------------------------------------
    auth_mode: AuthMode = AuthMode.MOCK
    mock_user_id: str = "00000000-0000-0000-0000-000000000001"
    mock_user_name: str = "Phase 1 Local User"
    mock_user_email: str = "local@example.invalid"
    mock_user_roles: str = "admin"

    sso_tenant_id: str = ""
    sso_client_id: str = ""
    sso_authority: str = ""
    sso_jwks_url: str = ""
    sso_audience: str = ""

    # -- Secrets -------------------------------------------------------------
    secrets_provider: SecretsProviderKind = SecretsProviderKind.ENV
    vault_addr: str = ""
    vault_token: str = ""
    vault_mount: str = "solarcell"

    # -- Solcast (Virtual Pyranometer) --------------------------------------
    solcast_api_key: str = ""
    solcast_base_url: str = "https://api.solcast.com.au"
    weather_cache_ttl_seconds: int = 1800

    # -- Analytics thresholds ------------------------------------------------
    pr_green_threshold: Decimal = Decimal("75.0")
    #: Specific-yield status threshold, expressed as a PERCENTAGE OF THE FLEET
    #: MEDIAN for the same day — the fallback used when PR% cannot be computed.
    #:
    #: Relative, not absolute, and deliberately so. An absolute kWh/kWp figure
    #: cannot work here: the number climbs all day, so any fixed threshold would
    #: call the whole fleet unhealthy every morning, and a genuinely overcast day
    #: would light up all 163 branches at once. Judged against its peers on the
    #: same day, a branch is flagged only when it falls behind branches under the
    #: same sky.
    yield_green_threshold_pct: Decimal = Decimal("80.0")
    #: Minimum branches reporting before the fleet median is trusted. With too
    #: few peers the median is an accident of which sites happened to report.
    yield_min_peers: int = 5
    string_variance_threshold_pct: Decimal = Decimal("10.0")
    #: Leave as None to derive it from the poll interval — see
    #: ``effective_offline_after_minutes``, which is what the query layer uses.
    #: Set an explicit number only to override that.
    device_offline_after_minutes: int | None = None

    # -- ESG -----------------------------------------------------------------
    tgo_grid_emission_factor: Decimal = Decimal("0.4999")
    tgo_ef_effective_year: int = 2024

    # -- Ingestion -----------------------------------------------------------
    ingestion_enabled: bool = True
    #: How often to poll vendor clouds during daylight.
    #:
    #: 15 matches Atmoce's native collection interval ("the shortest collection
    #: interval is 15 minutes, 96 data points per day"), so nothing is thrown
    #: away. Raising this to 60 discards three of every four available data
    #: points for no quota saving worth having — daylight-only polling already
    #: puts usage far under the cap.
    ingestion_poll_interval_min: int = 15
    #: Huawei's northbound API is rate-limited far more aggressively than
    #: Atmoce's: a single uninterrupted sweep of the 51 mapped branches already
    #: earns failCode 407 partway through. It therefore gets its own, much
    #: slower cadence rather than sharing the fleet-wide one.
    ingestion_poll_interval_min_huawei: int = 120
    #: Pause between per-site Huawei calls, for the same reason. Atmoce needs
    #: none: it serves 100 sites in one call.
    ingestion_huawei_site_delay_seconds: float = 1.5
    #: Daylight window, site-local (Asia/Bangkok). Outside it, PV output is zero
    #: and polling only burns vendor API quota.
    ingestion_daylight_start: str = "06:30"
    ingestion_daylight_end: str = "18:00"
    ingestion_timezone: str = "Asia/Bangkok"
    #: Panel/string-level detail is far more expensive per call than the bulk
    #: site endpoint, so it runs on its own slower cadence.
    ingestion_detail_interval_hours: int = 24
    #: Vendor accounts the scheduler polls, as "vendor:secrets_ref[:base_url]"
    #: separated by commas.
    #:
    #: Explicit rather than derived, because one vendor can have several
    #: accounts: Huawei's 50-branch fleet and Mueang Nan sit under different
    #: logins, and each must be swept separately. ``secrets_ref`` is a LOOKUP
    #: KEY resolved through SecretsProviderInterface at time of use — no
    #: credential value appears here or anywhere else in configuration.
    ingestion_accounts: str = (
        "atmoce:atmoce_main,huawei:huawei_korn,huawei:huawei_nan"
    )
    scraper_headless: bool = True
    scraper_timeout_seconds: int = 60

    # -- Derived -------------------------------------------------------------

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def mock_user_role_list(self) -> list[str]:
        return [role.strip() for role in self.mock_user_roles.split(",") if role.strip()]

    @property
    def weather_enabled(self) -> bool:
        """PR% can only be computed with an irradiance baseline.

        When this is False the system still runs; performance_ratio is reported
        as null and pins show as UNKNOWN rather than misleading colours.
        """
        return bool(self.solcast_api_key)

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def ingestion_account_list(self) -> list[tuple[str, str, str | None]]:
        """Parsed ``ingestion_accounts`` as (vendor, secrets_ref, base_url)."""
        accounts: list[tuple[str, str, str | None]] = []
        for entry in self.ingestion_accounts.split(","):
            entry = entry.strip()
            if not entry:
                continue
            parts = [p.strip() for p in entry.split(":", 2)]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                raise ValueError(
                    f"INGESTION_ACCOUNTS entry {entry!r} must be "
                    f"'vendor:secrets_ref' with an optional ':base_url'."
                )
            vendor, secrets_ref = parts[0].lower(), parts[1]
            base_url = parts[2] if len(parts) == 3 and parts[2] else None
            accounts.append((vendor, secrets_ref, base_url))
        return accounts

    def accounts_for(self, vendor_key: str) -> list[tuple[str, str, str | None]]:
        return [a for a in self.ingestion_account_list if a[0] == vendor_key.lower()]

    def poll_interval_for(self, vendor_key: str | None) -> int:
        """Poll cadence for one vendor.

        Vendors do not share a rate limit, so they must not share a schedule.
        Atmoce absorbs a 15-minute sweep comfortably — one call covers 100
        sites. Huawei charges one call per site and starts refusing partway
        through a single pass.
        """
        if (vendor_key or "").strip().lower() == "huawei":
            return self.ingestion_poll_interval_min_huawei
        return self.ingestion_poll_interval_min

    def offline_after_minutes_for(self, vendor_key: str | None) -> int:
        """Staleness threshold for one vendor, derived from its own cadence.

        This MUST track the vendor's poll interval. A branch polled every two
        hours can never look fresher than two hours old, so judging it by a
        threshold sized for 15-minute polling marks every Huawei branch offline
        permanently — the alarm would be reporting our schedule, not the plant.
        """
        if self.device_offline_after_minutes is not None:
            return self.device_offline_after_minutes
        return max(15, int(self.poll_interval_for(vendor_key) * 2.5))

    @property
    def offline_thresholds_by_vendor(self) -> dict[str, int]:
        """Per-vendor staleness thresholds, keyed by lower-case vendor key.

        Consumed by the fleet query, which builds a CASE expression from it.
        Returning a map rather than letting the query name vendors keeps vendor
        policy in configuration and out of SQL.
        """
        return {
            vendor: self.offline_after_minutes_for(vendor)
            for vendor in ("huawei",)
            if self.offline_after_minutes_for(vendor)
            != self.effective_offline_after_minutes
        }

    @property
    def effective_offline_after_minutes(self) -> int:
        """How stale a reading must be before a site counts as offline.

        Derived from the poll interval unless explicitly overridden, because the
        two CANNOT be set independently: a site can never look fresher than the
        rate at which it is polled. Hard-coding 15 minutes while polling hourly
        would mark the entire fleet RED at all times — the alarm would be
        reporting the poll schedule, not the plant.

        The 2.5x multiplier tolerates one missed poll plus vendor-side lag
        before raising an alert, which is the difference between "the cloud was
        briefly slow" and "the inverter is down".
        """
        if self.device_offline_after_minutes is not None:
            return self.device_offline_after_minutes
        return max(15, int(self.ingestion_poll_interval_min * 2.5))

    @property
    def daylight_window(self) -> tuple[time, time]:
        return (
            time.fromisoformat(self.ingestion_daylight_start),
            time.fromisoformat(self.ingestion_daylight_end),
        )

    def is_daylight(self, moment: datetime | None = None) -> bool:
        """Whether ``moment`` falls inside the polling window, site-local."""
        now = (moment or datetime.now(UTC)).astimezone(ZoneInfo(self.ingestion_timezone))
        start, end = self.daylight_window
        return start <= now.time() <= end

    def estimated_monthly_polls(self, vendor_key: str | None = None) -> int:
        """Polls per month implied by the current schedule.

        Exposed so the quota budget can be asserted in a test and printed at
        startup, rather than being a calculation someone did once in a comment
        and never revisited as the fleet grew.
        """
        start, end = self.daylight_window
        daylight_minutes = (
            end.hour * 60 + end.minute - (start.hour * 60 + start.minute)
        )
        per_day = daylight_minutes // self.poll_interval_for(vendor_key)
        return per_day * 30

    # -- Validation ----------------------------------------------------------

    @field_validator("device_offline_after_minutes", mode="before")
    @classmethod
    def _blank_means_derive(cls, value: object) -> object:
        """Treat a blank .env entry as "not set", so derivation kicks in.

        ``DEVICE_OFFLINE_AFTER_MINUTES=`` arrives as an empty string, not as
        absent, and pydantic would otherwise reject it as an invalid integer.
        The env template tells operators to leave it blank, so blank has to
        work.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("pr_green_threshold")
    @classmethod
    def _validate_pr_threshold(cls, value: Decimal) -> Decimal:
        if not Decimal("0") < value <= Decimal("100"):
            raise ValueError("pr_green_threshold must be a percentage in (0, 100]")
        return value

    @field_validator("string_variance_threshold_pct")
    @classmethod
    def _validate_variance_threshold(cls, value: Decimal) -> Decimal:
        if not Decimal("0") < value <= Decimal("100"):
            raise ValueError("string_variance_threshold_pct must be a percentage in (0, 100]")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton.

    Cached so that reading configuration inside a request handler costs nothing.
    Tests clear it with ``get_settings.cache_clear()``.
    """
    return Settings()
