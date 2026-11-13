#!/usr/bin/env python3
"""Трекер прогресса: проекты, интервью, ошибки, темы, интервальные повторения.

Проект = цель (например, «Собеседование в Acme»). В проекте может быть несколько записей
(интервью), у проекта свой план: темы и уроки. Тема живёт внутри проекта.

Команды (запускать обычным python3, зависимостей нет):
    progress.py pending                                       # записи, ждущие разбора
    progress.py project list | new "<название>"               # проекты
    progress.py ingest progress/sessions/<id>.analysis.json   # загрузить анализ интервью
    progress.py status                                        # дашборд (+ progress/DASHBOARD.md)
    progress.py due                                           # что пора повторить / проверить
    progress.py check <topic_key> <score 0-100> [--note ".."] # записать результат проверки
    progress.py topics [<project_id>]                         # темы с ключами
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
SESSIONS = PROG / "sessions"
INTERVALS = [1, 3, 7, 14, 30]   # дни до следующей проверки после каждого успешного прохождения
PASS_SCORE = 80
MASTERED_STAGE = 4              # 4 успешных проверки подряд (1д → 3д → 7д → 14д)
CEFR_ORDER = ["A1", "A2", "B1", "B2", "C1", "C2"]
CAUSE_RU = {"language": "язык", "question_understanding": "не понял вопрос",
            "knowledge": "знание темы", "mixed": "смешанная", "none": "ответ был хорошим"}
REQUIRED = ["session_id", "project", "date", "level", "errors", "answers", "topics"]


# ---------- хранилище ----------
def today() -> dt.date:
    return dt.date.today()


def load() -> dict:
    state = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    state.setdefault("projects", {})
    state.setdefault("sessions", [])
    state.setdefault("topics", {})
    state.setdefault("error_categories", {})
    return state


def save(state: dict) -> None:
    PROG.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9а-яё]+", "-", s.lower()).strip("-")


def topic_key(pid: str, t: dict) -> str:
    return f"{pid}/{t['type']}:{slug(t['name'])}"


def new_project(state: dict, name: str) -> str:
    """Создаёт проект и возвращает его id (уникальный slug названия)."""
    name = " ".join(name.split())
    if not name:
        raise ValueError("Название проекта пустое")
    base = slug(name)[:40].strip("-") or "project"
    pid, n = base, 2
    while pid in state["projects"]:
        pid, n = f"{base}-{n}", n + 1
    state["projects"][pid] = {"name": name, "created": today().isoformat(), "sessions": []}
    return pid


def read_meta(sid: str) -> dict | None:
    f = SESSIONS / f"{sid}.meta.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def write_meta(meta: dict) -> None:
    SESSIONS.mkdir(parents=True, exist_ok=True)
    (SESSIONS / f"{meta['id']}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def pending_interviews(state: dict) -> list:
    """Записи, у которых есть расшифровка, но ещё нет разбора."""
    analyzed = {s["id"] for s in state["sessions"]}
    out = []
    for f in sorted(SESSIONS.glob("*.meta.json")):
        try:
            m = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m["id"] not in analyzed and m.get("status") in ("transcribing", "transcribed", "uploaded", "failed"):
            out.append(m)
    return out


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


def project_sessions(state: dict, pid: str) -> list:
    """Разобранные интервью проекта по дате."""
    return sorted((s for s in state["sessions"] if s.get("project") == pid), key=lambda s: (s["date"], s["id"]))


def quiet_streak(state: dict, pid: str, t: dict) -> int:
    """Сколько последних разобранных интервью проекта подряд тема не встречалась."""
    n = 0
    for s in reversed(project_sessions(state, pid)):
        if s["id"] in t["sessions"]:
            break
        n += 1
    return n


# ---------- ingest ----------
def cmd_ingest(path: str) -> int:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        print(f"В анализе нет обязательных полей: {', '.join(missing)}", file=sys.stderr)
        return 1
    sid, pid = data["session_id"], data["project"]
    if data["level"].get("cefr") not in CEFR_ORDER:
        print(f"level.cefr должен быть одним из {CEFR_ORDER}", file=sys.stderr)
        return 1

    state = load()
    if pid not in state["projects"]:
        print(f"Нет проекта '{pid}'. Существующие: {', '.join(state['projects']) or '—'}. "
              f"Создать: progress.py project new \"<название>\"", file=sys.stderr)
        return 1
    proj = state["projects"][pid]

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
    if not scored:
        print("answers пуст: нечего оценивать", file=sys.stderr)
        return 1
    ov = dict(data.get("overall") or {})
    ov.setdefault("content", round(sum(a["score_content"] for a in scored) / len(scored), 1))
    ov.setdefault("english", round(sum(a["score_english"] for a in scored) / len(scored), 1))
    ov.setdefault("total", round((ov["content"] + ov["english"]) / 2, 1))

    session = {
        "id": sid, "project": pid, "date": data["date"], "role": data.get("role", ""),
        "title": (read_meta(sid) or {}).get("title", ""),
        "cefr": data["level"]["cefr"], "scores": data["level"].get("scores", {}),
        "candidate_words": words, "errors": n_err,
        "errors_per_100w": round(n_err / words * 100, 1) if words else None,
        "overall": ov, "causes": causes,
    }
    state["sessions"] = [s for s in state["sessions"] if s["id"] != sid] + [session]
    state["sessions"].sort(key=lambda s: (s["date"], s["id"]))
    if sid not in proj["sessions"]:
        proj["sessions"].append(sid)

    # ошибки по категориям (идемпотентно: перезаписываем счётчик этой сессии)
    for cat in state["error_categories"].values():
        cat.pop(sid, None)
    for e in data["errors"]:
        cat = state["error_categories"].setdefault(e["category"], {})
        cat[sid] = cat.get(sid, 0) + 1
    state["error_categories"] = {k: v for k, v in state["error_categories"].items() if v}

    # темы проекта; чтобы повторная загрузка не плодила дубли, сначала снимаем эту сессию со всех тем
    before = {k for k, t in state["topics"].items() if t["project"] == pid and sid not in t["sessions"]}
    for k, t in list(state["topics"].items()):
        if t["project"] == pid and sid in t["sessions"]:
            t["sessions"].remove(sid)
            if not t["sessions"] and not t["checks"]:
                del state["topics"][k]
    seen, new, relapsed = [], [], []
    for t in data["topics"]:
        key = topic_key(pid, t)
        seen.append(key)
        cur = state["topics"].get(key)
        if cur is None:
            state["topics"][key] = {
                "name": t["name"], "type": t["type"], "category": t.get("category", ""), "project": pid,
                "first_seen": data["date"], "sessions": [sid], "stage": 0, "mastery": None,
                "status": "open", "next_review": None, "checks": [],
            }
            new.append(key)
        else:
            if sid not in cur["sessions"]:
                cur["sessions"].append(sid)
            if cur["status"] == "mastered":          # ошибка вернулась после «освоено»
                cur["status"], cur["stage"] = "relapsed", 0
                cur["next_review"] = today().isoformat()
                relapsed.append(key)
    meta = read_meta(sid)
    if meta:
        meta["status"] = "analyzed"
        write_meta(meta)

    save(state)
    print(f"Интервью {sid} → проект «{proj['name']}»: уровень {session['cefr']}, "
          f"итог {ov['total']}/10 (ответы {ov['content']}, английский {ov['english']}), "
          f"ошибок {n_err}" + (f" ({session['errors_per_100w']} на 100 слов)" if words else
                               " (слова кандидата не посчитаны: нет .labeled.md)"))

    # что изменилось в плане проекта: основа для корректировки плана
    n_int = len(project_sessions(state, pid))
    recurring = [k for k in seen if k in before]
    quiet = [k for k in before if k not in seen and state["topics"][k]["status"] != "mastered"]
    print(f"\nПлан проекта «{proj['name']}»: интервью №{n_int}")
    nm = lambda k: state["topics"][k]["name"]  # noqa: E731
    print("  новые темы:          " + ("; ".join(nm(k) for k in new) or "—"))
    print("  повторились:         " + ("; ".join(nm(k) for k in recurring) or "—"))
    print("  вернулись (relapse): " + ("; ".join(nm(k) for k in relapsed) or "—"))
    print("  не встретились:      " + ("; ".join(f"{nm(k)} (подряд: {quiet_streak(state, pid, state['topics'][k])})"
                                                for k in quiet) or "—"))
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


# ---------- due / topics / projects / pending ----------
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


def cmd_topics(pid: str | None) -> int:
    for k, t in load()["topics"].items():
        if pid and t["project"] != pid:
            continue
        print(f"{k}\t{t['status']}\t{t['mastery'] if t['mastery'] is not None else '-'}\t{t['name']}")
    return 0


def cmd_project(action: str, name: str | None) -> int:
    state = load()
    if action == "new":
        try:
            pid = new_project(state, name or "")
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        save(state)
        print(pid)
        return 0
    for pid, p in state["projects"].items():
        n_an = len(project_sessions(state, pid))
        print(f"{pid}\t{p['name']}\tинтервью: {len(p['sessions'])} (разобрано {n_an})")
    return 0


def cmd_pending() -> int:
    state = load()
    pend = pending_interviews(state)
    if not pend:
        print("Нет записей, ждущих разбора.")
        return 0
    for m in pend:
        p = state["projects"].get(m["project"], {})
        mode = "новый проект: составить план" if m.get("mode") == "new" else "добавлено в существующий: скорректировать план"
        tr = PROG / "transcripts" / f"{m['id']}.md"
        print(f"- {m['id']}\tпроект {m['project']} «{p.get('name', '?')}»\t{m.get('status')}\t{mode}\t"
              f"{'транскрипт: ' + str(tr.relative_to(ROOT)) if tr.exists() else 'транскрипта нет'}")
    return 0


# ---------- dashboard ----------
def spark(vals: list) -> str:
    bars = "▁▂▃▄▅▆▇█"
    vals = [v for v in vals if v is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    return "".join(bars[0 if hi == lo else round((v - lo) / (hi - lo) * 7)] for v in vals)


def project_block(state: dict, pid: str) -> list:
    p = state["projects"][pid]
    S = project_sessions(state, pid)
    L = [f"## Проект «{p['name']}» (`{pid}`)", ""]
    if not S:
        return L + ["Разобранных интервью пока нет.", ""]
    L += ["| Дата | Запись | CEFR | Ошибок / 100 слов | Ответы /10 | Английский /10 | Итог /10 |",
          "|---|---|---|---|---|---|---|"]
    for s in S:
        o = s["overall"]
        L.append(f"| {s['date']} | {s.get('title') or s['role'] or s['id']} | {s['cefr']} | "
                 f"{s['errors_per_100w'] if s['errors_per_100w'] is not None else '—'} | "
                 f"{o['content']} | {o['english']} | **{o['total']}** |")
    if len(S) > 1:
        L += ["", f"Ошибок на 100 слов: {spark([s['errors_per_100w'] for s in S])} (ниже — лучше) · "
                  f"Итог: {spark([s['overall']['total'] for s in S])}"]
        a, b = S[-2]["cefr"], S[-1]["cefr"]
        if a != b:
            L.append(f"Уровень: {a} → {b}")

    ids = {s["id"] for s in S}
    cats = {c: {k: v for k, v in per.items() if k in ids} for c, per in state["error_categories"].items()}
    cats = {c: per for c, per in cats.items() if per}
    if cats:
        last, prev = S[-1]["id"], (S[-2]["id"] if len(S) > 1 else None)
        L += ["", "### Ошибки по категориям", "", "| Категория | Всего | Последняя запись | Динамика |", "|---|---|---|---|"]
        for c, per in sorted(cats.items(), key=lambda kv: -sum(kv[1].values())):
            now, before = per.get(last, 0), per.get(prev, 0) if prev else None
            trend = "—" if before is None else ("↓ лучше" if now < before else "↑ хуже" if now > before else "=")
            L.append(f"| {c} | {sum(per.values())} | {now} | {trend} |")

    causes = {}
    for s in S:
        for c, n in s["causes"].items():
            causes[c] = causes.get(c, 0) + n
    if causes:
        L += ["", "### Почему ответы были слабыми", ""]
        total = sum(causes.values())
        L += [f"- {CAUSE_RU.get(c, c)}: {n} из {total}" for c, n in sorted(causes.items(), key=lambda kv: -kv[1])]

    order = {"relapsed": 0, "open": 1, "learning": 2, "mastered": 3}
    tp = sorted(((k, t) for k, t in state["topics"].items() if t["project"] == pid),
                key=lambda kv: (order[kv[1]["status"]], kv[1]["name"]))
    if tp:
        L += ["", "### План: темы", "", "| Ключ | Тема | Тип | Статус | Освоение | След. проверка | Не встречалась подряд |",
              "|---|---|---|---|---|---|---|"]
        for k, t in tp:
            m = f"{t['mastery']:.0f}%" if t["mastery"] is not None else "—"
            L.append(f"| `{k}` | {t['name']} | {'язык' if t['type'] == 'grammar' else 'знание'} | "
                     f"{t['status']} {t['stage']}/{MASTERED_STAGE} | {m} | {t['next_review'] or '—'} | "
                     f"{quiet_streak(state, pid, t)} |")
    return L + [""]


def cmd_status(quiet_header: bool = False) -> int:
    state = load()
    if not state["sessions"] and not state["projects"]:
        print("Пока нет проектов. Загрузи запись интервью в приложении или напиши Claude.")
        return 0
    L = ["# Прогресс по английскому для интервью", f"_Обновлено {today().isoformat()}_", ""]
    for pid in state["projects"]:
        L += project_block(state, pid)
    d = due_topics(state)
    L += ["## Пора проверить сегодня", ""]
    L += [f"- `{k}` — {t['name']} ({state['projects'][t['project']]['name']})" for k, t in d] or ["- ничего, всё по графику"]
    text = "\n".join(L) + "\n"
    DASH.write_text(text, encoding="utf-8")
    if not quiet_header:
        print(text)
    else:
        print(f"\nДашборд обновлён: {DASH.relative_to(ROOT)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for n in ("status", "due", "pending"):
        sub.add_parser(n)
    p = sub.add_parser("topics")
    p.add_argument("project", nargs="?")
    p = sub.add_parser("project")
    p.add_argument("action", choices=["list", "new"])
    p.add_argument("name", nargs="?")
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
    if a.cmd == "topics":
        return cmd_topics(a.project)
    if a.cmd == "project":
        return cmd_project(a.action, a.name)
    return {"status": cmd_status, "due": cmd_due, "pending": cmd_pending}[a.cmd]()


if __name__ == "__main__":
    sys.exit(main())
