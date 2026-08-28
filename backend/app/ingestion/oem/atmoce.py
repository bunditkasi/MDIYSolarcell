"""Atmoce Cloud adapter.

Written against Atmoce-Cloud API Reference V1.2.2 (2025-02-12), then corrected
against a working client after the documented calls were rejected live.

THREE PLACES THE PUBLISHED DOCUMENT IS WRONG
--------------------------------------------
1. The session endpoint is /openapi/v1/auth/token. The document gives
   /auth/auth_token, which on the live service is the OAuth 2.0 flow and
   rejects grant_type=system outright (code 465).
2. The session token needs the "Bearer " scheme on the Authorization header.
   A bare token is treated as unauthenticated.
3. Several response fields are misspelled in the document — dailySoalrGeneration
   and lifetimeSoalrGeneration are dailySolarGeneration and
   lifetimeSolarGeneration on the wire. Reading the documented spelling returns
   None, which is worse than an error: the yield series just goes quiet.

TWO FACTS ABOUT THIS VENDOR SHAPE THE WHOLE ADAPTER
---------------------------------------------------
1. QUOTA: 10,000 calls per month per token, 5 concurrent. That is the tightest
   constraint in the system. ``getSitesLastPower`` takes up to 100 site ids per
   call, and using it is the only reason a 127-site fleet fits in the budget —
   polling sites individually would need ~440,000 calls a month.

2. NO CURRENT, NO VOLTAGE, NO MPPT. Atmoce is a microinverter platform. All 63
   pages of the reference contain zero occurrences of "MPPT", and the only
   voltage fields anywhere are ``gridVoltage``/``gridVoltageA-C`` on the
   gateway. Per panel it publishes ``pvData[].pvPower`` and cumulative
   generation, and nothing else. The specification's current-based
   Intra-String Peer Comparison is therefore impossible here; panel POWER is
   the correct substitute. See ``app/analytics/string_variance.py``.

Session handling matters for the same quota reason: the access token is valid
for 30 days and the refresh token for 180, and generating a session is itself a
metered call. This adapter caches the token and refreshes rather than
re-authenticating.
"""

from __future__ import annotations

import logging
import time as _time
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.core.secrets.base import SecretNotFoundError, SecretsProviderInterface
from app.domain.models import MeasurementBasis
from app.ingestion.base import (
    AuthenticationError,
    DeviceReading,
    IngestionError,
    InverterDataSourceInterface,
    PanelReading,
    QuotaExceededError,
    SiteReading,
    TransientVendorError,
    VendorAlarm,
    VendorDevice,
    VendorSite,
)

logger = logging.getLogger(__name__)

__all__ = ["AtmoceAdapter", "AtmoceSession"]

#: Atmoce's own words: "Each API token can be used to make up to 10,000 calls
#: per month" and "up to 5 calls at a time".
MONTHLY_CALL_QUOTA = 10_000
MAX_CONCURRENT_CALLS = 5

#: "Up to 100 sites can be queried at a time."
MAX_SITES_PER_CALL = 100

#: How long a ``getMIsLastData`` response may be reused, in seconds.
#:
#: Sized to span one site's turn in a sweep and nothing more — see
#: ``_mi_last_data``. Raising it toward the poll interval would start serving
#: stale readings to the map.
MI_CACHE_SECONDS = 120

#: Vendor status strings -> the normalised vocabulary. From section 5.1.1.5.
_SITE_STATUS = {
    "Normal": "ONLINE",
    "Fault": "FAULT",
    "Offline": "OFFLINE",
    "Uncompleted": "UNKNOWN",
}

#: Microinverter status strings, from section 5.2.1.5. "waitLight" is a
#: microinverter with no sun on it — normal at dawn and dusk, and emphatically
#: not a fault, which is why it maps to OFFLINE rather than FAULT.
_MI_STATUS = {
    "onGrid": "ONLINE",
    "standby": "ONLINE",
    "waitLight": "OFFLINE",
    "Offline": "OFFLINE",
    "offManual": "OFFLINE",
    "upgrading": "OFFLINE",
    "offError": "FAULT",
}


class AtmoceSession:
    """Holds the tokens so they survive between polls."""

    def __init__(self) -> None:
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.obtained_at: datetime | None = None

    @property
    def is_established(self) -> bool:
        return self.access_token is not None


def _decimal(value: Any) -> Decimal | None:
    """Vendor number -> Decimal, or None.

    Returns None rather than 0 for anything unparseable. A missing reading and
    a reading of zero are different claims, and conflating them puts a fake
    zero into the yield series.
    """
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _watts_to_kw(value: Any) -> Decimal | None:
    """Atmoce reports power in W (``generationPower``, ``pvPower``). We store kW."""
    watts = _decimal(value)
    return None if watts is None else watts / Decimal("1000")


def _dmy_date(value: Any) -> date | None:
    """Parse Atmoce's ``gridTiedTime``, which is DD/MM/YYYY.

    Day first, not month first. "27/01/2026" is 27 January; read the American
    way it is an invalid date, and the ambiguous ones — "03/04/2026" — would be
    silently wrong by two months rather than failing.
    """
    if not value:
        return None
    try:
        return datetime.strptime(str(value).strip(), "%d/%m/%Y").date()
    except (ValueError, TypeError):
        logger.debug("Unparseable gridTiedTime %r", value)
        return None


def _epoch_ms(value: Any) -> datetime:
    """``lastReportedTime`` is epoch milliseconds."""
    millis = _decimal(value)
    if millis is None:
        return datetime.now(UTC)
    return datetime.fromtimestamp(float(millis) / 1000, tz=UTC)


class AtmoceAdapter(InverterDataSourceInterface):
    vendor_key = "atmoce"
    measurement_basis = MeasurementBasis.PANEL
    max_sites_per_call = MAX_SITES_PER_CALL
    supports_panel_data = True

    def __init__(
        self,
        *,
        base_url: str,
        secrets_ref: str,
        secrets: SecretsProviderInterface,
        session: AtmoceSession | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secrets_ref = secrets_ref
        self._secrets = secrets
        self._session = session or AtmoceSession()
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/openapi/v1",
            timeout=timeout_seconds,
            # Matches the vendor's stated concurrency ceiling. Exceeding it
            # earns 429s that cost quota without returning data.
            limits=httpx.Limits(max_connections=MAX_CONCURRENT_CALLS),
        )
        #: Last ``getMIsLastData`` payload per site, with the monotonic time it
        #: was fetched. See ``_mi_last_data``.
        self._mi_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        #: Site ids the bulk endpoint refuses. See ``_last_power``.
        self._unreadable_sites: set[str] = set()
        #: Calls made by this instance, for quota accounting in the worker log.
        self.call_count = 0

    # -- Session ---------------------------------------------------------- #

    async def authenticate(self) -> None:
        if self._session.is_established:
            return

        try:
            credential = await self._secrets.get_credential(self._secrets_ref)
        except SecretNotFoundError as exc:
            raise AuthenticationError(
                f"No credential stored under '{self._secrets_ref}'. Set it in the "
                f"secrets provider; it is never read from the database."
            ) from exc

        # Atmoce calls these app_key / app_secret. They arrive through the
        # generic Credential fields.
        app_key = credential.api_key or credential.username
        app_secret = credential.password
        if not app_key or not app_secret:
            raise AuthenticationError(
                f"Credential '{self._secrets_ref}' is missing app_key or app_secret."
            )

        payload = await self._post(
            "/auth/token",
            json={
                "app_key": app_key,
                "app_secret": app_secret,
                "grant_type": "system",
            },
            authenticated=False,
        )

        data = payload.get("data") or {}
        access = data.get("access_token")
        if not access:
            raise AuthenticationError("Atmoce returned no access_token.")

        self._session.access_token = access
        self._session.refresh_token = data.get("refresh_token")
        self._session.obtained_at = datetime.now(UTC)
        # Deliberately logs the reference, never the token.
        logger.info("Atmoce session established for secrets_ref=%s", self._secrets_ref)

    async def _refresh(self) -> None:
        """Trade the long-lived token for a new session token.

        Cheaper than re-authenticating and, more importantly, it is what the
        vendor intends: the access token lasts 30 days, the refresh token 180.
        """
        if not self._session.refresh_token:
            self._session.access_token = None
            await self.authenticate()
            return

        payload = await self._post(
            "/auth/token",
            json={"grant_type": "refresh", "refresh_token": self._session.refresh_token},
            authenticated=False,
        )
        data = payload.get("data") or {}
        if not data.get("access_token"):
            # Refresh token has expired too (180 days). Start over.
            self._session = AtmoceSession()
            await self.authenticate()
            return

        self._session.access_token = data["access_token"]
        self._session.refresh_token = data.get("refresh_token", self._session.refresh_token)
        self._session.obtained_at = datetime.now(UTC)

    # -- HTTP ------------------------------------------------------------- #

    def _headers(self) -> dict[str, str]:
        # "Bearer " prefix required. A bare token is silently unauthenticated.
        return {"Authorization": f"Bearer {self._session.access_token or ''}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        authenticated: bool = True,
        _retried: bool = False,
    ) -> dict[str, Any]:
        self.call_count += 1
        try:
            response = await self._client.request(
                method,
                path,
                params=params,
                json=json,
                headers=self._headers() if authenticated else None,
            )
        except httpx.TimeoutException as exc:
            raise TransientVendorError(f"Atmoce timed out on {path}") from exc
        except httpx.HTTPError as exc:
            raise TransientVendorError(f"Atmoce transport error on {path}: {exc}") from exc

        if response.status_code == 429:
            raise QuotaExceededError(
                "Atmoce returned 429. The monthly call budget is 10,000; check the "
                "poll interval and daylight window before retrying."
            )

        if response.status_code == 401 and authenticated and not _retried:
            # Session expired. Refresh once, then retry exactly once — an
            # unbounded retry loop against 401 burns quota fast.
            await self._refresh()
            return await self._request(
                method, path, params=params, json=json, authenticated=True, _retried=True
            )

        if response.status_code == 401:
            raise AuthenticationError(f"Atmoce rejected the session on {path}.")

        if response.status_code >= 500:
            raise TransientVendorError(f"Atmoce {response.status_code} on {path}")

        if response.status_code >= 400:
            raise IngestionError(f"Atmoce {response.status_code} on {path}")

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise TransientVendorError(f"Atmoce returned non-JSON on {path}") from exc

        if not payload.get("success", False):
            # `reason` can echo request content, so it is logged rather than
            # raised into a message that might reach a user.
            logger.warning(
                "Atmoce reported failure on %s: code=%s reason=%s",
                path,
                payload.get("code"),
                payload.get("reason"),
            )
            raise IngestionError(f"Atmoce call failed on {path} (code={payload.get('code')})")

        return payload

    async def _get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self._request("GET", path, **kwargs)

    async def _post(self, path: str, **kwargs: Any) -> dict[str, Any]:
        return await self._request("POST", path, **kwargs)

    # -- Reads ------------------------------------------------------------ #

    async def list_sites(self) -> list[VendorSite]:
        sites: list[VendorSite] = []
        page = 1
        while True:
            payload = await self._get("/sites/getSites", params={"page": page})
            rows = payload.get("data") or []
            for row in rows:
                sites.append(
                    VendorSite(
                        vendor_site_id=str(row.get("siteId")),
                        name=str(row.get("name") or ""),
                        # solarCapacity is ALREADY kWp, despite the reference
                        # describing it as watts-peak. Verified against the
                        # workbook: the API returns 37.18 for PRMB, PKKB, PPYB
                        # and PKRP, and all four are recorded as 37.18 kWp.
                        # Treating it as watts made every vendor-registered
                        # branch 1000x too small, which would then divide into
                        # every kWh/kWp and status figure built on it.
                        capacity_kwp=_decimal(row.get("solarCapacity")),
                        # Atmoce publishes real positions. Taking them means a
                        # branch the roster has never seen still lands on the
                        # map, instead of waiting for the workbook to catch up.
                        latitude=_decimal(row.get("latitude")),
                        longitude=_decimal(row.get("longitude")),
                        battery_capacity_kwh=_decimal(row.get("batteryCapacity")),
                        grid_tied_on=_dmy_date(row.get("gridTiedTime")),
                    )
                )
            # The API signals the last page explicitly. Stopping on an empty
            # page instead would spend one wasted call every sweep.
            if payload.get("pageEnd") == 1 or not rows:
                break
            page += 1
        return sites

    async def list_devices(self, vendor_site_id: str) -> list[VendorDevice]:
        payload = await self._get(
            "/device/getDevicesBySite", params={"siteId": vendor_site_id}
        )
        devices: list[VendorDevice] = []
        for row in payload.get("data") or []:
            # Field is spelled "deivceSN" in the reference — a vendor typo, not
            # one of ours. Both spellings are accepted so a future correction
            # on their side does not silently return nothing.
            serial = row.get("deivceSN") or row.get("deviceSN")
            if not serial:
                continue
            devices.append(
                VendorDevice(
                    vendor_site_id=str(row.get("siteId") or vendor_site_id),
                    serial_number=str(serial),
                    model=row.get("deviceMode"),
                    capacity_kw=_watts_to_kw(row.get("MICapacity")),
                    # Microinverters have no MPPT.
                    mppt_count=None,
                )
            )
        return devices

    async def fetch_site_readings(self, vendor_site_ids: list[str]) -> list[SiteReading]:
        """The workhorse call — up to 100 sites for the price of one request."""
        if not vendor_site_ids:
            return []
        if len(vendor_site_ids) > MAX_SITES_PER_CALL:
            raise ValueError(
                f"Atmoce accepts at most {MAX_SITES_PER_CALL} site ids per call; "
                f"got {len(vendor_site_ids)}. Batch before calling."
            )

        wanted = [i for i in vendor_site_ids if i not in self._unreadable_sites]
        if not wanted:
            return []
        return await self._last_power(wanted)

    async def _last_power(self, vendor_site_ids: list[str]) -> list[SiteReading]:
        """Read last power for a batch, isolating any site the API refuses.

        The endpoint rejects the ENTIRE batch — code 1000, "Failed to obtain
        data" — when any single site in it has never reported. One branch
        commissioned but not yet generating therefore blinds the whole fleet's
        cheap sweep, which is what pushed ingestion onto the per-site endpoint
        and put the monthly call budget at risk.

        So a failed batch is bisected rather than abandoned: ~14 extra calls
        finds the offender in a batch of 100, against 100 for asking one site at
        a time. The offender is then remembered, so the cost is paid once per
        worker lifetime rather than once per sweep. A restart re-tests it, which
        is how a branch that starts reporting gets picked back up.

        Only vendor-level rejections are bisected, and the three subclasses of
        IngestionError are re-raised first. That order is load-bearing: quota
        exhaustion, a rejected session and a timeout are all properties of the
        REQUEST rather than of the sites in it, so bisecting them would split a
        failure that is going to recur, double the load on an endpoint already
        in trouble, and — worst — end by blacklisting healthy branches as
        unreadable.
        """
        try:
            payload = await self._get(
                "/sites/getSitesLastPower",
                params={"siteIds": ",".join(vendor_site_ids)},
            )
        except (QuotaExceededError, AuthenticationError, TransientVendorError):
            raise
        except IngestionError:
            if len(vendor_site_ids) == 1:
                site_id = vendor_site_ids[0]
                self._unreadable_sites.add(site_id)
                logger.warning(
                    "Atmoce has no last-power data for site %s. Skipping it for "
                    "the rest of this session; it will be retried on restart.",
                    site_id,
                )
                return []
            middle = len(vendor_site_ids) // 2
            first = await self._last_power(vendor_site_ids[:middle])
            second = await self._last_power(vendor_site_ids[middle:])
            return first + second

        readings: list[SiteReading] = []
        for row in payload.get("data") or []:
            readings.append(
                SiteReading(
                    vendor_site_id=str(row.get("siteId")),
                    measured_at=_epoch_ms(row.get("lastReportedTime")),
                    active_power_kw=_watts_to_kw(row.get("solarGenerationPower")),
                    # These three are already kWh; note the vendor's spelling.
                    daily_yield_kwh=_decimal(row.get("dailySolarGeneration")),
                    monthly_yield_kwh=_decimal(row.get("monthlySolarGeneration")),
                    total_yield_kwh=_decimal(row.get("lifetimeSolarGeneration")),
                    status=_SITE_STATUS.get(str(row.get("status")), "UNKNOWN"),
                )
            )
        return readings

    async def _mi_last_data(self, vendor_site_id: str) -> dict[str, Any]:
        """One microinverter payload per site, shared by both readers.

        ``fetch_device_readings`` and ``fetch_panel_readings`` want different
        parts of the SAME response, and the detail sweep calls them one after
        the other for each branch. Without this they issue two identical
        requests per site — 214 calls a day across the fleet instead of 107,
        which alone is enough to push the month past its 10,000-call budget.

        The window is deliberately short. It exists to collapse two calls that
        are milliseconds apart, not to serve data across sweeps: the poll
        interval is 15 minutes, so no scheduled run can ever read from it.
        """
        cached = self._mi_cache.get(vendor_site_id)
        if cached is not None and (_time.monotonic() - cached[0]) < MI_CACHE_SECONDS:
            return cached[1]

        payload = await self._get(
            "/microInverter/getMIsLastData", params={"siteId": vendor_site_id}
        )
        self._mi_cache[vendor_site_id] = (_time.monotonic(), payload)
        return payload

    async def fetch_device_readings(self, vendor_site_id: str) -> list[DeviceReading]:
        payload = await self._mi_last_data(vendor_site_id)
        readings: list[DeviceReading] = []
        for row in payload.get("data") or []:
            serial = row.get("SN")
            if not serial:
                continue
            readings.append(
                DeviceReading(
                    serial_number=str(serial),
                    measured_at=_epoch_ms(row.get("lastReportedTime")),
                    active_power_kw=_watts_to_kw(row.get("generationPower")),
                    daily_yield_kwh=_decimal(row.get("dailyGeneration")),
                    total_yield_kwh=_decimal(row.get("lifetimeGeneration")),
                    # Microinverters report no grid voltage or current, and no
                    # numeric status code — only the status strings above.
                    grid_voltage=None,
                    grid_current=None,
                    status_code=None,
                    raw={"status": row.get("status")},
                )
            )
        return readings

    async def fetch_panel_readings(self, vendor_site_id: str) -> list[PanelReading]:
        """Per-panel power.

        ``pvData`` is null for a 1-in-1 microinverter (one panel, no branches) —
        the reference says so explicitly. That is an absence of branches, not a
        failure, so those devices simply contribute nothing here.
        """
        payload = await self._mi_last_data(vendor_site_id)
        readings: list[PanelReading] = []
        for row in payload.get("data") or []:
            serial = row.get("SN")
            if not serial:
                continue
            measured_at = _epoch_ms(row.get("lastReportedTime"))

            for branch in row.get("pvData") or []:
                number = branch.get("pvNumber")
                if number is None:
                    continue
                readings.append(
                    PanelReading(
                        serial_number=str(serial),
                        measured_at=measured_at,
                        # No MPPT exists on this hardware; 0 is the agreed
                        # placeholder and string_index carries the panel number.
                        mppt_index=0,
                        string_index=int(number),
                        pv_voltage=None,
                        pv_current=None,
                        pv_power_kw=_watts_to_kw(branch.get("pvPower")),
                    )
                )
        return readings

    async def fetch_alarms(self, vendor_site_id: str) -> list[VendorAlarm]:
        """Active vendor alarms.

        CAVEAT: the reference documents the alarm payload far less precisely
        than the monitoring endpoints. The field names below
        (occurTime / severity / alarmName / alarmId) are a best reading of it
        and should be checked against one real response before alarm ingestion
        is trusted. Monitoring data is unaffected either way.
        """
        payload = await self._get(
            "/device/getAlarmsBySite", params={"siteId": vendor_site_id}
        )
        alarms: list[VendorAlarm] = []
        for row in payload.get("data") or []:
            alarms.append(
                VendorAlarm(
                    vendor_site_id=vendor_site_id,
                    serial_number=(
                        str(row["deviceSN"]) if row.get("deviceSN") else None
                    ),
                    raised_at=_epoch_ms(row.get("occurTime") or row.get("raiseTime")),
                    severity=_normalise_severity(row.get("severity") or row.get("level")),
                    message=str(row.get("alarmName") or row.get("message") or "Vendor alarm"),
                    vendor_code=(
                        str(row["alarmId"]) if row.get("alarmId") is not None else None
                    ),
                )
            )
        return alarms

    async def close(self) -> None:
        await self._client.aclose()


def _normalise_severity(value: Any) -> str:
    """Map a vendor severity onto CRITICAL / MAJOR / MINOR.

    Unknown values become MAJOR rather than MINOR: an alarm nobody recognises
    should be looked at, not filed at the bottom of the list.
    """
    text = str(value or "").strip().lower()
    if text in {"1", "critical", "urgent", "fatal", "severe"}:
        return "CRITICAL"
    if text in {"3", "minor", "warning", "info", "low"}:
        return "MINOR"
    return "MAJOR"
