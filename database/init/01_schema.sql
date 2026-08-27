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
