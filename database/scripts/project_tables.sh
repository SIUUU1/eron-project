#!/usr/bin/env bash
# =====================================================================
# ER:ON — 재적재 대상 테이블 화이트리스트 (단일 정본)
#
# dump / inspect / restore 스크립트가 모두 이 파일 하나만 읽는다.
# 여기에 없는 테이블은 어떤 스크립트도 조회조차 하지 않는다.
#
# 순서 = FK 부모 → 자식. pg_dump 가 뽑는 COPY 순서와 같아,
# 복원 시 FK 를 끄지 않고도 제약을 만족한다(= 어긋나면 롤백된다).
#
# ⚠ 의도적으로 제외한 것과 그 이유
#   clinicalnlp.*           의료용어·KCD·정책·Vector. 서버에서만 적재하며 로컬에는 없다.
#   public.*                backend CRUD 도메인. 로컬은 스모크 테스트 행뿐이고
#                           서버에는 실제 clinical_records·kcd_codes 가 있을 수 있다.
#   mimic_ed.*              코드가 참조하지 않는 초기 적재 잔재.
#   public.test_connection  초기 연결 확인용 잔재.
# =====================================================================

PROJECT_TABLES=(
  # mimic 스키마 — MIMIC-IV 원천 서브셋
  mimic.patients        # 부모 없음
  mimic.admissions      # 부모 없음
  mimic.edstays         # → mimic.patients, mimic.admissions
  mimic.triage          # → mimic.edstays
  mimic.ed_vitalsign    # → mimic.edstays
  mimic.ed_diagnosis    # → mimic.edstays
  mimic.icustays        # → mimic.admissions
  mimic.chartevents     # → mimic.icustays
  mimic.labevents       # → mimic.patients

  # app 스키마 — 애플리케이션 생성 데이터
  app.bed               # 부모 없음
  app.cohort            # 부모 없음 (FK 없음: 01_schema.sql 주석 참조)
  app.demo_clock        # 부모 없음 (한 행짜리 데모 시계)
  app.demo_stay         # → mimic.edstays
  app.patient_alias     # → mimic.edstays
  app.prediction        # → mimic.edstays
  app.prediction_ack    # 부모 없음
  app.alert             # → mimic.edstays
  app.bed_assignment    # → app.bed, mimic.edstays
)

# psql / SQL 에서 쓰기 좋은 형태
PROJECT_TABLES_CSV=$(printf '%s, ' "${PROJECT_TABLES[@]}"); PROJECT_TABLES_CSV=${PROJECT_TABLES_CSV%, }
PROJECT_TABLES_SQLARRAY=$(printf "'%s'," "${PROJECT_TABLES[@]}"); PROJECT_TABLES_SQLARRAY="ARRAY[${PROJECT_TABLES_SQLARRAY%,}]"
