"""적재 스크립트가 공유하는 DB 헬퍼.

로컬에 psycopg / psql 을 요구하지 않는다. docker compose 의 postgres 컨테이너
안에서 psql 을 실행해 처리한다. 행 단위 INSERT 는 쓰지 않고 COPY 로 밀어넣는다.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


def env_from_dotenv(key: str) -> str:
    import os

    val = os.environ.get(key)
    if val:
        return val
    dotenv = REPO / ".env"
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    log(f"[FATAL] {key} 를 찾을 수 없습니다 (.env 또는 환경변수)")
    raise SystemExit(2)


PGUSER = env_from_dotenv("POSTGRES_USER")
PGDB = env_from_dotenv("POSTGRES_DB")

PSQL_BASE = [
    "docker", "compose", "exec", "-T", "postgres",
    "psql", "-U", PGUSER, "-d", PGDB, "-v", "ON_ERROR_STOP=1", "--no-psqlrc",
]


def psql(sql: str, quiet: bool = True) -> str:
    args = PSQL_BASE + (["-q"] if quiet else []) + ["-c", sql]
    p = subprocess.run(args, cwd=REPO, capture_output=True, text=True)
    if p.returncode != 0:
        log(f"[FATAL] psql 실패:\n{p.stderr.strip()}")
        raise SystemExit(1)
    return p.stdout


def psql_file(path: Path) -> None:
    p = subprocess.run(
        PSQL_BASE + ["-q", "-f", "-"],
        cwd=REPO, input=path.read_text(), capture_output=True, text=True,
    )
    if p.returncode != 0:
        log(f"[FATAL] {path.name} 적용 실패:\n{p.stderr.strip()}")
        raise SystemExit(1)


def scalar(sql: str) -> str:
    p = subprocess.run(
        PSQL_BASE + ["-t", "-A", "-c", f"SELECT {sql}"],
        cwd=REPO, capture_output=True, text=True,
    )
    if p.returncode != 0:
        log(f"[FATAL] psql 실패:\n{p.stderr.strip()}")
        raise SystemExit(1)
    return p.stdout.strip()


def rows(sql: str) -> list[list[str]]:
    """SELECT 결과를 CSV 로 받아 파싱한다 (헤더 제외)."""
    p = subprocess.run(
        PSQL_BASE + ["-c", f"COPY ({sql}) TO STDOUT WITH (FORMAT csv)"],
        cwd=REPO, capture_output=True, text=True,
    )
    if p.returncode != 0:
        log(f"[FATAL] psql 실패:\n{p.stderr.strip()}")
        raise SystemExit(1)
    return [r for r in csv.reader(p.stdout.splitlines())]


def copy_rows(table: str, columns: list[str], row_iter) -> int:
    """COPY ... FROM STDIN 으로 밀어넣는다."""
    cols = ", ".join(columns)
    cmd = PSQL_BASE + [
        "-q", "-c", f"\\copy {table} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '')"
    ]
    proc = subprocess.Popen(cmd, cwd=REPO, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert proc.stdin is not None
    writer = csv.writer(proc.stdin)
    n = 0
    try:
        for row in row_iter:
            writer.writerow(row)
            n += 1
    except BrokenPipeError:
        pass
    finally:
        try:
            proc.stdin.close()
        except (BrokenPipeError, ValueError):
            pass
        # communicate() 가 닫힌 stdin 을 다시 flush 하지 않도록 떼어낸다
        proc.stdin = None
    _, err = proc.communicate()
    if proc.returncode != 0:
        log(f"[FATAL] COPY {table} 실패:\n{err.strip()}")
        raise SystemExit(1)
    return n
