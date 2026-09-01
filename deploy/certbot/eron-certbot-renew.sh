#!/bin/sh
# ER:ON Let's Encrypt 인증서 자동 갱신
#
# 발급/갱신은 호스트에 certbot 을 설치하지 않고 certbot/certbot 컨테이너로 수행한다.
# 인증 방식은 webroot 이며 /etc/letsencrypt/renewal/eron.co.kr.conf 에 저장된
# 파라미터를 그대로 사용한다. nginx 는 /var/www/certbot 을 읽기전용으로 마운트한다.
#
# certbot 은 만료 30일 이내에만 실제로 갱신하므로 매일 실행해도 안전하다.
set -eu

docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/lib/letsencrypt:/var/lib/letsencrypt \
  -v /var/www/certbot:/var/www/certbot \
  certbot/certbot renew --non-interactive

# 갱신되지 않은 경우에도 graceful reload 라 무해하다.
# 컨테이너가 없으면 실패하지만 기존 인증서로 서비스는 계속 유지된다.
docker exec eron-nginx nginx -s reload
