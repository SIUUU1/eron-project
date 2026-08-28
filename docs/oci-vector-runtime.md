# OCI Vector 런타임 설계 초안

> 상태: 아키텍처 결정용 초안
>
> 기준일: 2026-08-28
>
> 범위: 의료용어·정책 검색 인덱스. 환자 원문, 개인식별정보, 운영 비밀값은 이 문서와 공용 지식 인덱스에 포함하지 않는다.

## 결론

ER:ON의 첫 OCI 배포에서는 **현재 SQLite 기반 의료용어·정책 벡터 인덱스를 유지하고, Object Storage를 버전이 붙은 배포 아티팩트의 원본 저장소로, Compute에 연결한 Block Volume을 런타임 저장소로 사용**하는 구성을 권고한다.

현재 ClinicalNLP 구현의 `api3_vectors.sqlite`와 `policy_vectors.sqlite`는 빌드된 읽기 중심 인덱스다. 따라서 첫 배포부터 별도 네트워크 VectorDB를 도입하는 것보다 다음 흐름이 변경량과 운영 부담이 작다.

```text
사전·정책 원본 → 오프라인 인덱스 빌드 및 검증
                         ↓
             OCI Object Storage
             (버전별 불변 아티팩트)
                         ↓ 배포 시 다운로드·해시 검증
          OCI Compute + Block Volume
                         ↓ read-only mount
                ClinicalNLP 컨테이너
```

검색 인덱스의 온라인 갱신, 여러 추론 인스턴스의 동시 쓰기, 중앙 관리가 실제 요구사항이 되는 시점에는 **OCI Database with PostgreSQL + `pgvector`**를 우선 승격 경로로 삼는다. Oracle Database 23ai AI Vector Search는 Oracle Database를 주 데이터 플랫폼으로 채택하거나 관계형 조건과 벡터/키워드 검색을 Oracle SQL 안에서 통합해야 할 때 검토한다.

## 현재 ER:ON과의 접점

- 본 프로젝트의 `docker-compose.yml`은 애플리케이션 DB에 `pgvector/pgvector:pg16` 이미지를 사용한다.
- `.env.example`에는 `QDRANT_URL` 자리가 있지만, 현재 Compose에는 Qdrant 서비스가 정의되어 있지 않다.
- ClinicalNLP의 의료용어 및 정책 인덱스는 현재 SQLite 파일로 생성되고 읽힌다. 즉, 현 단계의 핵심 요구는 분산 VectorDB보다 **검증된 인덱스 파일의 배포, 영속성, 롤백과 백업**이다.
- 공용 의료용어·정책 corpus와 환자별 임상 기록은 저장 수명과 접근 권한이 다르므로 같은 컬렉션에 섞지 않는다. 환자별 검색이 이후 필요해지면 별도 데이터 모델, 접근통제, 보존·삭제 정책을 먼저 정한다.

## 선택지 비교

| 선택지 | 적합한 시점 | 장점 | 부담·주의점 | ER:ON 판단 |
|---|---|---|---|---|
| SQLite 인덱스 아티팩트 + Object Storage + Compute/Block Volume | 읽기 중심 corpus, 배치 갱신, 초기 OCI 배포 | 현재 코드 유지, 버전 단위 롤백, 네트워크 DB 의존성 없음 | 온라인 부분 갱신과 다중 writer에 부적합; 배포 자동화 필요 | **1차 권고** |
| Compute + Block Volume에 자체 VectorDB | 전용 VectorDB API/필터/성능 특성이 필요하고 운영 책임을 수용할 때 | 제품 선택 자유, 기존 API를 보존하기 쉬움 | 패치, 모니터링, 장애조치, 일관된 백업·복구를 직접 운영 | 조건부 |
| OCI Database with PostgreSQL + `pgvector` | 중앙 온라인 인덱스, SQL metadata filter, PostgreSQL 통합이 필요할 때 | 관리형 PostgreSQL과 벡터를 한 서비스로 운영 | 확장 활성화·마이그레이션·성능 검증 필요 | **2차 우선 승격 경로** |
| Oracle Database 23ai AI Vector Search | Oracle Database 표준화 또는 관계형+벡터+키워드 통합이 핵심일 때 | 네이티브 `VECTOR`, 유사도·하이브리드 검색 | 현재 PostgreSQL 기반 ER:ON에는 DB 전환 비용과 운영 기술 변경이 큼 | 전략적 대안 |

## 1. OCI Compute + Block Volume 기반 자체 런타임

OCI Compute 인스턴스의 로컬 드라이브 변경은 인스턴스 종료 시 사라지지만 연결된 볼륨의 변경은 유지된다. 또한 Block Volume은 인스턴스에서 분리해 다른 인스턴스에 데이터 손실 없이 연결할 수 있는 영속 블록 스토리지다. 따라서 컨테이너 이미지와 실행 파일은 교체 가능하게 두고, 인덱스·DB 데이터 경로만 별도 Block Volume에 둔다. ([Compute 개요](https://docs.oracle.com/en-us/iaas/Content/Compute/Concepts/computeoverview.htm), [Block Volume 개요](https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/overview.htm))

권장 마운트 경계는 다음과 같다.

```text
/srv/eron/vector/
├─ releases/<index-version>/     # 배포된 SQLite 인덱스 또는 자체 VectorDB 데이터
├─ current -> releases/...       # 검증 후 원자적으로 전환할 포인터
├─ staging/                      # 다운로드·해시 검증 중인 파일
└─ export/                       # Object Storage로 보낼 논리 백업/내보내기
```

현재 Compose의 Docker named volume `eron_postgres_data`는 그 이름만으로 별도 OCI Block Volume에 배치되는 것이 아니다. OCI 배포 Compose에서는 운영체제에 마운트한 Block Volume의 명시적 호스트 경로를 bind mount하거나, Docker data root 전체가 Block Volume을 사용하도록 배포 표준을 정해야 한다.

컨테이너의 root filesystem은 영속 저장소로 취급하지 않는다. Oracle의 OKE 저장소 문서도 컨테이너 root filesystem은 컨테이너 삭제·재생성 시 사라질 수 있으며, Block Volume 기반 persistent volume을 사용하면 컨테이너가 종료되어도 데이터가 유지된다고 설명한다. 이 원칙을 단일 Compute의 Docker/Compose 배포에도 동일하게 적용한다. ([OKE 영속 스토리지 구성](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengcreatingpersistentvolumeclaim.htm))

Block Volume은 여러 스토리지 서버에 자동 복제되지만 Oracle은 availability domain 장애에 대비해 정기 백업을 권고한다. 볼륨 자체의 내구성과 사용자의 삭제·논리 손상·리전 장애에서 복구하는 백업은 별개로 본다. ([Block Volume 내구성 및 복제](https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/overview.htm))

### 자체 VectorDB를 선택할 때의 최소 운영 조건

1. 데이터 디렉터리를 Block Volume에 명시적으로 bind mount한다.
2. VectorDB 프로세스와 포트는 private subnet/NSG 안에 두고, 인터넷에 직접 공개하지 않는다.
3. 제품이 제공하는 논리 snapshot/export를 먼저 만들고, 그 결과물을 Object Storage로 복제한다.
4. Block Volume backup policy를 보조 복구 수단으로 설정한다. OCI는 사용자 정의 backup policy에 주기, 보존기간, 선택적 cross-region copy를 설정할 수 있다. ([Block Volume backup policy](https://docs.oracle.com/en-us/iaas/Content/Block/Tasks/schedulingvolumebackups.htm))
5. 백업 성공 여부뿐 아니라 정기 restore drill로 RPO/RTO를 검증한다.

현재 `.env.example`의 `QDRANT_URL`을 실제 Qdrant로 연결한다면 컨테이너의
`/qdrant/storage`를 Block Volume 경로에 마운트한다. Qdrant는 collection snapshot을
로컬 파일 또는 S3-compatible storage에 저장할 수 있고, OCI Object Storage도 Amazon
S3 Compatibility API를 제공한다. 다만 두 제품 문서가 이 조합을 직접 인증하는 것은
아니므로 endpoint, 인증, 업로드·복원까지 PoC한 뒤 운영 경로로 채택한다.
([Qdrant snapshot](https://qdrant.tech/documentation/operations/snapshots/),
[OCI Object Storage S3 Compatibility API](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/s3compatibleapi.htm))

Block Volume 백업은 볼륨 데이터의 point-in-time 사본이며 원본과 독립된 수명으로 Object Storage에 저장된다. 수동 백업에는 보존기간과 retention lock을 설정할 수 있다. 다만 실행 중인 DB의 파일을 그대로 캡처한 인프라 백업만으로 애플리케이션 수준 일관성이 자동 보장된다고 가정하지 않는다. ([Block Volume 백업 생성](https://docs.oracle.com/en-us/iaas/Content/Block/Tasks/create-bv-backup.htm)) 여러 볼륨을 함께 써야 한다면 volume group backup은 여러 볼륨을 point-in-time 및 crash-consistent하게 백업한다. ([Volume Group](https://docs.oracle.com/en-us/iaas/Content/Block/Concepts/volumegroups.htm))

## 2. OCI Object Storage의 역할

Object Storage는 확장 가능하고 내구성 있는 객체 저장 서비스이므로 다음 자료의 장기 원본 보관에 사용한다. ([Object Storage 개요](https://docs.oracle.com/en-us/iaas/Content/Object/home.htm))

- 의료사전·정책 원본 패키지
- 빌드 완료된 SQLite 벡터 인덱스와 manifest/checksum
- 자체 VectorDB의 논리 snapshot/export
- 임베딩 모델 또는 tokenizer 등 버전 고정 배포 아티팩트
- 복구 절차 검증에 사용하는 비식별 테스트 자료

Object Storage를 VectorDB의 live data directory처럼 사용하지 않는다. 런타임 랜덤 I/O는 Block Volume 또는 관리형 DB가 담당하고, Object Storage는 배포·백업·재생성 가능한 아티팩트 계층으로 둔다.

아티팩트 bucket은 object versioning을 켜면 덮어쓰기나 삭제 시 이전 버전을 보존할 수 있다. 이전 버전은 명시적으로 삭제하기 전까지 유지되며 저장 비용이 발생하므로 lifecycle policy로 정리 주기를 둔다. ([Object versioning](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingversioning.htm), [Object Lifecycle Management](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usinglifecyclepolicies.htm))

규정상 WORM 성격의 보존이 필요하면 별도의 backup bucket에 retention rule을 사용한다. 잠긴 retention rule은 tenancy 관리자나 Oracle Support도 삭제할 수 없고 기간 단축도 불가능하므로 시험 bucket에서 먼저 검증해야 한다. ([Object Storage retention rule](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingretentionrules_topic-To_create_a_retention_rule.htm)) Object versioning이 활성화된 bucket에는 retention rule을 추가할 수 없으므로, **롤백용 versioned artifact bucket과 규정 보존용 retention bucket을 분리**한다. ([versioning과 retention rule 제약](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usingversioning.htm#usingversioning_topic-Interaction_Between_Versioning_and_Other_Object_Storage_Features))

## 3. Oracle Database 23ai AI Vector Search

Oracle의 현재 제품 문서에서는 **Oracle AI Database 26ai가 Oracle Database 23ai를
대체**한다고 안내한다. 아래 23ai 기능 설명은 기존 23ai 기준선으로 유효하지만, 신규
OCI 구축에서는 실제 제공 서비스와 26ai 릴리스에서 필요한 기능의 가용성을 확인한다.
([Oracle Database FAQ](https://www.oracle.com/database/faq/))

Oracle AI Vector Search는 벡터 임베딩을 저장·색인하고 유사도 검색을 수행하며, 의미 기반 검색을 관계형 비즈니스 데이터 검색과 한 시스템에서 결합할 수 있다. ([Oracle AI Vector Search User's Guide](https://docs.oracle.com/en/database/oracle/oracle-database/23/vecse/oracle-ai-vector-search-users-guide.pdf))

23ai 계열 기능 중 ER:ON에 관련 있는 항목은 다음과 같다.

- `VECTOR` 데이터 타입으로 임베딩을 관계형 행과 함께 저장한다. ([사용자 가이드](https://docs.oracle.com/en/database/oracle/oracle-database/23/vecse/oracle-ai-vector-search-users-guide.pdf))
- native SQL에서 정확 또는 근사 유사도 검색을 관계형 조건과 결합할 수 있다. ([유사도·하이브리드 검색](https://docs.oracle.com/en/database/oracle/oracle-database/23/vecse/query-data-similarity-and-hybrid-searches.html))
- hybrid vector index는 Oracle Text의 키워드 검색과 vector similarity search를 단일 인덱스에서 결합한다. ([Hybrid Vector Index 관리](https://docs.oracle.com/en/database/oracle/oracle-database/23/vecse/manage-hybrid-vector-indexes.html))
- `DBMS_VECTOR`, `DBMS_VECTOR_CHAIN`, `DBMS_HYBRID_VECTOR` 패키지는 chunking, embedding, similarity/hybrid search 작업을 지원한다. ([Vector Search PL/SQL packages](https://docs.oracle.com/en/database/oracle/oracle-database/23/vecse/vector-search-pl-sql-packages-node.html))

이는 정책 문서의 키워드 조항 번호와 의미 검색을 함께 처리할 때 매력적이다. 그러나 ER:ON은 현재 PostgreSQL/SQLAlchemy 기반이므로, 단지 벡터 저장을 위해 Oracle Database로 전환하는 것은 첫 OCI 배포의 기본안으로 삼지 않는다. 실제 선택 전에는 대상 OCI Database 서비스/에디션·릴리스에서 필요한 vector 및 hybrid 기능이 제공되는지, 비용·백업·HA 요건이 맞는지 별도 PoC로 확인한다.

## 4. OCI Database with PostgreSQL의 `pgvector`

OCI Database with PostgreSQL은 공식 지원 확장 목록에 `pgvector`를 포함한다. 다만 기본 활성 확장이 아니며, 관리자가 custom configuration에서 먼저 허용해야 한다. 그 뒤 대상 DB에서 `pg_available_extensions`를 확인하고 실제 PostgreSQL 확장명인 `vector`를 `CREATE EXTENSION vector;`로 활성화한다. ([지원 확장 목록](https://docs.oracle.com/en-us/iaas/Content/postgresql/extensions.htm), [OCI 확장 활성화 절차](https://docs.oracle.com/en-us/iaas/Content/postgresql/config-list-enable-extension.htm), [pgvector 공식 설치 절차](https://github.com/pgvector/pgvector#installation))

서비스는 관리형 PostgreSQL 호환 DB이며 storage가 Compute와 분리되어 확장된다. 자동 백업은 일·주·월 단위로 예약할 수 있고 최대 35일 보존하며, 수동 백업과 cross-region copy도 제공한다. ([서비스 개요](https://docs.oracle.com/en-us/iaas/Content/postgresql/overview.htm), [PostgreSQL backup](https://docs.oracle.com/en-us/iaas/Content/postgresql/backups.htm)) Point-in-time recovery는 WAL과 주기적 data/WAL volume backup을 사용해 설정된 복구 창 안의 시점으로 새 DB system을 만든다. ([Point-in-time recovery](https://docs.oracle.com/en-us/iaas/Content/postgresql/point-time-recovery.htm))

ER:ON에서 다음 조건이 생기면 이 경로가 우선이다.

- 인덱스가 배치 파일이 아니라 API를 통해 지속적으로 갱신된다.
- 여러 ClinicalNLP 인스턴스가 같은 인덱스를 조회·수정한다.
- 의료용어/정책 metadata의 관계형 filter와 vector search를 한 트랜잭션 경계에서 다루고 싶다.
- 별도 VectorDB 운영보다 관리형 backup, HA, patching을 우선한다.

도입 전에는 현재 `sqlite-vec` 쿼리의 distance metric, 차원 수, filtering, top-k 결과가 `pgvector` 구현에서 동일한 의미를 갖는지 회귀 corpus로 비교한다. 현재 해시 기반 embedding과 인덱스 schema version을 그대로 이식할지, embedding 모델을 교체할지도 마이그레이션과 분리해 결정한다.

## 5. 권고 배포 단계

### 단계 1 — 현재 구조를 OCI에 안전하게 재현

1. 인덱스 빌드 산출물에 `index_version`, source hash, schema version, embedding identifier, vector dimension을 담은 manifest를 생성한다.
2. 산출물과 manifest를 Object Storage의 versioned artifact bucket에 업로드한다.
3. Compute 인스턴스 시작 또는 배포 시 staging 디렉터리로 내려받아 checksum과 schema를 검증한다.
4. 검증 성공 후 Block Volume의 새 release 디렉터리로 이동하고 `current` 포인터를 전환한다.
5. ClinicalNLP 컨테이너에는 `current` 인덱스를 읽기 전용으로 마운트한다.
6. 이전 정상 release를 최소 1개 유지해 즉시 롤백 가능하게 한다.

### 단계 2 — 백업과 복구를 운영 절차로 만들기

1. Block Volume에 사용자 정의 backup policy를 연결하고 보존기간을 명시한다.
2. 별도 Object Storage backup bucket에 논리 export/snapshot을 저장한다.
3. 필요하면 cross-region backup copy를 켠다. OCI Block Volume 사용자 정의 정책은 선택적 cross-region copy를 지원한다. ([Backup policy](https://docs.oracle.com/en-us/iaas/Content/Block/Tasks/schedulingvolumebackups.htm))
4. 분기별로 빈 Compute/새 볼륨에 복구하여 인덱스 health check와 고정 검색 질의를 실행한다.
5. 결과에 실제 RPO, RTO, 복구 성공 여부와 인덱스 버전을 기록한다.

### 단계 3 — 관리형 DB 승격 조건을 계측

다음 중 하나가 발생하면 OCI Database with PostgreSQL + `pgvector` PoC를 시작한다.

- 무중단 또는 분 단위 인덱스 갱신이 필요하다.
- 단일 SQLite 파일 복제 방식으로 추론 인스턴스 수를 감당하기 어렵다.
- 검색 metadata와 애플리케이션 관계형 데이터 사이에 강한 일관성이 필요하다.
- 자체 VectorDB의 장애조치·백업·보안 운영 비용이 관리형 DB 비용보다 커진다.

PoC 통과 기준은 검색 품질, p95 latency, 인덱스 재구축 시간, 백업 복구 시간, 월 비용, 운영 복잡도를 함께 측정해 정한다. 제품 교체 자체를 목표로 하지 않는다.

## 6. 결정 사항과 보류 사항

### 지금 결정

- 공용 의료용어·정책 인덱스의 OCI 1차 런타임은 SQLite 아티팩트 + Compute/Block Volume로 한다.
- Object Storage는 원본·release·논리 백업의 저장소로 사용하고 live DB 파일시스템으로 사용하지 않는다.
- 컨테이너 데이터는 root filesystem이나 교체 가능한 Compute 인스턴스에만 두지 않는다.
- 인덱스 버전과 source hash 없이는 production 승격하지 않는다.
- 환자별 임상 데이터는 공용 corpus 인덱스에서 분리한다.

### PoC 후 결정

- 중앙 온라인 VectorDB가 필요한지 여부
- 필요할 경우 OCI Database with PostgreSQL + `pgvector`와 Compute 기반 자체 VectorDB 중 어느 쪽을 선택할지
- Oracle Database 23ai AI Vector Search를 ER:ON의 주 데이터 플랫폼 후보로 볼지
- 목표 RPO/RTO, cross-region DR, retention lock 기간과 비용

## 7. 배포 전 체크리스트

- [ ] Block Volume mount가 재부팅 후에도 복원되고 컨테이너가 그 경로를 사용한다.
- [ ] 컨테이너 재생성 뒤에도 인덱스가 유지된다.
- [ ] Object Storage artifact에 manifest와 checksum이 있다.
- [ ] 새 인덱스 검증 실패 시 `current`가 바뀌지 않는다.
- [ ] 직전 정상 인덱스로 롤백하는 절차가 시험되었다.
- [ ] backup policy의 실제 보존기간과 cross-region copy 여부가 기록되었다.
- [ ] 새 볼륨/새 인스턴스로 restore drill을 통과했다.
- [ ] VectorDB 또는 ClinicalNLP endpoint는 private network에만 노출된다.
- [ ] 공용 사전·정책 corpus에 환자 원문이나 개인식별정보가 포함되지 않는다.
- [ ] 관리형 PostgreSQL 사용 시 custom configuration에서 `pgvector` 활성화를 확인했다.
