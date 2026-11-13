#!/usr/bin/env python3
"""Локальная транскрипция аудио интервью (faster-whisper, всё на твоём Mac).

Использование:
    .venv/bin/python scripts/transcribe.py путь/к/записи.m4a --id 2026-11-12-acme
    .venv/bin/python scripts/transcribe.py rec.mp3 --model medium   # точнее, но медленнее

Результат (в progress/transcripts/):
    <id>.md    — читаемая версия: [мм:сс] текст, паузы и сомнительные места помечены
    <id>.json  — сегменты с таймкодами и уверенностью + статистика
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "progress" / "transcripts"

FILLERS = [r"\bum+\b", r"\buh+\b", r"\ber+\b", r"\bhmm+\b", r"\byou know\b", r"\bi mean\b",
           r"\bkind of\b", r"\bsort of\b", r"\blike\b", r"\bactually\b", r"\bbasically\b"]
PAUSE_MARK = 1.5       # сек: пауза между сегментами, которую стоит пометить
LOW_CONF = -1.0        # avg_logprob ниже — слово(а) могли быть распознаны неверно


def mmss(t: float) -> str:
    t = int(t)
    return f"{t // 60:02d}:{t % 60:02d}"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "interview"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("audio", help="аудио/видео файл (mp3, m4a, wav, mp4, webm...)")
    ap.add_argument("--id", help="id сессии, по умолчанию: <дата>-<имя файла>")
    ap.add_argument("--model", default="small",
                    help="tiny | base | small (по умолчанию) | medium | large-v3")
    ap.add_argument("--language", default="en", help="язык записи (по умолчанию en)")
    args = ap.parse_args()

    audio = Path(args.audio).expanduser()
    if not audio.exists():
        print(f"Файл не найден: {audio}", file=sys.stderr)
        return 1

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("faster-whisper не установлен. Запусти: ./setup.sh", file=sys.stderr)
        return 1

    sid = args.id or f"{dt.date.today().isoformat()}-{slug(audio.stem)}"
    print(f"Загружаю модель '{args.model}' (при первом запуске скачается)…", file=sys.stderr)
    model = WhisperModel(args.model, device="cpu", compute_type="int8")

    print("Транскрибирую…", file=sys.stderr)
    # condition_on_previous_text=False: меньше риска, что Whisper «подтянет» грамматику
    # кандидата под предыдущий контекст. VAD отсекает тишину.
    seg_iter, info = model.transcribe(
        str(audio), language=args.language, beam_size=5,
        vad_filter=True, condition_on_previous_text=False,
    )

    segments = []
    for s in seg_iter:
        text = s.text.strip()
        if text:
            segments.append({"start": round(s.start, 2), "end": round(s.end, 2), "text": text,
                             "avg_logprob": round(s.avg_logprob, 3),
                             "no_speech_prob": round(s.no_speech_prob, 3)})
        print(f"\r  {mmss(s.end)} / {mmss(info.duration)}", end="", file=sys.stderr)
    print(file=sys.stderr)

    full = " ".join(s["text"] for s in segments)
    words = len(re.findall(r"[A-Za-z']+", full))
    speech_sec = sum(s["end"] - s["start"] for s in segments) or 1
    filler_counts = {}
    for pat in FILLERS:
        n = len(re.findall(pat, full, flags=re.I))
        if n:
            filler_counts[pat.strip(r"\b+")] = n
    stats = {
        "duration_sec": round(info.duration, 1),
        "words_total": words,
        "words_per_min_speaking": round(words / speech_sec * 60),
        "fillers": filler_counts,
        "low_confidence_segments": sum(1 for s in segments if s["avg_logprob"] < LOW_CONF),
        "model": args.model,
    }

    OUT.mkdir(parents=True, exist_ok=True)
    lines = [f"# Транскрипт: {sid}", "",
             f"_Источник: {audio.name} · {mmss(info.duration)} · модель {args.model}_",
             "_Пометки: `[пауза Ns]` — тишина ≥1.5с; `(?)` — Whisper сомневался в этом фрагменте._",
             "_Whisper склонен «исправлять» грамматику — спорные ошибки перепроверяй по аудио._", ""]
    prev_end = None
    for s in segments:
        if prev_end is not None and s["start"] - prev_end >= PAUSE_MARK:
            lines.append(f"[пауза {s['start'] - prev_end:.1f}с]")
        mark = " (?)" if s["avg_logprob"] < LOW_CONF else ""
        lines.append(f"[{mmss(s['start'])}] {s['text']}{mark}")
        prev_end = s["end"]
    (OUT / f"{sid}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / f"{sid}.json").write_text(
        json.dumps({"id": sid, "source": audio.name, "stats": stats, "segments": segments},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Готово: progress/transcripts/{sid}.md")
    print(f"Слов: {words} · темп речи ≈ {stats['words_per_min_speaking']} сл/мин · "
          f"слов-паразитов: {sum(filler_counts.values())} · сомнительных сегментов: "
          f"{stats['low_confidence_segments']}")
    print(f"ID сессии: {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
