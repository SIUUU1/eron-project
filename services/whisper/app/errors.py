from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderTranscriptionError(Exception):
    error_code: str

    def __str__(self) -> str:
        return self.error_code


@dataclass
class APIError(Exception):
    code: str
    message: str
    status_code: int
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.code


ERROR_MESSAGES = {
    "UNSUPPORTED_MEDIA_TYPE": "지원하지 않는 음성 형식입니다.",
    "EMPTY_AUDIO": "음성 파일이 비어 있습니다.",
    "FILE_TOO_LARGE": "음성 파일이 허용된 크기를 초과했습니다.",
    "INVALID_AUDIO": "음성 파일을 읽을 수 없습니다.",
    "AUDIO_TOO_LONG": "음성 길이가 허용된 시간을 초과했습니다.",
    "QUEUE_FULL": "전사 대기열이 가득 찼습니다.",
    "JOB_NOT_FOUND": "전사 작업을 찾을 수 없습니다.",
    "RESULT_NOT_READY": "전사 결과가 아직 준비되지 않았습니다.",
    "TRANSCRIPTION_FAILED": "음성 전사에 실패했습니다.",
    "VALIDATION_ERROR": "요청 형식이 올바르지 않습니다.",
    "INTERNAL_ERROR": "서버 내부 오류가 발생했습니다.",
}


def api_error(
    code: str,
    status_code: int,
    *,
    details: dict[str, Any] | None = None,
) -> APIError:
    return APIError(
        code=code,
        message=ERROR_MESSAGES[code],
        status_code=status_code,
        details=details,
    )
