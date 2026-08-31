# ClinicalNLP PostgreSQL migration

ClinicalNLP의 검색·버전·승인 데이터를 ER:ON PostgreSQL의 `clinicalnlp` schema로
이전한다. 기존 `public`, `app`, `mimic` schema는 변경하지 않는다.

## 최종 저장 경계

PostgreSQL이 관리한다.

- 버전이 부여된 의료용어 사전과 KCD 코드
- 의료용어 및 정책 임베딩(`vector(256)`)
- 정책 문서, 원문 청크, rule ID 연결
- 승인 별칭 후보, 의료진 확인, release 이력

파일로 유지한다.

- scispaCy Python 환경과 mention model
- UMLS linker 지식 베이스와 cache
- Guardrail/Threshold JSON과 원본 정책 문서
- 감사 및 재이관용 SQLite 원본(운영 컨테이너에는 마운트하지 않음)

## Migration 001-004

- `05_clinicalnlp.sql`: schema, source release, 용어/KCD/정책/별칭/pgvector 테이블
- `06_clinicalnlp_import_source_rows.sql`: 공식 source row identity 보존
- `07_clinicalnlp_medical_vector_releases.sql`: collection별 의료 Vector release
- `08_clinicalnlp_policy_partial_dates.sql`: `YYYY-MM` 같은 정책 발행일 정밀도 보존

같은 source hash는 재실행해도 중복되지 않는다. hash가 바뀌면 새 immutable release를
만들고 완전성 확인 후 활성 release만 교체한다.

```bash
python3 database/scripts/apply_clinicalnlp_schema.py
python3 -m unittest database.tests.test_clinicalnlp_migration
```

성공 출력 예시:

```json
{"migration":"004","schema":"clinicalnlp","status":"ready","table_count":15,"vector_dimensions":256}
```

## 데이터 이관

의료용어·KCD:

```bash
python3 database/scripts/import_clinicalnlp_dictionaries.py \
  --dictionary-root "$PWD/runtime/clinicalnlp/medical-dictionaries"
```

의료 Vector와 정책 문서/Vector:

```bash
docker compose --profile clinical run --rm --no-deps \
  -v "$PWD/runtime/clinicalnlp/vectors:/runtime/vectors:ro" clinicalnlp \
  python scripts/import_medical_vectors.py \
  --index /runtime/vectors/api3_vectors.sqlite

docker compose --profile clinical run --rm --no-deps \
  -v "$PWD/runtime/clinicalnlp/policy:/runtime/policy:ro" clinicalnlp \
  python scripts/import_policy_index.py \
  --index /runtime/policy/policy_vectors.sqlite
```

Importer는 문서·청크 해시, page/article 추적 정보, 벡터 차원과 행 수를 검증한다.

## 운영 전환

ClinicalNLP HTTP runtime은 PostgreSQL 전용이다. `sqlite`/`shadow` backend 설정은
없으며 기존 backend/path 변수가 남으면 시작 설정 오류로 처리한다.
`CLINICALNLP_DATABASE_URL`이 필수다. Compose는 PostgreSQL healthcheck 이후
서비스를 시작하고 `/runtime/scispacy`만 읽기 전용으로 마운트한다.

SQLite 비교 도구와 importer 코드는 감사·재현을 위해 남겨두되 운영 프로세스에서는
호출하지 않는다. rollback이 필요하면 DB의 이전 active release를 다시 활성화하며,
운영 서비스를 SQLite 모드로 되돌리지 않는다.
