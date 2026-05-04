"""CLI for local Russian speech-to-text via GigaAM."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import gigaam

from pipeline import transcribe_audio


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python transcribe.py <file>")
        print("  Accepts any audio/video format ffmpeg can read")
        print("  (mp3, m4a, wav, ogg, flac, aac, opus, mp4, mkv, mov, webm, ...)")
        sys.exit(1)

    input_path = Path(sys.argv[1]).expanduser().resolve()
    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)
    if input_path.is_dir():
        print(f"Path is a directory, expected a file: {input_path}")
        sys.exit(1)

    print("Loading GigaAM v3...")
    model = gigaam.load_model("v3_e2e_rnnt")

    last_stage = ""

    def progress(frac: float, stage: str) -> None:
        nonlocal last_stage
        if stage != last_stage:
            print(f"[{int(frac * 100):3d}%] {stage}")
            last_stage = stage

    with tempfile.TemporaryDirectory() as tmp:
        try:
            result = transcribe_audio(model, str(input_path), Path(tmp), on_progress=progress)
        except RuntimeError as e:
            print(f"Error: {e}")
            sys.exit(1)

    print(f"\nAudio duration: {result.duration:.1f} sec")
    print("\n--- Result ---")
    for ln in result.lines:
        print(f"[{ln.start:6.1f} - {ln.end:6.1f}] {ln.text}")

    print("\n--- Full text ---")
    print(result.full_text)


if __name__ == "__main__":
    main()
