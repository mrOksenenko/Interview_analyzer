#!/usr/bin/env bash
# Запуск приложения: открывается в браузере на http://127.0.0.1:8765
cd "$(dirname "$0")"
exec python3 scripts/server.py "$@"
