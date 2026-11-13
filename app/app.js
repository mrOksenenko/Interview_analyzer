"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CAUSE = { language: "Язык", question_understanding: "Не понял вопрос", knowledge: "Знание темы", mixed: "Смешанная", none: "Ответ хороший" };
const CATS = { tenses: "Времена", articles: "Артикли", prepositions: "Предлоги", word_order: "Порядок слов", agreement: "Согласование", modals_conditionals: "Модальные и условные", passive_voice: "Пассив", verb_forms: "Формы глагола", sentence_structure: "Структура фраз", vocabulary: "Лексика", discourse: "Связность", fluency: "Беглость" };
const STATUS = { open: "Новая", learning: "Учу", mastered: "Освоено", relapsed: "Вернулась" };

let D = null, view = "home", P = null, timer = null, curSession = null;

/* ---------- утилиты ---------- */
const scoreCls = v => (v >= 8 ? "ok" : v >= 5 ? "warn" : "bad");
const scoreCol = v => `var(--${scoreCls(v)})`;
const topicName = k => D.progress.topics[k]?.name || k;

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("on");
  clearTimeout(toast.h); toast.h = setTimeout(() => t.classList.remove("on"), 3200);
}

function ring(v, size, label) {
  const r = size / 2 - 7, c = 2 * Math.PI * r, p = Math.max(0, Math.min(1, v / 10));
  return `<div><div class="ring" style="width:${size}px;height:${size}px;font-size:${Math.round(size / 3.4)}px">
    <svg width="${size}" height="${size}" role="img" aria-label="${esc(label)}: ${v} из 10"><circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="var(--track)" stroke-width="7"/>
    <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${scoreCol(v)}" stroke-width="7" stroke-linecap="round" stroke-dasharray="${(c * p).toFixed(1)} ${c.toFixed(1)}" transform="rotate(-90 ${size / 2} ${size / 2})"/></svg>
    <b style="color:${scoreCol(v)}">${Number(v).toFixed(1)}</b></div><div class="ringlbl">${esc(label)}</div></div>`;
}

const pill = (label, v) => `<span class="pill ${scoreCls(v)}">${label} <b>${v}</b>/10</span>`;

async function api(path, body) {
  const res = await fetch(path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Ошибка ${res.status}`);
  return data;
}

async function load() {
  try { D = await api("/api/state"); }
  catch (e) { $("#app").innerHTML = `<div class="card" style="margin-top:40px"><h2>Сервер недоступен</h2><p class="muted">Запусти ./start.sh и обнови страницу.</p></div>`; return; }
  if (!curSession || !D.sessions.find(s => s.session_id === curSession)) curSession = D.sessions.length ? D.sessions[D.sessions.length - 1].session_id : null;
  render();
}

/* ---------- каркас ---------- */
function topbar() {
  const g = D.game;
  return `<header class="topbar"><div class="logo">Interview Coach</div>
    <span class="chip fire" title="Дней подряд">🔥 ${g.streak}</span>
    <span class="chip" title="Опыт">Ур. ${g.level} <span class="xpbar"><i style="width:${g.xp_in_level / g.xp_per_level * 100}%"></i></span> ${g.xp} XP</span></header>`;
}
function tabbar() {
  const t = [["home", "🏠", "Главная"], ["interview", "🎤", "Интервью"], ["topics", "🧩", "Темы"]];
  return `<nav class="tabs">${t.map(([k, i, l]) => `<button class="tab ${view === k ? "on" : ""}" data-act="tab" data-v="${k}"><span>${i}</span>${l}</button>`).join("")}</nav>`;
}

function render() {
  const app = $("#app");
  if (P) { app.innerHTML = playerView(); bindPlayer(); return; }
  app.innerHTML = topbar() + `<main>${({ home, interview, topics })[view]()}</main>` + tabbar();
  window.scrollTo(0, 0);
}

/* ---------- главная ---------- */
function topicCard(k, big) {
  const t = D.progress.topics[k], hasLesson = D.lessons[k], due = D.due.includes(k);
  const dots = `<span class="dots" title="Стадия ${t.stage} из ${D.mastered_stage}">${Array.from({ length: D.mastered_stage }, (_, i) => `<i class="${i < t.stage ? "on" : ""}"></i>`).join("")}</span>`;
  const next = t.status === "mastered" ? "освоено" : t.next_review ? (due ? "тест сегодня" : `тест ${t.next_review}`) : "тест после урока";
  return `<div class="card topic"><div class="grow stack" style="--g:6px">
    <div class="q">${esc(t.name)}</div>
    <div class="row wrap"><span class="badge ${t.type === "grammar" ? "acc" : "warn"}">${t.type === "grammar" ? "Язык" : "Знание"}</span><span class="badge">${STATUS[t.status]}</span>${dots}<span class="small muted">${next}${t.mastery != null ? ` · ${Math.round(t.mastery)}%` : ""}</span></div>
    ${hasLesson ? `<div class="btns"><button class="btn pri small" data-act="lesson" data-k="${esc(k)}">Урок</button>${due ? `<button class="btn acc small" data-act="test" data-k="${esc(k)}">Тест</button>` : ""}</div>` : `<div class="small muted">Урока пока нет. Попроси Claude составить план по этой теме.</div>`}
  </div></div>`;
}

function interviewMini(s) {
  const o = s.overall;
  return `<div class="card"><div class="ringbox">${ring(o.total, 96, "Итог")}<div class="grow stack"><div class="q">Последнее интервью · ${esc(s.date)}</div>
    <div class="row wrap">${pill("Ответы", o.content)}${pill("Английский", o.english)}<span class="badge acc">${esc(s.level.cefr)}</span></div>
    <button class="btn small" data-act="tab" data-v="interview">Подробный разбор</button></div></div></div>`;
}

function home() {
  const g = D.game, lastS = D.sessions[D.sessions.length - 1];
  const dueNow = D.due.filter(k => D.lessons[k]), noLesson = D.due.filter(k => !D.lessons[k]);
  let h = `<div class="card hero stack"><h1>${g.active_today ? "Отлично, сегодня ты уже занимался" : "Пора размяться"}</h1>
    <p class="muted">${g.streak ? `Серия ${g.streak} дн. подряд. ` : "Начни серию сегодня. "}${dueNow.length ? `К проверке готово тем: ${dueNow.length}.` : "На сегодня проверок нет."}</p></div>`;
  if (dueNow.length) h += `<div class="sec">Сегодня</div><div class="stack">${dueNow.map(k => topicCard(k)).join("")}</div>`;
  if (noLesson.length) h += `<p class="small muted" style="margin-top:12px">Ещё ${noLesson.length} тем ждут урока: открой вкладку «Темы».</p>`;
  if (lastS) h += `<div class="sec">Последнее интервью</div>${interviewMini(lastS)}`;
  else h += `<div class="card" style="margin-top:16px"><h3>Пока нет интервью</h3><p class="muted">Напиши Claude в чате: «Разбери интервью» и путь к записи.</p></div>`;
  return h;
}

/* ---------- интервью ---------- */
function interview() {
  const s = D.sessions.find(x => x.session_id === curSession);
  if (!s) return `<div class="card" style="margin-top:16px"><h3>Пока нет интервью</h3><p class="muted">Напиши Claude в чате: «Разбери интервью» и путь к записи.</p></div>`;
  const o = s.overall, ans = s.answers || [];
  const sel = D.sessions.length > 1 ? `<select data-act="session">${D.sessions.map(x => `<option value="${esc(x.session_id)}" ${x.session_id === curSession ? "selected" : ""}>${esc(x.date)} · ${esc(x.role || x.session_id)}</option>`).join("")}</select>` : "";
  let h = `<div class="row between wrap" style="margin-top:6px"><div><h2>Разбор интервью</h2><p class="small muted">${esc(s.role || "")} · ${esc(s.date)}</p></div>${sel}</div>
  <div class="card stack" style="margin-top:12px">
    <div class="ringbox">${ring(o.total, 124, "Итог интервью")}
      <div class="grow stack"><div class="row wrap">${ring(o.content, 72, "Ответы")}${ring(o.english, 72, "Английский")}<div class="grow"></div><span class="badge acc" style="font-size:15px">${esc(s.level.cefr)}</span></div></div></div>
    <p class="small muted">Итог — среднее между правильностью ответов и качеством английского. Small talk не учитывается.</p>
    ${o.summary ? `<p>${esc(o.summary)}</p>` : ""}</div>`;
  h += `<div class="sec">Оценка по вопросам</div><div class="stack">${ans.map(a => `<div class="card stack">
    <div class="row between wrap"><div class="q grow">${esc(a.question)}</div><span class="small muted">${esc(a.timestamp || "")}</span></div>
    <div class="row wrap">${pill("Ответ", a.score_content)}${pill("Английский", a.score_english)}<span class="badge ${a.cause === "none" ? "ok" : "warn"}">${CAUSE[a.cause] || esc(a.cause)}</span>${a.smalltalk ? `<span class="badge">small talk</span>` : ""}</div>
    ${a.answer_summary ? `<p class="small">${esc(a.answer_summary)}</p>` : ""}
    ${a.evidence ? `<p class="ev">${esc(a.evidence)}</p>` : ""}</div>`).join("")}</div>`;
  const errs = s.errors || [], groups = {};
  errs.forEach(e => (groups[e.category] = groups[e.category] || []).push(e));
  h += `<div class="sec">Ошибки (${errs.length})</div><div class="stack">${Object.entries(groups).sort((a, b) => b[1].length - a[1].length).map(([c, list]) => `<div class="card">
    <div class="row between"><h3>${CATS[c] || esc(c)}</h3><span class="badge">${list.length}</span></div>
    ${list.map(e => `<div class="err"><div><span class="x">${esc(e.quote)}</span></div><div><span class="v">${esc(e.correction)}</span> <span class="small muted">${esc(e.timestamp || "")}${e.confidence && e.confidence !== "high" ? " · проверить по аудио" : ""}</span></div><div class="r">${esc(e.rule || "")}</div></div>`).join("")}</div>`).join("")}</div>`;
  if ((s.strengths || []).length) h += `<div class="sec">Сильные стороны</div><div class="card stack">${s.strengths.map(x => `<p>✅ ${esc(x)}</p>`).join("")}</div>`;
  return h;
}

/* ---------- темы ---------- */
function topics() {
  const order = { relapsed: 0, open: 1, learning: 2, mastered: 3 };
  const ks = Object.keys(D.progress.topics).sort((a, b) => order[D.progress.topics[a].status] - order[D.progress.topics[b].status]);
  return `<div style="margin-top:6px"><h2>Темы для проработки</h2><p class="small muted">Тест открывается по графику: на следующий день после урока, потом через 3, 7 и 14 дней. Зачёт — ${D.pass_score}%.</p></div>
    <div class="stack" style="margin-top:12px">${ks.length ? ks.map(k => topicCard(k)).join("") : `<div class="card"><p class="muted">Тем пока нет.</p></div>`}</div>`;
}

/* ---------- проверка ответов ---------- */
function normAns(s, stripSubj) {
  s = String(s).toLowerCase().replace(/[’‘`´]/g, "'")
    .replace(/'ve\b/g, " have").replace(/'m\b/g, " am").replace(/'re\b/g, " are").replace(/'ll\b/g, " will")
    .replace(/[.,!?;:"“”]/g, " ").replace(/\s+/g, " ").trim();
  return stripSubj ? s.replace(/^(i|he|she|we|they|you|it) /, "") : s;
}
function isCorrect(it, val) {
  if (it.type === "choice") return val === it.answer;
  const g = it.type === "gap", n = normAns(val, g);
  return it.accepted.some(a => normAns(a, g) === n);
}
const expected = it => it.type === "choice" ? it.answer : it.accepted[0];

/* ---------- плеер: урок и тест ---------- */
async function openLesson(key, mode) {
  try {
    const L = await api("/api/lesson?key=" + encodeURIComponent(key));
    const items = mode === "test" ? L.test : L.practice;
    if (!items || !items.length) { toast("В этом уроке нет заданий для режима."); return; }
    P = { key, mode, L, items, i: 0, res: [], phase: mode === "test" ? "intro" : "rule", sel: null, checked: false, t0: Date.now() };
    clearInterval(timer);
    if (mode === "test") timer = setInterval(() => { const e = $("#timer"); if (e) e.textContent = fmt(Date.now() - P.t0); }, 1000);
    render();
  } catch (e) { toast(e.message); }
}
const fmt = ms => { const s = Math.floor(ms / 1000); return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; };
function closePlayer() { clearInterval(timer); P = null; load(); }

function phead() {
  const n = P.items.length, done = P.phase === "done" ? n : P.i;
  return `<div class="phead"><button class="x-btn" data-act="close" aria-label="Закрыть">✕</button><div class="bar"><i style="width:${P.phase === "rule" || P.phase === "intro" ? 0 : done / n * 100}%"></i></div>
    ${P.mode === "test" ? `<span class="chip" id="timer">${fmt(Date.now() - P.t0)}</span>` : `<span class="small muted">${Math.min(P.i + 1, n)}/${n}</span>`}</div>`;
}

function playerView() {
  const head = phead();
  if (P.phase === "rule") return `<div class="player">${head}<h1>${esc(P.L.title)}</h1><p class="muted" style="margin:8px 0 16px">${esc(P.L.intro || "")}</p>
    <div class="stack rule">${P.L.rule.map(r => `<div class="card"><h3>${esc(r.h)}</h3><p style="margin-top:6px">${esc(r.p)}</p>${(r.ex || []).map(e => `<div class="ex">${esc(e)}</div>`).join("")}${r.note ? `<p class="note">${esc(r.note)}</p>` : ""}</div>`).join("")}</div>
    <div class="dock"><div class="dock-in"><button class="btn pri wide" data-act="start">Начать · ${P.items.length} заданий</button></div></div>`;
  if (P.phase === "intro") return `<div class="player">${head}<h1>Проверка: ${esc(P.L.title)}</h1><p class="muted" style="margin:8px 0">${P.items.length} вопросов. Без подсказок, результат покажем в конце. Зачёт — ${D.pass_score}%.</p>
    <div class="dock"><div class="dock-in"><button class="btn pri wide" data-act="start">Начать тест</button></div></div>`;
  if (P.phase === "done") return `<div class="player">${head}${P.mode === "test" ? testResult() : practiceResult()}</div>`;
  const it = P.items[P.i], r = P.checked ? P.res[P.i] : null, practice = P.mode === "practice";
  let body = `<div class="prompt">${esc(it.prompt)}</div>${it.hint ? `<p class="sub">${esc(it.hint)}</p>` : ""}`;
  if (it.type === "choice") {
    body += `<div class="opts">${it.options.map((o, idx) => {
      let c = "opt";
      if (r && practice) c += o === it.answer ? " right" : (o === r.val ? " wrong" : "");
      else if (P.sel === o) c += " sel";
      return `<button class="${c}" data-act="pick" data-i="${idx}" ${r && practice ? "disabled" : ""}>${esc(o)}</button>`;
    }).join("")}</div>`;
  } else {
    const c = r && practice ? (r.ok ? " right" : " wrong") : "";
    body += `<input class="answer-input${c}" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Твой ответ" value="${esc(P.sel || "")}" ${r && practice ? "disabled" : ""}>`;
  }
  body += `<div class="errmsg" id="err"></div>`;
  let dock;
  if (r && practice) dock = `<div class="dock ${r.ok ? "right" : "wrong"}"><div class="dock-in"><h3>${r.ok ? "Верно!" : "Почти. Правильно:"}</h3><p>${r.ok ? "" : `<b>${esc(expected(it))}</b><br>`}${esc(it.explain || "")}</p><button class="btn ${r.ok ? "pri" : ""} wide" data-act="next">${P.i + 1 < P.items.length ? "Дальше" : "Завершить"}</button></div></div>`;
  else dock = `<div class="dock"><div class="dock-in"><button class="btn pri wide" data-act="${practice ? "check" : "next"}">${practice ? "Проверить" : (P.i + 1 < P.items.length ? "Дальше" : "Завершить тест")}</button></div></div>`;
  return `<div class="player">${head}${body}${dock}</div>`;
}

function bindPlayer() {
  const inp = $(".answer-input");
  if (inp && !inp.disabled) { setTimeout(() => inp.focus(), 30); inp.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); act(P.mode === "practice" ? "check" : "next"); } }); }
}

function currentVal() {
  const it = P.items[P.i];
  return it.type === "choice" ? P.sel : ($(".answer-input")?.value ?? "").trim();
}

function commit() {
  const it = P.items[P.i], val = currentVal();
  if (!val) { $("#err").textContent = it.type === "choice" ? "Выбери вариант" : "Введи ответ"; return false; }
  P.res[P.i] = { val, ok: isCorrect(it, val) };
  return true;
}

function practiceResult() {
  const ok = P.res.filter(r => r.ok).length, n = P.items.length;
  const test = D.due.includes(P.key) && D.lessons[P.key];
  return `<div class="stack center" style="padding-top:24px"><div class="big">${ok} из ${n}</div><p class="muted">${ok === n ? "Без ошибок!" : "Ошибки — это материал для следующего захода."}</p>
    <p class="chip" id="xpgain" style="justify-content:center">+${ok * 5 + 20} XP</p>
    <div class="stack" style="margin-top:16px">${test ? `<button class="btn pri wide" data-act="retest">Пройти тест</button>` : ""}<button class="btn wide" data-act="restart">Ещё раз</button><button class="btn wide" data-act="close">На главную</button></div></div>`;
}

function testResult() {
  const R = P.result, items = P.items;
  const rows = items.map((it, i) => {
    const r = P.res[i];
    return `<div class="result-row"><div class="row between"><div class="q grow">${i + 1}. ${esc(it.prompt)}</div><span class="badge ${r.ok ? "ok" : "bad"}">${r.ok ? "верно" : "ошибка"}</span></div>
      ${r.ok ? "" : `<p class="small" style="margin-top:4px">Ты: <span style="color:var(--bad)">${esc(r.val)}</span><br>Правильно: <b>${esc(expected(it))}</b></p>`}
      <p class="small muted">${esc(it.explain || "")}</p>
      ${!r.ok && it.type !== "choice" ? `<button class="btn small" style="margin-top:6px" data-act="review" data-i="${i}" ${r.sent ? "disabled" : ""}>${r.sent ? "Отправлено Claude" : "Мой вариант тоже верный"}</button>` : ""}</div>`;
  }).join("");
  const t = R.topic;
  return `<div class="stack center" style="padding-top:16px"><div class="big" style="color:var(--${R.passed ? "ok" : "bad"})">${R.score}%</div>
    <h2>${R.passed ? "Зачёт!" : "Пока не зачёт"}</h2>
    <p class="muted">${R.passed ? `Стадия ${t.stage} из ${D.mastered_stage}. Следующая проверка: ${esc(t.next_review)}.` : `Нужно ${D.pass_score}%. Повтори урок и приходи завтра: ${esc(t.next_review)}.`}</p>
    <span class="chip" style="justify-content:center">+${R.xp_gained} XP</span></div>
    <div class="sec">Разбор</div><div class="card" style="padding:4px 16px">${rows}</div>
    <p class="small muted" style="margin-top:10px">Балл посчитан по точному совпадению с ключом. Если твой вариант тоже верный, отправь его Claude: он проверит в чате.</p>
    <div class="stack" style="margin-top:14px;padding-bottom:20px"><button class="btn pri wide" data-act="close">На главную</button></div>`;
}

async function finishPractice() {
  P.phase = "done";
  const ok = P.res.filter(r => r.ok).length;
  render();
  try { await api("/api/practice", { topic: P.key, correct: ok, total: P.items.length }); } catch (e) { toast(e.message); }
}

async function finishTest() {
  const ok = P.res.filter(r => r.ok).length;
  clearInterval(timer);
  try {
    P.result = await api("/api/test", { topic: P.key, correct: ok, total: P.items.length });
    P.phase = "done"; render();
  } catch (e) { toast(e.message); }
}

/* ---------- действия ---------- */
async function act(a, el) {
  if (a === "tab") { view = el.dataset.v; render(); }
  else if (a === "lesson") openLesson(el.dataset.k, "practice");
  else if (a === "test") openLesson(el.dataset.k, "test");
  else if (a === "close") closePlayer();
  else if (a === "start") { P.phase = "run"; P.t0 = Date.now(); render(); }
  else if (a === "pick") { if (!P.checked || P.mode === "test") { P.sel = P.items[P.i].options[+el.dataset.i]; render(); } }
  else if (a === "check") { if (commit()) { P.checked = true; render(); } }
  else if (a === "next") {
    if (P.mode === "test" && !P.checked) { if (!commit()) return; }
    P.i += 1; P.sel = null; P.checked = false;
    if (P.i >= P.items.length) { P.i = P.items.length - 1; return P.mode === "test" ? finishTest() : finishPractice(); }
    render();
  }
  else if (a === "restart") { const k = P.key; clearInterval(timer); P = null; openLesson(k, "practice"); }
  else if (a === "retest") { const k = P.key; clearInterval(timer); P = null; openLesson(k, "test"); }
  else if (a === "review") {
    const i = +el.dataset.i, it = P.items[i], r = P.res[i];
    try { await api("/api/review", { topic: P.key, prompt: it.prompt, answer: r.val, expected: expected(it) }); r.sent = true; render(); toast("Отправлено. Попроси Claude проверить ответы из приложения."); }
    catch (e) { toast(e.message); }
  }
}

document.addEventListener("click", e => {
  const el = e.target.closest("[data-act]");
  if (el && el.tagName !== "SELECT" && !el.disabled) act(el.dataset.act, el);
});
document.addEventListener("change", e => {
  if (e.target.dataset?.act === "session") { curSession = e.target.value; render(); }
});
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && P && P.phase !== "done") closePlayer();
  else if (e.key === "Enter" && P && P.phase === "run" && P.mode === "practice" && P.checked && document.activeElement?.tagName !== "BUTTON") act("next");
});

load();
