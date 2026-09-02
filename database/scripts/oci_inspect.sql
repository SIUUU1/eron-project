-- =====================================================================
-- ER:ON — OCI DB 사전 점검 (읽기 전용 · SELECT 만 실행)
--
-- 파일 전송 없이 로컬에서 바로 흘려보낼 수 있다:
--   ssh <user>@<oci-host> 'cd ~/eron-project && set -a && . ./.env && set +a \
--     && docker exec -i eron-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1' \
--     < database/scripts/oci_inspect.sql
--
-- 자격증명은 OCI 의 .env 안에서만 읽히며 화면·로그에 출력되지 않는다.
-- =====================================================================

\echo '### 1. 프로젝트 대상 18개 테이블 — 존재 여부와 row 수'
WITH want(t) AS (SELECT unnest(ARRAY[
  'mimic.patients','mimic.admissions','mimic.edstays','mimic.triage','mimic.ed_vitalsign',
  'mimic.ed_diagnosis','mimic.icustays','mimic.chartevents','mimic.labevents',
  'app.bed','app.cohort','app.demo_clock','app.demo_stay','app.patient_alias',
  'app.prediction','app.prediction_ack','app.alert','app.bed_assignment']))
SELECT w.t AS target_table, (to_regclass(w.t) IS NOT NULL) AS exists,
       CASE WHEN to_regclass(w.t) IS NULL THEN NULL ELSE
         (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %s', w.t), false,true,'')))[1]::text::bigint
       END AS rows
FROM want w ORDER BY 1;

\echo '### 2. 이 DB 의 전체 스키마/테이블 (다른 프로젝트가 섞여 있는지 확인)'
SELECT n.nspname AS schema, count(*) AS tables,
       string_agg(c.relname, ', ' ORDER BY c.relname) AS table_names
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema')
GROUP BY 1 ORDER BY 1;

\echo '### 3. 화이트리스트 밖에서 대상 테이블을 참조하는 FK (반드시 0행이어야 한다)'
SELECT con.conrelid::regclass::text AS referencing_table, con.conname,
       con.confrelid::regclass::text AS target_table
FROM pg_constraint con
WHERE con.contype='f'
  AND con.confrelid::regclass::text = ANY(ARRAY[
      'mimic.patients','mimic.admissions','mimic.edstays','mimic.triage','mimic.ed_vitalsign',
      'mimic.ed_diagnosis','mimic.icustays','mimic.chartevents','mimic.labevents',
      'app.bed','app.cohort','app.demo_clock','app.demo_stay','app.patient_alias',
      'app.prediction','app.prediction_ack','app.alert','app.bed_assignment'])
  AND con.conrelid::regclass::text <> ALL(ARRAY[
      'mimic.patients','mimic.admissions','mimic.edstays','mimic.triage','mimic.ed_vitalsign',
      'mimic.ed_diagnosis','mimic.icustays','mimic.chartevents','mimic.labevents',
      'app.bed','app.cohort','app.demo_clock','app.demo_stay','app.patient_alias',
      'app.prediction','app.prediction_ack','app.alert','app.bed_assignment'])
ORDER BY 1;

\echo '### 4. 제외 대상 확인 — public 스키마 실데이터 보유 여부'
SELECT 'public.clinical_records' AS t, count(*) FROM public.clinical_records
UNION ALL SELECT 'public.kcd_codes', count(*) FROM public.kcd_codes;

\echo '### 5. 데모 상태 요약'
SELECT 'demo_clock' AS k, coalesce((SELECT 'epoch='||epoch_virtual||' anchor_virtual='||anchor_virtual||' speed='||speed FROM app.demo_clock WHERE id=1),'(없음)') AS v
UNION ALL SELECT 'cohort seed', coalesce((SELECT string_agg(DISTINCT seed,',') FROM app.cohort),'(비어 있음)')
UNION ALL SELECT 'prediction model_version', coalesce((SELECT string_agg(DISTINCT model_version,',') FROM app.prediction),'(비어 있음)')
UNION ALL SELECT 'prediction 최신 created_at', coalesce((SELECT max(created_at)::text FROM app.prediction),'(비어 있음)')
UNION ALL SELECT 'demo_stay active', (SELECT count(*) FILTER (WHERE is_active)::text FROM app.demo_stay);

\echo '### 6. 시퀀스 현재값'
SELECT schemaname||'.'||sequencename AS sequence, last_value
FROM pg_sequences WHERE schemaname IN ('mimic','app') ORDER BY 1;
