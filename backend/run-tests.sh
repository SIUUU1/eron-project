#!/usr/bin/env sh
# 백엔드 단위 테스트 실행.
#
# 🔑 왜 스크립트가 필요한가
#    backend 이미지에는 `app` 과 `scripts` 만 들어간다(Dockerfile). 테스트 코드는
#    운영 이미지에 넣지 않는다. 그렇다고 호스트에서 바로 돌릴 수도 없다 —
#    sqlalchemy·pydantic 이 컨테이너 안에만 있다.
#    그래서 같은 이미지에 tests/ 와 artifacts/ 만 얹어 일회성으로 돌린다.
#
#    ./run-tests.sh                      전체
#    ./run-tests.sh tests.test_kcd_search_terms -v   특정 모듈
#
# DB 를 건드리지 않는다. DATABASE_URL 은 import 를 통과시키기 위한 더미다.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/.." && pwd)
image=${BACKEND_IMAGE:-eron-project-backend}

if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "이미지가 없습니다: $image" >&2
    echo "먼저 빌드하세요:  docker compose build backend" >&2
    exit 2
fi

# 인자가 없으면 tests/ 전체를 돌린다.
# `unittest discover` 는 tests/ 에 __init__.py 를 요구하므로 모듈을 직접 나열한다
# (패키지 파일을 새로 만들지 않기 위함이다).
if [ "$#" -eq 0 ]; then
    set -- $(cd "$here/tests" && ls test_*.py | sed 's/\.py$//; s/^/tests./')
fi

exec docker run --rm \
    -v "$here/tests:/app/tests:ro" \
    -v "$repo/artifacts:/app/artifacts:ro" \
    -e DATABASE_URL="postgresql+psycopg://unused:unused@localhost:5432/unused" \
    -w /app "$image" \
    python -m unittest "$@"
