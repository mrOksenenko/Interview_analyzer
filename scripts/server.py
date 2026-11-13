#!/usr/bin/env python3
"""Локальный сервер приложения (только 127.0.0.1, без зависимостей).

    ./start.sh            # или: python3 scripts/server.py [--port 8765] [--no-open]

API:
    GET  /api/state                     проекты, интервью, темы, игра, ожидающие записи
    GET  /api/jobs                      статус расшифровок (для опроса)
    GET  /api/lesson?key=<topic>        урок по теме (progress/plans/<проект>/<тема>.lesson.json)
    POST /api/upload?...                сырое аудио в теле запроса → проект + расшифровка в фоне
    POST /api/retry                     {session_id} повторить расшифровку сохранённого аудио
    POST /api/practice                  {topic, correct, total} → XP и серия дней
    POST /api/test                      {topic, correct, total} → запись проверки (progress.py check)
    POST /api/review                    {topic, prompt, answer} → «мой вариант тоже верный»
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import progress as P  # noqa: E402

APP = P.ROOT / "app"
PLANS = P.PROG / "plans"
SESSIONS = P.PROG / "sessions"
AUDIO = P.PROG / "audio"
TRANSCRIPTS = P.PROG / "transcripts"
INBOX = P.PROG / "inbox"
GAME = P.PROG / "game.json"
VENV_PY = P.ROOT / ".venv" / "bin" / "python"
LOCK = threading.Lock()
JOBS: dict = {}                     # session_id → {stage, pos, dur, error, started}
TRANSCRIBE_SLOT = threading.Semaphore(1)   # одна расшифровка за раз
MAX_BODY = 1_000_000
MAX_UPLOAD = 2 * 1024 ** 3
AUDIO_EXT = {".mp3", ".m4a", ".wav", ".mp4", ".webm", ".ogg", ".oga", ".opus", ".flac", ".aac",
             ".mov", ".mkv", ".aiff", ".caf"}
MODELS = {"tiny", "base", "small", "medium", "large-v3"}
DEVICES = {"auto", "cpu", "mps"}
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".json": "application/json"}
XP_PER_LEVEL = 100
TOPIC_RE = re.compile(r"[a-z0-9а-яё-]+/(grammar|knowledge):[a-z0-9а-яё-]+")


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
    if not TOPIC_RE.fullmatch(key):
        return None
    pid, rest = key.split("/", 1)
    return PLANS / pid / f"{rest.split(':', 1)[1]}.lesson.json"


def build_state() -> dict:
    state = P.load()
    sessions = []
    for f in sorted(SESSIONS.glob("*.analysis.json")):
        try:
            sessions.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    done = {x["id"]: x for x in state["sessions"]}
    for s in sessions:                      # итог, посчитанный progress.py, надёжнее ручного
        rec = done.get(s.get("session_id"), {})
        s["overall"] = {**(s.get("overall") or {}), **(rec.get("overall") or {})}
        s["title"] = rec.get("title") or s.get("title", "")
    sessions.sort(key=lambda s: (s.get("date", ""), s.get("session_id", "")))
    lessons = {k: bool((lesson_path(k) or Path("/nonexistent")).exists()) for k in state["topics"]}
    return {"progress": state, "game": game_view(load_game()), "sessions": sessions,
            "pending": pending_view(state), "jobs": JOBS,
            "due": [k for k, _ in P.due_topics(state)], "lessons": lessons,
            "today": dt.date.today().isoformat(), "pass_score": P.PASS_SCORE,
            "mastered_stage": P.MASTERED_STAGE}


def pending_view(state: dict) -> list:
    out = []
    for m in P.pending_interviews(state):
        out.append({**m, "has_transcript": (TRANSCRIPTS / f"{m['id']}.md").exists(),
                    "project_name": state["projects"].get(m["project"], {}).get("name", m["project"])})
    return out


# ---------- расшифровка в фоне ----------
def set_meta(sid: str, **upd) -> None:
    m = P.read_meta(sid)
    if m:
        m.update(upd)
        P.write_meta(m)


def run_transcription(sid: str) -> None:
    meta = P.read_meta(sid)
    JOBS[sid] = {"stage": "queued", "pos": 0, "dur": 0, "error": None}
    with TRANSCRIBE_SLOT:
        try:
            if not VENV_PY.exists():
                raise RuntimeError("Не установлена расшифровка. Запусти ./setup.sh")
            audio = P.ROOT / meta["audio"]
            JOBS[sid].update(stage="loading")
            set_meta(sid, status="transcribing", error=None)
            proc = subprocess.Popen(
                [str(VENV_PY), str(P.ROOT / "scripts" / "transcribe.py"), str(audio), "--id", sid,
                 "--model", meta.get("model", "small"), "--device", meta.get("device", "auto")],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(P.ROOT))
            tail = []
            buf = b""
            while True:
                chunk = proc.stderr.read(256)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for raw in parts:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    m = re.match(r"(\d\d):(\d\d) / (\d\d):(\d\d)", line)
                    if m:
                        a, b, c, d = map(int, m.groups())
                        JOBS[sid].update(stage="transcribing", pos=a * 60 + b, dur=c * 60 + d)
                    elif "Транскрибирую" in line:
                        JOBS[sid].update(stage="transcribing")
                    elif "Warning" not in line and "HF_TOKEN" not in line:
                        tail = (tail + [line])[-6:]
            proc.wait()
            if proc.returncode != 0:
                raise RuntimeError(" | ".join(tail[-3:]) or f"transcribe.py завершился с кодом {proc.returncode}")
            stats = json.loads((TRANSCRIPTS / f"{sid}.json").read_text(encoding="utf-8")).get("stats", {})
            set_meta(sid, status="transcribed", stats=stats, error=None)
            JOBS[sid].update(stage="done", pos=JOBS[sid].get("dur", 0))
        except Exception as e:  # noqa: BLE001
            set_meta(sid, status="failed", error=str(e)[:300])
            JOBS[sid].update(stage="failed", error=str(e)[:300])


def start_job(sid: str) -> None:
    threading.Thread(target=run_transcription, args=(sid,), daemon=True).start()


def mark_interrupted() -> None:
    for f in SESSIONS.glob("*.meta.json"):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("status") in ("transcribing", "uploaded"):
            set_meta(m["id"], status="failed", error="Прервано перезапуском сервера. Нажми «Повторить».")


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "InterviewCoach/2.0"

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
        if u.path == "/api/jobs":
            with LOCK:
                return self._json(200, {"jobs": JOBS, "pending": pending_view(P.load())})
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

    # ----- загрузка аудио -----
    def _upload(self, query: dict) -> None:
        q = {k: (v[0] if v else "") for k, v in query.items()}
        fname = os.path.basename(q.get("filename", "") or "recording.webm")
        ext = Path(fname).suffix.lower()
        mode = q.get("mode", "new")
        model, device = q.get("model", "small"), q.get("device", "auto")
        title = " ".join((q.get("title") or Path(fname).stem).split())[:80]
        pname = " ".join((q.get("project_name") or "").split())[:80]
        pid = q.get("project", "")
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        if ext not in AUDIO_EXT:
            return self._json(400, {"error": f"Формат {ext or '?'} не поддерживается. Подойдут: mp3, m4a, wav, mp4, webm, ogg, flac."})
        if mode not in ("new", "existing") or model not in MODELS or device not in DEVICES:
            return self._json(400, {"error": "Неверные параметры загрузки."})
        if mode == "new" and not pname:
            return self._json(400, {"error": "Назови проект."})
        if not 0 < size <= MAX_UPLOAD:
            return self._json(413, {"error": "Файл пустой или больше 2 ГБ."})
        if mode == "existing" and pid not in P.load()["projects"]:
            return self._json(404, {"error": "Такого проекта нет."})

        AUDIO.mkdir(parents=True, exist_ok=True)
        tmp = AUDIO / f".upload-{uuid.uuid4().hex}.part"
        got = 0
        try:
            with open(tmp, "wb") as fh:
                while got < size:
                    chunk = self.rfile.read(min(1 << 20, size - got))
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
            if got != size:
                tmp.unlink(missing_ok=True)
                return self._json(400, {"error": "Загрузка оборвалась. Попробуй ещё раз."})
            with LOCK:
                state = P.load()
                if mode == "new":
                    pid = P.new_project(state, pname)
                proj = state["projects"][pid]
                n = len(proj["sessions"]) + 1
                today = dt.date.today().isoformat()
                sid = f"{today}-{pid}-{n}"
                while (SESSIONS / f"{sid}.meta.json").exists():
                    n += 1
                    sid = f"{today}-{pid}-{n}"
                final = AUDIO / f"{sid}{ext}"
                os.replace(tmp, final)
                proj["sessions"].append(sid)
                P.save(state)
                P.write_meta({"id": sid, "project": pid, "project_name": proj["name"], "mode": mode,
                              "title": title, "created": today, "audio": str(final.relative_to(P.ROOT)),
                              "model": model, "device": device, "status": "uploaded",
                              "original_name": fname})
            start_job(sid)
            return self._json(200, {"session_id": sid, "project": pid, "project_name": proj["name"], "mode": mode})
        finally:
            tmp.unlink(missing_ok=True)

    def do_POST(self):
        if not self._local_ok():
            return self._json(403, {"error": "forbidden"})
        u = urlparse(self.path)
        if u.path == "/api/upload":
            return self._upload(parse_qs(u.query))
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._json(400, {"error": "bad json"})
        path = u.path
        with LOCK:
            state = P.load()
            if path == "/api/retry":
                sid = str(body.get("session_id", ""))
                meta = P.read_meta(sid)
                if not meta or meta.get("status") != "failed" or not (P.ROOT / (meta.get("audio") or "x")).exists():
                    return self._json(404, {"error": "Нечего повторять."})
                set_meta(sid, status="uploaded", error=None)
                start_job(sid)
                return self._json(200, {"ok": True})
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
    mark_interrupted()
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
