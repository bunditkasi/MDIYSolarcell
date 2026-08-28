"""Huawei FusionSolar (SmartPVMS) northbound adapter.

Endpoint family: ``https://<region>.fusionsolar.huawei.com/thirdData/*``
Auth: POST /login with {userName, systemCode}; the reply carries an XSRF-TOKEN
that every later call must echo in a header.

This is the vendor where the specification's Intra-String Peer Comparison works
as written: ``getDevRealKpi`` for a string inverter (devTypeId 1) returns
per-string voltage and current as ``pv1_u``/``pv1_i``, ``pv2_u``/``pv2_i``, and
so on.

FIVE TRAPS, ALL OF THEM ENCODED BELOW
--------------------------------------
0. TWO UNITS, TWO PLACES. Station capacity is MEGAWATTS on getStationList, while
   device active_power is WATTS on getDevRealKpi — and the station KPI's own
   active_power is already kW. Three different scales on one vendor. Each is
   converted where it is read.
1. UNITS. ``active_power`` is documented as kW and actually returns W. This is
   a well-known discrepancy, not a guess — an integration reading it as kW
   reports the fleet at 1000x its real output and nobody notices until the
   numbers reach a board pack. Converted here, once.

2. MPPT GROUPING IS NOT PUBLISHED. The API exposes PV *inputs* (pv1, pv2 …) but
   never says which input belongs to which MPPT, and that mapping is a property
   of the inverter model. Comparing strings across different MPPTs is exactly
   the false-alarm generator the analytics module warns about, so this adapter
   derives the grouping from ``mppt_count`` assuming Huawei's standard
   sequential layout (inputs distributed evenly and in order across MPPTs).
   VERIFY THIS PER MODEL against the datasheet before trusting an alert.

3. PEER GROUPS CAN BE TOO SMALL. Many commercial SUN2000 models give 2 inputs
   per MPPT. Two strings cannot establish a norm — a single fault makes both
   look equally wrong — so ``analyse_device_strings`` will correctly decline to
   flag anything on those. That is a real limit of the hardware layout, not a
   bug: it needs 3+ strings per MPPT, or a different comparison.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time as _time
from datetime import UTC, datetime
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

__all__ = ["HuaweiFusionSolarAdapter", "derive_mppt_index"]

#: devTypeId for a string inverter. Others seen in the wild: 38 residential
#: inverter, 39 battery, 17 grid meter, 10 EMI, 47 power sensor.
DEV_TYPE_STRING_INVERTER = 1

#: FusionSolar throttles per interface; 5 minutes is the interval its own
#: reference implementations use and is comfortably inside the limits.
MIN_POLL_INTERVAL_SECONDS = 300

#: ``getStationRealKpi`` and ``getDevRealKpi`` accept comma-separated ids. 100
#: is the conventional safe batch — larger requests start returning failCode 407
#: (interface access frequency) on busy accounts.
MAX_IDS_PER_CALL = 100

#: Minimum gap between consecutive FusionSolar requests, in seconds.
#:
#: Not a guess: a single uninterrupted sweep of the 51 mapped branches was
#: refused with failCode 407 from roughly the fifth site onward. Huawei throttles
#: on request RATE, and its only response to being exceeded is to lock the
#: interface out — retrying extends the lockout rather than clearing it.
#:
#: Paced here rather than in the scheduler because the limit belongs to this
#: vendor's API, not to any one caller. A CLI rehearsal must be throttled the
#: same way the worker is.
CALL_SPACING_SECONDS = 1.5

_PV_FIELD = re.compile(r"^pv(\d+)_([ui])$")

#: ``inverter_state`` is numeric; anything non-zero and non-512 generally means
#: not producing. Kept as the raw code in telemetry so nothing is lost.
_RUNNING_STATES = {512}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    # FusionSolar uses these sentinels for "no reading". Treating them as real
    # numbers puts -1 kW into the yield series.
    if result in (Decimal("-1"), Decimal("-99999")):
        return None
    return result


def derive_mppt_index(pv_number: int, mppt_count: int | None, pv_input_count: int) -> int:
    """Map a PV input number onto an MPPT index.

    FusionSolar does not publish this mapping, so it is inferred from the
    inverter's MPPT count assuming Huawei's standard sequential layout: inputs
    are distributed evenly and in order, so on a 6-MPPT/12-input model pv1-pv2
    sit on MPPT 0, pv3-pv4 on MPPT 1, and so on.

    Falls back to a single group when ``mppt_count`` is unknown. That is the
    conservative choice for *this* function — the peer-comparison rule then has
    a group to work with — but it means the caller must treat results from
    devices with an unknown MPPT count as lower confidence.
    """
    if not mppt_count or mppt_count <= 0 or pv_input_count <= 0:
        return 0
    inputs_per_mppt = max(1, pv_input_count // mppt_count)
    return (pv_number - 1) // inputs_per_mppt


class HuaweiFusionSolarAdapter(InverterDataSourceInterface):
    vendor_key = "huawei"
    measurement_basis = MeasurementBasis.STRING
    max_sites_per_call = MAX_IDS_PER_CALL
    supports_panel_data = True

    def __init__(
        self,
        *,
        base_url: str,
        secrets_ref: str,
        secrets: SecretsProviderInterface,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._secrets_ref = secrets_ref
        self._secrets = secrets
        self._token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=f"{self._base_url}/thirdData",
            timeout=timeout_seconds,
        )
        #: Cached so panel readings can group inputs by MPPT without another call.
        self._mppt_counts: dict[str, int | None] = {}
        self.call_count = 0
        #: Monotonic timestamp of the last request, for rate pacing. Monotonic
        #: rather than wall clock so an NTP correction mid-sweep cannot make the
        #: gap look longer than it was.
        self._last_call_at: float | None = None

    # -- Session ---------------------------------------------------------- #

    async def _pace(self) -> None:
        """Hold off until ``CALL_SPACING_SECONDS`` have passed since the last call.

        Sleeps only when a call would otherwise be too early, so a sweep that is
        already slow enough — one waiting on the database between sites — pays
        nothing for this.
        """
        now = _time.monotonic()
        if self._last_call_at is not None:
            wait = CALL_SPACING_SECONDS - (now - self._last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
                now = _time.monotonic()
        self._last_call_at = now

    async def authenticate(self) -> None:
        if self._token:
            return

        try:
            credential = await self._secrets.get_credential(self._secrets_ref)
        except SecretNotFoundError as exc:
            raise AuthenticationError(
                f"No credential stored under '{self._secrets_ref}'."
            ) from exc

        user_name = credential.username
        system_code = credential.password
        if not user_name or not system_code:
            raise AuthenticationError(
                f"Credential '{self._secrets_ref}' needs userName and systemCode."
            )

        self.call_count += 1
        try:
            response = await self._client.post(
                "/login", json={"userName": user_name, "systemCode": system_code}
            )
        except httpx.HTTPError as exc:
            raise TransientVendorError(f"FusionSolar login transport error: {exc}") from exc

        # The token arrives as a header on success and, on some deployments,
        # as a cookie. Both are checked because which one you get depends on
        # the regional instance.
        token = response.headers.get("XSRF-TOKEN") or response.cookies.get("XSRF-TOKEN")
        if not token:
            payload = _safe_json(response)
            raise AuthenticationError(
                "FusionSolar login returned no XSRF-TOKEN "
                f"(failCode={payload.get('failCode')})."
            )

        self._token = token
        logger.info("FusionSolar session established for secrets_ref=%s", self._secrets_ref)

    async def _post(
        self, path: str, payload: dict[str, Any], *, _retried: bool = False
    ) -> dict[str, Any]:
        await self.authenticate()
        await self._pace()
        self.call_count += 1

        try:
            response = await self._client.post(
                path, json=payload, headers={"XSRF-TOKEN": self._token or ""}
            )
        except httpx.TimeoutException as exc:
            raise TransientVendorError(f"FusionSolar timed out on {path}") from exc
        except httpx.HTTPError as exc:
            raise TransientVendorError(f"FusionSolar transport error on {path}") from exc

        if response.status_code >= 500:
            raise TransientVendorError(f"FusionSolar {response.status_code} on {path}")
        if response.status_code >= 400:
            raise IngestionError(f"FusionSolar {response.status_code} on {path}")

        body = _safe_json(response)

        if body.get("success"):
            return body

        fail_code = body.get("failCode")

        # 305/401: session gone. Re-login once, then give up rather than loop.
        if fail_code in (305, 401) and not _retried:
            self._token = None
            return await self._post(path, payload, _retried=True)

        # 407: interface access frequency exceeded. Backing off is the only
        # correct response — retrying immediately extends the lockout.
        if fail_code == 407:
            raise QuotaExceededError(
                f"FusionSolar refused {path} with failCode 407 (access frequency). "
                f"Poll no more often than every {MIN_POLL_INTERVAL_SECONDS}s."
            )

        raise IngestionError(f"FusionSolar {path} failed with failCode={fail_code}")

    # -- Reads ------------------------------------------------------------ #

    async def list_sites(self) -> list[VendorSite]:
        body = await self._post("/getStationList", {})
        container = body.get("data") or {}
        # Older deployments return a bare list; newer ones wrap it in {list: []}.
        rows = container.get("list") if isinstance(container, dict) else container

        sites: list[VendorSite] = []
        for row in rows or []:
            code = row.get("plantCode") or row.get("stationCode")
            if not code:
                continue
            sites.append(
                VendorSite(
                    vendor_site_id=str(code),
                    name=str(row.get("plantName") or row.get("stationName") or ""),
                    # capacity is MW on this API. Storing it unconverted
                    # reports a 40 kWp branch as 0.04 kWp, and every yield-per-kWp
                    # figure derived from it is then 1000x too high.
                    capacity_kwp=_mw_to_kw(row.get("capacity")),
                    latitude=_decimal(row.get("latitude")),
                    longitude=_decimal(row.get("longitude")),
                )
            )
        return sites

    async def list_devices(self, vendor_site_id: str) -> list[VendorDevice]:
        body = await self._post("/getDevList", {"stationCodes": vendor_site_id})
        devices: list[VendorDevice] = []

        for row in body.get("data") or []:
            if row.get("devTypeId") != DEV_TYPE_STRING_INVERTER:
                continue
            serial = row.get("esnCode")
            if not serial:
                continue

            # `invType` carries the model string; the MPPT count is not in the
            # payload, so it stays None until someone records it from the
            # datasheet. derive_mppt_index() degrades safely when it is None.
            self._mppt_counts[str(serial)] = None
            devices.append(
                VendorDevice(
                    vendor_site_id=vendor_site_id,
                    serial_number=str(serial),
                    name=row.get("devName"),
                    model=row.get("invType"),
                    capacity_kw=_decimal(row.get("capacity")),
                    mppt_count=None,
                )
            )
        return devices

    async def fetch_site_readings(self, vendor_site_ids: list[str]) -> list[SiteReading]:
        if not vendor_site_ids:
            return []
        if len(vendor_site_ids) > MAX_IDS_PER_CALL:
            raise ValueError(
                f"FusionSolar accepts at most {MAX_IDS_PER_CALL} station codes per call."
            )

        body = await self._post(
            "/getStationRealKpi", {"stationCodes": ",".join(vendor_site_ids)}
        )
        now = datetime.now(UTC)
        readings: list[SiteReading] = []

        for row in body.get("data") or []:
            items = row.get("dataItemMap") or {}
            code = row.get("stationCode")
            if not code:
                continue
            readings.append(
                SiteReading(
                    vendor_site_id=str(code),
                    # This endpoint carries no timestamp; the reading is "now".
                    measured_at=now,
                    # Station KPI reports kW here, unlike the device endpoint
                    # where active_power is watts.
                    active_power_kw=_decimal(
                        items.get("active_power") or items.get("current_power")
                    ),
                    daily_yield_kwh=_decimal(items.get("day_power")),
                    monthly_yield_kwh=_decimal(items.get("month_power")),
                    total_yield_kwh=_decimal(items.get("total_power")),
                    status=_station_status(items.get("real_health_state")),
                )
            )
        return readings

    async def fetch_device_readings(self, vendor_site_id: str) -> list[DeviceReading]:
        serials = [d.serial_number for d in await self.list_devices(vendor_site_id)]
        if not serials:
            return []

        body = await self._post(
            "/getDevRealKpi",
            {"devIds": ",".join(serials), "devTypeId": DEV_TYPE_STRING_INVERTER},
        )
        now = datetime.now(UTC)
        readings: list[DeviceReading] = []

        for row in body.get("data") or []:
            items = row.get("dataItemMap") or {}
            serial = row.get("devId") or row.get("esnCode")
            if not serial:
                continue

            state = _decimal(items.get("inverter_state"))
            readings.append(
                DeviceReading(
                    serial_number=str(serial),
                    measured_at=now,
                    # THE UNIT TRAP: documented as kW, returns W.
                    active_power_kw=_watts_to_kw(items.get("active_power")),
                    daily_yield_kwh=_decimal(items.get("day_cap")),
                    total_yield_kwh=_decimal(items.get("total_cap")),
                    # Phase A stands in for single-figure grid voltage.
                    grid_voltage=_decimal(items.get("a_u")),
                    grid_current=_decimal(items.get("a_i")),
                    status_code=int(state) if state is not None else None,
                    raw={
                        "inverter_state": items.get("inverter_state"),
                        "temperature": items.get("temperature"),
                        "efficiency": items.get("efficiency"),
                        "elec_freq": items.get("elec_freq"),
                        "running": state is not None and int(state) in _RUNNING_STATES,
                    },
                )
            )
        return readings

    async def fetch_panel_readings(self, vendor_site_id: str) -> list[PanelReading]:
        """Per-string voltage and current — the data the spec's rule needs."""
        devices = await self.list_devices(vendor_site_id)
        serials = [d.serial_number for d in devices]
        if not serials:
            return []

        body = await self._post(
            "/getDevRealKpi",
            {"devIds": ",".join(serials), "devTypeId": DEV_TYPE_STRING_INVERTER},
        )
        now = datetime.now(UTC)
        readings: list[PanelReading] = []

        for row in body.get("data") or []:
            items = row.get("dataItemMap") or {}
            serial = row.get("devId") or row.get("esnCode")
            if not serial:
                continue

            strings = _extract_pv_inputs(items)
            if not strings:
                continue

            mppt_count = self._mppt_counts.get(str(serial))
            input_count = len(strings)

            for pv_number in sorted(strings):
                voltage, current = strings[pv_number]
                power_kw = None
                if voltage is not None and current is not None:
                    power_kw = (voltage * current / Decimal("1000")).quantize(
                        Decimal("0.001")
                    )
                readings.append(
                    PanelReading(
                        serial_number=str(serial),
                        measured_at=now,
                        mppt_index=derive_mppt_index(pv_number, mppt_count, input_count),
                        string_index=pv_number,
                        pv_voltage=voltage,
                        pv_current=current,
                        pv_power_kw=power_kw,
                    )
                )
        return readings

    async def fetch_alarms(self, vendor_site_id: str) -> list[VendorAlarm]:
        """Not implemented.

        FusionSolar exposes alarms through /getAlarmList, which is gated
        separately from the monitoring interfaces on many accounts. Returning
        an empty list keeps ingestion working; device status and the analytics
        engine still raise alerts of our own.
        """
        logger.debug("FusionSolar alarm ingestion is not implemented; skipping.")
        return []

    async def close(self) -> None:
        await self._client.aclose()


def _mw_to_kw(value: Any) -> Decimal | None:
    """Station capacity arrives in megawatts."""
    mw = _decimal(value)
    return None if mw is None else (mw * Decimal("1000")).quantize(Decimal("0.001"))


def _station_status(value: Any) -> str:
    """Map real_health_state onto the normalised vocabulary.

    3 is healthy and 1 is disconnected; anything else is a fault. Unknown values
    map to FAULT rather than ONLINE — a state nobody recognised should be looked
    at, not assumed to be fine.
    """
    if value is None:
        return "UNKNOWN"
    try:
        state = int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return "UNKNOWN"
    if state == 3:
        return "ONLINE"
    if state == 1:
        return "OFFLINE"
    return "FAULT"


def _watts_to_kw(value: Any) -> Decimal | None:
    watts = _decimal(value)
    return None if watts is None else (watts / Decimal("1000")).quantize(Decimal("0.001"))


def _extract_pv_inputs(items: dict[str, Any]) -> dict[int, tuple[Decimal | None, Decimal | None]]:
    """Collect pvN_u / pvN_i pairs out of a flat dataItemMap.

    Parsed by pattern rather than by a fixed list because the number of inputs
    varies by model — small residential units have 2, large commercial ones up
    to 24 — and a hard-coded range would silently drop strings on big inverters.
    """
    found: dict[int, list[Decimal | None]] = {}
    for key, value in items.items():
        match = _PV_FIELD.match(str(key))
        if not match:
            continue
        number = int(match.group(1))
        slot = 0 if match.group(2) == "u" else 1
        found.setdefault(number, [None, None])[slot] = _decimal(value)

    return {number: (pair[0], pair[1]) for number, pair in found.items()}


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
