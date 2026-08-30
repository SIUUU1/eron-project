from __future__ import annotations

import io
import os
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api import create_api_app
from app.audio import safe_audio_suffix
from app.config import Settings


class FakeTranscriber:
    def __init__(self) -> None:
        self.is_loaded = False

    def preload(self) -> dict:
        self.is_loaded = True
        return self.status()

    def status(self) -> dict:
        return {"provider": "fake", "model": "fake", "loaded": self.is_loaded}

    def transcribe(self, audio_path: Path) -> dict:
        if not audio_path.is_file():
            raise AssertionError("spooled audio is missing")
        return {
            "text": "합성 흉통 문장",
            "segments": [
                {
                    "id": "seg_0001",
                    "start": 0.0,
                    "end": 0.1,
                    "text": "합성 흉통 문장",
                }
            ],
            "language": "ko",
            "duration_seconds": 0.1,
            "processing": {"provider": "fake", "elapsed_seconds": 0.0},
        }


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 1_600)
    return output.getvalue()


class WhisperApiContractTests(unittest.TestCase):
    def test_browser_webm_content_type_with_codec_is_supported(self):
        self.assertEqual(
            safe_audio_suffix("recording", "audio/webm;codecs=opus"),
            ".webm",
        )

    def test_transcription_does_not_require_an_internal_api_key(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            environment = {
                "STT_PROVIDER": "groq",
                "STT_OUTPUT_DIR": str(root / "outputs"),
                "STT_SPOOL_DIR": str(root / "spool"),
                "STT_DATABASE_PATH": str(root / "runtime" / "jobs.db"),
            }
            with patch.dict(os.environ, environment, clear=True):
                settings = Settings.from_env()
            app = create_api_app(settings=settings, transcriber=FakeTranscriber())

            with TestClient(app) as client:
                submitted = client.post(
                    "/v1/transcriptions",
                    files={"audio": ("synthetic.wav", wav_bytes(), "audio/wav")},
                )

            self.assertEqual(submitted.status_code, 202)

    def test_async_job_preserves_the_api1_result_contract(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = Settings(
                root_dir=root,
                output_dir=root / "outputs",
                spool_dir=root / "spool",
                database_path=root / "runtime" / "jobs.db",
                max_upload_bytes=1024 * 1024,
                max_audio_duration_seconds=60,
                queue_capacity=10,
                preload_model=True,
                worker_count=1,
            )
            app = create_api_app(settings=settings, transcriber=FakeTranscriber())
            with TestClient(app) as client:
                submitted = client.post(
                    "/v1/transcriptions",
                    files={"audio": ("synthetic.wav", wav_bytes(), "audio/wav")},
                )
                self.assertEqual(submitted.status_code, 202)
                job_id = submitted.json()["id"]
                for _ in range(100):
                    status = client.get(
                        f"/v1/transcriptions/{job_id}",
                    )
                    if status.json()["status"] == "completed":
                        break
                    time.sleep(0.01)
                else:
                    self.fail("transcription did not complete")
                result = client.get(
                    f"/v1/transcriptions/{job_id}/result",
                )
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["segments"][0]["id"], "seg_0001")
            self.assertNotIn("filename", result.json()["source"])
            self.assertEqual(list((root / "spool").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
