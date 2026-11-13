#!/usr/bin/env python3
"""Локальный сервер приложения (только 127.0.0.1, без зависимостей).

    ./start.sh            # или: python3 scripts/server.py [--port 8765] [--no-open]

API:
    GET  /api/state                 прогресс, игра, анализы интервью, что пора проверить
    GET  /api/lesson?key=<topic>    урок по теме (progress/plans/<slug>.lesson.json)
    POST /api/practice              {topic, correct, total} → XP и серия дней
    POST /api/test                  {topic, score, correct, total} → запись проверки (progress.py check)
    POST /api/review                {topic, prompt, answer} → «мой вариант тоже верный», в progress/inbox/
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import progress as P  # noqa: E402

APP = P.ROOT / "app"
PLANS = P.PROG / "plans"
SESSIONS = P.PROG / "sessions"
INBOX = P.PROG / "inbox"
GAME = P.PROG / "game.json"
LOCK = threading.Lock()
MAX_BODY = 1_000_000
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}
XP_PER_LEVEL = 100


# ---------- игра: XP и серия дней ----------
def load_game() -> dict:
    if GAME.exists():
        return json.loads(GAME.read_text(encoding="utf-8"))
    return {"xp": 0, "streak": 0, "best_streak": 0, "last_active": None, "days": []}


def award(game: dict, xp: int) -> None:
    today = dt.date.today()
    last = dt.date.fromisoformat(game["last_active"]) if game["last_active"] else None
    if last != today:
        game["streak"] = game["streak"] + 1 if last == today - dt.timedelta(days=1) else 1
        game["best_streak"] = max(game["best_streak"], game["streak"])
        game["last_active"] = today.isoformat()
        game["days"] = (game["days"] + [today.isoformat()])[-60:]
    game["xp"] += max(0, int(xp))


def save_game(game: dict) -> None:
    P.PROG.mkdir(parents=True, exist_ok=True)
    GAME.write_text(json.dumps(game, ensure_ascii=False, indent=2), encoding="utf-8")


def game_view(game: dict) -> dict:
    today = dt.date.today()
    last = dt.date.fromisoformat(game["last_active"]) if game["last_active"] else None
    alive = last is not None and (today - last).days <= 1     # серия «сгорает» через день пропуска
    return {**game, "streak": game["streak"] if alive else 0,
            "level": game["xp"] // XP_PER_LEVEL + 1, "xp_in_level": game["xp"] % XP_PER_LEVEL,
            "xp_per_level": XP_PER_LEVEL, "active_today": last == today}


# ---------- данные ----------
def lesson_path(key: str) -> Path | None:
    if not re.fullmatch(r"(grammar|knowledge):[a-z0-9а-яё-]+", key):
        return None
    return PLANS / f"{key.split(':', 1)[1]}.lesson.json"


def build_state() -> dict:
    state = P.load()
    sessions = []
    for f in sorted(SESSIONS.glob("*.analysis.json")):
        try:
            sessions.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    ov = {x["id"]: x.get("overall") for x in state["sessions"]}
    for s in sessions:                      # итог, посчитанный progress.py, надёжнее ручного
        s["overall"] = {**(s.get("overall") or {}), **(ov.get(s.get("session_id")) or {})}
    sessions.sort(key=lambda s: s.get("date", ""))
    lessons = {k: bool((lesson_path(k) or Path("/nonexistent")).exists()) for k in state["topics"]}
    return {"progress": state, "game": game_view(load_game()), "sessions": sessions,
            "due": [k for k, _ in P.due_topics(state)], "lessons": lessons,
            "today": dt.date.today().isoformat(), "pass_score": P.PASS_SCORE,
            "mastered_stage": P.MASTERED_STAGE}


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "InterviewCoach/1.0"

    def log_message(self, fmt, *args):  # тише в консоли
        pass

    # защита: только локальный Host/Origin (против DNS rebinding и чужих сайтов)
    def _local_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            o = urlparse(origin)
            return o.hostname in ("127.0.0.1", "localhost") and o.port == self.server.server_address[1]
        return True

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        if not self._local_ok():
            return self._json(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path == "/api/state":
            with LOCK:
                return self._json(200, build_state())
        if u.path == "/api/lesson":
            key = (parse_qs(u.query).get("key") or [""])[0]
            f = lesson_path(key)
            if key not in P.load()["topics"] or f is None or not f.exists():
                return self._json(404, {"error": "Для этой темы урока пока нет. Попроси Claude составить план."})
            return self._json(200, json.loads(f.read_text(encoding="utf-8")))
        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        target = (APP / rel).resolve()
        if APP.resolve() not in target.parents or not target.is_file():
            return self._json(404, {"error": "not found"})
        self._send(200, target.read_bytes(), TYPES.get(target.suffix, "application/octet-stream"))

    def do_POST(self):
        if not self._local_ok():
            return self._json(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        path = urlparse(self.path).path
        with LOCK:
            state = P.load()
            topic = body.get("topic")
            if topic not in state["topics"]:
                return self._json(404, {"error": "unknown topic"})
            game = load_game()
            if path == "/api/practice":
                correct, total = _int(body.get("correct")), _int(body.get("total"))
                if not 0 <= correct <= total <= 100:
                    return self._json(400, {"error": "bad numbers"})
                gained = correct * 5 + (20 if total else 0)
                award(game, gained)
                save_game(game)
                return self._json(200, {"xp_gained": gained, "game": game_view(game)})
            if path == "/api/test":
                if topic not in {k for k, _ in P.due_topics(state)}:
                    return self._json(409, {"error": "Тест по этой теме пока недоступен: рано по графику."})
                correct, total = _int(body.get("correct")), _int(body.get("total"))
                if total <= 0 or not 0 <= correct <= total <= 100:
                    return self._json(400, {"error": "bad numbers"})
                score = round(correct / total * 100)
                with contextlib.redirect_stdout(io.StringIO()):
                    P.cmd_check(topic, score, f"app: {correct}/{total}")
                passed = score >= P.PASS_SCORE
                gained = 10 + (50 if passed else 0)
                award(game, gained)
                save_game(game)
                INBOX.mkdir(parents=True, exist_ok=True)
                with open(INBOX / "tests.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"),
                                         "topic": topic, "correct": correct, "total": total,
                                         "score": score}, ensure_ascii=False) + "\n")
                return self._json(200, {"score": score, "passed": passed, "xp_gained": gained,
                                        "topic": P.load()["topics"][topic], "game": game_view(game)})
            if path == "/api/review":
                INBOX.mkdir(parents=True, exist_ok=True)
                rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "topic": topic,
                       "prompt": str(body.get("prompt", ""))[:500], "answer": str(body.get("answer", ""))[:500],
                       "expected": str(body.get("expected", ""))[:500]}
                with open(INBOX / "review.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found"})


def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError as e:
        print(f"Порт {a.port} занят ({e}). Попробуй: --port 8766", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{a.port}/"
    print(f"Приложение: {url}  (Ctrl+C — остановить)")
    if not a.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
