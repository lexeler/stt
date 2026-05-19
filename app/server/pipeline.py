"""Reusable transcription pipeline shared between CLI and web server."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

MAX_CHUNK_SEC = 22.0
TARGET_CHUNK_SEC = 18.0
SILENCE_THRESHOLD_DB = -30
MIN_SILENCE_SEC = 0.25
DISPLAY_GAP_SEC = 0.8

# Hard cap on any ffmpeg/ffprobe invocation. Without this a crafted input
# can wedge a subprocess forever, locking the worker thread → full DoS.
# 10 min covers normalize/silence-detect on a 5h CPU job with headroom.
FFMPEG_TIMEOUT_SEC = 600


def _run_ff(cmd: list[str], *, check_returncode: bool = False, what: str = "ffmpeg") -> subprocess.CompletedProcess:
    """subprocess.run with a hard timeout so a wedged ffmpeg can't hang us."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{what} timed out after {FFMPEG_TIMEOUT_SEC}s — file may be corrupt")
    if check_returncode and proc.returncode != 0:
        tail = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "unknown error"
        raise RuntimeError(f"{what} failed: {tail}")
    return proc


@dataclass
class TranscriptLine:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    duration: float
    full_text: str
    lines: list[TranscriptLine] = field(default_factory=list)

    def render_clean(self) -> str:
        return self.full_text + "\n"

    def render_timestamped(self) -> str:
        out: list[str] = []
        for ln in self.lines:
            out.append(f"[{ln.start:7.2f} - {ln.end:7.2f}]  {ln.text}")
        return "\n".join(out) + "\n"


class CancelledByUser(Exception):
    """Raised when transcription is cancelled by user request."""
    pass


def normalize_to_wav(src: str, dst: str) -> None:
    _run_ff(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", src,
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
         dst],
        check_returncode=True,
        what=f"ffmpeg normalize {Path(src).name!r}",
    )
    if not Path(dst).exists() or Path(dst).stat().st_size < 1024:
        raise RuntimeError(f"Conversion produced empty/invalid output for {Path(src).name!r}")


def get_duration(path: str) -> float:
    """Robust duration probe.

    Browser MediaRecorder produces WebM/Opus blobs whose container does not
    carry a Duration element (chunks are streamed without finalising the
    Segment Info). ffprobe then returns the literal string 'N/A'.
    Fallback chain: format duration → stream duration → demux-only ffmpeg
    pass that prints `time=HH:MM:SS.MS` for the actual presentation length.
    """
    # 1. format duration
    out = _run_ff(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        what="ffprobe format",
    )
    s = out.stdout.strip()
    try:
        d = float(s)
        if d > 0:
            return d
    except ValueError:
        pass

    # 2. stream duration of first audio stream
    out = _run_ff(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=duration", "-of", "csv=p=0", path],
        what="ffprobe stream",
    )
    s = out.stdout.strip()
    try:
        d = float(s)
        if d > 0:
            return d
    except ValueError:
        pass

    # 3. demux-only ffmpeg pass — works on streamed WebM/Opus from browsers
    out = _run_ff(
        ["ffmpeg", "-nostdin", "-i", path, "-c", "copy", "-f", "null", "-"],
        what="ffmpeg demux-probe",
    )
    matches = re.findall(r"time=(\d+):(\d+):([\d.]+)", out.stderr)
    if matches:
        h, m, sec = matches[-1]
        return int(h) * 3600 + int(m) * 60 + float(sec)

    raise RuntimeError(f"Не удалось определить длительность файла {Path(path).name}")


def detect_silences(path: str) -> list[tuple[float, float]]:
    out = _run_ff(
        ["ffmpeg", "-i", path,
         "-af", f"silencedetect=noise={SILENCE_THRESHOLD_DB}dB:d={MIN_SILENCE_SEC}",
         "-f", "null", "-"],
        what="ffmpeg silencedetect",
    )
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", out.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", out.stderr)]
    return list(zip(starts, ends))


def build_chunks(duration: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        deadline = start + MAX_CHUNK_SEC
        if deadline >= duration:
            chunks.append((start, duration))
            break
        midpoints = [
            (s + e) / 2 for s, e in silences
            if start + TARGET_CHUNK_SEC * 0.5 <= (s + e) / 2 <= deadline
        ]
        if midpoints:
            cut = min(midpoints, key=lambda m: abs(m - (start + TARGET_CHUNK_SEC)))
            chunks.append((start, cut))
            start = cut
        else:
            chunks.append((start, deadline))
            start = deadline
    return chunks


def extract_chunk(src: str, start: float, end: float, dst: str) -> None:
    _run_ff(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-i", src, "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
         "-ar", "16000", "-ac", "1", dst],
        check_returncode=True,
        what="ffmpeg chunk-extract",
    )


def transcribe_audio(
    model,
    input_path: str,
    work_dir: Path,
    on_progress: Optional[Callable[[float, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> TranscriptResult:
    """Run full pipeline: normalize → silence-detect → chunk → ASR → assemble.

    on_progress(fraction, stage) is called periodically. fraction in [0, 1].
    should_cancel() is checked before each chunk; if it returns True,
    raises CancelledByUser.
    """
    def _check_cancel() -> None:
        if should_cancel and should_cancel():
            raise CancelledByUser()

    work_dir.mkdir(parents=True, exist_ok=True)
    normalized = work_dir / "input.wav"

    _check_cancel()
    if on_progress:
        on_progress(0.02, "Декодирование аудио")
    normalize_to_wav(input_path, str(normalized))

    duration = get_duration(str(normalized))
    if duration < 0.1:
        raise RuntimeError("Аудио слишком короткое или пустое")

    if duration <= MAX_CHUNK_SEC:
        _check_cancel()
        if on_progress:
            on_progress(0.3, "Распознавание")
        text = model.transcribe(str(normalized)).text.strip()
        if on_progress:
            on_progress(1.0, "Готово")
        return TranscriptResult(
            duration=duration,
            full_text=text,
            lines=[TranscriptLine(start=0.0, end=duration, text=text)],
        )

    _check_cancel()
    if on_progress:
        on_progress(0.05, "Анализ пауз")
    silences = detect_silences(str(normalized))
    chunks = build_chunks(duration, silences)
    n_chunks = len(chunks)

    all_words: list[tuple[float, float, str]] = []
    for i, (start, end) in enumerate(chunks):
        _check_cancel()
        if on_progress:
            frac = 0.1 + 0.85 * (i / n_chunks)
            on_progress(frac, f"Распознавание ({i + 1}/{n_chunks})")
        chunk_file = work_dir / f"chunk_{i:04d}.wav"
        extract_chunk(str(normalized), start, end, str(chunk_file))
        res = model.transcribe(str(chunk_file), word_timestamps=True)
        for w in res.words or []:
            all_words.append((start + w.start, start + w.end, w.text))
        chunk_file.unlink(missing_ok=True)
    normalized.unlink(missing_ok=True)

    if on_progress:
        on_progress(0.97, "Сборка результата")

    lines: list[TranscriptLine] = []
    buf: list[tuple[float, float, str]] = []
    for w in all_words:
        if buf and (w[0] - buf[-1][1]) > DISPLAY_GAP_SEC:
            lines.append(TranscriptLine(
                start=buf[0][0],
                end=buf[-1][1],
                text=" ".join(x[2] for x in buf),
            ))
            buf = []
        buf.append(w)
    if buf:
        lines.append(TranscriptLine(
            start=buf[0][0],
            end=buf[-1][1],
            text=" ".join(x[2] for x in buf),
        ))

    full_text = " ".join(w[2] for w in all_words)

    if on_progress:
        on_progress(1.0, "Готово")

    return TranscriptResult(duration=duration, full_text=full_text, lines=lines)
