-- =============================================================================
-- 03_demo_telemetry.sql — DEMONSTRATION DATA ONLY. NEVER LOAD IN PRODUCTION.
--
-- Synthesises devices, irradiance, inverter readings, per-string currents and
-- alerts so the full status pipeline can be exercised before real OEM ingestion
-- exists. Without it every pin is RED (nothing reporting), which is correct but
-- proves nothing about GREEN / YELLOW / UNKNOWN.
--
--   docker compose exec -T db psql -U solarcell -d solarcell -f /seed/03_demo_telemetry.sql
--
-- Re-runnable: deletes what it previously created before regenerating.
--
-- The site mix is chosen to cover every branch of classify_pr_status():
--   ~70% healthy and online              -> GREEN
--   ~12% online but below PR threshold   -> YELLOW
--   ~6%  online, healthy PR, bad string  -> YELLOW  (the case PR alone misses)
--   ~7%  no recent reading               -> RED
--   ~5%  online but no irradiance data   -> UNKNOWN
-- =============================================================================

BEGIN;

-- Idempotency: telemetry cascades from devices, so removing the demo devices
-- clears their readings too.
DELETE FROM alerts  WHERE details ->> 'source' = 'demo';
DELETE FROM devices WHERE serial_number LIKE 'DEMO-%';
DELETE FROM weather_data WHERE source = 'demo';


-- --------------------------------------------------------------------------
-- One inverter per located store. Two MPPTs each, which is what makes the
-- intra-string peer comparison meaningful.
-- --------------------------------------------------------------------------
INSERT INTO devices (store_id, brand, model, serial_number, device_type,
                     capacity_kw, mppt_count, installed_at)
SELECT s.store_id,
       (ARRAY['Huawei', 'Sungrow', 'Growatt'])[1 + (abs(hashtext(s.store_code)) % 3)],
       'DEMO-INV',
       'DEMO-' || s.store_code,
       'INVERTER',
       s.installed_kwp,
       2,
       s.commissioned_at
  FROM stores s
 WHERE s.lat IS NOT NULL AND s.is_active;


-- --------------------------------------------------------------------------
-- Buckets. hashtext gives a stable pseudo-random assignment, so re-running
-- produces the same fleet picture instead of a different one each time.
-- --------------------------------------------------------------------------
CREATE TEMP TABLE demo_profile ON COMMIT DROP AS
SELECT d.device_id,
       d.store_id,
       s.installed_kwp,
       (abs(hashtext(s.store_code || 'x')) % 100) AS bucket
  FROM devices d
  JOIN stores s USING (store_id)
 WHERE d.serial_number LIKE 'DEMO-%';


-- --------------------------------------------------------------------------
-- Irradiance — six hourly samples today at 600 W/m2 plane-of-array.
-- Gives peak_sun_hours = 600/1000 * 6 = 3.6, so theoretical yield is
-- installed_kwp * 3.6 and the PR values below land where intended.
--
-- The UNKNOWN bucket (>= 95) deliberately gets no rows: no irradiance baseline
-- means PR cannot be computed at all.
-- --------------------------------------------------------------------------
INSERT INTO weather_data ("time", store_id, ghi, poa_irradiance, ambient_temp, source)
SELECT date_trunc('hour', now()) - make_interval(hours => g),
       p.store_id,
       620.0,
       600.0,
       33.5,
       'demo'
  FROM demo_profile p
 CROSS JOIN generate_series(0, 5) AS g
 WHERE p.bucket < 95
ON CONFLICT (store_id, "time") DO NOTHING;


-- --------------------------------------------------------------------------
-- Inverter readings.
--
-- The RED bucket (88-94) gets a reading 45 minutes old — past the 15-minute
-- offline threshold but still inside the query's 24h lookback window, which is
-- what a genuinely disconnected site looks like. Omitting the row entirely
-- would also produce RED, but would not prove the threshold is what did it.
-- --------------------------------------------------------------------------
INSERT INTO telemetry_raw ("time", device_id, active_power_kw, daily_yield_kwh,
                           total_yield_kwh, grid_voltage, grid_current, status_code)
SELECT CASE WHEN p.bucket BETWEEN 88 AND 94
            THEN now() - INTERVAL '45 minutes'
            ELSE now() - INTERVAL '3 minutes'
       END,
       p.device_id,
       ROUND(p.installed_kwp * 0.62, 3),
       ROUND(
           p.installed_kwp * 3.6 *
           CASE
               WHEN p.bucket < 70 THEN 0.86   -- GREEN   : PR ~86%
               WHEN p.bucket < 82 THEN 0.63   -- YELLOW  : PR ~63%, below 75
               WHEN p.bucket < 88 THEN 0.88   -- YELLOW  : healthy PR, bad string
               WHEN p.bucket < 95 THEN 0.80   -- RED     : stale reading
               ELSE 0.85                      -- UNKNOWN : no irradiance
           END, 3),
       ROUND(p.installed_kwp * 1450, 3),
       402.5,
       ROUND(p.installed_kwp * 0.9, 2),
       0
  FROM demo_profile p
ON CONFLICT (device_id, "time") DO NOTHING;


-- --------------------------------------------------------------------------
-- Per-string currents: 3 strings on each of 2 MPPTs.
--
-- Bucket 82-87 gets one collapsed string on MPPT 0 (2.1 A against a 8.4 A
-- median, roughly -75%). Every other string tracks its peers closely. Note the
-- two MPPTs run at deliberately different currents — a correct implementation
-- must not flag that, and test_strings_on_different_mppts_are_never_compared
-- covers the same rule.
-- --------------------------------------------------------------------------
INSERT INTO telemetry_string ("time", device_id, mppt_index, string_index,
                              pv_voltage, pv_current, pv_power_kw)
SELECT now() - INTERVAL '3 minutes',
       p.device_id,
       m.mppt_index,
       st.string_index,
       620.0,
       CASE
           WHEN p.bucket BETWEEN 82 AND 87
                AND m.mppt_index = 0 AND st.string_index = 2 THEN 2.1
           WHEN m.mppt_index = 0 THEN 8.40 + (st.string_index * 0.05)
           ELSE 6.90 + (st.string_index * 0.05)
       END,
       -- Power derived from THIS string's own voltage and current rather
       -- than a constant, so the collapsed string below actually shows a
       -- power drop too. A fixed value here made the array view report every
       -- string as healthy while the current-based alert fired.
       ROUND(
           620.0 * CASE
               WHEN p.bucket BETWEEN 82 AND 87
                    AND m.mppt_index = 0 AND st.string_index = 2 THEN 2.1
               WHEN m.mppt_index = 0 THEN 8.40 + (st.string_index * 0.05)
               ELSE 6.90 + (st.string_index * 0.05)
           END / 1000.0, 3)
  FROM demo_profile p
 CROSS JOIN (VALUES (0), (1)) AS m(mppt_index)
 CROSS JOIN (VALUES (0), (1), (2)) AS st(string_index)
 WHERE p.bucket < 88
ON CONFLICT (device_id, mppt_index, string_index, "time") DO NOTHING;


-- --------------------------------------------------------------------------
-- Alerts.
--
-- In production the analytics worker raises these. Written here directly so the
-- map's alert colouring and the dashboard's alert counters have something real
-- to render. uq_alerts_active_problem keeps a re-run from duplicating them.
-- --------------------------------------------------------------------------
INSERT INTO alerts (store_id, device_id, alert_type, severity, message, status, details)
SELECT p.store_id,
       p.device_id,
       'STRING_VARIANCE',
       'MAJOR',
       'MPPT 0 string 2: 2.10A is 75% below the 8.45A median of 3 peer strings',
       'OPEN',
       jsonb_build_object(
           'source', 'demo',
           'mppt_index', 0,
           'string_index', 2,
           'measured_current', 2.10,
           'expected_current', 8.45,
           'deviation_pct', -75.15,
           'threshold_pct', 10.0
       )
  FROM demo_profile p
 WHERE p.bucket BETWEEN 82 AND 87
ON CONFLICT DO NOTHING;

INSERT INTO alerts (store_id, device_id, alert_type, severity, message, status, details)
SELECT p.store_id,
       p.device_id,
       'DEVICE_OFFLINE',
       'CRITICAL',
       'No reading received for more than 15 minutes',
       'OPEN',
       jsonb_build_object('source', 'demo', 'last_seen_minutes_ago', 45)
  FROM demo_profile p
 WHERE p.bucket BETWEEN 88 AND 94
ON CONFLICT DO NOTHING;

COMMIT;

\echo 'Demo telemetry loaded.'
SELECT (SELECT COUNT(*) FROM devices          WHERE serial_number LIKE 'DEMO-%') AS devices,
       (SELECT COUNT(*) FROM telemetry_raw)                                      AS raw_readings,
       (SELECT COUNT(*) FROM telemetry_string)                                   AS string_readings,
       (SELECT COUNT(*) FROM weather_data)                                       AS weather_rows,
       (SELECT COUNT(*) FROM alerts WHERE status <> 'RESOLVED')                   AS open_alerts;
