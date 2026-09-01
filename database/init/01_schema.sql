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
