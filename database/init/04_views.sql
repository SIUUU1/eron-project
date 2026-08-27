-- =====================================================================
-- ER:ON — 조회용 View
--
-- · 이상치 필터와 °F→℃ 변환을 한 곳에서만 처리한다.
-- · stay 당 "최신 1건" 은 LATERAL 로 뽑는다 (ix_vitalsign_stay_time 활용).
-- · Materialized view 는 쓰지 않는다. 300 stay / 2.5천 행 규모에서는
--   조기 최적화다. 전체 적재 시 EXPLAIN ANALYZE 로 재검토한다.
-- =====================================================================

-- 생리학적으로 불가능한 값을 NULL 로 만들고, 체온을 섭씨로 바꾼다.
-- 원본 mimic.ed_vitalsign 은 그대로 둔다.
CREATE OR REPLACE VIEW mimic.v_ed_vitalsign_clean AS
SELECT
    id,
    stay_id,
    subject_id,
    charttime,
    CASE WHEN heartrate   BETWEEN  20 AND 300 THEN heartrate   END AS heartrate,
    CASE WHEN resprate    BETWEEN   4 AND  80 THEN resprate    END AS resprate,
    CASE WHEN o2sat       BETWEEN  50 AND 100 THEN o2sat       END AS o2sat,
    CASE WHEN sbp         BETWEEN  30 AND 300 THEN sbp         END AS sbp,
    CASE WHEN dbp         BETWEEN  10 AND 200 THEN dbp         END AS dbp,
    CASE WHEN temperature BETWEEN  90 AND 115
         THEN round(((temperature - 32) / 1.8)::numeric, 1)::double precision END AS temperature_c,
    rhythm,
    pain
FROM mimic.ed_vitalsign;


-- 데모 시간축을 조회 시점에 계산한다.
-- now_ref 는 고정값이고 now() 만 흐르므로, 시간이 지나도 코호트가 과거로 밀리지 않는다.
CREATE OR REPLACE VIEW app.v_demo_stay AS
SELECT
    d.ed_stay_id,
    d.is_active,
    d.now_ref,
    -- 오프셋은 고정 기준점(epoch)으로 잡는다. demo_now() 로 잡으면 시계를 진행해도
    -- 양변이 함께 밀려 화면이 그대로가 된다.
    (app.demo_epoch() - d.now_ref)                                   AS demo_offset,
    e.intime  + (app.demo_epoch() - d.now_ref)                       AS demo_intime,
    e.outtime + (app.demo_epoch() - d.now_ref)                       AS demo_outtime,
    (
        e.outtime IS NOT NULL
        AND e.outtime + (app.demo_epoch() - d.now_ref) <= app.demo_now()
    )                                                                AS has_departed
FROM app.demo_stay d
JOIN mimic.edstays e ON e.stay_id = d.ed_stay_id;


-- stay 당 최신 vital 1건
CREATE OR REPLACE VIEW app.v_latest_vitalsign AS
SELECT
    e.stay_id,
    v.charttime AS measured_at,
    v.heartrate,
    v.resprate,
    v.sbp,
    v.dbp,
    v.o2sat,
    v.temperature_c
FROM mimic.edstays e
JOIN app.v_demo_stay d ON d.ed_stay_id = e.stay_id
LEFT JOIN LATERAL (
    SELECT c.charttime, c.heartrate, c.resprate, c.sbp, c.dbp, c.o2sat, c.temperature_c
    FROM mimic.v_ed_vitalsign_clean c
    WHERE c.stay_id = e.stay_id
      -- 데모 시계보다 나중 측정은 아직 일어나지 않은 것으로 본다
      AND c.charttime + d.demo_offset <= app.demo_now()
    ORDER BY c.charttime DESC
    LIMIT 1
) v ON TRUE;


-- stay 당 최신 예측 1건. 모델 미연동 상태에서는 전부 NULL 이다.
CREATE OR REPLACE VIEW app.v_latest_prediction AS
SELECT
    e.stay_id,
    p.prediction_time,
    p.risk_probability,
    p.risk_level,
    p.detail,
    p.model_version
FROM mimic.edstays e
JOIN app.v_demo_stay d ON d.ed_stay_id = e.stay_id
LEFT JOIN LATERAL (
    SELECT pr.prediction_time, pr.risk_probability, pr.risk_level, pr.detail, pr.model_version
    FROM app.prediction pr
    WHERE pr.ed_stay_id = e.stay_id
      -- 아직 도래하지 않은 예측 시점은 감춘다
      AND pr.prediction_time + d.demo_offset <= app.demo_now()
    ORDER BY pr.prediction_time DESC
    LIMIT 1
) p ON TRUE;
