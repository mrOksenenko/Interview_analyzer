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
    POST /api/vocab/answer              {id, ok, claimed?, answer?} → статус слова, XP
    POST /api/vocab/status              {id, status: learning|learned|known}
    POST /api/vocab/finish              {correct} → бонус за завершённый тест слов
    POST /api/vocab/add                 {project, words:[{en,ru,accepted?,example?,example_ru?}]} → пачка слов «на изучение»
    POST /api/vocab/delete              {id} → удалить слово, добавленное вручную
    POST /api/spelling/record           {items:[{typed,right,lang,topic?,ctx?}]} → слова с опечатками в раздел «Правописание»
    POST /api/spelling/answer           {id, ok} → ответ в тренировке написания, XP
    POST /api/spelling/status           {id, status: learning|fixed}
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
VOCAB_DIR = P.PROG / "vocab"
VOCAB_STATE = P.PROG / "vocab_state.json"
SPELLING = P.PROG / "spelling.json"
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
MAX_MANUAL_WORDS = 200
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
            "vocab": {"items": load_vocab_items(), "state": load_vocab_state()},
            "spelling": load_spelling(),
            "due": [k for k, _ in P.due_topics(state)], "lessons": lessons,
            "today": dt.date.today().isoformat(), "pass_score": P.PASS_SCORE,
            "mastered_stage": P.MASTERED_STAGE}


def pending_view(state: dict) -> list:
    out = []
    for m in P.pending_interviews(state):
        out.append({**m, "has_transcript": (TRANSCRIPTS / f"{m['id']}.md").exists(),
                    "project_name": state["projects"].get(m["project"], {}).get("name", m["project"])})
    return out


# ---------- словарь ----------
def load_vocab_items() -> list:
    """Слова из progress/vocab/<проект>.vocab.json; битые записи пропускаются."""
    out, seen = [], set()
    for f in sorted(VOCAB_DIR.glob("*.vocab.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = data.get("project") or f.name.split(".")[0]
        for it in data.get("items", []):
            en, ru = str(it.get("en", "")).strip(), str(it.get("ru", "")).strip()
            if not en or not ru:
                continue
            vid = f"{pid}/{P.slug(en)}"
            if vid in seen:
                continue
            seen.add(vid)
            acc = [ru] + [str(a) for a in it.get("accepted", []) if str(a).strip()]
            out.append({"id": vid, "project": pid, "en": en, "kind": it.get("kind", "word"), "ru": ru,
                        "accepted": list(dict.fromkeys(acc)), "example": it.get("example", ""),
                        "example_ru": it.get("example_ru", ""), "note": it.get("note", ""),
                        "source": it.get("source"), "manual": bool(it.get("manual"))})
    return out


def load_vocab_state() -> dict:
    if VOCAB_STATE.exists():
        try:
            return json.loads(VOCAB_STATE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"words": {}}


def save_vocab_state(vs: dict) -> None:
    P.PROG.mkdir(parents=True, exist_ok=True)
    VOCAB_STATE.write_text(json.dumps(vs, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


def vocab_word(vs: dict, vid: str) -> dict:
    return vs["words"].setdefault(vid, {"status": "new", "seen": 0, "correct": 0, "wrong": 0, "streak": 0, "last": None})


# ---------- правописание ----------
MAX_SPELL_ITEMS = 30


def load_spelling() -> dict:
    if SPELLING.exists():
        try:
            data = json.loads(SPELLING.read_text(encoding="utf-8"))
            if isinstance(data.get("words"), dict):
                return data
        except ValueError:
            pass
    return {"words": {}}


def save_spelling(sp: dict) -> None:
    P.PROG.mkdir(parents=True, exist_ok=True)
    SPELLING.write_text(json.dumps(sp, ensure_ascii=False, indent=2), encoding="utf-8")


def _spelling(self, path: str, body: dict) -> None:
    """Раздел «Правописание». Вызывается под LOCK."""
    sp, today = load_spelling(), dt.date.today().isoformat()
    if path == "/api/spelling/record":
        items = body.get("items")
        if not isinstance(items, list) or len(items) > MAX_SPELL_ITEMS:
            return self._json(400, {"error": "bad items"})
        n = 0
        for it in items:
            if not isinstance(it, dict):
                continue
            lang = "ru" if it.get("lang") == "ru" else "en"
            right, typed = _clean(it.get("right"), 60).lower(), _clean(it.get("typed"), 60).lower()
            if not right or not typed or right == typed:
                continue
            w = sp["words"].setdefault(f"{lang}:{right}", {
                "right": right, "lang": lang, "typed": {}, "count": 0, "first": today, "last": today,
                "streak": 0, "status": "learning", "topic": "", "ctx": ""})
            w["typed"][typed] = w["typed"].get(typed, 0) + 1
            w["count"] += 1
            w["last"] = today
            w["streak"] = 0
            w["status"] = "learning"            # ошибся снова: слово опять на тренировку
            if it.get("topic") and TOPIC_RE.fullmatch(str(it["topic"])):
                w["topic"] = str(it["topic"])
            if it.get("ctx"):
                w["ctx"] = _clean(it["ctx"], 80)
            n += 1
        save_spelling(sp)
        return self._json(200, {"recorded": n, "words": len(sp["words"])})
    wid = str(body.get("id", ""))
    w = sp["words"].get(wid)
    if not w:
        return self._json(404, {"error": "unknown word"})
    if path == "/api/spelling/answer":
        game, ok = load_game(), bool(body.get("ok"))
        w["last"] = today
        if ok:
            w["streak"] += 1
            if w["streak"] >= 3:
                w["status"] = "fixed"
        else:
            w["streak"] = 0
            w["status"] = "learning"
        gained = 2 if ok else 0
        award(game, gained)
        save_game(game)
        save_spelling(sp)
        return self._json(200, {"word": w, "xp_gained": gained, "game": game_view(game)})
    if path == "/api/spelling/status":
        st = body.get("status")
        if st not in ("learning", "fixed"):
            return self._json(400, {"error": "bad status"})
        w["status"] = st
        w["streak"] = 3 if st == "fixed" else 0
        save_spelling(sp)
        return self._json(200, {"word": w})
    return self._json(404, {"error": "not found"})


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

    def _vocab_add(self, body: dict) -> None:
        """Пачка слов, добавленных вручную: идут в «на изучении» (как неизвестные). Под LOCK."""
        pid = str(body.get("project") or "manual")
        if pid != "manual" and pid not in P.load()["projects"]:
            return self._json(404, {"error": "Такого проекта нет."})
        words = body.get("words")
        if not isinstance(words, list) or not words:
            return self._json(400, {"error": "Список слов пуст."})
        if len(words) > MAX_MANUAL_WORDS:
            return self._json(400, {"error": f"За раз можно добавить не больше {MAX_MANUAL_WORDS} слов."})
        VOCAB_DIR.mkdir(parents=True, exist_ok=True)
        f = VOCAB_DIR / f"{pid}.vocab.json"
        try:
            data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"project": pid, "items": []}
        except ValueError:
            return self._json(500, {"error": "Файл словаря повреждён."})
        data.setdefault("items", [])
        known = {P.slug(i["en"]): i for i in load_vocab_items()}
        in_file = {P.slug(x.get("en", "")): x for x in data["items"]}
        vs, today = load_vocab_state(), dt.date.today().isoformat()
        res = {"added": [], "updated": [], "marked": [], "skipped": []}

        def mark(vid: str) -> None:
            w = vocab_word(vs, vid)
            w["status"], w["streak"] = "learning", 0
            w["added"] = w.get("added") or today

        for n, w in enumerate(words, 1):
            if not isinstance(w, dict):
                res["skipped"].append({"n": n, "reason": "не слово"})
                continue
            en, ru = _clean(w.get("en"), 80), _clean(w.get("ru"), 200)
            if not en or not ru or not re.search(r"[A-Za-z]", en):
                res["skipped"].append({"n": n, "reason": "нужны английское слово и перевод"})
                continue
            acc = [_clean(a, 200) for a in (w.get("accepted") or []) if isinstance(a, str)]
            acc = list(dict.fromkeys([ru] + [a for a in acc if a]))[:30]
            entry = {"en": en, "kind": "phrase" if " " in en else "word", "ru": ru, "accepted": acc,
                     "example": _clean(w.get("example"), 300), "example_ru": _clean(w.get("example_ru"), 300),
                     "manual": True}
            note = _clean(w.get("note"), 300)
            if note:
                entry["note"] = note
            slug = P.slug(en)
            found = known.get(slug)
            if found and not (found.get("manual") and found["project"] == pid):
                mark(found["id"])                  # слово уже есть (например из интервью): ставим на изучение
                res["marked"].append(found["id"])
                continue
            vid = f"{pid}/{slug}"
            if slug in in_file:                    # своё слово: обновляем перевод
                in_file[slug].update(entry)
                res["updated"].append(vid)
            else:
                entry["added"] = today
                data["items"].append(entry)
                in_file[slug] = entry
                res["added"].append(vid)
            mark(vid)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)
        save_vocab_state(vs)
        return self._json(200, res)

    def _vocab_delete(self, body: dict) -> None:
        """Удаляет только слово, добавленное вручную. Под LOCK."""
        vid = str(body.get("id", ""))
        item = next((i for i in load_vocab_items() if i["id"] == vid), None)
        if not item or not item.get("manual"):
            return self._json(404, {"error": "Удалять можно только слова, добавленные вручную."})
        f = VOCAB_DIR / f"{item['project']}.vocab.json"
        data = json.loads(f.read_text(encoding="utf-8"))
        data["items"] = [x for x in data.get("items", []) if P.slug(x.get("en", "")) != vid.split("/", 1)[1]]
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, f)
        vs = load_vocab_state()
        vs["words"].pop(vid, None)
        save_vocab_state(vs)
        return self._json(200, {"ok": True})

    def _vocab(self, path: str, body: dict) -> None:
        """Ответы на слова. Вызывается под LOCK."""
        if path == "/api/vocab/add":
            return self._vocab_add(body)
        if path == "/api/vocab/delete":
            return self._vocab_delete(body)
        vid = str(body.get("id", ""))
        if path != "/api/vocab/finish" and vid not in {i["id"] for i in load_vocab_items()}:
            return self._json(404, {"error": "unknown word"})
        game, vs = load_game(), load_vocab_state()
        today = dt.date.today().isoformat()
        if path == "/api/vocab/answer":
            ok, w = bool(body.get("ok")), vocab_word(vs, vid)
            prev = w["status"]
            w["seen"] += 1
            w["last"] = today
            if ok:
                w["correct"] += 1
                w["streak"] += 1
                if prev == "new":
                    w["status"] = "known"
                elif prev == "learning" and w["streak"] >= 3:
                    w["status"] = "learned"
            else:
                w["wrong"] += 1
                w["streak"] = 0
                w["status"] = "learning"
                w["added"] = w.get("added") or today
            if body.get("claimed"):               # «мой вариант тоже верный»: пишем для проверки
                INBOX.mkdir(parents=True, exist_ok=True)
                with open(INBOX / "vocab_review.jsonl", "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": dt.datetime.now().isoformat(timespec="seconds"), "id": vid,
                                         "answer": str(body.get("answer", ""))[:200]}, ensure_ascii=False) + "\n")
            gained = 2 if ok else 0
            award(game, gained)
            save_vocab_state(vs)
            save_game(game)
            return self._json(200, {"word": w, "learned": prev == "learning" and w["status"] == "learned",
                                    "xp_gained": gained, "game": game_view(game)})
        if path == "/api/vocab/status":
            st = body.get("status")
            if st not in ("learning", "learned", "known"):
                return self._json(400, {"error": "bad status"})
            w = vocab_word(vs, vid)
            w["status"] = st
            w["streak"] = 3 if st == "learned" else 0
            if st == "learning":
                w["added"] = w.get("added") or today
            save_vocab_state(vs)
            return self._json(200, {"word": w})
        if path == "/api/vocab/finish":
            n = _int(body.get("correct"))
            gained = 10 if n >= 0 else 0
            award(game, gained)
            save_game(game)
            return self._json(200, {"xp_gained": gained, "game": game_view(game)})
        return self._json(404, {"error": "not found"})

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
            if path.startswith("/api/vocab/"):
                return self._vocab(path, body)
            if path.startswith("/api/spelling/"):
                return _spelling(self, path, body)
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
