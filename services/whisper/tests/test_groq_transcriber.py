import tempfile
import unittest
from pathlib import Path

from app.groq_transcriber import GroqWhisperConfig, GroqWhisperTranscriber


class _Transcriptions:
    def __init__(self, payload):
        self.payload = payload

    def create(self, **_kwargs):
        return self.payload


class _Client:
    def __init__(self, payload):
        self.audio = type("Audio", (), {"transcriptions": _Transcriptions(payload)})()


def _transcriber(payload):
    return GroqWhisperTranscriber(
        config=GroqWhisperConfig(api_key="test"),
        client=_Client(payload),
    )


class GroqTranscriberFilteringTests(unittest.TestCase):
    def _transcribe(self, payload):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "recording.m4a"
            audio.write_bytes(b"audio")
            return _transcriber(payload).transcribe(audio)

    def test_filters_known_subtitle_hallucination(self):
        result = self._transcribe(
            {
                "text": "시청해 주셔서 감사합니다.",
                "duration": 2.0,
                "segments": [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": "시청해 주셔서 감사합니다.",
                        "no_speech_prob": 0.1,
                    }
                ],
            }
        )

        self.assertEqual(result["text"], "")
        self.assertEqual(result["segments"], [])

    def test_preserves_uncertain_clinical_utterance(self):
        result = self._transcribe(
            {
                "text": "으으, 배가 아파요.",
                "duration": 2.0,
                "segments": [
                    {
                        "start": 0.0,
                        "end": 2.0,
                        "text": "으으, 배가 아파요.",
                        "no_speech_prob": 0.4,
                    }
                ],
            }
        )

        self.assertEqual(result["text"], "으으, 배가 아파요.")
        self.assertEqual(result["segments"][0]["text"], "으으, 배가 아파요.")


if __name__ == "__main__":
    unittest.main()
