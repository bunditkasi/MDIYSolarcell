"""Adapter tests against payloads shaped like the published references.

These run entirely offline. No vendor account, no network, no credentials — the
HTTP layer is replaced with a transport that replays canned responses. That is
deliberate: the thing worth testing is the FIELD MAPPING, and mapping bugs are
exactly what a live smoke test hides, because a live call that returns 200 looks
like success whether or not the numbers were read correctly.

The payload shapes come from:
  * Atmoce-Cloud API Reference V1.2.2, sections 5.1.1 and 5.2.1
  * FusionSolar northbound getDevRealKpi dataItemMap
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.core.secrets.base import Credential, SecretsProviderInterface
from app.domain.models import MeasurementBasis
from app.ingestion.base import AuthenticationError, QuotaExceededError
from app.ingestion.oem.atmoce import AtmoceAdapter
from app.ingestion.oem.huawei import HuaweiFusionSolarAdapter, derive_mppt_index


class StubSecrets(SecretsProviderInterface):
    def __init__(self, credential: Credential | None = None) -> None:
        self._credential = credential or Credential(
            username="user", password="secret", api_key="key"
        )

    async def get_credential(self, secrets_ref: str) -> Credential:
        return self._credential

    async def healthy(self) -> bool:
        return True


def atmoce_adapter(handler) -> AtmoceAdapter:
    adapter = AtmoceAdapter(
        base_url="https://example.invalid",
        secrets_ref="test_ref",
        secrets=StubSecrets(),
    )
    adapter._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        transport=httpx.MockTransport(handler),
        base_url="https://example.invalid/openapi/v1",
    )
    return adapter


def huawei_adapter(handler) -> HuaweiFusionSolarAdapter:
    adapter = HuaweiFusionSolarAdapter(
        base_url="https://example.invalid",
        secrets_ref="test_ref",
        secrets=StubSecrets(),
    )
    adapter._client = httpx.AsyncClient(  # noqa: SLF001 - test seam
        transport=httpx.MockTransport(handler),
        base_url="https://example.invalid/thirdData",
    )
    return adapter


# --------------------------------------------------------------------------- #
# Atmoce
# --------------------------------------------------------------------------- #


async def test_atmoce_site_readings_convert_watts_to_kilowatts() -> None:
    """`solarGenerationPower` is watts. Storing it unconverted would report the
    fleet at 1000x its real output."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200,
                json={"success": True, "data": {"access_token": "t", "refresh_token": "r"}},
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "siteId": "1",
                        "lastReportedTime": 1_700_000_000_000,
                        "status": "Normal",
                        "solarGenerationPower": 42_000,
                        "dailySolarGeneration": 123.45,
                        "lifetimeSolarGeneration": 98_765.4,
                    }
                ],
            },
        )

    adapter = atmoce_adapter(handler)
    await adapter.authenticate()
    readings = await adapter.fetch_site_readings(["1"])

    assert len(readings) == 1
    assert readings[0].active_power_kw == Decimal("42")
    assert readings[0].daily_yield_kwh == Decimal("123.45")
    assert readings[0].status == "ONLINE"
    await adapter.close()


async def test_atmoce_wait_light_is_offline_not_fault() -> None:
    """`waitLight` means "no sun yet" — normal at dawn. Reporting it as a fault
    would raise a critical alert on every site every morning."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"siteId": "1", "status": "Uncompleted", "lastReportedTime": 0}],
            },
        )

    adapter = atmoce_adapter(handler)
    await adapter.authenticate()
    reading = (await adapter.fetch_site_readings(["1"]))[0]
    assert reading.status == "UNKNOWN"
    await adapter.close()


async def test_atmoce_panel_readings_have_power_but_no_current() -> None:
    """The defining property of this vendor: per-panel power, never current."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "siteId": "1",
                        "SN": "MI-001",
                        "lastReportedTime": 1_700_000_000_000,
                        "status": "onGrid",
                        "generationPower": 1_200,
                        "dailyGeneration": 8.4,
                        "pvData": [
                            {"pvNumber": 1, "pvPower": 400},
                            {"pvNumber": 2, "pvPower": 380},
                        ],
                    }
                ],
            },
        )

    adapter = atmoce_adapter(handler)
    await adapter.authenticate()
    panels = await adapter.fetch_panel_readings("1")

    assert len(panels) == 2
    assert panels[0].pv_power_kw == Decimal("0.4")
    assert panels[0].pv_current is None
    assert panels[0].pv_voltage is None
    # No MPPT exists on microinverters; 0 is the agreed placeholder.
    assert {p.mppt_index for p in panels} == {0}
    assert [p.string_index for p in panels] == [1, 2]
    await adapter.close()


async def test_atmoce_one_in_one_microinverter_yields_no_panel_rows() -> None:
    """The reference says pvData is null for a 1-in-1 unit. Absence of branches
    is not a failure, and must not become a row of zeros."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"siteId": "1", "SN": "MI-002", "pvData": None}],
            },
        )

    adapter = atmoce_adapter(handler)
    await adapter.authenticate()
    assert await adapter.fetch_panel_readings("1") == []
    await adapter.close()


async def test_atmoce_batch_larger_than_one_hundred_is_refused() -> None:
    """The vendor caps a call at 100 site ids. Failing loudly here beats a 400
    that costs quota and returns nothing."""
    adapter = atmoce_adapter(lambda r: httpx.Response(200, json={"success": True}))
    with pytest.raises(ValueError, match="at most 100"):
        await adapter.fetch_site_readings([str(i) for i in range(101)])
    await adapter.close()


async def test_atmoce_429_raises_quota_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        return httpx.Response(429, json={"success": False, "code": 429})

    adapter = atmoce_adapter(handler)
    await adapter.authenticate()
    with pytest.raises(QuotaExceededError):
        await adapter.fetch_site_readings(["1"])
    await adapter.close()


async def test_atmoce_missing_credential_is_an_authentication_error() -> None:
    class Empty(SecretsProviderInterface):
        async def get_credential(self, secrets_ref: str) -> Credential:
            return Credential()

        async def healthy(self) -> bool:
            return True

    adapter = AtmoceAdapter(
        base_url="https://example.invalid", secrets_ref="ref", secrets=Empty()
    )
    with pytest.raises(AuthenticationError, match="app_key"):
        await adapter.authenticate()
    await adapter.close()


async def test_atmoce_declares_panel_basis() -> None:
    adapter = atmoce_adapter(lambda r: httpx.Response(200, json={"success": True}))
    assert adapter.measurement_basis is MeasurementBasis.PANEL
    assert adapter.max_sites_per_call == 100
    await adapter.close()


# --------------------------------------------------------------------------- #
# Huawei FusionSolar
# --------------------------------------------------------------------------- #

_DEV_KPI = {
    "success": True,
    "data": [
        {
            "devId": "INV-001",
            "dataItemMap": {
                # Documented as kW, actually returns W.
                "active_power": 25_400,
                "day_cap": 148.6,
                "total_cap": 512_300.0,
                "a_u": 402.1,
                "a_i": 36.5,
                "inverter_state": 512,
                "temperature": 41.2,
                "pv1_u": 620.4,
                "pv1_i": 8.42,
                "pv2_u": 619.8,
                "pv2_i": 8.38,
                "pv3_u": 618.9,
                "pv3_i": 8.40,
                "pv4_u": 621.0,
                "pv4_i": 2.10,
            },
        }
    ],
}


def _huawei_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/login"):
        return httpx.Response(200, json={"success": True}, headers={"XSRF-TOKEN": "tok"})
    if path.endswith("/getDevList"):
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {"esnCode": "INV-001", "devTypeId": 1, "invType": "SUN2000-60KTL"},
                    {"esnCode": "MTR-001", "devTypeId": 17},
                ],
            },
        )
    if path.endswith("/getDevRealKpi"):
        return httpx.Response(200, json=_DEV_KPI)
    return httpx.Response(200, json={"success": True, "data": []})


async def test_huawei_active_power_is_converted_from_watts() -> None:
    """The trap this integration is most likely to fall into: the field is
    documented in kW and returns W. 25,400 W is 25.4 kW, not 25,400 kW."""
    adapter = huawei_adapter(_huawei_handler)
    readings = await adapter.fetch_device_readings("ST-1")

    assert len(readings) == 1
    assert readings[0].active_power_kw == Decimal("25.400")
    assert readings[0].daily_yield_kwh == Decimal("148.6")
    assert readings[0].status_code == 512
    await adapter.close()


async def test_huawei_only_string_inverters_are_ingested() -> None:
    """A grid meter (devTypeId 17) is not a PV inverter and has no strings."""
    adapter = huawei_adapter(_huawei_handler)
    devices = await adapter.list_devices("ST-1")

    assert [d.serial_number for d in devices] == ["INV-001"]
    await adapter.close()


async def test_huawei_extracts_every_pv_string_pair() -> None:
    adapter = huawei_adapter(_huawei_handler)
    panels = await adapter.fetch_panel_readings("ST-1")

    assert [p.string_index for p in panels] == [1, 2, 3, 4]
    assert panels[0].pv_voltage == Decimal("620.4")
    assert panels[0].pv_current == Decimal("8.42")
    # Power is derived, since the API gives V and I but no per-string power.
    assert panels[0].pv_power_kw == Decimal("5.224")
    await adapter.close()


async def test_huawei_string_data_feeds_the_peer_comparison() -> None:
    """End to end: pv4 is collapsed to 2.1 A against ~8.4 A peers, and the
    analytics module must flag exactly that string."""
    from app.analytics.string_variance import StringReading, analyse_mppt_group

    adapter = huawei_adapter(_huawei_handler)
    panels = await adapter.fetch_panel_readings("ST-1")
    await adapter.close()

    anomalies = analyse_mppt_group(
        [
            StringReading(
                device_id=__import__("uuid").uuid4(),
                mppt_index=p.mppt_index,
                string_index=p.string_index,
                pv_current=p.pv_current,
                pv_voltage=p.pv_voltage,
            )
            for p in panels
        ],
        threshold_pct=Decimal("10"),
    )

    assert len(anomalies) == 1
    assert anomalies[0].string_index == 4
    assert anomalies[0].is_underperforming


async def test_huawei_sentinel_values_are_not_read_as_measurements() -> None:
    """FusionSolar returns -1 for "no reading". Storing it produces negative
    power in the yield series."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"success": True}, headers={"XSRF-TOKEN": "t"})
        if request.url.path.endswith("/getDevList"):
            return httpx.Response(
                200, json={"success": True, "data": [{"esnCode": "I1", "devTypeId": 1}]}
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"devId": "I1", "dataItemMap": {"active_power": -1, "day_cap": -1}}],
            },
        )

    adapter = huawei_adapter(handler)
    reading = (await adapter.fetch_device_readings("ST-1"))[0]

    assert reading.active_power_kw is None
    assert reading.daily_yield_kwh is None
    await adapter.close()


async def test_huawei_fail_code_407_is_a_quota_error() -> None:
    """407 is "interface access frequency exceeded". Retrying extends the
    lockout, so it must not be treated as transient."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"success": True}, headers={"XSRF-TOKEN": "t"})
        return httpx.Response(200, json={"success": False, "failCode": 407})

    adapter = huawei_adapter(handler)
    with pytest.raises(QuotaExceededError, match="407"):
        await adapter.fetch_site_readings(["ST-1"])
    await adapter.close()


async def test_huawei_declares_string_basis() -> None:
    adapter = huawei_adapter(_huawei_handler)
    assert adapter.measurement_basis is MeasurementBasis.STRING
    await adapter.close()


# --------------------------------------------------------------------------- #
# MPPT grouping
# --------------------------------------------------------------------------- #


def test_mppt_grouping_follows_the_sequential_layout() -> None:
    """A 6-MPPT / 12-input SUN2000: pv1-2 on MPPT 0, pv3-4 on MPPT 1, and so on."""
    assert derive_mppt_index(1, mppt_count=6, pv_input_count=12) == 0
    assert derive_mppt_index(2, mppt_count=6, pv_input_count=12) == 0
    assert derive_mppt_index(3, mppt_count=6, pv_input_count=12) == 1
    assert derive_mppt_index(12, mppt_count=6, pv_input_count=12) == 5


def test_mppt_grouping_falls_back_to_one_group_when_unknown() -> None:
    """FusionSolar does not publish the mapping. With no MPPT count recorded,
    everything lands in one group — results from such devices are lower
    confidence and the adapter says so."""
    assert derive_mppt_index(3, mppt_count=None, pv_input_count=4) == 0
    assert derive_mppt_index(4, mppt_count=0, pv_input_count=4) == 0


async def test_atmoce_uses_the_bearer_scheme() -> None:
    """A bare token is accepted by the transport and rejected by the API, so
    the missing "Bearer " prefix shows up as an auth failure far from its cause."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "TOK"}}
            )
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"success": True, "data": []})

    adapter = atmoce_adapter(handler)
    await adapter.authenticate()
    await adapter.fetch_site_readings(["1"])

    assert seen["authorization"] == "Bearer TOK"
    await adapter.close()


async def test_atmoce_site_listing_stops_on_page_end() -> None:
    """The API flags the final page. Paging until an empty response instead
    spends one wasted call on every sweep, against a 10,000/month budget."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        calls["n"] += 1
        return httpx.Response(
            200,
            json={
                "success": True,
                "pageEnd": 1,
                "data": [
                    {
                        "siteId": "1",
                        "name": "PABC",
                        "solarCapacity": 37.18,
                        "batteryCapacity": 14.0,
                        "gridTiedTime": "27/01/2026",
                    }
                ],
            },
        )

    adapter = atmoce_adapter(handler)
    sites = await adapter.list_sites()

    assert len(sites) == 1
    assert sites[0].name == "PABC"
    # solarCapacity is kWp as sent, NOT watts as the reference describes it.
    # Reading it as watts made every vendor-registered branch 1000x too small,
    # and that figure divides into every kWh/kWp and status number downstream.
    assert sites[0].capacity_kwp == Decimal("37.18")
    assert sites[0].battery_capacity_kwh == Decimal("14.0")
    assert sites[0].grid_tied_on == date(2026, 1, 27)
    assert calls["n"] == 1, "pageEnd=1 must stop paging immediately"
    await adapter.close()


# ---------------------------------------------------------------------------
# Batch isolation
#
# Atmoce's bulk endpoint rejects the WHOLE batch — code 1000 — when any single
# site in it has never reported. One branch commissioned but not yet generating
# therefore blinds the entire fleet's cheap sweep, which is what pushed
# ingestion onto the per-site endpoint and put the monthly call budget at risk.
# ---------------------------------------------------------------------------


def _last_power_handler(poison: set[str], calls: list[list[str]]):
    """Serve last-power, refusing any batch containing a site in ``poison``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        ids = (request.url.params.get("siteIds") or "").split(",")
        calls.append(ids)
        if poison & set(ids):
            return httpx.Response(
                200,
                json={
                    "success": False,
                    "code": 1000,
                    "reason": "Failed to obtain data.",
                },
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "siteId": site_id,
                        "lastReportedTime": 1735689600000,
                        "solarGenerationPower": 1000,
                        "dailySolarGeneration": 5,
                        "lifetimeSolarGeneration": 500,
                        "status": "1",
                    }
                    for site_id in ids
                ],
            },
        )

    return handler


@pytest.mark.anyio
async def test_one_dead_site_does_not_lose_the_batch() -> None:
    calls: list[list[str]] = []
    adapter = atmoce_adapter(_last_power_handler({"S7"}, calls))

    readings = await adapter.fetch_site_readings([f"S{i}" for i in range(1, 11)])

    assert {r.vendor_site_id for r in readings} == {
        f"S{i}" for i in range(1, 11)
    } - {"S7"}
    # Bisection, not one request per site: nine survivors must not cost nine
    # extra calls against a metered budget.
    assert len(calls) < 10


@pytest.mark.anyio
async def test_a_refused_site_is_remembered_for_the_session() -> None:
    """Otherwise the bisection is re-paid on every sweep, four times an hour."""
    calls: list[list[str]] = []
    adapter = atmoce_adapter(_last_power_handler({"S3"}, calls))

    await adapter.fetch_site_readings(["S1", "S2", "S3", "S4"])
    first_pass = len(calls)
    calls.clear()

    readings = await adapter.fetch_site_readings(["S1", "S2", "S3", "S4"])

    assert {r.vendor_site_id for r in readings} == {"S1", "S2", "S4"}
    assert len(calls) == 1
    assert "S3" not in calls[0]
    assert first_pass > 1


@pytest.mark.anyio
async def test_quota_refusal_is_never_bisected() -> None:
    """A 429 is a property of the request, not of the sites in it.

    Bisecting one would split a failure that is certain to recur, spend more of
    an exhausted budget doing it, and end by blacklisting healthy branches as
    permanently unreadable.
    """
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        calls.append(1)
        return httpx.Response(429, json={"success": False, "code": 429})

    adapter = atmoce_adapter(handler)
    with pytest.raises(QuotaExceededError):
        await adapter.fetch_site_readings(["S1", "S2", "S3", "S4"])

    assert len(calls) == 1
    assert adapter._unreadable_sites == set()  # noqa: SLF001 - test seam


@pytest.mark.anyio
async def test_device_and_panel_readings_share_one_request() -> None:
    """Both read the same endpoint, and the detail sweep calls them back to back.

    Two identical requests per branch is 214 calls a day across the fleet
    instead of 107 — on its own enough to push the month past its 10,000 cap.
    """
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "SN": "MI-1",
                        "lastReportedTime": 1735689600000,
                        "generationPower": 300,
                        "dailyGeneration": 1.5,
                        "lifetimeGeneration": 120,
                        "status": "1",
                        "pvData": [
                            {"pvNumber": 1, "pvPower": 150},
                            {"pvNumber": 2, "pvPower": 150},
                        ],
                    }
                ],
            },
        )

    adapter = atmoce_adapter(handler)
    devices = await adapter.fetch_device_readings("S1")
    panels = await adapter.fetch_panel_readings("S1")

    assert devices and panels
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Fleet-table fields
#
# Units are the whole point of these tests. A misread unit produces a number
# that renders perfectly and is wrong by three orders of magnitude, and the only
# symptom is a figure nobody happens to sanity-check — which is exactly how nine
# branches came to be recorded at 0.04 kWp while generating 129 kWh a day.
# ---------------------------------------------------------------------------


def _sites_handler(rows: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        return httpx.Response(200, json={"success": True, "data": rows, "pageEnd": 1})

    return handler


@pytest.mark.anyio
async def test_atmoce_site_capacity_is_already_kwp() -> None:
    """``solarCapacity`` arrives in kWp, whatever the reference calls it.

    Verified against the live account: PRMB reports 37.18 and is a 37.18 kWp
    array. Dividing by 1000 registered it as 0.04 kWp, which then divided into
    every kWh/kWp and status figure derived from it.
    """
    adapter = atmoce_adapter(
        _sites_handler(
            [
                {
                    "siteId": "1",
                    "name": "PRMB",
                    "solarCapacity": 37.18,
                    "batteryCapacity": 14.0,
                    "gridTiedTime": "27/01/2026",
                    "latitude": 13.78,
                    "longitude": 100.70,
                }
            ]
        )
    )

    site = (await adapter.list_sites())[0]

    assert site.capacity_kwp == Decimal("37.18")
    assert site.battery_capacity_kwh == Decimal("14.0")
    assert site.grid_tied_on == date(2026, 1, 27)


@pytest.mark.anyio
async def test_atmoce_grid_tied_time_is_day_first() -> None:
    """"03/04/2026" is 3 April.

    Read month-first it is 4 March — a plausible-looking date, wrong by a month,
    with nothing to flag it. Only the unambiguous day values would ever fail.
    """
    adapter = atmoce_adapter(
        _sites_handler([{"siteId": "1", "name": "PXXX", "gridTiedTime": "03/04/2026"}])
    )
    assert (await adapter.list_sites())[0].grid_tied_on == date(2026, 4, 3)


@pytest.mark.anyio
async def test_atmoce_missing_optional_site_fields_stay_none() -> None:
    """Huawei-style branches report no battery. None, never zero.

    Zero would read as "no battery fitted", which is a different claim from
    "the vendor did not say".
    """
    adapter = atmoce_adapter(_sites_handler([{"siteId": "1", "name": "PXXX"}]))
    site = (await adapter.list_sites())[0]
    assert site.battery_capacity_kwh is None
    assert site.grid_tied_on is None
    assert site.capacity_kwp is None


@pytest.mark.anyio
async def test_atmoce_site_reading_carries_month_and_lifetime() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(
                200, json={"success": True, "data": {"access_token": "t"}}
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "siteId": "1",
                        "lastReportedTime": 1735689600000,
                        "solarGenerationPower": 15375.0,
                        "dailySolarGeneration": 134.62,
                        "monthlySolarGeneration": 3711.11,
                        "lifetimeSolarGeneration": 33583.52,
                        "status": "1",
                    }
                ],
            },
        )

    adapter = atmoce_adapter(handler)
    reading = (await adapter.fetch_site_readings(["1"]))[0]

    # Power is watts here and kWh for the three totals — the one place in this
    # payload where the units genuinely differ.
    assert reading.active_power_kw == Decimal("15.375")
    assert reading.daily_yield_kwh == Decimal("134.62")
    assert reading.monthly_yield_kwh == Decimal("3711.11")
    assert reading.total_yield_kwh == Decimal("33583.52")


@pytest.mark.anyio
async def test_huawei_station_reading_carries_month() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/login"):
            return httpx.Response(
                200, json={"success": True}, headers={"XSRF-TOKEN": "t"}
            )
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": [
                    {
                        "stationCode": "NE=1",
                        "dataItemMap": {
                            "day_power": 72.27,
                            "month_power": 2738.78,
                            "total_power": 67413.42,
                            "real_health_state": 3,
                        },
                    }
                ],
            },
        )

    adapter = huawei_adapter(handler)
    reading = (await adapter.fetch_site_readings(["NE=1"]))[0]

    assert reading.daily_yield_kwh == Decimal("72.27")
    assert reading.monthly_yield_kwh == Decimal("2738.78")
    assert reading.total_yield_kwh == Decimal("67413.42")
    # FusionSolar publishes no real-time power for a station, so this stays
    # None rather than being reported as a plant sitting at zero output.
    assert reading.active_power_kw is None
