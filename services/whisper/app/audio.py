from __future__ import annotations

from pathlib import Path


ALLOWED_SUFFIXES = {".wav", ".mp3", ".m4a", ".mp4", ".webm", ".ogg", ".flac"}
MIME_SUFFIXES = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "video/mp4": ".mp4",
    "audio/flac": ".flac",
}


def safe_audio_suffix(filename: str | None, content_type: str | None) -> str | None:
    suffix = Path(filename or "").suffix.lower()
    if suffix in ALLOWED_SUFFIXES:
        return suffix
    media_type = (content_type or "").partition(";")[0].strip().lower()
    return MIME_SUFFIXES.get(media_type)


def probe_audio_duration(path: Path) -> float:
    import av

    try:
        with av.open(str(path)) as container:
            audio_streams = [
                stream for stream in container.streams if stream.type == "audio"
            ]
            if not audio_streams:
                raise ValueError("no_audio_stream")
            if container.duration is not None:
                return float(container.duration / av.time_base)
            stream = audio_streams[0]
            if stream.duration is not None and stream.time_base is not None:
                return float(stream.duration * stream.time_base)
            end_time = 0.0
            for frame in container.decode(stream):
                if frame.time is None or frame.sample_rate is None:
                    continue
                end_time = max(
                    end_time,
                    float(frame.time + frame.samples / frame.sample_rate),
                )
            if end_time <= 0:
                raise ValueError("unknown_audio_duration")
            return end_time
    except Exception as exc:
        raise ValueError("invalid_audio") from exc
