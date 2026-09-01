-- =====================================================================
-- ER:ON — 인덱스
--
-- 실제 API 쿼리 패턴에서 역산했다. 모든 컬럼에 인덱스를 걸지 않는다.
-- 대량 COPY 적재가 끝난 뒤에 적용한다 (적재 중 인덱스 유지 비용 회피).
-- =====================================================================

-- ED stay
CREATE INDEX IF NOT EXISTS ix_edstays_subject     ON mimic.edstays (subject_id);
CREATE INDEX IF NOT EXISTS ix_edstays_hadm        ON mimic.edstays (hadm_id) WHERE hadm_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_edstays_intime      ON mimic.edstays (intime DESC);

-- triage: acuity 필터
CREATE INDEX IF NOT EXISTS ix_triage_acuity       ON mimic.triage (acuity);

-- 가장 중요: 추이 조회 + "최신 1건" 을 하나로 커버
CREATE INDEX IF NOT EXISTS ix_vitalsign_stay_time ON mimic.ed_vitalsign (stay_id, charttime DESC);

-- ICU 이동 여부 판정
CREATE INDEX IF NOT EXISTS ix_icustays_hadm       ON mimic.icustays (hadm_id);

-- 검사 결과: 모델 어댑터가 subject_id + storetime 으로 훑는다.
-- itemid 단독 인덱스는 itemid 목록으로 거르는 IN 절을 위한 것이다.
CREATE INDEX IF NOT EXISTS ix_labevents_subject_time ON mimic.labevents (subject_id, charttime);
CREATE INDEX IF NOT EXISTS ix_labevents_subject_store ON mimic.labevents (subject_id, storetime);
CREATE INDEX IF NOT EXISTS ix_labevents_item          ON mimic.labevents (itemid);

-- ICU 활력징후: 모델 어댑터가 hadm_id + charttime 으로 훑는다.
CREATE INDEX IF NOT EXISTS ix_chartevents_hadm_time   ON mimic.chartevents (hadm_id, charttime);
CREATE INDEX IF NOT EXISTS ix_chartevents_stay_time   ON mimic.chartevents (icu_stay_id, charttime);
CREATE INDEX IF NOT EXISTS ix_chartevents_item_time   ON mimic.chartevents (itemid, charttime);

-- 예측: 최신 1건 + 확률 추이
CREATE INDEX IF NOT EXISTS ix_prediction_stay_time ON app.prediction (ed_stay_id, prediction_time DESC);
CREATE INDEX IF NOT EXISTS ix_prediction_level     ON app.prediction (risk_level);

-- 경고 목록
CREATE INDEX IF NOT EXISTS ix_alert_time           ON app.alert (alert_time DESC);

-- 현재 병상 배정
CREATE INDEX IF NOT EXISTS ix_bed_assign_stay      ON app.bed_assignment (ed_stay_id) WHERE released_at IS NULL;

-- 데모 시간축에는 인덱스를 만들지 않는다.
-- demo_intime 은 저장 컬럼이 아니라 조회 시점에 now() - now_ref 로 계산되는
-- 값이라(app.v_demo_stay) 인덱싱 대상이 아니다. 300 stay 규모에서는 불필요하다.

-- mimic.ed_diagnosis 는 PK(stay_id, seq_num) 선행 컬럼으로 충분 → 별도 인덱스 없음
