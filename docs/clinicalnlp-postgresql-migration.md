# ClinicalNLP PostgreSQL migration

ClinicalNLP 검색 데이터를 기존 ER:ON PostgreSQL의 `clinicalnlp` 전용 schema로
이전한다. 기존 `public`, `app`, `mimic` schema와 테이블은 변경하지 않는다.

## 저장 경계

PostgreSQL이 관리하는 데이터:

- 버전이 부여된 의료용어 사전과 KCD 코드
- 의료용어·정책 임베딩(`vector(256)`)
- 정책 문서, 원문 청크, rule ID 연결
- 승인 별칭 후보, 의료진 확인, release 이력

파일로 유지하는 런타임 자산:

- scispaCy Python 환경과 mention model
- UMLS linker 지식 베이스와 cache
- 원본 PDF·JSON·SQLite migration source

SQLite 원본은 PG importer와 결과 동등성 검증이 완료될 때까지 삭제하지 않는다.

## Schema version 001-002

[`database/init/05_clinicalnlp.sql`](../database/init/05_clinicalnlp.sql)은 다음을
생성한다.

- `vector`, `pg_trgm` 확장
- `clinicalnlp` schema와 `schema_migrations`
- 원본을 덮어쓰지 않는 `source_releases`
- 의료용어 exact/FTS/trigram/pgvector 검색 테이블
- KCD exact/FTS/trigram 검색 테이블
- 정책 문서·청크·FTS·pgvector 검색 테이블
- 승인 별칭 후보와 version 이력 테이블

[`database/init/06_clinicalnlp_import_source_rows.sql`](../database/init/06_clinicalnlp_import_source_rows.sql)은
정규화 문자열이 같아도 서로 다른 공식 source row를 각각 보존하도록 의료용어와
KCD term identity를 추가한다.

같은 source의 hash가 바뀌면 `source_releases`에 새로운 행으로 적재한다. 활성
release만 교체하며 기존 release는 감사·rollback 목적으로 보존한다.

## 적용 방법

새 PostgreSQL volume에는 Docker entrypoint가 migration을 자동 적용한다. 이미
초기화된 로컬 또는 OCI volume에는 저장소 루트에서 다음 명령을 실행한다.

```bash
python3 database/scripts/apply_clinicalnlp_schema.py
```

다른 DB에 검증할 때만 대상을 명시한다.

```bash
python3 database/scripts/apply_clinicalnlp_schema.py --database DATABASE_NAME
```

성공 출력 예시:

```json
{"migration":"001","schema":"clinicalnlp","status":"ready","table_count":15,"vector_dimensions":256}
```

계약 테스트는 고유한 임시 DB를 만들고 migration을 두 번 적용한 후 그 임시 DB만
삭제한다.

```bash
python3 -m unittest database.tests.test_clinicalnlp_migration
```

## 의료용어·KCD importer

기존 SQLite 원본 5개를 active PostgreSQL release로 적재한다.

```bash
python3 database/scripts/import_clinicalnlp_dictionaries.py
```

Importer는 SQLite 파일 SHA-256과 importer schema version을 release identity로
사용한다. 같은 release를 다시 실행해도 중복되지 않으며, 새 release가 완전히
적재된 뒤에만 이전 release를 inactive로 전환한다. `ready`는 active release 수와
SQLite에서 계산한 concept·term 기대 행 수가 PostgreSQL과 모두 일치할 때만 반환한다.

```bash
python3 -m unittest database.tests.test_clinicalnlp_dictionary_import
```

## 이번 migration에 포함되지 않는 작업

- 기존 embedding을 PostgreSQL로 복사
- ClinicalNLP 조회 adapter를 PostgreSQL로 전환
- SQLite fallback 제거
- UMLS runtime 자산을 PostgreSQL에 저장

이 작업들은 importer, dual-read parity, PG 기본 전환 순으로 별도 커밋한다.
