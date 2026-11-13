#!/usr/bin/env python3
"""Трекер прогресса: сессии интервью, ошибки, темы, интервальные повторения.

Команды (запускать обычным python3, зависимостей нет):
    progress.py ingest progress/sessions/<id>.analysis.json   # загрузить анализ новой сессии
    progress.py status                                        # дашборд (+ progress/DASHBOARD.md)
    progress.py due                                           # что пора повторить / проверить
    progress.py check <topic_key> <score 0-100> [--note ".."] # записать результат проверки
    progress.py topics                                        # список тем с ключами
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROG = ROOT / "progress"
STATE = PROG / "progress.json"
DASH = PROG / "DASHBOARD.md"
INTERVALS = [1, 3, 7, 14, 30]   # дни до следующей проверки после каждого успешного прохождения
PASS_SCORE = 80
MASTERED_STAGE = 4              # 4 успешных проверки подряд (1д → 3д → 7д → 14д)
CEFR_ORDER = ["A1", "A2", "B1", "B2", "C1", "C2"]
CAUSE_RU = {"language": "язык", "question_understanding": "не понял вопрос",
            "knowledge": "знание темы", "mixed": "смешанная", "none": "ответ был хорошим"}
REQUIRED = ["session_id", "date", "level", "errors", "answers", "topics"]


# ---------- хранилище ----------
def today() -> dt.date:
    return dt.date.today()


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"sessions": [], "topics": {}, "error_categories": {}}


def save(state: dict) -> None:
    PROG.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]+", "-", s.lower()).strip("-")


def topic_key(t: dict) -> str:
    return f"{t['type']}:{slug(t['name'])}"


def count_candidate_words(session_id: str) -> int | None:
    """Считает слова кандидата в размеченном транскрипте (строки 'Candidate: ...')."""
    f = PROG / "transcripts" / f"{session_id}.labeled.md"
    if not f.exists():
        return None
    n = 0
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^(?:\[\d+:\d+\]\s*)?Candidate:\s*(.*)$", line, flags=re.I)
        if m:
            n += len(re.findall(r"[A-Za-z']+", m.group(1)))
    return n or None


# ---------- ingest ----------
def cmd_ingest(path: str) -> int:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        print(f"В анализе нет обязательных полей: {', '.join(missing)}", file=sys.stderr)
        return 1
    sid = data["session_id"]
    if data["level"].get("cefr") not in CEFR_ORDER:
        print(f"level.cefr должен быть одним из {CEFR_ORDER}", file=sys.stderr)
        return 1

    state = load()
    words = data.get("candidate_word_count") or count_candidate_words(sid)
    n_err = len(data["errors"])
    causes = {}
    for a in data["answers"]:
        c = a.get("cause", "none")
        causes[c] = causes.get(c, 0) + 1
        for k in ("score_content", "score_english"):
            v = a.get(k)
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 1 <= v <= 10:
                print(f"answers['{str(a.get('question', '?'))[:40]}']: {k} должен быть числом от 1 до 10",
                      file=sys.stderr)
                return 1

    # итоговая оценка интервью: среднее по содержательным ответам (small talk не считаем)
    scored = [a for a in data["answers"] if not a.get("smalltalk")] or data["answers"]
    ov = dict(data.get("overall") or {})
    if not scored:
        print("answers пуст: нечего оценивать", file=sys.stderr)
        return 1
    ov.setdefault("content", round(sum(a["score_content"] for a in scored) / len(scored), 1))
    ov.setdefault("english", round(sum(a["score_english"] for a in scored) / len(scored), 1))
    ov.setdefault("total", round((ov["content"] + ov["english"]) / 2, 1))

    session = {
        "id": sid, "date": data["date"], "role": data.get("role", ""),
        "cefr": data["level"]["cefr"], "scores": data["level"].get("scores", {}),
        "candidate_words": words, "errors": n_err,
        "errors_per_100w": round(n_err / words * 100, 1) if words else None,
        "overall": ov,
        "causes": causes,
    }
    state["sessions"] = [s for s in state["sessions"] if s["id"] != sid] + [session]
    state["sessions"].sort(key=lambda s: s["date"])

    # ошибки по категориям (идемпотентно: перезаписываем счётчик этой сессии)
    for cat in state["error_categories"].values():
        cat.pop(sid, None)
    for e in data["errors"]:
        cat = state["error_categories"].setdefault(e["category"], {})
        cat[sid] = cat.get(sid, 0) + 1
    state["error_categories"] = {k: v for k, v in state["error_categories"].items() if v}

    # темы
    for t in data["topics"]:
        key = topic_key(t)
        cur = state["topics"].get(key)
        if cur is None:
            state["topics"][key] = {
                "name": t["name"], "type": t["type"], "category": t.get("category", ""),
                "first_seen": data["date"], "sessions": [sid], "stage": 0, "mastery": None,
                "status": "open", "next_review": None, "checks": [],
            }
        else:
            if sid not in cur["sessions"]:
                cur["sessions"].append(sid)
                if cur["status"] == "mastered":      # ошибка вернулась после «освоено»
                    cur["status"], cur["stage"] = "relapsed", 0
                    cur["next_review"] = today().isoformat()

    save(state)
    print(f"Сессия {sid} загружена: уровень {session['cefr']}, ошибок {n_err}"
          + (f" ({session['errors_per_100w']} на 100 слов)" if words else
             " (слова кандидата не посчитаны — нет .labeled.md или candidate_word_count)"))
    print(f"Тем в работе: {sum(1 for t in state['topics'].values() if t['status'] != 'mastered')}")
    return cmd_status(quiet_header=True)


# ---------- check ----------
def cmd_check(key: str, score: float, note: str) -> int:
    state = load()
    t = state["topics"].get(key)
    if not t:
        print(f"Нет темы '{key}'. Список: progress.py topics", file=sys.stderr)
        return 1
    t["checks"].append({"date": today().isoformat(), "score": score, "note": note})
    t["mastery"] = score
    if score >= PASS_SCORE:
        t["stage"] += 1
        if t["stage"] >= MASTERED_STAGE:
            t["status"], t["next_review"] = "mastered", (
                today() + dt.timedelta(days=INTERVALS[min(t["stage"], len(INTERVALS) - 1)])).isoformat()
        else:
            t["status"] = "learning"
            t["next_review"] = (today() + dt.timedelta(days=INTERVALS[t["stage"] - 1])).isoformat()
        print(f"✓ {t['name']}: {score:.0f}% — зачёт. Стадия {t['stage']}/{MASTERED_STAGE}, "
              f"следующая проверка {t['next_review']}.")
    else:
        t["stage"], t["status"] = 0, "learning"
        t["next_review"] = (today() + dt.timedelta(days=1)).isoformat()
        print(f"✗ {t['name']}: {score:.0f}% (порог {PASS_SCORE}%). Повторить завтра, {t['next_review']}.")
    save(state)
    return 0


# ---------- due / topics ----------
def due_topics(state: dict) -> list:
    td = today().isoformat()
    out = []
    for k, t in state["topics"].items():
        if t["status"] == "mastered" and (t["next_review"] or "9999") > td:
            continue
        if t["next_review"] is None or t["next_review"] <= td:
            out.append((k, t))
    return out


def cmd_due() -> int:
    state = load()
    d = due_topics(state)
    if not d:
        print("На сегодня проверок нет.")
        return 0
    for k, t in d:
        tag = {"open": "новая", "relapsed": "ошибка вернулась", "learning": "повтор",
               "mastered": "контрольный повтор"}[t["status"]]
        print(f"- {k}  «{t['name']}»  [{tag}]")
    return 0


def cmd_topics() -> int:
    for k, t in load()["topics"].items():
        print(f"{k}\t{t['status']}\t{t['mastery'] if t['mastery'] is not None else '-'}\t{t['name']}")
    return 0


# ---------- dashboard ----------
def spark(vals: list) -> str:
    bars = "▁▂▃▄▅▆▇█"
    vals = [v for v in vals if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    return "".join(bars[0 if hi == lo else round((v - lo) / (hi - lo) * 7)] for v in vals)


def cmd_status(quiet_header: bool = False) -> int:
    state = load()
    S = state["sessions"]
    if not S:
        print("Пока нет сессий. Транскрибируй запись и запусти анализ.")
        return 0
    L = ["# Прогресс по английскому для интервью", f"_Обновлено {today().isoformat()}_", ""]

    L += ["## Сессии", "", "| Дата | Роль | CEFR | Ошибок / 100 слов | Ответы /10 | Английский /10 | Итог /10 |",
          "|---|---|---|---|---|---|---|"]
    for s in S:
        o = s["overall"]
        L.append(f"| {s['date']} | {s['role'] or '—'} | {s['cefr']} | "
                 f"{s['errors_per_100w'] if s['errors_per_100w'] is not None else '—'} | "
                 f"{o['content']} | {o['english']} | **{o['total']}** |")
    if len(S) > 1:
        L += ["", f"Ошибок на 100 слов: {spark([s['errors_per_100w'] for s in S])} "
                  f"(ниже — лучше) · Итог: {spark([s['overall']['total'] for s in S])}"]
        a, b = S[-2]["cefr"], S[-1]["cefr"]
        if CEFR_ORDER.index(b) != CEFR_ORDER.index(a):
            L.append(f"Уровень: {a} → {b}")

    cats = state["error_categories"]
    if cats:
        last, prev = S[-1]["id"], (S[-2]["id"] if len(S) > 1 else None)
        L += ["", "## Ошибки по категориям", "", "| Категория | Всего | Последняя сессия | Динамика |",
              "|---|---|---|---|"]
        for c, per in sorted(cats.items(), key=lambda kv: -sum(kv[1].values())):
            now, before = per.get(last, 0), per.get(prev, 0) if prev else None
            trend = "—" if before is None else ("↓ лучше" if now < before else "↑ хуже" if now > before else "=")
            L.append(f"| {c} | {sum(per.values())} | {now} | {trend} |")

    causes = {}
    for s in S:
        for c, n in s["causes"].items():
            causes[c] = causes.get(c, 0) + n
    if causes:
        L += ["", "## Почему ответы были слабыми", ""]
        total = sum(causes.values())
        for c, n in sorted(causes.items(), key=lambda kv: -kv[1]):
            L.append(f"- {CAUSE_RU.get(c, c)}: {n} из {total}")

    L += ["", "## Темы для проработки", "", "| Ключ | Тема | Тип | Статус | Освоение | След. проверка |",
          "|---|---|---|---|---|---|"]
    order = {"relapsed": 0, "open": 1, "learning": 2, "mastered": 3}
    for k, t in sorted(state["topics"].items(), key=lambda kv: (order[kv[1]["status"]], kv[1]["name"])):
        m = f"{t['mastery']:.0f}%" if t["mastery"] is not None else "—"
        L.append(f"| `{k}` | {t['name']} | {'язык' if t['type'] == 'grammar' else 'знание'} | "
                 f"{t['status']} {t['stage']}/{MASTERED_STAGE} | {m} | {t['next_review'] or '—'} |")

    d = due_topics(state)
    L += ["", "## Пора проверить сегодня", ""]
    L += [f"- `{k}` — {t['name']}" for k, t in d] or ["- ничего, всё по графику"]

    text = "\n".join(L) + "\n"
    DASH.write_text(text, encoding="utf-8")
    if not quiet_header:
        print(text)
    else:
        print(f"Дашборд обновлён: {DASH.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("due")
    sub.add_parser("topics")
    p = sub.add_parser("ingest")
    p.add_argument("analysis")
    p = sub.add_parser("check")
    p.add_argument("topic")
    p.add_argument("score", type=float)
    p.add_argument("--note", default="")
    a = ap.parse_args()
    if a.cmd == "ingest":
        return cmd_ingest(a.analysis)
    if a.cmd == "check":
        return cmd_check(a.topic, a.score, a.note)
    return {"status": cmd_status, "due": cmd_due, "topics": cmd_topics}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
