# OCI 배포 · 도메인 · HTTPS

`eron.co.kr` 운영 환경의 구성과 절차. 로컬 개발은 `README.md` 를 따른다.

## 구성

```
Internet → eron.co.kr / www.eron.co.kr (A record → OCI Public IP)
             ↓  80 / 443  (외부에 여는 포트는 이 둘뿐이다)
          ┌──────────┐
          │  nginx   │  /api/ · /docs · /health → backend:8000
          └────┬─────┘  그 외                    → frontend:3000
               │
   postgres:5432 · riskmodel:8790 · clinicalnlp:8765 · whisper:8780
   (전부 eron-network 내부 전용. 호스트·외부에 공개하지 않는다)
```

## nginx 설정 구조

한 벌의 라우팅 규칙을 HTTP/HTTPS 두 server 블록이 공유한다.

| 파일 | 역할 |
|---|---|
| `nginx/conf.d/eron.conf` | upstream 정의 + `:80` default_server + `tls.d/*.conf` include |
| `nginx/conf.d/eron-proxy.inc` | 프록시 location 공통 규칙. 두 server 블록이 include 한다 |
| `nginx/tls-available/eron-tls.conf` | `:443` 본 서비스 + `:80` → HTTPS 리다이렉트 |
| `nginx/tls-enabled/` | 비어 있는 기본값. 로컬에서는 HTTPS 가 꺼진다 |

`.env` 의 `NGINX_TLS_DIR` 이 스위치다.

```sh
NGINX_TLS_DIR=./nginx/tls-available   # OCI: HTTPS 켜짐
NGINX_TLS_DIR=                        # 로컬: :80 만 뜬다(기본값 tls-enabled)
```

`eron-proxy.inc` 의 확장자가 `.conf` 가 아닌 이유는 nginx 가
`/etc/nginx/conf.d/*.conf` 를 http 컨텍스트에서 자동 include 하기 때문이다.
location 만 담긴 파일이 http 컨텍스트에 놓이면 문법 오류가 난다.

`:80` 이 두 개인 것도 의도된 구조다. nginx 는 `server_name` 일치를
`default_server` 보다 먼저 고르므로, 도메인으로 온 요청만 HTTPS 로 리다이렉트되고
IP 직접 접속 등 나머지는 `eron.conf` 의 default_server 가 받는다.

## 최초 배포

```sh
git clone -b dev https://github.com/SIUUU1/eron-project.git
cd eron-project
cp .env.example .env      # 값은 서버에서 직접 채운다. 절대 커밋하지 않는다
```

### 1. 런타임 디렉터리

`/runtime/` 은 `.gitignore` 대상이라 clone 직후에는 없다. compose 의 bind mount 는
`create_host_path: false` 라 디렉터리가 없으면 backend 가 기동하지 않는다.

```sh
mkdir -p runtime/clinicalnlp/scispacy runtime/clinicalnlp/medical-dictionaries
```

`medical-dictionaries/` 가 비어 있어도 backend 는 뜬다. KCD 약어 확장만
빈 결과를 반환한다(`backend/app/api/kcd.py` 가 파일 부재를 정상 처리한다).

### 2. git 에 없는 자산

용량·데이터 정책상 저장소에 없으므로 별도로 전달한다.

| 자산 | 경로 | 비고 |
|---|---|---|
| 모델 가중치 | `artifacts/*.pkl` | 34MB. `.gitignore` 의 `artifacts/*.pkl` |
| MIMIC 원본 | `MIMIC-DEMO/` | 환자 유래 데이터. **절대 커밋 금지** |
| 의료사전 | `runtime/clinicalnlp/medical-dictionaries/` | KCD 약어 확장용 |

전송 후 무결성을 확인한다.

```sh
cd artifacts && sha256sum -c CHECKSUMS.txt
```

### 3. DB

볼륨이 비어 있을 때만 `database/init/*.sql` 이 자동 실행된다. **이미 데이터가 있는
DB 에 스키마 변경을 반영할 때는 직접 적용한다.** `01~03` 은 전부
`IF NOT EXISTS` 가드가 걸린 추가 전용이라 재실행해도 안전하다.

```sh
docker exec -i eron-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v ON_ERROR_STOP=1 < database/init/01_schema.sql
```

lab/ICU 활력징후는 별도로 적재한다. `--events-only` 는 시연 상태(`app.demo_stay`)를
건드리지 않는다.

```sh
MIMIC_DATA_DIR=$PWD/MIMIC-DEMO python3 database/scripts/load_subset.py --events-only
```

> 모델 100개 feature 중 36개가 `lab_*` 다. `mimic.labevents` 가 비어 있으면
> 예측은 나오지만 근거가 크게 빈 상태가 된다. 반드시 적재하고 배포한다.

### 4. 기동

```sh
docker compose config                                   # 검증 먼저
docker compose --profile risk --profile clinical --profile stt up -d
```

profile 없이 띄우면 예측·초안·STT 가 각각 503 을 반환한다(backend 자체는 기동한다).

## HTTPS

인증서는 호스트에 certbot 을 설치하지 않고 `certbot/certbot` 컨테이너로 발급한다.
인증 방식은 **webroot** 다 — standalone 은 갱신할 때마다 nginx 를 내려야 해서 쓰지 않는다.

```sh
sudo mkdir -p /var/www/certbot

sudo docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/lib/letsencrypt:/var/lib/letsencrypt \
  -v /var/www/certbot:/var/www/certbot \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  --cert-name eron.co.kr \
  -d eron.co.kr -d www.eron.co.kr \
  --email <운영자 메일> --agree-tos --no-eff-email --non-interactive
```

`-d` 를 두 도메인 모두 지정해야 한다. 하나라도 빠지면 그 도메인은 브라우저에서
인증서 이름 불일치 경고가 난다.

`.env` 에 실제 경로를 지정한다.

```sh
NGINX_TLS_DIR=./nginx/tls-available
LETSENCRYPT_DIR=/etc/letsencrypt
CERTBOT_WEBROOT=/var/www/certbot
```

### 자동 갱신

`deploy/certbot/` 의 스크립트와 systemd 유닛을 설치한다. 하루 두 번 확인하고
만료 30일 이내일 때만 실제로 갱신한다.

```sh
sudo install -m 755 deploy/certbot/eron-certbot-renew.sh /usr/local/bin/
sudo install -m 644 deploy/certbot/eron-certbot-renew.{service,timer} \
  /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now eron-certbot-renew.timer
```

검증:

```sh
sudo docker run --rm -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/lib/letsencrypt:/var/lib/letsencrypt \
  -v /var/www/certbot:/var/www/certbot \
  certbot/certbot renew --dry-run
systemctl list-timers eron-certbot-renew.timer
```

## 검증

```sh
curl -I  http://eron.co.kr                 # 301 → https
curl -I  https://www.eron.co.kr            # 200 (인증서 검증 통과해야 한다)
curl -s  https://eron.co.kr/health/db
curl -s  https://eron.co.kr/api/ed/dashboard/summary   # model_connected: true

echo | openssl s_client -connect eron.co.kr:443 -servername eron.co.kr 2>/dev/null \
  | openssl x509 -noout -dates -ext subjectAltName     # SAN 에 두 도메인
```

`model_connected` 가 false 면 `app.prediction` 이 비어 있다는 뜻이다.
`PREDICT_AI_URL` 이 `http://riskmodel:8790` 인지, risk profile 이 떴는지 확인한다.

## 주의

- `docker compose down -v` 는 DB 볼륨(`eron-postgres-data`)을 지운다. 쓰지 않는다.
- 스키마 변경 전에는 `docker exec eron-postgres pg_dump -Fc` 로 먼저 받아둔다.
- `.env`, `services/*/.env`, `artifacts/*.pkl`, `MIMIC-DEMO/` 는 커밋하지 않는다.
- backend/frontend 만 재생성해도 nginx 는 재시작하지 않아도 된다.
  upstream 에 `resolve` 가 걸려 있어 Docker DNS 로 IP 를 다시 해석한다.
