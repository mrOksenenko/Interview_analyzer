#!/usr/bin/env bash
# Одноразовая установка: виртуальное окружение + локальная транскрипция (faster-whisper).
# ffmpeg не нужен — faster-whisper декодирует аудио сам (PyAV).
set -euo pipefail
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet faster-whisper
mkdir -p progress/transcripts progress/sessions progress/plans progress/reports

echo "Готово. Пример:"
echo "  .venv/bin/python scripts/transcribe.py ~/Downloads/interview.m4a --id 2026-11-12-company"
