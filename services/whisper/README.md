# Whisper STT service

API1의 비동기 `/v1/transcriptions` 계약을 유지한 Groq 전용 내부 서비스입니다.
음성은 Groq Cloud로 전송되므로 합성 또는 적절히 비식별화되어 전송 승인을 받은 데이터만 사용합니다.

1. `.env.example`을 `.env`로 복사합니다.
2. `GROQ_API_KEY`를 `services/whisper/.env`에만 입력합니다.
3. `docker compose --profile clinical --profile stt up -d --build`로 실행합니다.

Whisper 서비스는 호스트 포트나 OCI ingress에 공개하지 않으며 Docker 내부의 `whisper:8780`으로만 호출합니다.
업로드 원본은 처리 직후 삭제됩니다. 작업 상태와 전사 결과 JSON은 `eron-whisper-state` 볼륨에 남으므로 실제 운영 전 보존 기간과 삭제 정책을 별도로 확정해야 합니다.
