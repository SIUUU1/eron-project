-- =====================================================================
-- ER:ON — PostgreSQL schema
--
--   mimic  : MIMIC-IV 원천 서브셋 (읽기 전용, 적재 스크립트만 씀)
--   app    : 애플리케이션 생성 데이터 (예측 · 데모 스캐폴딩)
--   public : 기존 backend CRUD 도메인 (SQLAlchemy create_all 이 관리, 여기서 안 건드림)
--
-- 멱등(idempotent) 하게 작성한다. docker-entrypoint-initdb.d 는 볼륨이
-- 비어 있을 때만 실행되므로, load_subset.py 도 이 파일을 다시 적용한다.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS mimic;
CREATE SCHEMA IF NOT EXISTS app;


-- ---------------------------------------------------------------------
-- mimic 스키마
-- ---------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS mimic.patients (
    subject_id        BIGINT PRIMARY KEY,
    gender            CHAR(1),
    anchor_age        SMALLINT,
    anchor_year       SMALLINT,
    anchor_year_group TEXT,
    dod               DATE
);

CREATE TABLE IF NOT EXISTS mimic.admissions (
    hadm_id              BIGINT PRIMARY KEY,
    subject_id           BIGINT NOT NULL,
    admittime            TIMESTAMP,
    dischtime            TIMESTAMP,
    deathtime            TIMESTAMP,
    admission_type       TEXT,
    admission_location   TEXT,
    discharge_location   TEXT,
    insurance            TEXT,
    marital_status       TEXT,
    race                 TEXT,
    edregtime            TIMESTAMP,
    edouttime            TIMESTAMP,
    hospital_expire_flag SMALLINT
);

CREATE TABLE IF NOT EXISTS mimic.edstays (
    stay_id           BIGINT PRIMARY KEY,
    subject_id        BIGINT NOT NULL,
    hadm_id           BIGINT,
    intime            TIMESTAMP NOT NULL,
    outtime           TIMESTAMP,
    gender            CHAR(1),
    race              TEXT,
    arrival_transport TEXT,
    disposition       TEXT
);

CREATE TABLE IF NOT EXISTS mimic.triage (
    stay_id        BIGINT PRIMARY KEY,
    subject_id     BIGINT NOT NULL,
    temperature    DOUBLE PRECISION,   -- 화씨(°F)
    heartrate      DOUBLE PRECISION,
    resprate       DOUBLE PRECISION,
    o2sat          DOUBLE PRECISION,
    sbp            DOUBLE PRECISION,
    dbp            DOUBLE PRECISION,
    pain           TEXT,               -- 자유텍스트('unable','uta' 등) 존재 → 숫자 캐스팅 금지
    acuity         SMALLINT,           -- ESI 1~5 (KTAS 아님)
    chiefcomplaint TEXT
);

CREATE TABLE IF NOT EXISTS mimic.ed_vitalsign (
    id          BIGSERIAL PRIMARY KEY,
    stay_id     BIGINT    NOT NULL,
    subject_id  BIGINT    NOT NULL,
    charttime   TIMESTAMP NOT NULL,
    temperature DOUBLE PRECISION,      -- 화씨(°F)
    heartrate   DOUBLE PRECISION,
    resprate    DOUBLE PRECISION,
    o2sat       DOUBLE PRECISION,
    sbp         DOUBLE PRECISION,
    dbp         DOUBLE PRECISION,
    rhythm      TEXT,
    pain        TEXT
);

CREATE TABLE IF NOT EXISTS mimic.ed_diagnosis (
    stay_id     BIGINT   NOT NULL,
    seq_num     SMALLINT NOT NULL,
    subject_id  BIGINT   NOT NULL,
    icd_code    TEXT     NOT NULL,
    icd_version SMALLINT NOT NULL,
    icd_title   TEXT,
    PRIMARY KEY (stay_id, seq_num)
);

-- ICU 의 stay_id 는 ED 의 stay_id 와 다른 식별자 체계 → 이름을 분리한다
CREATE TABLE IF NOT EXISTS mimic.icustays (
    icu_stay_id    BIGINT PRIMARY KEY,
    subject_id     BIGINT NOT NULL,
    hadm_id        BIGINT NOT NULL,
    first_careunit TEXT,
    last_careunit  TEXT,
    intime         TIMESTAMP,
    outtime        TIMESTAMP,
    los            DOUBLE PRECISION
);


-- 검사 결과. 악화 예측 모델(services/riskmodel)의 lab feature 36개가 여기서 나온다.
--
-- 🔑 시간창으로 자르지 않는다. 모델의 lab_*_dt / lab_*_last 는 "환자의 마지막 검사가
--    언제였나" 를 보는 feature 이고, 학습 분포상 그 간격의 중앙값이 약 95일,
--    99 분위가 약 5.6년이다(artifacts/feature_spec.json). 체류 구간 근처만 적재하면
--    참조 대상이 더 과거의 검사로 밀려 배치와 값이 어긋난다 — 에러 없이 성능만 떨어진다.
--    관측 시점 컷오프(storetime <= t)는 DB 가 아니라 feature layer 가 적용한다.
--
-- itemid 화이트리스트도 걸지 않는다. 걸어두면 모델 개정으로 필요한 검사가 늘 때
-- 조용히 결측이 된다.
--
-- hadm_id 에는 FK 를 걸지 않는다. 응급실에서 귀가한 환자의 검사는 입원 건에 묶이지
-- 않아 NULL 비율이 높고, 시간창에 코호트 밖 입원의 검사가 걸릴 수 있다.
CREATE TABLE IF NOT EXISTS mimic.labevents (
    labevent_id BIGINT PRIMARY KEY,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT,
    itemid      INTEGER   NOT NULL,
    charttime   TIMESTAMP NOT NULL,   -- 채혈 시각
    storetime   TIMESTAMP,            -- 결과 보고 시각. feature 는 이쪽을 쓴다
    valuenum    DOUBLE PRECISION
);

-- ICU 활력징후. ED 퇴실 후 구간을 메우는 보조 원천이다(커버리지 낮음).
--
-- itemid 는 artifacts/bundle.json["vital_itemids"] 를 그대로 쓴다. 적재 스크립트가
-- 그 파일에서 읽으므로 여기에 목록을 적어두지 않는다 — 두 곳에 적으면 어긋난다.
-- 원본 stay_id 는 ED 의 stay_id 와 다른 식별자 체계라 icu_stay_id 로 이름을 분리한다.
CREATE TABLE IF NOT EXISTS mimic.chartevents (
    id          BIGSERIAL PRIMARY KEY,
    icu_stay_id BIGINT    NOT NULL,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT    NOT NULL,
    itemid      INTEGER   NOT NULL,
    charttime   TIMESTAMP NOT NULL,
    valuenum    DOUBLE PRECISION
);


-- ---------------------------------------------------------------------
-- 악화 라벨 원천 (feature 아님 — 성능 모니터링의 outcome 판정에만 쓴다)
--
-- 🔑 학습 파이프라인(`src/data/build_events.py`)이 y_deterioration 을 만들 때 읽는
--    원천 4개다. 예측에는 필요 없고, "예측이 맞았는지" 를 판정할 때만 필요하다.
--    악화 정의: (t, t+3h] 안의 호흡부전 처치 OR 승압제 OR CPR OR 사망 OR 생리학적 악화 onset.
--    생리학적 악화는 mimic.ed_vitalsign + mimic.chartevents 로 산출하므로 여기 없다.
--
-- 🔑 itemid 화이트리스트를 걸지 않는다. mimic.labevents 와 같은 이유다 — 걸어두면
--    라벨 정의가 개정될 때(승압제 목록 추가 등) 조용히 결측이 된다. 데모 데이터셋
--    기준 네 파일 합쳐 8만 행이 안 되므로 전량을 담아도 비용이 없다.
--    실제 판정 조건은 backend/app/services/model_monitoring_label.py 한 곳에만 둔다.
--
-- 🔑 hadm_id 에 FK 를 걸지 않는다. mimic.labevents 와 같은 이유이며, 코호트 밖 입원의
--    행이 섞여 들어와도 라벨 조인에서 자연히 걸러진다.
-- ---------------------------------------------------------------------

-- 호흡부전 처치(삽관·침습/비침습 환기)·CPR. 판정은 starttime 기준이다.
CREATE TABLE IF NOT EXISTS mimic.procedureevents (
    id          BIGSERIAL PRIMARY KEY,
    icu_stay_id BIGINT,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT,
    itemid      INTEGER   NOT NULL,
    starttime   TIMESTAMP NOT NULL
);

-- 승압제 1순위 원천. 연속 주입행을 에피소드로 묶어야 하므로 endtime 도 담는다
-- (gap 1h 초과면 새 에피소드 · 지속 1h 미만 에피소드는 이벤트로 세지 않는다).
CREATE TABLE IF NOT EXISTS mimic.inputevents (
    id          BIGSERIAL PRIMARY KEY,
    icu_stay_id BIGINT,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT,
    itemid      INTEGER   NOT NULL,
    starttime   TIMESTAMP NOT NULL,
    endtime     TIMESTAMP
);

-- 승압제 2순위 원천(실제 투여 시작 시각). 약물명·투여 이벤트 문자열로 판정한다.
CREATE TABLE IF NOT EXISTS mimic.emar (
    id          BIGSERIAL PRIMARY KEY,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT,
    charttime   TIMESTAMP NOT NULL,
    medication  TEXT,
    event_txt   TEXT
);

-- 승압제 3순위 원천(처방 시각). route 가 정맥 투여인 것만 이벤트로 본다.
CREATE TABLE IF NOT EXISTS mimic.prescriptions (
    id          BIGSERIAL PRIMARY KEY,
    subject_id  BIGINT    NOT NULL,
    hadm_id     BIGINT,
    starttime   TIMESTAMP NOT NULL,
    drug        TEXT,
    route       TEXT
);


-- ---------------------------------------------------------------------
-- app 스키마
-- ---------------------------------------------------------------------

-- 예측 결과. 모델 output 구조 미확정 → 최소 필드 + detail JSONB (D-TODO 확정)
CREATE TABLE IF NOT EXISTS app.prediction (
    id               BIGSERIAL PRIMARY KEY,
    ed_stay_id       BIGINT    NOT NULL,
    model_version    TEXT      NOT NULL,
    prediction_time  TIMESTAMP NOT NULL,
    t_idx            INTEGER,
    horizon_minutes  INTEGER,
    risk_probability DOUBLE PRECISION NOT NULL
                     CHECK (risk_probability >= 0 AND risk_probability <= 1),
    risk_level       TEXT NOT NULL
                     CHECK (risk_level IN ('stable','watch','rising','critical')),
    detail           JSONB,
    created_at       TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT prediction_unique UNIQUE (ed_stay_id, model_version, prediction_time)
);

-- 예측 시점의 model feature 값 (drift 모니터링 전용).
--
-- 🔑 왜 app.prediction.detail 에 넣지 않는가
--    detail 은 app.v_latest_prediction 을 통해 병상·알림·환자목록 응답에 그대로 실린다.
--    거기에 feature 100개를 넣으면 대시보드 응답이 통째로 무거워진다. drift 는
--    별도 화면에서만 쓰므로 조회 경로를 분리한다.
--
-- 🔑 feature 는 riskmodel 이 만든 값을 그대로 받아 적는다. backend 가 다시 만들지 않는다
--    (repositories/ml_features.py 주석과 같은 이유 — 규칙이 두 곳에 생기면 조용히 어긋난다).
--    riskmodel 은 include_features=true 일 때만 실어 보낸다.
--
-- ⚠ 이름은 여기 저장하지 않는다. bundle.json["features"] 순서가 정본이고,
--   feature_values 는 그 순서에 대응하는 값 배열이다. 이름을 행마다 반복하면 정본이 둘이 된다.
CREATE TABLE IF NOT EXISTS app.prediction_feature (
    ed_stay_id      BIGINT    NOT NULL,
    prediction_time TIMESTAMP NOT NULL,   -- MIMIC 원본 시간축 (app.prediction 과 같다)
    model_version   TEXT      NOT NULL,
    -- feature_hash 가 다르면 feature 구성이 바뀐 것이다. 섞어서 분포를 내면 안 된다.
    feature_hash    TEXT,
    -- 숫자 배열. 결측은 null 이다(0 이 아니다). values 는 SQL 예약어라 이름을 붙여 쓴다.
    feature_values  JSONB     NOT NULL,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (ed_stay_id, prediction_time, model_version)
);


-- 코호트 정의 (선별 결과).
--
-- 적재 대상 stay 목록과 선별 메타데이터를 DB 에 둔다. 파일(cohort.csv)에 두면
-- 저장소에 커밋되지 않아 팀원 간 코호트가 어긋날 수 있고, DB 만 봐서는
-- 어떤 기준으로 뽑힌 환자인지 알 수 없다.
--
-- mimic.edstays 보다 먼저 채워지므로 FK 를 걸지 않는다.
-- (적재 후 정합성은 load_subset.py 의 검증 쿼리가 확인한다)
CREATE TABLE IF NOT EXISTS app.cohort (
    ed_stay_id  BIGINT   PRIMARY KEY,
    subject_id  BIGINT   NOT NULL,
    hadm_id     BIGINT,
    tier        CHAR(1)  NOT NULL CHECK (tier IN ('A','B','C','D')),
    acuity      SMALLINT NOT NULL,
    vital_count INTEGER  NOT NULL,
    seed        TEXT     NOT NULL,
    selected_at TIMESTAMP NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_cohort_tier ON app.cohort (tier);


-- D1 확정: 결정론적 가명
CREATE TABLE IF NOT EXISTS app.patient_alias (
    ed_stay_id   BIGINT  PRIMARY KEY,
    display_name TEXT    NOT NULL,
    is_pseudonym BOOLEAN NOT NULL DEFAULT TRUE
);

-- D6 확정: 데모 시간축 (원천 timestamp 는 손대지 않는다)
--
-- 오프셋을 값으로 저장하지 않는다. 적재 시점에 고정하면 실제 시간이 흐를수록
-- 코호트 전체가 과거로 밀려 "현재 재실 환자" 가 사라진다.
-- 대신 원본 시간축에서 '현재'에 대응하는 시점(now_ref)만 저장하고,
-- 오프셋은 조회할 때 now() - now_ref 로 계산한다 (app.v_demo_stay).
DO $$
BEGIN
    -- 구버전(demo_offset/demo_intime 고정 저장) 테이블이면 갈아엎는다
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'app' AND table_name = 'demo_stay'
          AND column_name = 'demo_offset'
    ) THEN
        DROP TABLE app.demo_stay CASCADE;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.demo_stay (
    ed_stay_id BIGINT    PRIMARY KEY,
    now_ref    TIMESTAMP NOT NULL,
    is_active  BOOLEAN   NOT NULL DEFAULT TRUE
);

-- D2 확정: 병상 데모 배정
CREATE TABLE IF NOT EXISTS app.bed (
    bed_id TEXT PRIMARY KEY,
    zone   TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS app.bed_assignment (
    id          BIGSERIAL PRIMARY KEY,
    bed_id      TEXT      NOT NULL REFERENCES app.bed(bed_id),
    ed_stay_id  BIGINT,
    devices     TEXT[]    NOT NULL DEFAULT '{}',
    assigned_at TIMESTAMP NOT NULL DEFAULT now(),
    released_at TIMESTAMP
);

-- ---------------------------------------------------------------------
-- 데모 시계
--
-- 화면의 모든 시각은 app.v_demo_stay 한 곳에서 파생된다. 그 기준을 now() 대신
-- app.demo_now() 로 두면, 시계 하나만 조작해서 목록·상세·차트·병상·퇴실 판정을
-- 한꺼번에 움직일 수 있다. 1시간 단위 악화 예측 시연용이다.
--
--   speed = 1     평상시 (실제 시간과 동일하게 흐름)
--   speed = 0     정지 (설명하는 동안 화면 고정)
--   speed = 3600  배속 (실제 1초 = 데모 1시간)
--   스텝          anchor_virtual 을 +1시간 하고 anchor_real 을 now() 로 재설정
-- ---------------------------------------------------------------------

-- 구버전(epoch_virtual 없음) 테이블이면 갈아엎는다
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema='app' AND table_name='demo_clock')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema='app' AND table_name='demo_clock'
                 AND column_name='epoch_virtual') THEN
        DROP TABLE app.demo_clock CASCADE;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.demo_clock (
    id             SMALLINT  PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    -- 매핑 기준점(고정). demo_stay.now_ref 가 이 가상 시각에 대응한다.
    -- 시계를 진행해도 이 값은 바뀌지 않는다 — 바뀌면 오프셋이 함께 밀려
    -- 시계를 돌려도 화면이 그대로인 문제가 생긴다.
    epoch_virtual  TIMESTAMP NOT NULL DEFAULT now(),
    -- 시계 보간 기준. advance / speed 변경 때마다 갱신된다.
    anchor_real    TIMESTAMP NOT NULL DEFAULT now(),
    anchor_virtual TIMESTAMP NOT NULL DEFAULT now(),
    speed          NUMERIC   NOT NULL DEFAULT 1 CHECK (speed >= 0),
    updated_at     TIMESTAMP NOT NULL DEFAULT now()
);

INSERT INTO app.demo_clock (id) VALUES (1) ON CONFLICT (id) DO NOTHING;

-- 지금 시각(가상). 시계가 흐르는 축.
CREATE OR REPLACE FUNCTION app.demo_now() RETURNS timestamp
LANGUAGE sql STABLE AS $$
    SELECT coalesce(
        (SELECT c.anchor_virtual + (now()::timestamp - c.anchor_real) * c.speed
           FROM app.demo_clock c WHERE c.id = 1),
        now()::timestamp
    )
$$;

-- 매핑 기준점. 원본 시각 → 화면 시각 변환에만 쓰며 시계와 무관하게 고정이다.
CREATE OR REPLACE FUNCTION app.demo_epoch() RETURNS timestamp
LANGUAGE sql STABLE AS $$
    SELECT coalesce(
        (SELECT c.epoch_virtual FROM app.demo_clock c WHERE c.id = 1),
        now()::timestamp
    )
$$;

-- 의료진 "재검토 완료" 확인 상태.
--
-- 🔑 경고 자체는 app.prediction 에서 조회 시점에 파생한다(app.alert 는 쓰지 않는다).
--    여기 저장하는 것은 **의료진이 확인했다는 사실** 하나뿐이다.
--
-- 🔑 PK 에 prediction_time 을 포함하는 이유
--    확인은 "그 시점 예측에 대한 확인"이다. 다음 예측이 생기면 최신 prediction_time 이
--    달라져 이 행과 짝이 맞지 않으므로 확인 표시가 저절로 풀린다.
--    (별도 리셋 스케줄러가 필요 없다 — 최신 예측과의 관계로 계산한다)
--
-- ⚠ 모델의 alarm/band 를 바꾸지 않는다. AI 상태와 의료진 확인 상태는 별개다.
CREATE TABLE IF NOT EXISTS app.prediction_ack (
    ed_stay_id      BIGINT    NOT NULL,
    -- app.prediction.prediction_time 과 같은 **MIMIC 원본 시간축**이다.
    prediction_time TIMESTAMP NOT NULL,
    -- ⏱ 실제 서버 시각(감사 기록용).
    acknowledged_at TIMESTAMP NOT NULL DEFAULT now(),
    -- ⏱ **데모 시각**. 확인이 유효한지는 이 값으로 판정한다.
    --    데모 시계를 되돌리면 그보다 나중에 한 확인은 '아직 하지 않은 것'이 되어야 한다
    --    (기록을 지우지 않고 시간 기준으로만 무효화한다 — 다시 앞으로 가면 되살아난다).
    acknowledged_demo_at TIMESTAMP NOT NULL DEFAULT app.demo_now(),
    acknowledged_by TEXT,
    created_at      TIMESTAMP NOT NULL DEFAULT now(),
    PRIMARY KEY (ed_stay_id, prediction_time)
);

-- 기존 배포본 보완 (컬럼이 없으면 추가하고 데모 현재 시각으로 채운다)
ALTER TABLE app.prediction_ack
    ADD COLUMN IF NOT EXISTS acknowledged_demo_at TIMESTAMP;
UPDATE app.prediction_ack SET acknowledged_demo_at = app.demo_now()
 WHERE acknowledged_demo_at IS NULL;
ALTER TABLE app.prediction_ack
    ALTER COLUMN acknowledged_demo_at SET NOT NULL,
    ALTER COLUMN acknowledged_demo_at SET DEFAULT app.demo_now();


-- 모델 연동 전까지 비어 있다 (가짜 경고를 만들지 않는다)
CREATE TABLE IF NOT EXISTS app.alert (
    id              BIGSERIAL PRIMARY KEY,
    ed_stay_id      BIGINT    NOT NULL,
    alert_time      TIMESTAMP NOT NULL,
    level           TEXT      NOT NULL,
    message         TEXT      NOT NULL,
    acknowledged_at TIMESTAMP,
    acknowledged_by TEXT
);


-- ---------------------------------------------------------------------
-- AI 모델 성능 모니터링
--
-- 🔑 모델 메타데이터 테이블(model_registry)은 만들지 않는다.
--    모델명·버전·threshold·feature 수·horizon 의 정본은 artifacts/bundle.json 이다.
--    DB 에 복제하면 정본이 둘이 되고, 재학습 때 조용히 어긋난다.
--
-- 🔑 여기 있는 것은 **계산 결과 캐시**다. 원천은 app.prediction 과 mimic.* 이며,
--    지우고 다시 계산해도 같은 값이 나온다.
-- ---------------------------------------------------------------------

-- 예측 1건과 그 관찰창의 실제 악화 여부.
--
-- 🔑 시간축은 전부 **MIMIC 원본 축**이다(app.prediction.prediction_time 과 같다).
--    데모 시계는 "이 관찰창이 화면상 도래했는가" 만 판정하며 저장하지 않는다.
--    demo_offset 은 stay 마다 다르므로(app.v_demo_stay) 저장하면 시계를 움직일 때 어긋난다.
--
-- 🔑 label_definition 이 UNIQUE 에 들어가는 이유
--    악화 정의는 학습 사양에 딸린 값이다. 나중에 정의가 개정되면 새 정의로 행이 추가될 뿐
--    기존 행을 덮어쓰지 않는다 — 과거 성능 수치의 근거가 사라지면 안 된다.
CREATE TABLE IF NOT EXISTS app.model_outcome (
    id                  BIGSERIAL PRIMARY KEY,
    ed_stay_id          BIGINT    NOT NULL,
    prediction_time     TIMESTAMP NOT NULL,
    -- 악화 정의의 식별자. 예: training_v2.0.0_labelB_srcC_win3h
    label_definition    TEXT      NOT NULL,
    -- prediction_time + label_window_h. 이 시각이 지나야 판정할 수 있다.
    evaluation_end_time TIMESTAMP NOT NULL,
    -- 그 stay 의 관측 종료(obs_end). 관찰창이 이 시각을 넘으면 일부 이벤트를 볼 수 없다.
    observation_end     TIMESTAMP,
    -- 1 = 악화 발생, 0 = 미발생. 중도절단이면 NULL 이고 지표 계산에서 빠진다.
    outcome_label       SMALLINT  CHECK (outcome_label IN (0, 1)),
    -- 양성일 때 어떤 이벤트였는지. resp_inv | resp_niv | cpr | vaso | death | abnormal
    event_type          TEXT,
    event_time          TIMESTAMP,
    -- 관측 경로 자체가 없어 판정할 수 없는 행(입원 기록이 없어 처치·사망을 볼 수 없다).
    -- 음성으로 세지 않는다 — 지표에서 제외한다.
    is_censored         BOOLEAN   NOT NULL DEFAULT FALSE,
    -- 학습 grid 는 첫 악화 시점에서 끊긴다(bundle.grid.truncate_at_first_event).
    -- 그 이후 시점은 학습 분포에 없으므로 지표에서 제외하고 표시만 한다.
    is_truncated        BOOLEAN   NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMP NOT NULL DEFAULT now(),
    CONSTRAINT model_outcome_unique UNIQUE (ed_stay_id, prediction_time, label_definition)
);


-- 기간별 성능 snapshot (이력).
--
-- 🔑 **평가 집합이 달라졌을 때만** 한 행이 늘어난다. 조회할 때마다 적으면 30초 폴링에
--    하루 2,880 행이 쌓이는데 값은 대부분 같다. 평가 완료 건수·양성 건수·평가 구간 끝이
--    직전 snapshot 과 같으면 적지 않는다(repositories/model_monitoring.record_metric_snapshot).
--    지표 자체는 언제든 app.model_outcome 에서 다시 계산할 수 있다. 이 표는
--    "그때는 이랬다" 를 남기기 위한 것이다 — 모델 교체 전후 비교 등.
CREATE TABLE IF NOT EXISTS app.model_performance_metric (
    id                      BIGSERIAL PRIMARY KEY,
    model_version           TEXT      NOT NULL,
    label_definition        TEXT      NOT NULL,
    evaluation_period_start TIMESTAMP NOT NULL,
    evaluation_period_end   TIMESTAMP NOT NULL,
    -- sample_count = 기간 안의 전체 예측. evaluated 만 지표에 들어간다.
    sample_count            INTEGER   NOT NULL DEFAULT 0,
    evaluated_count         INTEGER   NOT NULL DEFAULT 0,
    pending_count           INTEGER   NOT NULL DEFAULT 0,
    censored_count          INTEGER   NOT NULL DEFAULT 0,
    positive_count          INTEGER   NOT NULL DEFAULT 0,
    -- 표본이 부족하면 NULL 이다. 0 이 아니다 — 0.0 은 "성능이 0" 으로 읽힌다.
    pr_auc                  DOUBLE PRECISION,
    auroc                   DOUBLE PRECISION,
    recall                  DOUBLE PRECISION,
    precision               DOUBLE PRECISION,
    f1                      DOUBLE PRECISION,
    true_positive           INTEGER,
    true_negative           INTEGER,
    false_positive          INTEGER,
    false_negative          INTEGER,
    threshold               DOUBLE PRECISION,
    created_at              TIMESTAMP NOT NULL DEFAULT now()
);


-- Data drift.
--
-- ⚠ 현재는 비어 있다. 채우려면 예측 시점의 feature 100개 분포가 필요한데,
--   app.prediction.detail 에는 기여 상위 신호만 남고 feature 벡터는 저장되지 않는다.
--   backend 가 feature 를 다시 만드는 것은 금지다 — 그 규칙은 riskmodel 한 곳에만 둔다
--   (repositories/ml_features.py 주석). riskmodel 이 feature row 를 함께 돌려주고
--   그것을 저장하게 되면 그때 채운다. 그전까지 API 는 insufficient_data 를 반환한다.
--   가짜 PSI 를 만들지 않는다.
CREATE TABLE IF NOT EXISTS app.model_drift_metric (
    id                BIGSERIAL PRIMARY KEY,
    model_version     TEXT      NOT NULL,
    feature_name      TEXT      NOT NULL,
    psi               DOUBLE PRECISION,
    status            TEXT      NOT NULL
                      CHECK (status IN ('ok', 'warning', 'critical', 'insufficient_data')),
    reference_source  TEXT,
    sample_count      INTEGER,
    computed_at       TIMESTAMP NOT NULL DEFAULT now()
);
