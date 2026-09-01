# 응급진료기록 임시저장 및 인증저장

AI가 생성한 응급진료기록은 의료진 검토용 `DRAFT`로 취급한다. 의료진은 같은 응급실
방문(`ed_stay_id`)의 초안을 여러 번 임시저장할 수 있으며, `SIGNED`로 인증저장한 뒤에는
내용과 복수의 KCD 코드를 변경하거나 다시 인증할 수 없다.

현재 ER:ON에는 MIMIC 응급실 방문과 별도로 관리되는 encounter 모델이 없으므로,
`public.clinical_records.ed_stay_id`를 임시 연결 키로 사용한다. 자체 encounter 모델이
확정되면 이 연결 키를 encounter FK로 마이그레이션한다.

## API

- `GET /api/clinical-records/by-stay/{ed_stay_id}`: 저장된 DRAFT/SIGNED 복원. 없으면 `null`.
- `PUT /api/clinical-records/by-stay/{ed_stay_id}`: DRAFT 생성 또는 반복 임시저장.
- `POST /api/clinical-records/{record_id}/sign`: 최신 저장 DRAFT를 SIGNED로 전환.

임시저장과 인증저장은 모두 부모 MIMIC ED stay가 존재해야 한다. SIGNED 기록에 대한
PUT 또는 재인증 요청은 `409`를 반환한다. `signed_at`과 `signed_by`는 서버가 설정한다.

별도 sign-validation endpoint와 경고 단계는 현재 제품 범위에서 제외했다. 사용자는 기존
누락 검사와 수정, KCD 추천 또는 직접 입력을 완료한 뒤 최종 확인 다이얼로그에서 인증한다.

## KCD-9차 상병마스터 검색

`GET /api/kcd/search?q={코드 또는 진단명}&limit=10`은 `public.kcd_codes`에서 완전코드,
한글명, 영문명을 검색한다. 화면은 누락 검사 후 추정진단을 검색어로 사용하며 의료진이
코드 또는 진단명을 직접 검색할 수도 있다. 검색 결과는 의사결정 지원용 후보이며 최종
코드는 의료진이 하나 이상 선택하고 인증한다. `selected_kcd`는 새 기록에서 배열로 저장하며,
기존 단일 객체 기록도 조회 시 호환한다.
각 선택 항목의 `is_rule_out` 값으로 진단별 의증(R/O) 여부를 저장한다. 코드 선택 전
임시저장에서도 상태를 유지하도록 `record_payload.diagnosis_rule_outs`에도 진단 순서대로 저장한다.

## 알레르기 필드 키 호환

12개 상위 임상 필드 중 6번 필드의 canonical 키와 화면 명칭은 `allergy`/`알레르기`를
사용한다. 이전 `drug_allergy` JSON은 조회 시 `allergy`로 변환하며, 이후 임시저장 또는
`backend/scripts/migrate_clinical_record_allergy_key.py` 실행 시 새 키로 DB에 저장한다.

검색어가 ClinicalNLP 응급의학용어 사전의 공식 약어 또는 동의어와 정확히 일치하면
`aliases`와 `terms`를 통해 표준 한글·영문 진단명으로 확장한 뒤 KCD를 검색한다. Docker
Compose는 `runtime/clinicalnlp/medical-dictionaries`를 백엔드에 읽기 전용으로 마운트한다.
컨테이너 밖에서 백엔드를 실행할 때는 `KCD_ALIAS_DB_PATH`에
`ERON_응급의학용어_DB_v1.sqlite`의 절대 경로를 설정한다. 사전이 없거나 읽을 수 없으면
기존 코드·진단명 검색만 수행한다. 임상 검토가 끝나지 않은 별칭은 검색 확장에 사용하지
않는다.

배포용 상병마스터 엑셀의 `상병분류기호(완전코드)` 시트를 적재한다.

```sh
cd backend
python scripts/import_kcd9.py /absolute/path/to/kcd9-master.xlsx
```

원본 엑셀과 PDF는 Git이나 컨테이너 이미지에 포함하지 않는다. 배포 환경마다 승인된
KCD-9차 원본의 버전과 해시를 확인한 뒤 적재한다.
