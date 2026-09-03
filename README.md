# eron-project
위험 신호는 놓치지 않고, 기록의 빈틈은 남기지 않게.  ER:ON, 더 이로운 응급실을 만들다.

## 문서

| 문서 | 내용 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 시스템 구성, 레이어 책임, 스키마 3분할 |
| [docs/api-design.md](docs/api-design.md) | API 계약. **최신 정본은 `/openapi.json`** |
| [docs/database-design.md](docs/database-design.md) | 테이블 DDL, MIMIC 매핑, 적재 전략, 데모 시간축 |
| [docs/oci-deployment.md](docs/oci-deployment.md) | OCI 배포, 도메인, HTTPS, 인증서 갱신, DB 데이터 이관 |
| [docs/clinical-record-persistence.md](docs/clinical-record-persistence.md) | 응급진료기록 임시저장·인증저장 규칙 |
| [docs/clinicalnlp-integration.md](docs/clinicalnlp-integration.md) | ClinicalNLP 연동 계약 |
| [docs/adr/](docs/adr/) | 아키텍처 결정 기록 |

`architecture.md` · `api-design.md` · `database-design.md` 세 편은 2026-08-26 rev.3
설계 기록이 바탕이라, 그 이후 달라진 부분은 각 문서 상단과 개정 표시에 따로 적어 두었습니다.

개발 규칙은 [CLAUDE.md](CLAUDE.md) 와 [AGENTS.md](AGENTS.md) 를 참고하세요.

## 음성 기반 응급기록 초안

선택적 `stt` 프로필은 API1의 비동기 계약을 유지하는 Groq Whisper 내부 서비스를 실행합니다.

1. `services/whisper/.env.example`을 `services/whisper/.env`로 복사하고 Groq 키를 입력합니다.
2. 다음 명령으로 Whisper와 ClinicalNLP를 함께 실행합니다.

```sh
docker compose --profile clinical --profile stt up -d --build
```

브라우저에서 음성 파일을 선택하면 multipart `audio` 필드를 받는
`POST /api/clinical-records/transcribe`가 즉시 API1을 실행합니다. 반환된 Whisper
segment는 대화 기록에 표시되고, 사용자가 초안 생성을 누르면 같은 Whisper JSON을
`POST /api/clinical-records/draft`로 전달합니다. 기존 통합 경로인
`POST /api/clinical-records/draft/audio`도 호환성을 위해 유지합니다. Whisper
컨테이너는 호스트 포트를 공개하지 않으므로 OCI ingress 규칙이 필요하지 않습니다.

기록 화면의 녹음 시작·일시정지·재개·종료는 브라우저 `MediaRecorder`를 사용합니다.
녹음을 종료하면 미리듣기와 녹음 정보를 확인할 수 있으며, 사용자가 `이 녹음 사용`을
선택한 경우에만 생성된 오디오를 `/api/clinical-records/transcribe`로 전달합니다.
브라우저 마이크 권한은 `localhost` 또는 HTTPS 환경에서만 사용할 수 있으므로 OCI
배포에서는 HTTPS가 필요합니다.

음성은 Groq Cloud로 전송됩니다. 합성 또는 적절히 비식별화되었고 외부 전송이
승인된 데이터만 사용해야 합니다.
