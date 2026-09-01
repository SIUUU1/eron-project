# ER:ON — 백엔드/프론트 프록시 규칙 (server 블록 안에서 include 한다)
#
# HTTP 전용(로컬)과 HTTPS(OCI) 두 server 블록이 같은 라우팅을 써야 하므로
# 공통 부분만 이 파일에 둔다. 두 곳에 복사해 두면 한쪽만 고쳐져 어긋난다.
#
# ⚠ 확장자가 .conf 가 아닌 이유
#   nginx 기본 설정이 /etc/nginx/conf.d/*.conf 를 http 컨텍스트에서 자동 include 한다.
#   이 파일은 location 만 담고 있어 http 컨텍스트에 단독으로 놓이면 문법 오류가 난다.
#   .inc 로 두면 자동 include 대상에서 빠지고, server 블록에서만 명시적으로 읽힌다.

client_max_body_size 8m;

gzip on;
gzip_vary on;
gzip_min_length 1024;
gzip_types text/plain text/css application/javascript application/json
           application/x-javascript text/xml image/svg+xml;

# ClinicalNLP backend deadline(620s)이 먼저 구조화된 504를 반환하도록
# 이 endpoint에만 10초의 proxy 여유를 둔다.
location = /api/clinical-records/draft {
    proxy_pass         http://eron_backend;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_connect_timeout 5s;
    proxy_send_timeout 630s;
    proxy_read_timeout 630s;
}

# 음성 파일 선택 직후 API1 전사가 끝날 때까지 기다린다.
# backend STT deadline(300s)이 먼저 구조화된 504를 반환하도록 여유를 둔다.
location = /api/clinical-records/transcribe {
    client_max_body_size 26m;
    proxy_pass         http://eron_backend;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_connect_timeout 5s;
    proxy_send_timeout 310s;
    proxy_read_timeout 310s;
}

# Groq STT 대기 후 ClinicalNLP 초안 생성까지 한 요청에서 수행한다.
# 업로드 제한은 Groq 직접 업로드 상한과 맞추며 다른 API에는 적용하지 않는다.
location = /api/clinical-records/draft/audio {
    # multipart 경계 오버헤드를 허용하되 backend가 실제 파일을 25MB로 재검증한다.
    client_max_body_size 26m;
    proxy_pass         http://eron_backend;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_connect_timeout 5s;
    proxy_send_timeout 930s;
    proxy_read_timeout 930s;
}

# --- 백엔드 API ---------------------------------------------------
# location /api/ 는 location / 보다 긴 접두사라 nginx 매칭 규칙상
# 먼저 선택된다. 블록 순서에 의존하지 않는다.
location /api/ {
    proxy_pass         http://eron_backend;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_read_timeout 30s;
}

# --- Swagger / OpenAPI (D7: FastAPI 기본 경로 유지) ----------------
location = /docs         { proxy_pass http://eron_backend; }
location = /redoc        { proxy_pass http://eron_backend; }
location = /openapi.json { proxy_pass http://eron_backend; }
location = /health       { proxy_pass http://eron_backend; }
location = /health/db    { proxy_pass http://eron_backend; }

# --- 프론트엔드 SSR ------------------------------------------------
location / {
    proxy_pass         http://eron_frontend;
    proxy_http_version 1.1;
    proxy_set_header   Host              $host;
    proxy_set_header   X-Real-IP         $remote_addr;
    proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header   X-Forwarded-Proto $scheme;
    proxy_set_header   Upgrade           $http_upgrade;
    proxy_set_header   Connection        "upgrade";
    proxy_read_timeout 60s;
}
