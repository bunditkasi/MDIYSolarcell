-- =============================================================================
-- 02_schema.sql  ("init.sql")
-- MR.DIY Thailand — Enterprise Solar PV Monitoring System, Phase 1 schema.
--
-- Every statement is idempotent: re-running this file must never error.
--
-- PHASE 2 PORTABILITY RULES observed throughout this file, because corporate IT
-- will move this schema onto an enterprise SQL server (engine not yet known):
--   * No PostgreSQL ENUM types  -> VARCHAR + CHECK constraint instead.
--     (MS SQL Server and Oracle have no CREATE TYPE ... AS ENUM equivalent.)
--   * JSONB only for non-authoritative payload/detail columns, never for data
--     the application filters or joins on. Those map to NVARCHAR(MAX) / CLOB.
--   * TIMESTAMPTZ everywhere. Never a naive timestamp.
--   * NUMERIC (not FLOAT) for money and energy, where rounding error compounds.
--     FLOAT is used only for irradiance, which is itself a modelled estimate.
-- =============================================================================


-- =============================================================================
-- Shared trigger: keep updated_at honest even for direct SQL writes.
-- Phase 2 equivalents: MS SQL AFTER UPDATE trigger, MySQL ON UPDATE CURRENT_TIMESTAMP.
-- =============================================================================
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $trg$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$trg$ LANGUAGE plpgsql;


-- =============================================================================
-- RELATIONAL / METADATA TABLES
-- =============================================================================

-- -----------------------------------------------------------------------------
-- tariffs — TOU rate cards used by the financial engine.
--
-- WARNING: rates are deliberately NOT seeded with invented numbers. They must be
-- entered from the official PEA / MEA published tariff announcements.
-- See db/seed/01_tariffs.sql.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tariffs (
    tariff_id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    tariff_name         VARCHAR(64)   NOT NULL,
    -- Official utility tariff category, e.g. '3.2.2', '2.1.2', '3.1.2'.
    -- This is what the "Tariff Rate" column of Solar Report.xlsx records.
    tariff_code         VARCHAR(16)   NOT NULL,
    utility             VARCHAR(8)    NOT NULL,
    on_peak_rate        NUMERIC(10,4) NOT NULL,
    -- Nullable: the source report publishes on-peak rates only. Leaving this
    -- NULL is honest — a zero or a guessed figure would silently corrupt every
    -- TOU savings calculation downstream.
    off_peak_rate       NUMERIC(10,4),
    demand_charge_rate  NUMERIC(10,4) NOT NULL DEFAULT 0,
    currency            CHAR(3)       NOT NULL DEFAULT 'THB',
    -- Tariffs are revised periodically. Keeping validity dates means a report
    -- re-run for last year still reproduces last year's numbers.
    effective_from      DATE          NOT NULL,
    effective_to        DATE,
    created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT ck_tariffs_utility        CHECK (utility IN ('PEA', 'MEA')),
    CONSTRAINT ck_tariffs_rates_positive CHECK (
        on_peak_rate >= 0
        AND (off_peak_rate IS NULL OR off_peak_rate >= 0)
        AND demand_charge_rate >= 0
    ),
    CONSTRAINT ck_tariffs_effective_range CHECK (
        effective_to IS NULL OR effective_to > effective_from
    ),
    CONSTRAINT uq_tariffs_code_from UNIQUE (utility, tariff_code, effective_from)
);

DROP TRIGGER IF EXISTS trg_tariffs_updated_at ON tariffs;
CREATE TRIGGER trg_tariffs_updated_at
    BEFORE UPDATE ON tariffs
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- -----------------------------------------------------------------------------
-- stores — one row per MR.DIY branch with a PV installation.
--
-- Column choices reconciled against the live roster in Solar Report.xlsx
-- ("Solar Store Info" sheet, 153 sites):
--
--   store_code        <- "Name Code", the 4-letter site abbreviation. Unique and
--                        populated for all 153 rows, so it is the natural key.
--   retail_store_code <- "Store code" (B105, BU65 …). Populated for only 54 of
--                        153 rows, therefore nullable and NOT the primary
--                        business key, despite the name.
--   tariff_id         <- the spec's `tariff_type`, as an FK. Referencing the
--                        tariff row keeps rate changes in one place; a
--                        denormalised text copy beside the FK would drift.
--
-- lat/lng are NULLABLE. 35 of the 153 sites currently have no coordinates in
-- the source workbook. Forcing NOT NULL would mean either refusing to load a
-- third of the real fleet or inventing positions for it — both worse than
-- recording honestly that the location is not yet known. The map endpoint skips
-- these and reports how many were skipped.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stores (
    store_id          UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    store_code        VARCHAR(32)   NOT NULL,
    retail_store_code VARCHAR(32),
    store_name        VARCHAR(255)  NOT NULL,
    region            VARCHAR(64),
    province          VARCHAR(64),
    address           TEXT,
    installed_kwp     NUMERIC(10,2) NOT NULL,
    lat               NUMERIC(9,6),
    lng               NUMERIC(9,6),
    tariff_id         UUID          REFERENCES tariffs (tariff_id) ON DELETE SET NULL,
    -- Rollout wave (1-7 in the current roster). Operations track progress by it.
    rollout_phase     SMALLINT,
    -- Monitoring platform the site reports through, e.g. 'atmoce'.
    monitoring_source VARCHAR(64),
    commissioned_at   DATE,
    -- CapEx, for payback and ROI reporting on the >1,000M THB programme.
    capex_before_vat  NUMERIC(14,2),
    capex_vat         NUMERIC(14,2),
    capex_net         NUMERIC(14,2),
    -- Usable battery storage. Null for branches whose vendor publishes no such
    -- field (Huawei), so an unreported site is never shown a confident zero.
    battery_capacity_kwh NUMERIC(10,2),
    is_active         BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT uq_stores_store_code        UNIQUE (store_code),
    CONSTRAINT uq_stores_retail_store_code UNIQUE (retail_store_code),
    CONSTRAINT ck_stores_kwp_positive      CHECK (installed_kwp > 0),
    -- Coordinates must be supplied as a pair or not at all. This is the exact
    -- defect found in the source workbook, where the Long column had been
    -- populated with a copy of Lat for every row.
    CONSTRAINT ck_stores_latlng_paired CHECK (
        (lat IS NULL) = (lng IS NULL)
    ),
    CONSTRAINT ck_stores_lat_range CHECK (lat IS NULL OR lat BETWEEN -90  AND 90),
    CONSTRAINT ck_stores_lng_range CHECK (lng IS NULL OR lng BETWEEN -180 AND 180),
    CONSTRAINT ck_stores_rollout_phase CHECK (rollout_phase IS NULL OR rollout_phase > 0)
);

CREATE INDEX IF NOT EXISTS idx_stores_region    ON stores (region);
CREATE INDEX IF NOT EXISTS idx_stores_is_active ON stores (is_active);
-- Map viewport queries filter by bounding box.
CREATE INDEX IF NOT EXISTS idx_stores_lat_lng   ON stores (lat, lng);
-- Fuzzy search over store_code / store_name (StoreFilter.search).
CREATE INDEX IF NOT EXISTS idx_stores_name_trgm ON stores USING gin (store_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_stores_code_trgm ON stores USING gin (store_code gin_trgm_ops);

DROP TRIGGER IF EXISTS trg_stores_updated_at ON stores;
CREATE TRIGGER trg_stores_updated_at
    BEFORE UPDATE ON stores
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- -----------------------------------------------------------------------------
-- devices — inverters, meters and loggers belonging to a store.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    device_id     UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id      UUID          NOT NULL REFERENCES stores (store_id) ON DELETE CASCADE,
    brand         VARCHAR(64)   NOT NULL,
    model         VARCHAR(128),
    serial_number VARCHAR(128)  NOT NULL,
    device_type   VARCHAR(32)   NOT NULL DEFAULT 'INVERTER',
    capacity_kw   NUMERIC(10,2),
    -- Needed by Intra-String Peer Comparison: strings are only comparable when
    -- they sit on the same MPPT.
    mppt_count    SMALLINT,
    installed_at  DATE,
    is_active     BOOLEAN       NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT uq_devices_serial_number UNIQUE (serial_number),
    CONSTRAINT ck_devices_device_type   CHECK (
        device_type IN ('INVERTER', 'METER', 'LOGGER', 'WEATHER_STATION')
    ),
    CONSTRAINT ck_devices_mppt_count CHECK (mppt_count  IS NULL OR mppt_count  > 0),
    CONSTRAINT ck_devices_capacity   CHECK (capacity_kw IS NULL OR capacity_kw > 0)
);

CREATE INDEX IF NOT EXISTS idx_devices_store_id ON devices (store_id);
CREATE INDEX IF NOT EXISTS idx_devices_brand    ON devices (brand);

DROP TRIGGER IF EXISTS trg_devices_updated_at ON devices;
CREATE TRIGGER trg_devices_updated_at
    BEFORE UPDATE ON devices
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- -----------------------------------------------------------------------------
-- data_adapters — how each device's data is collected.
--
-- SECURITY: `secrets_ref` holds a LOOKUP KEY ONLY (e.g. 'huawei_central_01').
-- The credential itself is fetched in-memory at run time through
-- SecretsProviderInterface. Never store a password, token or API key in this
-- column, not even encrypted — a database backup must not become a credential leak.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS data_adapters (
    adapter_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    device_id         UUID         NOT NULL REFERENCES devices (device_id) ON DELETE CASCADE,
    adapter_type      VARCHAR(16)  NOT NULL,
    vendor_key        VARCHAR(64)  NOT NULL,
    endpoint_url      TEXT,
    secrets_ref       VARCHAR(255) NOT NULL,
    sync_interval_min INTEGER      NOT NULL DEFAULT 15,
    is_enabled        BOOLEAN      NOT NULL DEFAULT TRUE,
    last_sync_at      TIMESTAMPTZ,
    last_sync_status  VARCHAR(16),
    last_error        TEXT,
    created_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT ck_data_adapters_type     CHECK (adapter_type IN ('API', 'SCRAPER')),
    CONSTRAINT ck_data_adapters_interval CHECK (sync_interval_min > 0),
    CONSTRAINT ck_data_adapters_status   CHECK (
        last_sync_status IS NULL
        OR last_sync_status IN ('SUCCESS', 'FAILED', 'PARTIAL')
    ),
    CONSTRAINT uq_data_adapters_device UNIQUE (device_id)
);

CREATE INDEX IF NOT EXISTS idx_data_adapters_due
    ON data_adapters (is_enabled, last_sync_at);
CREATE INDEX IF NOT EXISTS idx_data_adapters_vendor
    ON data_adapters (vendor_key);

DROP TRIGGER IF EXISTS trg_data_adapters_updated_at ON data_adapters;
CREATE TRIGGER trg_data_adapters_updated_at
    BEFORE UPDATE ON data_adapters
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- -----------------------------------------------------------------------------
-- alerts — string variance, offline devices, low PR, ingestion failures.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    alert_id        UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    store_id        UUID        NOT NULL REFERENCES stores  (store_id)  ON DELETE CASCADE,
    device_id       UUID                 REFERENCES devices (device_id) ON DELETE CASCADE,
    alert_type      VARCHAR(32) NOT NULL,
    severity        VARCHAR(16) NOT NULL,
    message         TEXT        NOT NULL,
    status          VARCHAR(16) NOT NULL DEFAULT 'OPEN',
    -- Diagnostic context only (measured variance, string indices, thresholds).
    -- Nothing here is filtered on, so it stays portable.
    details         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    acknowledged_at TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,

    CONSTRAINT ck_alerts_type CHECK (alert_type IN (
        'STRING_VARIANCE', 'DEVICE_OFFLINE', 'LOW_PR', 'DATA_GAP', 'ADAPTER_FAILURE'
    )),
    CONSTRAINT ck_alerts_severity CHECK (severity IN ('CRITICAL', 'MAJOR', 'MINOR')),
    CONSTRAINT ck_alerts_status   CHECK (status   IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    CONSTRAINT ck_alerts_resolved_has_timestamp CHECK (
        status <> 'RESOLVED' OR resolved_at IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_alerts_store_created ON alerts (store_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_open
    ON alerts (store_id, severity) WHERE status <> 'RESOLVED';

-- Deduplication guard. The analytics job re-evaluates every device on every
-- cycle; without this a single faulty string would create a new alert row every
-- few minutes and bury the operations team. A repeat of the same unresolved
-- problem updates the existing row instead of inserting another.
CREATE UNIQUE INDEX IF NOT EXISTS uq_alerts_active_problem
    ON alerts (store_id, device_id, alert_type)
    WHERE status <> 'RESOLVED';


-- =============================================================================
-- TIME-SERIES HYPERTABLES
--
-- chunk_time_interval is 7 days for telemetry. Rule of thumb: one chunk's
-- indexes should fit comfortably in memory. At Phase 1 pilot volume this is
-- generous; revisit once the full 200+ store fleet reports.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- telemetry_raw — inverter-level readings.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_raw (
    "time"          TIMESTAMPTZ   NOT NULL,
    device_id       UUID          NOT NULL REFERENCES devices (device_id) ON DELETE CASCADE,
    active_power_kw NUMERIC(12,3),
    daily_yield_kwh NUMERIC(12,3),
    -- Month to date as the VENDOR counts it, not derived from the rows above:
    -- our history starts at switch-on, so a derived total under-reports every
    -- branch until a full month has passed.
    monthly_yield_kwh NUMERIC(14,3),
    total_yield_kwh NUMERIC(14,3),
    grid_voltage    NUMERIC(8,2),
    grid_current    NUMERIC(8,2),
    status_code     INTEGER,
    -- Untouched vendor response, kept for debugging adapter mapping bugs.
    payload         JSONB,
    ingested_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),

    -- Composite PK makes re-polling the same window idempotent (ON CONFLICT).
    -- A hypertable's primary key must include the partitioning column.
    CONSTRAINT pk_telemetry_raw PRIMARY KEY (device_id, "time")
);

SELECT create_hypertable(
    'telemetry_raw', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists       => TRUE
);

CREATE INDEX IF NOT EXISTS idx_telemetry_raw_device_time
    ON telemetry_raw (device_id, "time" DESC);


-- -----------------------------------------------------------------------------
-- telemetry_string — per-string DC readings.
--
-- mppt_index is what makes Intra-String Peer Comparison possible: strings are
-- only meaningfully comparable against peers on the SAME MPPT, because separate
-- MPPTs track independently and legitimately differ.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS telemetry_string (
    "time"       TIMESTAMPTZ   NOT NULL,
    device_id    UUID          NOT NULL REFERENCES devices (device_id) ON DELETE CASCADE,
    mppt_index   SMALLINT      NOT NULL,
    string_index SMALLINT      NOT NULL,
    pv_voltage   NUMERIC(8,2),
    pv_current   NUMERIC(8,2),
    pv_power_kw  NUMERIC(12,3),
    ingested_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),

    CONSTRAINT pk_telemetry_string PRIMARY KEY (device_id, mppt_index, string_index, "time"),
    CONSTRAINT ck_telemetry_string_mppt   CHECK (mppt_index   >= 0),
    CONSTRAINT ck_telemetry_string_string CHECK (string_index >= 0)
);

SELECT create_hypertable(
    'telemetry_string', 'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists       => TRUE
);

-- Serves the peer-comparison query: "all strings on this device's MPPT n at time t".
CREATE INDEX IF NOT EXISTS idx_telemetry_string_device_time_mppt
    ON telemetry_string (device_id, "time" DESC, mppt_index);


-- -----------------------------------------------------------------------------
-- weather_data — Virtual Pyranometer input (Solcast), keyed by STORE.
--
-- Stored per store rather than per device because the satellite estimate is
-- resolved from the site's lat/lng; every inverter at one branch shares it.
-- FLOAT is appropriate here: irradiance is a modelled estimate, not a metered
-- quantity, so NUMERIC precision would be false confidence.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS weather_data (
    "time"         TIMESTAMPTZ      NOT NULL,
    store_id       UUID             NOT NULL REFERENCES stores (store_id) ON DELETE CASCADE,
    ghi            DOUBLE PRECISION,
    poa_irradiance DOUBLE PRECISION,
    ambient_temp   DOUBLE PRECISION,
    source         VARCHAR(32)      NOT NULL DEFAULT 'solcast',
    ingested_at    TIMESTAMPTZ      NOT NULL DEFAULT now(),

    CONSTRAINT pk_weather_data PRIMARY KEY (store_id, "time"),
    CONSTRAINT ck_weather_ghi CHECK (ghi            IS NULL OR ghi            >= 0),
    CONSTRAINT ck_weather_poa CHECK (poa_irradiance IS NULL OR poa_irradiance >= 0)
);

SELECT create_hypertable(
    'weather_data', 'time',
    chunk_time_interval => INTERVAL '30 days',
    if_not_exists       => TRUE
);

CREATE INDEX IF NOT EXISTS idx_weather_data_store_time
    ON weather_data (store_id, "time" DESC);


-- =============================================================================
-- DEFERRED: compression, retention and continuous aggregates.
--
-- Left switched off for the Phase 1 pilot, as agreed — at pilot volume they add
-- operational complexity with no measurable benefit. The statements below are
-- ready to enable once the fleet grows. Enable compression BEFORE the data set
-- gets large; compressing retroactively is far slower.
-- =============================================================================
--
-- ALTER TABLE telemetry_raw SET (
--     timescaledb.compress,
--     timescaledb.compress_segmentby = 'device_id',
--     timescaledb.compress_orderby   = '"time" DESC'
-- );
-- SELECT add_compression_policy('telemetry_raw', INTERVAL '14 days');
--
-- ALTER TABLE telemetry_string SET (
--     timescaledb.compress,
--     timescaledb.compress_segmentby = 'device_id, mppt_index',
--     timescaledb.compress_orderby   = '"time" DESC'
-- );
-- SELECT add_compression_policy('telemetry_string', INTERVAL '14 days');
--
-- SELECT add_retention_policy('telemetry_raw',    INTERVAL '3 years');
-- SELECT add_retention_policy('telemetry_string', INTERVAL '2 years');
--
-- CREATE MATERIALIZED VIEW IF NOT EXISTS telemetry_hourly
-- WITH (timescaledb.continuous) AS
-- SELECT
--     time_bucket(INTERVAL '1 hour', "time") AS bucket,
--     device_id,
--     avg(active_power_kw) AS avg_power_kw,
--     max(daily_yield_kwh) AS daily_yield_kwh
-- FROM telemetry_raw
-- GROUP BY bucket, device_id
-- WITH NO DATA;
--
-- SELECT add_continuous_aggregate_policy('telemetry_hourly',
--     start_offset      => INTERVAL '3 days',
--     end_offset        => INTERVAL '1 hour',
--     schedule_interval => INTERVAL '30 minutes');


DO $notice$
BEGIN
    RAISE NOTICE 'Schema ready. Hypertables: %',
        (SELECT string_agg(hypertable_name, ', ' ORDER BY hypertable_name)
           FROM timescaledb_information.hypertables);
END
$notice$;
