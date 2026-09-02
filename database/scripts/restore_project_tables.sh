#!/usr/bin/env bash
# =====================================================================
# ER:ON — 대상 DB 에 프로젝트 테이블만 재적재 (OCI 서버·다른 로컬 머신 공용)
#
# ⚠ 이 스크립트만이 유일하게 DB 를 변경한다. 변경 범위는
#   project_tables.sh 의 화이트리스트 18개 테이블뿐이다.
#
# 하지 않는 것 (코드에 존재하지 않는다)
#   DROP DATABASE / DROP SCHEMA / DROP TABLE / TRUNCATE ... CASCADE
#   docker compose down -v / docker volume rm / PGDATA 초기화
#   화이트리스트 밖 테이블에 대한 INSERT·UPDATE·DELETE·TRUNCATE
#
# 순서
#   1. 대상 테이블 존재·row 수 조회 (읽기 전용)
#   2. 화이트리스트 밖에서 대상을 참조하는 FK 확인 → 있으면 중단
#   3. 대상 DB 현재 데이터를 18개 테이블만 백업 (롤백용)
#   4. 사용자 확인 입력 (--yes 로 생략)
#   5. 단일 트랜잭션 적용 — 실패하면 전부 롤백
#   6. 매니페스트와 row 수 대조
#
#   ./database/scripts/restore_project_tables.sh backups/eron_project_<stamp>.sql.gz
# =====================================================================
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
source "$ROOT/database/scripts/project_tables.sh"

SQL_GZ=${1:?"사용법: $0 <재적재 SQL(.sql.gz)> [--yes]"}
ASSUME_YES=${2:-}
[ -f "$SQL_GZ" ] || { echo "파일이 없다: $SQL_GZ" >&2; exit 1; }

CONTAINER=${CONTAINER:-eron-postgres}
OUT_DIR=${OUT_DIR:-$ROOT/backups}
[ -f "$ROOT/.env" ] && { set -a; . "$ROOT/.env"; set +a; }
: "${POSTGRES_USER:?POSTGRES_USER 가 필요하다 (.env)}"
: "${POSTGRES_DB:?POSTGRES_DB 가 필요하다 (.env)}"

psql_ro() { docker exec -i "$CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atq "$@"; }

# --- 0. 적용할 SQL 이 화이트리스트만 건드리는지 파일 자체를 검증한다 -------
echo "== 0. 적용 대상 SQL 검증: $SQL_GZ"
DESTRUCTIVE=$(gzip -cd "$SQL_GZ" | grep -cE '^\s*(DROP|DELETE|ALTER|CREATE|GRANT|REVOKE)\b' || true)
if [ "$DESTRUCTIVE" -ne 0 ]; then
  echo "중단: SQL 에 DROP/DELETE/ALTER/CREATE/GRANT/REVOKE 가 있다." >&2; exit 1
fi
TRUNC_LINES=$(gzip -cd "$SQL_GZ" | grep -cE '^\s*TRUNCATE\b' || true)
if [ "$TRUNC_LINES" -ne 1 ]; then
  echo "중단: TRUNCATE 구문이 정확히 1개가 아니다 (발견 ${TRUNC_LINES}개)." >&2; exit 1
fi
if gzip -cd "$SQL_GZ" | grep -qiE '^\s*TRUNCATE\b.*CASCADE'; then
  echo "중단: TRUNCATE ... CASCADE 는 허용하지 않는다." >&2; exit 1
fi
# TRUNCATE 대상이 화이트리스트와 정확히 일치하는지
TRUNC_SET=$(gzip -cd "$SQL_GZ" | grep -E '^\s*TRUNCATE\b' \
  | sed -E 's/^\s*TRUNCATE TABLE //; s/ RESTART IDENTITY;?$//' | tr ',' '\n' | tr -d ' ' | sort | tr '\n' ' ')
WANT_SET=$(printf '%s\n' "${PROJECT_TABLES[@]}" | sort | tr '\n' ' ')
if [ "$TRUNC_SET" != "$WANT_SET" ]; then
  echo "중단: TRUNCATE 대상이 화이트리스트와 다르다." >&2
  echo "  SQL : $TRUNC_SET" >&2
  echo "  화이트리스트: $WANT_SET" >&2
  exit 1
fi
# COPY 대상도 전부 화이트리스트 안이어야 한다
while read -r t; do
  printf '%s\n' "${PROJECT_TABLES[@]}" | grep -qx "$t" || {
    echo "중단: 화이트리스트 밖 테이블에 COPY 한다 → $t" >&2; exit 1; }
done < <(gzip -cd "$SQL_GZ" | grep -oE '^COPY [a-z_]+\.[a-z_]+' | awk '{print $2}' | sort -u)
echo "   OK — 파괴적 구문은 화이트리스트 ${#PROJECT_TABLES[@]}개에 대한 TRUNCATE 한 줄뿐이다."

# --- 1. 현재 상태 (읽기 전용) ---------------------------------------------
echo
echo "== 1. 대상 DB 현재 row 수 (대상 테이블만)"
docker exec -i "$CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
WITH want(t) AS (SELECT unnest(${PROJECT_TABLES_SQLARRAY}))
SELECT w.t AS target_table, (to_regclass(w.t) IS NOT NULL) AS exists,
       CASE WHEN to_regclass(w.t) IS NULL THEN NULL ELSE
         (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %s', w.t), false,true,'')))[1]::text::bigint
       END AS rows FROM want w ORDER BY 1;"

MISSING=$(psql_ro -c "
WITH want(t) AS (SELECT unnest(${PROJECT_TABLES_SQLARRAY}))
SELECT string_agg(t, ', ') FROM want WHERE to_regclass(t) IS NULL;")
if [ -n "$MISSING" ]; then
  echo "중단: 대상 DB 에 없는 테이블이 있다 → $MISSING" >&2
  echo "      database/init/01_schema.sql 을 먼저 적용해야 한다." >&2
  exit 1
fi

# --- 1b. 컬럼 호환성 (대상 스키마가 원본보다 오래됐는지 먼저 잡는다) -------
echo
echo "== 1b. 컬럼 호환성 — 덤프의 COPY 컬럼이 대상 테이블에 전부 있는가"
INCOMPAT=""
while IFS= read -r line; do
  tbl=$(printf '%s' "$line" | sed -E 's/^COPY ([a-z_]+\.[a-z_]+) \(.*/\1/')
  cols=$(printf '%s' "$line" | sed -E 's/^COPY [a-z_]+\.[a-z_]+ \((.*)\) FROM stdin;$/\1/' | tr -d ' "')
  miss=$(psql_ro -c "
    SELECT string_agg(c, ', ')
    FROM unnest(string_to_array('${cols}', ',')) AS c
    WHERE NOT EXISTS (
      SELECT 1 FROM information_schema.columns
       WHERE table_schema||'.'||table_name = '${tbl}' AND column_name = c);")
  [ -n "$miss" ] && INCOMPAT="${INCOMPAT}
  ${tbl} → 대상 DB 에 없는 컬럼: ${miss}"
done < <(gzip -cd "$SQL_GZ" | grep -E '^COPY [a-z_]+\.[a-z_]+ \(')
if [ -n "$INCOMPAT" ]; then
  echo "중단: 대상 DB 스키마가 원본과 다르다.${INCOMPAT}" >&2
  echo "      database/init/01_schema.sql 을 대상 DB 에 먼저 적용해야 한다." >&2
  exit 1
fi
echo "   OK — 모든 COPY 컬럼이 대상 스키마에 존재한다."

# --- 2. 화이트리스트 밖 참조 확인 -----------------------------------------
echo
echo "== 2. 화이트리스트 밖에서 대상을 참조하는 FK"
OUTSIDE=$(psql_ro -c "
SELECT string_agg(DISTINCT con.conrelid::regclass::text, ', ')
FROM pg_constraint con
WHERE con.contype='f'
  AND con.confrelid::regclass::text = ANY(${PROJECT_TABLES_SQLARRAY})
  AND con.conrelid::regclass::text <> ALL(${PROJECT_TABLES_SQLARRAY});")
if [ -n "$OUTSIDE" ]; then
  echo "중단: 다른 테이블이 대상을 참조한다 → $OUTSIDE" >&2
  echo "      TRUNCATE 가 그 데이터에 영향을 줄 수 있다. 사람이 판단해야 한다." >&2
  exit 1
fi
echo "   없음 — TRUNCATE 가 다른 테이블로 번지지 않는다."

# --- 3. 쓰기 중인 backend 확인 ---------------------------------------------
if docker ps --format '{{.Names}}' | grep -qx eron-backend; then
  echo
  echo "⚠ eron-backend 가 떠 있다. 재예측 스케줄러가 app.prediction 에 계속 INSERT 한다."
  echo "  적재 중 잠금 경합과 '적재 직후 새 행 추가'를 피하려면 먼저 멈추는 것을 권한다:"
  echo "      docker compose stop backend      # 볼륨·데이터는 그대로다"
  echo "  적재 후:  docker compose start backend"
fi

# --- 4. 대상 DB 현재 데이터 백업 (롤백용, 18개 테이블만) ------------------
echo
echo "== 3. 롤백용 백업 (대상 테이블만 · 다른 테이블은 담지 않는다)"
mkdir -p "$OUT_DIR"
STAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP="$OUT_DIR/oci_before_restore_${STAMP}.dump"
DUMP_ARGS=(); for t in "${PROJECT_TABLES[@]}"; do DUMP_ARGS+=(-t "$t"); done
docker exec -i "$CONTAINER" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -Fc --data-only --no-owner --no-privileges "${DUMP_ARGS[@]}" > "$BACKUP"
echo "   $BACKUP ($(du -h "$BACKUP" | cut -f1))"
echo "   되돌리려면: pg_restore --data-only --disable-triggers 로 이 파일을 적용한다."

# --- 5. 확인 --------------------------------------------------------------
if [ "$ASSUME_YES" != "--yes" ]; then
  echo
  echo "다음을 실행한다: DB=${POSTGRES_DB} · 대상 ${#PROJECT_TABLES[@]}개 테이블 TRUNCATE 후 재적재"
  read -r -p "계속하려면 정확히 'RELOAD' 를 입력: " ANSWER
  [ "$ANSWER" = "RELOAD" ] || { echo "취소했다. DB 는 변경되지 않았다."; exit 1; }
fi

# --- 6. 적용 (단일 트랜잭션) ----------------------------------------------
echo
echo "== 4. 적용 중 (단일 트랜잭션 · 실패 시 전체 롤백)"
gzip -cd "$SQL_GZ" | docker exec -i "$CONTAINER" \
  psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -q
echo "   완료."

# --- 7. 검증 --------------------------------------------------------------
echo
echo "== 5. 매니페스트 대조"
MANIFEST="${SQL_GZ%.sql.gz}.manifest.txt"
if [ -f "$MANIFEST" ]; then
  FAIL=0
  while IFS=$'\t' read -r t expect; do
    case "$t" in \#*|"") continue;; esac
    actual=$(psql_ro -c "SELECT count(*) FROM $t;")
    if [ "$actual" = "$expect" ]; then printf '   OK   %-22s %s\n' "$t" "$actual"
    else printf '   FAIL %-22s expected=%s actual=%s\n' "$t" "$expect" "$actual"; FAIL=1; fi
  done < "$MANIFEST"
  [ "$FAIL" -eq 0 ] || { echo "row 수가 매니페스트와 다르다. 확인이 필요하다." >&2; exit 1; }
else
  echo "   매니페스트 파일이 없다: $MANIFEST (건너뜀)"
fi

echo
echo "== 6. 화이트리스트 밖 스키마 테이블 수 (변경되지 않았는지 눈으로 확인)"
docker exec -i "$CONTAINER" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT n.nspname AS schema, count(*) AS tables
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE c.relkind='r' AND n.nspname NOT IN ('pg_catalog','information_schema')
GROUP BY 1 ORDER BY 1;"
