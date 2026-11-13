"use strict";
const $ = (s, r = document) => r.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const CAUSE = { language: "Язык", question_understanding: "Не понял вопрос", knowledge: "Знание темы", mixed: "Смешанная", none: "Ответ хороший" };
const CATS = { tenses: "Времена", articles: "Артикли", prepositions: "Предлоги", word_order: "Порядок слов", agreement: "Согласование", modals_conditionals: "Модальные и условные", passive_voice: "Пассив", verb_forms: "Формы глагола", sentence_structure: "Структура фраз", vocabulary: "Лексика", discourse: "Связность", fluency: "Беглость" };
const STATUS = { open: "Новая", learning: "Учу", mastered: "Освоено", relapsed: "Вернулась" };
const MODEL_HINT = { tiny: "очень быстро, но неточно", base: "быстро, средняя точность", small: "оптимально: быстро и точно", medium: "точнее, но заметно медленнее" };

let D = null, view = "home", P = null, timer = null, curSession = null, curProject = null, poll = null;
let U = null;           // состояние формы загрузки
let rec = null;         // идущая запись с микрофона

/* ---------- утилиты ---------- */
const scoreCls = v => (v >= 8 ? "ok" : v >= 5 ? "warn" : "bad");
const scoreCol = v => `var(--${scoreCls(v)})`;
const projects = () => D.progress.projects;
const projOf = pid => projects()[pid] || { name: pid, sessions: [] };
const analyzed = pid => D.sessions.filter(s => s.project === pid);
const fmtSize = b => b > 1048576 ? (b / 1048576).toFixed(1) + " МБ" : Math.max(1, Math.round(b / 1024)) + " КБ";
const fmtT = s => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;

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

function copyText(t) {
  const done = () => toast("Скопировано. Вставь в чат с Claude.");
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(t).then(done, () => fallbackCopy(t, done));
  else fallbackCopy(t, done);
}
function fallbackCopy(t, done) {
  const ta = document.createElement("textarea"); ta.value = t; document.body.appendChild(ta); ta.select();
  try { document.execCommand("copy"); done(); } catch { toast("Не удалось скопировать, выдели текст вручную."); }
  ta.remove();
}

async function load() {
  try { D = await api("/api/state"); }
  catch (e) { $("#app").innerHTML = `<div class="card" style="margin-top:40px"><h2>Сервер недоступен</h2><p class="muted">Запусти ./start.sh и обнови страницу.</p></div>`; return; }
  if (!curSession || !D.sessions.find(s => s.session_id === curSession)) curSession = D.sessions.length ? D.sessions[D.sessions.length - 1].session_id : null;
  if (curProject && !projects()[curProject]) curProject = null;
  render(); schedulePoll();
}

/* ---------- фоновый опрос расшифровок ---------- */
const active = () => (D?.pending || []).some(m => m.status === "transcribing" || m.status === "uploaded");
function schedulePoll() {
  if (active() && !poll) poll = setInterval(tick, 1500);
  if (!active() && poll) { clearInterval(poll); poll = null; }
}
async function tick() {
  try {
    const was = new Set(D.pending.filter(m => m.status === "transcribing" || m.status === "uploaded").map(m => m.id));
    const r = await api("/api/jobs");
    D.jobs = r.jobs; D.pending = r.pending;
    const finished = [...was].some(id => !r.pending.find(m => m.id === id && (m.status === "transcribing" || m.status === "uploaded")));
    if (finished) { await load(); return; }
    schedulePoll();
    if (["home", "projects", "project", "status"].includes(view) && !P) render(true);
  } catch { /* временный сбой — попробуем на следующем тике */ }
}

/* ---------- каркас ---------- */
function topbar() {
  const g = D.game;
  return `<header class="topbar"><div class="logo">Interview Coach</div>
    <span class="chip fire" title="Дней подряд">🔥 ${g.streak}</span>
    <span class="chip" title="Опыт">Ур. ${g.level} <span class="xpbar"><i style="width:${g.xp_in_level / g.xp_per_level * 100}%"></i></span> ${g.xp} XP</span></header>`;
}
function tabbar() {
  const on = v => ({ home: "home", projects: "projects", project: "projects", interview: "projects", topics: "topics", vocab: "vocab" }[view] === v ? "on" : "");
  return `<nav class="tabs"><button class="tab ${on("home")}" data-act="tab" data-v="home"><span>🏠</span>Главная</button>
    <button class="tab ${on("projects")}" data-act="tab" data-v="projects"><span>📁</span>Проекты</button>
    <button class="tab add ${view === "upload" || view === "status" ? "on" : ""}" data-act="newrec"><span>＋</span>Запись</button>
    <button class="tab ${on("topics")}" data-act="tab" data-v="topics"><span>🧩</span>Темы</button>
    <button class="tab ${on("vocab")}" data-act="tab" data-v="vocab"><span>📖</span>Словарь</button></nav>`;
}

function render(keepScroll) {
  const app = $("#app");
  if (P) { app.innerHTML = playerView(); bindPlayer(); return; }
  if (V) { app.innerHTML = vplayerView(); bindV(); return; }
  if (C) { app.innerHTML = cardsView(); return; }
  const y = window.scrollY;
  const v = { home, projects: projectsView, project: projectView, interview, topics, upload: uploadView, status: statusView, vocab: vocabView }[view];
  app.innerHTML = topbar() + `<main>${v()}</main>` + tabbar();
  if (view === "upload") bindUpload();
  window.scrollTo(0, keepScroll ? y : 0);
}

/* ---------- темы ---------- */
function topicCard(k, showProject) {
  const t = D.progress.topics[k], hasLesson = D.lessons[k], due = D.due.includes(k);
  const dots = `<span class="dots" title="Стадия ${t.stage} из ${D.mastered_stage}">${Array.from({ length: D.mastered_stage }, (_, i) => `<i class="${i < t.stage ? "on" : ""}"></i>`).join("")}</span>`;
  const next = t.status === "mastered" ? "освоено" : t.next_review ? (due ? "тест сегодня" : `тест ${t.next_review}`) : "тест после урока";
  return `<div class="card topic"><div class="grow stack" style="--g:6px">
    <div class="q">${esc(t.name)}</div>
    <div class="row wrap">${showProject ? `<span class="badge">${esc(projOf(t.project).name)}</span>` : ""}<span class="badge ${t.type === "grammar" ? "acc" : "warn"}">${t.type === "grammar" ? "Язык" : "Знание"}</span><span class="badge">${STATUS[t.status]}</span>${dots}<span class="small muted">${next}${t.mastery != null ? ` · ${Math.round(t.mastery)}%` : ""}</span></div>
    ${hasLesson ? `<div class="btns"><button class="btn pri small" data-act="lesson" data-k="${esc(k)}">Урок</button>${due ? `<button class="btn acc small" data-act="test" data-k="${esc(k)}">Тест</button>` : ""}</div>` : `<div class="small muted">Урока пока нет. Напиши Claude: «составь уроки для проекта ${esc(projOf(t.project).name)}».</div>`}
  </div></div>`;
}

/* ---------- ожидающие записи ---------- */
function claudePrompt(m) {
  const p = esc0(m.project_name || projOf(m.project).name);
  return m.mode === "new"
    ? `Разбери интервью ${m.id}. Проект «${p}» новый: после разбора составь план и уроки.`
    : `Разбери интервью ${m.id} из проекта «${p}» и скорректируй план с учётом прошлых интервью.`;
}
const esc0 = s => String(s ?? "").replace(/[«»]/g, "");

function jobBlock(m) {
  const j = D.jobs[m.id] || {};
  if (m.status === "failed") return `<div class="warnbox"><p class="small">Не получилось расшифровать: ${esc(m.error || "неизвестная ошибка")}</p><button class="btn small" data-act="retry" data-id="${esc(m.id)}">Повторить</button></div>`;
  if (m.status === "transcribed") return `<div class="promptbox"><p class="small"><b>Расшифровка готова.</b> Скопируй запрос и отправь Claude в чате: он разберёт интервью${m.mode === "new" ? " и составит план" : " и скорректирует план"}.</p>
    <code>${esc(claudePrompt(m))}</code><button class="btn pri small" data-act="copy" data-t="${esc(claudePrompt(m))}">Скопировать запрос</button></div>`;
  const stage = j.stage || "queued";
  const pct = j.dur ? Math.min(100, Math.round(j.pos / j.dur * 100)) : 0;
  const label = stage === "queued" ? "В очереди…" : stage === "loading" ? "Загружаю модель…" : j.dur ? `Расшифровка ${fmtT(j.pos)} из ${fmtT(j.dur)}` : "Расшифровываю…";
  return `<div class="stack"><div class="bar" style="height:10px"><i style="width:${stage === "transcribing" ? pct : 4}%"></i></div><p class="small muted">${label}</p></div>`;
}

function pendingCard(m, withProject) {
  return `<div class="card stack"><div class="row between wrap"><div class="q grow">${esc(m.title || m.id)}</div>${withProject ? `<span class="badge">${esc(m.project_name)}</span>` : ""}<span class="badge warn">ждёт разбора</span></div>${jobBlock(m)}</div>`;
}

/* ---------- главная ---------- */
function home() {
  const g = D.game, dueNow = D.due.filter(k => D.lessons[k]), pend = D.pending;
  let h = `<div class="card hero stack"><h1>${g.active_today ? "Отлично, сегодня ты уже занимался" : "Пора размяться"}</h1>
    <p class="muted">${g.streak ? `Серия ${g.streak} дн. подряд. ` : "Начни серию сегодня. "}${dueNow.length ? `К проверке готово тем: ${dueNow.length}.` : "На сегодня проверок нет."}</p>
    <button class="btn acc wide" data-act="newrec">＋ Новая запись интервью</button></div>`;
  if (pend.length) h += `<div class="sec">Записи в обработке</div><div class="stack">${pend.map(m => pendingCard(m, true)).join("")}</div>`;
  if (dueNow.length) h += `<div class="sec">Сегодня</div><div class="stack">${dueNow.map(k => topicCard(k, true)).join("")}</div>`;
  const vi = vitems();
  if (vi.length) {
    const cnt = s => vi.filter(w => vst(w.id).status === s).length;
    h += `<div class="sec">Словарь</div><div class="card topic"><div class="grow stack" style="--g:4px"><div class="q">Слова из твоих интервью</div>
      <div class="small muted">На изучении: ${cnt("learning")} · новых: ${cnt("new")} · выучено: ${cnt("learned") + cnt("known")}</div></div>
      <button class="btn pri small" data-act="vtest" data-m="new">Тест на слова</button></div>`;
  }
  const ps = Object.keys(projects());
  h += `<div class="sec">Проекты</div>`;
  h += ps.length ? `<div class="stack">${ps.map(projectCard).join("")}</div>` : `<div class="card"><h3>Пока нет проектов</h3><p class="muted" style="margin:6px 0 12px">Загрузи или запиши интервью: проект создастся вместе с первой записью.</p><button class="btn pri" data-act="newrec">Добавить запись</button></div>`;
  return h;
}

function projectCard(pid) {
  const p = projOf(pid), S = analyzed(pid), last = S[S.length - 1];
  const due = D.due.filter(k => D.progress.topics[k].project === pid).length;
  return `<div class="card topic" data-act="openproject" data-p="${esc(pid)}" style="cursor:pointer">
    ${last ? ring(last.overall.total, 64, "") : `<div class="ring" style="width:64px;height:64px;font-size:12px;color:var(--muted)">нет</div>`}
    <div class="grow stack" style="--g:4px"><div class="q">${esc(p.name)}</div>
    <div class="small muted">Записей: ${p.sessions.length}${last ? ` · итог ${last.overall.total}/10 · ${esc(last.level.cefr)}` : " · ждёт разбора"}${due ? ` · к проверке: ${due}` : ""}</div></div><span class="muted">›</span></div>`;
}

/* ---------- проекты ---------- */
function projectsView() {
  const ps = Object.keys(projects());
  return `<div class="row between" style="margin-top:6px"><h2>Проекты</h2><button class="btn pri small" data-act="newrec">＋ Запись</button></div>
    <p class="small muted" style="margin-top:4px">Проект — одна цель, например одно собеседование. В нём может быть несколько записей и общий план.</p>
    <div class="stack" style="margin-top:12px">${ps.length ? ps.map(projectCard).join("") : `<div class="card"><p class="muted">Проектов пока нет.</p></div>`}</div>`;
}

function trendSvg(vals) {
  if (vals.length < 2) return "";
  const w = 120, h = 36, lo = Math.min(...vals, 0), hi = 10;
  const pts = vals.map((v, i) => `${(i / (vals.length - 1) * (w - 8) + 4).toFixed(1)},${(h - 4 - (v - lo) / (hi - lo) * (h - 8)).toFixed(1)}`);
  return `<svg width="${w}" height="${h}" role="img" aria-label="Динамика итоговых оценок"><polyline points="${pts.join(" ")}" fill="none" stroke="var(--acc)" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>${pts.map(p => `<circle cx="${p.split(",")[0]}" cy="${p.split(",")[1]}" r="3.5" fill="var(--acc)"/>`).join("")}</svg>`;
}

function projectView() {
  const pid = curProject, p = projects()[pid];
  if (!p) return `<div class="card"><p class="muted">Проект не найден.</p></div>`;
  const S = analyzed(pid), pend = D.pending.filter(m => m.project === pid);
  const order = p.sessions;
  const tops = Object.keys(D.progress.topics).filter(k => D.progress.topics[k].project === pid);
  let h = `<div class="row" style="margin-top:6px"><button class="x-btn" data-act="tab" data-v="projects" aria-label="Назад">‹</button><div class="grow"><h2>${esc(p.name)}</h2><p class="small muted">Создан ${esc(p.created)} · записей: ${order.length}</p></div>
    <button class="btn pri small" data-act="newrec" data-p="${esc(pid)}">＋ Запись</button></div>`;
  if (S.length > 1) {
    const first = S[0].overall.total, last = S[S.length - 1].overall.total, d = Math.round((last - first) * 10) / 10;
    h += `<div class="card row" style="margin-top:12px"><div class="grow"><div class="q">Динамика итога</div><p class="small muted">${first} → ${last} (${d >= 0 ? "+" : ""}${d}) за ${S.length} записи(ей)</p></div>${trendSvg(S.map(s => s.overall.total))}</div>`;
  }
  h += `<div class="sec">Записи</div><div class="stack">${order.length ? order.map(sid => {
    const s = S.find(x => x.session_id === sid), m = pend.find(x => x.id === sid);
    if (s) return `<div class="card topic" data-act="openinterview" data-s="${esc(sid)}" style="cursor:pointer">${ring(s.overall.total, 64, "")}<div class="grow stack" style="--g:4px">
      <div class="q">${esc(s.title || s.role || sid)}</div><div class="row wrap">${pill("Ответ", s.overall.content)}${pill("Англ.", s.overall.english)}<span class="badge acc">${esc(s.level.cefr)}</span></div>
      <div class="small muted">${esc(s.date)}</div></div><span class="muted">›</span></div>`;
    if (m) return pendingCard(m, false);
    return `<div class="card"><div class="q">${esc(sid)}</div><p class="small muted">Запись без данных.</p></div>`;
  }).join("") : `<div class="card"><p class="muted">Записей нет.</p></div>`}</div>`;
  h += `<div class="sec">План проекта: темы</div><div class="stack">${tops.length ? tops.map(k => topicCard(k, false)).join("") : `<div class="card"><p class="muted">Тем пока нет: план появится после разбора первой записи.</p></div>`}</div>`;
  return h;
}

/* ---------- разбор интервью ---------- */
function interview() {
  const s = D.sessions.find(x => x.session_id === curSession);
  if (!s) return `<div class="card" style="margin-top:16px"><h3>Нет разбора</h3><p class="muted">Загрузи запись и попроси Claude разобрать интервью.</p></div>`;
  const o = s.overall, ans = s.answers || [], pr = projOf(s.project);
  let h = `<div class="row" style="margin-top:6px"><button class="x-btn" data-act="openproject" data-p="${esc(s.project)}" aria-label="Назад">‹</button><div class="grow"><h2>${esc(s.title || "Разбор интервью")}</h2><p class="small muted">${esc(pr.name)} · ${esc(s.date)}${s.role ? " · " + esc(s.role) : ""}</p></div></div>
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

/* ---------- темы (все проекты) ---------- */
function topics() {
  const order = { relapsed: 0, open: 1, learning: 2, mastered: 3 };
  let h = `<div style="margin-top:6px"><h2>Темы для проработки</h2><p class="small muted">Тест открывается по графику: на следующий день после урока, потом через 3, 7 и 14 дней. Зачёт — ${D.pass_score}%.</p></div>`;
  const ps = Object.keys(projects());
  if (!ps.length) return h + `<div class="card" style="margin-top:12px"><p class="muted">Тем пока нет.</p></div>`;
  ps.forEach(pid => {
    const ks = Object.keys(D.progress.topics).filter(k => D.progress.topics[k].project === pid).sort((a, b) => order[D.progress.topics[a].status] - order[D.progress.topics[b].status]);
    h += `<div class="sec">${esc(projOf(pid).name)}</div><div class="stack">${ks.length ? ks.map(k => topicCard(k, false)).join("") : `<div class="card"><p class="muted">План появится после разбора записи.</p></div>`}</div>`;
  });
  return h;
}

/* ---------- загрузка и запись ---------- */
function newUpload(pid) {
  const ids = Object.keys(projects());
  U = { mode: pid ? "existing" : "new", project: pid || ids[0] || "",
        pname: "", title: "", source: "file", file: null, blob: null, blobName: "", blobUrl: null,
        model: "small", device: "auto", err: "", uploading: false, pct: 0 };
}

function uploadView() {
  const ps = Object.keys(projects()), hasP = ps.length > 0;
  const seg = (name, val, items) => `<div class="seg">${items.map(([v, l, dis]) => `<button class="${val === v ? "on" : ""}" data-act="${name}" data-v="${v}" ${dis ? "disabled" : ""}>${l}</button>`).join("")}</div>`;
  const hint = U.mode === "new" ? "Для нового проекта Claude составит новый план." : "Claude учтёт прошлые записи проекта и скорректирует его план.";
  const fileInfo = U.file ? `<p class="small" style="margin-top:8px"><b>${esc(U.file.name)}</b> · ${fmtSize(U.file.size)}</p>` : "";
  let src;
  if (U.source === "file") {
    src = `<label class="drop" id="drop" for="fileinp"><div style="font-size:30px">🎧</div><div class="q">Выбрать файл с записью</div><div class="small muted">или перетащи его сюда · mp3, m4a, wav, mp4, webm</div>${fileInfo}</label>
      <input type="file" id="fileinp" accept="audio/*,video/*,.m4a,.mp3,.wav,.mp4,.webm,.ogg,.flac,.aac,.mov" hidden>`;
  } else if (rec) {
    src = `<div class="card center stack"><div class="big" style="color:var(--bad)" id="rectime">00:00</div><p class="muted">Идёт запись… Говори как на интервью.</p><button class="btn pri wide" data-act="recstop" style="background:var(--bad);border-color:#a8431f">■ Остановить</button></div>`;
  } else if (U.blob) {
    src = `<div class="card stack"><div class="q">Запись готова · ${fmtSize(U.blob.size)}</div><audio controls src="${U.blobUrl}" style="width:100%"></audio><button class="btn small" data-act="recreset">Записать заново</button></div>`;
  } else {
    src = `<div class="card center stack"><div style="font-size:30px">🎙️</div><p class="muted">Браузер попросит доступ к микрофону. Запись остаётся на этом компьютере.</p><button class="btn pri" data-act="recstart">● Начать запись</button></div>`;
  }
  return `<div style="margin-top:6px"><h2>Новая запись</h2><p class="small muted">Каждая запись — отдельное интервью. Проект может состоять из нескольких записей.</p></div>
  <div class="stack" style="margin-top:12px">
   <div class="card stack"><h3>1. Проект</h3>
    ${seg("pmode", U.mode, [["new", "Новый проект"], ["existing", "Добавить в существующий", !hasP]])}
    ${U.mode === "new"
      ? `<input class="field" id="pname" type="text" maxlength="80" placeholder="Название проекта, например: Собеседование в Acme" value="${esc(U.pname)}">`
      : `<select class="field" id="pproj">${ps.map(pid => `<option value="${esc(pid)}" ${pid === U.project ? "selected" : ""}>${esc(projOf(pid).name)} · записей: ${projOf(pid).sessions.length}</option>`).join("")}</select>`}
    <p class="small muted">${hint}</p>
    <input class="field" id="rtitle" type="text" maxlength="80" placeholder="Название записи (необязательно), например: технический раунд" value="${esc(U.title)}"></div>
   <div class="card stack"><h3>2. Запись</h3>${seg("src", U.source, [["file", "Загрузить файл"], ["mic", "Записать с микрофона"]])}${src}</div>
   <details class="card"><summary class="q" style="cursor:pointer">Дополнительно</summary><div class="stack" style="margin-top:12px">
    <label class="small muted">Модель расшифровки</label>
    <select class="field" id="model">${Object.keys(MODEL_HINT).map(m => `<option value="${m}" ${U.model === m ? "selected" : ""}>${m} — ${MODEL_HINT[m]}</option>`).join("")}</select>
    <label class="small muted">Ускорение</label>
    <select class="field" id="device"><option value="auto" ${U.device === "auto" ? "selected" : ""}>Авто: GPU Apple (MPS), если доступен</option><option value="cpu" ${U.device === "cpu" ? "selected" : ""}>Только процессор (CPU)</option></select></div></details>
   <div class="errmsg" id="uerr">${esc(U.err)}</div>
   ${U.uploading ? `<div class="stack"><div class="bar" style="height:12px"><i style="width:${U.pct}%"></i></div><p class="small muted center">Загрузка… ${U.pct}%</p></div>`
      : `<button class="btn pri wide" data-act="submit" ${rec ? "disabled" : ""}>Загрузить и расшифровать</button>`}
  </div>`;
}

function bindUpload() {
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  on("#pname", "input", e => { U.pname = e.target.value; });
  on("#pproj", "change", e => { U.project = e.target.value; });
  on("#rtitle", "input", e => { U.title = e.target.value; });
  on("#model", "change", e => { U.model = e.target.value; });
  on("#device", "change", e => { U.device = e.target.value; });
  on("#fileinp", "change", e => { setFile(e.target.files[0]); });
  const drop = $("#drop");
  if (drop) {
    ["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", e => setFile(e.dataTransfer.files[0]));
  }
}
function setFile(f) {
  if (!f) return;
  U.file = f; U.err = "";
  if (!U.title) U.title = f.name.replace(/\.[^.]+$/, "");
  render(true);
}

async function recStart() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { U.err = "Этот браузер не умеет записывать звук. Загрузи файл."; render(true); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const types = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"];
    const mime = types.find(t => MediaRecorder.isTypeSupported(t)) || "";
    const mr = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined), chunks = [];
    mr.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
    mr.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      clearInterval(rec.tick);
      const type = mr.mimeType || mime || "audio/webm";
      const ext = type.includes("mp4") ? "m4a" : type.includes("ogg") ? "ogg" : "webm";
      U.blob = new Blob(chunks, { type }); U.blobName = `recording.${ext}`;
      if (U.blobUrl) URL.revokeObjectURL(U.blobUrl);
      U.blobUrl = URL.createObjectURL(U.blob);
      if (!U.title) U.title = "Запись с микрофона";
      rec = null; render(true);
    };
    mr.start(1000);
    rec = { mr, t0: Date.now(), tick: setInterval(() => { const e = $("#rectime"); if (e) e.textContent = fmtT((Date.now() - rec.t0) / 1000); }, 500) };
    U.err = ""; render(true);
  } catch (e) {
    U.err = e.name === "NotAllowedError" ? "Нет доступа к микрофону. Разреши его в настройках браузера или загрузи файл." : `Не удалось включить микрофон: ${e.message}`;
    render(true);
  }
}
function recStop() { if (rec) rec.mr.stop(); }

function submitUpload() {
  const body = U.source === "file" ? U.file : U.blob;
  const name = U.source === "file" ? U.file?.name : U.blobName;
  if (U.mode === "new" && !U.pname.trim()) { U.err = "Назови проект."; return render(true); }
  if (U.mode === "existing" && !U.project) { U.err = "Выбери проект."; return render(true); }
  if (!body) { U.err = U.source === "file" ? "Выбери файл с записью." : "Сначала запиши звук."; return render(true); }
  const q = new URLSearchParams({ filename: name, mode: U.mode, model: U.model, device: U.device, title: U.title.trim() });
  q.set(U.mode === "new" ? "project_name" : "project", U.mode === "new" ? U.pname.trim() : U.project);
  U.uploading = true; U.pct = 0; U.err = ""; render(true);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", "/api/upload?" + q.toString());
  xhr.setRequestHeader("Content-Type", "application/octet-stream");
  xhr.upload.onprogress = e => {
    if (!e.lengthComputable) return;
    U.pct = Math.round(e.loaded / e.total * 100);
    const b = $(".bar > i"); if (b) b.style.width = U.pct + "%";
    const t = $(".center.small"); if (t) t.textContent = `Загрузка… ${U.pct}%`;
  };
  xhr.onerror = () => { U.uploading = false; U.err = "Не удалось отправить файл: сервер недоступен."; render(true); };
  xhr.onload = async () => {
    let r = {}; try { r = JSON.parse(xhr.responseText); } catch { /* не JSON */ }
    U.uploading = false;
    if (xhr.status !== 200) { U.err = r.error || `Ошибка ${xhr.status}`; return render(true); }
    U.done = r; curProject = r.project; view = "status";
    await load();
  };
  xhr.send(body);
}

function statusView() {
  const r = U?.done;
  if (!r) return `<div class="card"><p class="muted">Нет активной загрузки.</p></div>`;
  const m = D.pending.find(x => x.id === r.session_id);
  const doneM = !m ? analyzed(r.project).find(s => s.session_id === r.session_id) : null;
  return `<div style="margin-top:6px"><h2>Запись принята</h2><p class="small muted">Проект «${esc(r.project_name)}» · ${r.mode === "new" ? "новый проект" : "добавлена в существующий"}</p></div>
    <div class="stack" style="margin-top:12px">${m ? pendingCard(m, false) : `<div class="card"><p class="muted">${doneM ? "Интервью уже разобрано." : "Статус записи неизвестен."}</p></div>`}
    <div class="btns"><button class="btn" data-act="openproject" data-p="${esc(r.project)}">Открыть проект</button><button class="btn" data-act="newrec" data-p="${esc(r.project)}">Добавить ещё запись</button></div>
    <p class="small muted">Расшифровка идёт в фоне. Страницу можно закрыть: запись появится в проекте.</p></div>`;
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
  if (inp && !inp.disabled) { setTimeout(() => inp.focus(), 30); inp.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); if (!e.repeat) act(P.mode === "practice" ? "check" : "next"); } }); }
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

/* ---------- словарь ---------- */
let V = null, C = null, vtab = "learning";
const openW = new Set();
const vitems = () => D?.vocab?.items || [];
const vst = id => D.vocab.state.words[id] || { status: "new", seen: 0, correct: 0, wrong: 0, streak: 0 };
const VSTAT = { new: "новое", known: "знаю", learning: "учу", learned: "выучено" };
const PRAISE = ["Молодец!", "Отлично!", "Верно, так держать!", "Супер!"];
const shuffle = a => { const r = a.slice(); for (let i = r.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [r[i], r[j]] = [r[j], r[i]]; } return r; };
const normRu = s => String(s).toLowerCase().replace(/ё/g, "е").replace(/[.,;:!?()"«»'’\-—]/g, " ").replace(/\s+/g, " ").trim();
const isRuOk = (w, val) => { const n = normRu(val); return !!n && w.accepted.some(a => normRu(a) === n); };

let voiceChoice = "auto:US";
try { voiceChoice = localStorage.getItem("voice") || "auto:" + (localStorage.getItem("accent") || "US"); } catch { /* без хранилища */ }
let voiceList = [];
const loadVoices = () => {
  if (!("speechSynthesis" in window)) return;
  voiceList = speechSynthesis.getVoices();
  if (D && view === "vocab" && !P && !V && !C) render(true);
};
loadVoices();
if ("speechSynthesis" in window) speechSynthesis.addEventListener("voiceschanged", loadVoices);
const VOICE_PREF = {
  "en-US": ["Samantha", "Саманта", "Ava", "Ава", "Allison", "Эллисон", "Google US English", "Aaron", "Аарон", "Nicky", "Alex", "Алекс", "Tom", "Том"],
  "en-GB": ["Daniel", "Дэниэл", "Serena", "Серена", "Kate", "Кейт", "Oliver", "Оливер", "Stephanie", "Google UK English Female", "Google UK English Male"]
};
// шуточные и «звуковые» голоса macOS (названия бывают на русском) исключаем
const VOICE_JUNK = ["Eddy", "Flo", "Grandma", "Grandpa", "Reed", "Rocko", "Sandy", "Shelley", "Albert", "Альберт", "Bahh", "Бах", "Bells", "Колокольчик", "Boing", "Воббл",
  "Bubbles", "Пузырьки", "Cellos", "Виолончель", "Jester", "Шутник", "Junior", "Джуниор", "Kathy", "Кэти", "Organ", "Орган", "Ralph", "Ральф", "Superstar", "Суперзвезда",
  "Trinoids", "Триноид", "Whisper", "Шепот", "Zarvox", "Зарвокс", "Fred", "Фред", "Good News", "Хорошие новости", "Bad News", "Плохие новости", "Hysterical", "Pipe", "Прыг-скок", "Wobble"];
function pickVoice(lang) {
  const norm = v => v.lang.replace("_", "-");
  const byLang = voiceList.filter(v => norm(v) === lang && !VOICE_JUNK.some(j => v.name.includes(j)));
  const best = (VOICE_PREF[lang] || []).map(p => byLang.find(v => v.name.includes(p))).find(Boolean);
  return best || byLang[0] || voiceList.find(v => v.lang.startsWith("en") && !VOICE_JUNK.some(j => v.name.includes(j)));
}
function speak(text) {
  if (!("speechSynthesis" in window)) { toast("Браузер не умеет озвучивать текст."); return; }
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.9;
  let v = null;
  if (voiceChoice.startsWith("voice:")) {
    const [name, lang] = voiceChoice.slice(6).split("|");
    v = voiceList.find(x => x.name === name && x.lang === lang) || null;
  }
  if (v) { u.voice = v; u.lang = v.lang; }
  else {
    const lang = voiceChoice === "auto:UK" ? "en-GB" : "en-US";
    u.lang = lang;
    const a = pickVoice(lang);
    if (a) u.voice = a;
  }
  speechSynthesis.speak(u);
}
function voiceSelect() {
  const en = voiceList.filter(v => v.lang.toLowerCase().startsWith("en"));
  const junk = v => VOICE_JUNK.some(j => v.name.includes(j));
  const opt = v => { const val = "voice:" + v.name + "|" + v.lang; return `<option value="${esc(val)}" ${voiceChoice === val ? "selected" : ""}>${esc(v.name.split(" (")[0])} · ${esc(v.lang)}</option>`; };
  const good = en.filter(v => !junk(v)), fun = en.filter(junk);
  return `<select id="voice" aria-label="Голос озвучки"><option value="auto:US" ${voiceChoice === "auto:US" ? "selected" : ""}>Авто · US</option><option value="auto:UK" ${voiceChoice === "auto:UK" ? "selected" : ""}>Авто · UK</option>
    ${good.length ? `<optgroup label="Голоса устройства">${good.map(opt).join("")}</optgroup>` : ""}${fun.length ? `<optgroup label="Шуточные голоса">${fun.map(opt).join("")}</optgroup>` : ""}</select>`;
}
const spk = t => `<button class="spk" data-act="speak" data-w="${esc(t)}" aria-label="Прослушать: ${esc(t)}" title="Прослушать">🔊</button>`;

function wordRow(w) {
  const s = vst(w.id), open = openW.has(w.id);
  const src = w.source ? `<p class="small muted">Из интервью: «${esc(w.source.quote)}»</p>` : "";
  const details = !open ? "" : `<div class="stack" style="margin-top:10px;border-top:2px solid var(--line);padding-top:10px">
    ${w.example ? `<div class="row"><div class="grow"><p>${esc(w.example)}</p><p class="small muted">${esc(w.example_ru)}</p></div>${spk(w.example)}</div>` : ""}
    ${w.note ? `<p class="small">${esc(w.note)}</p>` : ""}${src}
    ${s.status === "learning" ? `<p class="small muted">Подряд верно: ${s.streak} из 3 (после трёх слово считается выученным).</p>` : ""}
    <div class="btns">${s.status === "learning" ? `<button class="btn small pri" data-act="wstatus" data-id="${esc(w.id)}" data-s="learned">Выучил</button>` : `<button class="btn small" data-act="wstatus" data-id="${esc(w.id)}" data-s="learning">Добавить в изучение</button>`}</div></div>`;
  const cls = s.status === "learning" ? "warn" : s.status === "new" ? "" : "ok";
  return `<div class="card word"><div class="row"><div class="grow" data-act="wtoggle" data-id="${esc(w.id)}" style="cursor:pointer"><div class="q">${esc(w.en)}</div><div class="small muted">${esc(w.ru)}</div></div>
    <span class="badge ${cls}">${VSTAT[s.status]}</span>${spk(w.en)}</div>${details}</div>`;
}

function vocabView() {
  const items = vitems();
  if (!items.length) return `<div style="margin-top:6px"><h2>Словарь</h2></div><div class="card" style="margin-top:12px"><h3>Слов пока нет</h3><p class="muted" style="margin-top:6px">Слова появятся после разбора записи: Claude отберёт слова и выражения из твоих интервью.</p></div>`;
  const by = s => items.filter(w => vst(w.id).status === s);
  const nL = by("learning").length, nD = by("learned").length + by("known").length, nN = by("new").length;
  const list = vtab === "learning" ? by("learning") : vtab === "learned" ? [...by("learned"), ...by("known")] : items;
  const seg = (v, l) => `<button class="${vtab === v ? "on" : ""}" data-act="vtab" data-v="${v}">${l}</button>`;
  return `<div class="row between" style="margin-top:6px"><div><h2>Словарь</h2><p class="small muted">Слова и выражения из твоих интервью</p></div>
      <div class="row">${voiceSelect()}</div></div>
    <div class="grid3" style="margin-top:12px"><div class="metric"><div class="small muted">На изучении</div><b>${nL}</b></div><div class="metric"><div class="small muted">Выучено</div><b>${nD}</b></div><div class="metric"><div class="small muted">Новых</div><b>${nN}</b></div></div>
    <div class="btns" style="margin-top:12px"><button class="btn pri" data-act="vtest" data-m="new">Тест на слова</button>
      <button class="btn acc" data-act="vcards" ${nL ? "" : "disabled"}>Учить карточки${nL ? ` (${nL})` : ""}</button>
      <button class="btn" data-act="vtest" data-m="review" ${nL ? "" : "disabled"}>Повторить словарь</button></div>
    <p class="small muted" style="margin-top:8px">Произношение: синтезированный английский голос вашего устройства (это не запись живого диктора). Голос можно выбрать справа вверху${(() => { const n = voiceList.filter(v => v.lang.toLowerCase().startsWith("en") && !VOICE_JUNK.some(j => v.name.includes(j))).length; return n <= 2 ? `. Сейчас доступно обычных английских голосов: ${n}. Больше голосов можно скачать в настройках системы (macOS: Системные настройки → Универсальный доступ → Устный контент → Системный голос → Управлять голосами), после этого обнови страницу` : ""; })()}.</p>
    <div class="seg" style="margin-top:14px">${seg("learning", `На изучении (${nL})`)}${seg("learned", `Выучено (${nD})`)}${seg("all", `Все (${items.length})`)}</div>
    <div class="stack" style="margin-top:12px">${list.length ? list.map(wordRow).join("") : `<div class="card"><p class="muted">${vtab === "learning" ? "Здесь появятся слова, на которых ты ошибся в тесте. Пока пусто: пройди тест на слова." : "Пока пусто."}</p></div>`}</div>`;
}

/* --- тест на слова --- */
function startVTest(mode) {
  const items = vitems();
  if (!items.length) { toast("Слов пока нет."); return; }
  const st = w => vst(w.id).status, last = w => vst(w.id).last || "";
  const learning = items.filter(w => st(w) === "learning").sort((a, b) => last(a).localeCompare(last(b)));
  const fresh = shuffle(items.filter(w => st(w) === "new")), done = shuffle(items.filter(w => ["known", "learned"].includes(st(w))));
  let pick;
  if (mode === "review") pick = learning.slice(0, 10);
  else {
    pick = learning.slice(0, 4);
    for (const w of fresh) { if (pick.length >= 10) break; pick.push(w); }
    for (const w of done) { if (pick.length >= 10) break; pick.push(w); }
  }
  if (!pick.length) { toast(mode === "review" ? "В словаре пока нет слов на изучении." : "Слов для теста нет."); return; }
  V = { items: shuffle(pick), i: 0, res: [], checked: false, phase: "run", mode, val: "" };
  render();
}

function vhead(n, done, label) {
  return `<div class="phead"><button class="x-btn" data-act="vclose" aria-label="Закрыть">✕</button><div class="bar"><i style="width:${done / n * 100}%"></i></div><span class="small muted">${label}</span></div>`;
}

function vplayerView() {
  const n = V.items.length;
  if (V.phase === "done") return `<div class="player">${vhead(n, n, `${n}/${n}`)}${vresult()}</div>`;
  const w = V.items[V.i], r = V.checked ? V.res[V.i] : null;
  const cls = r ? (r.ok ? " right" : " wrong") : "";
  let dock;
  if (r) {
    const others = w.accepted.filter(a => normRu(a) !== normRu(w.ru)).slice(0, 3);
    dock = `<div class="dock ${r.ok ? "right" : "wrong"}"><div class="dock-in"><h3>${r.ok ? PRAISE[Math.floor(Math.random() * PRAISE.length)] + " 🎉" : "Пока нет. Перевод:"}</h3>
      <p><b>${esc(w.ru)}</b>${others.length ? `<span class="small"> · также: ${esc(others.join(", "))}</span>` : ""}${w.example ? `<br><span class="small">${esc(w.example)} — ${esc(w.example_ru)}</span>` : ""}${r.ok ? "" : `<br><span class="small">Слово добавлено в твой словарь.</span>`}</p>
      <div class="btns">${!r.ok ? `<button class="btn small" data-act="vclaim">Мой вариант тоже верный</button>` : ""}<button class="btn ${r.ok ? "pri" : ""} grow" data-act="vnext">${V.i + 1 < n ? "Дальше" : "Завершить"}</button></div></div></div>`;
  } else dock = `<div class="dock"><div class="dock-in"><button class="btn pri wide" data-act="vcheck">Проверить</button></div></div>`;
  return `<div class="player">${vhead(n, V.i, `${V.i + 1}/${n}`)}
    <div class="prompt row" style="justify-content:space-between"><span>${esc(w.en)}</span>${spk(w.en)}</div>
    <p class="sub">${w.kind === "phrase" ? "Выражение" : "Слово"}: введи перевод на русский</p>
    <input class="answer-input${cls}" type="text" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="Перевод" value="${esc(V.val || "")}" ${r ? "disabled" : ""}>
    <div class="errmsg" id="verr"></div>${dock}</div>`;
}

function bindV() {
  const inp = $(".answer-input");
  if (inp && !inp.disabled) { setTimeout(() => inp.focus(), 30); inp.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); if (!e.repeat) act("vcheck"); } }); }
}

function vcheck() {
  const w = V.items[V.i], val = ($(".answer-input")?.value || "").trim();
  if (!val) { $("#verr").textContent = "Введи перевод"; return; }
  V.val = val; V.res[V.i] = { val, ok: isRuOk(w, val), claimed: false }; V.checked = true; render();
}

async function postWord(w, ok, claimed, answer) {
  try {
    const r = await api("/api/vocab/answer", { id: w.id, ok, claimed, answer });
    D.vocab.state.words[w.id] = r.word; D.game = r.game;
    if (r.learned) toast(`Выучено: ${w.en}`);
  } catch (e) { toast(e.message); }
}

async function vnext() {
  const w = V.items[V.i], r = V.res[V.i];
  await postWord(w, r.ok, r.claimed, r.val);
  V.i += 1; V.checked = false; V.val = "";
  if (V.i >= V.items.length) {
    V.i = V.items.length - 1; V.phase = "done";
    const ok = V.res.filter(x => x.ok).length;
    try { const f = await api("/api/vocab/finish", { correct: ok }); D.game = f.game; V.bonus = f.xp_gained; } catch { /* не критично */ }
  }
  render();
}

function vresult() {
  const n = V.items.length, ok = V.res.filter(x => x.ok).length, bad = V.items.filter((w, i) => !V.res[i].ok);
  const xp = ok * 2 + (V.bonus || 0);
  return `<div class="stack center" style="padding-top:20px"><div class="big">${ok} из ${n}</div>
    <p class="muted">${bad.length ? `Неизвестные слова (${bad.length}) добавлены в твой словарь.` : "Все слова на месте. Молодец!"}</p><span class="chip" style="justify-content:center">+${xp} XP</span></div>
    ${bad.length ? `<div class="sec">Добавлено в словарь</div><div class="card" style="padding:4px 16px">${bad.map(w => `<div class="result-row row"><div class="grow"><div class="q">${esc(w.en)}</div><div class="small muted">${esc(w.ru)}</div></div>${spk(w.en)}</div>`).join("")}</div>` : ""}
    <div class="stack" style="margin-top:16px;padding-bottom:20px"><button class="btn pri wide" data-act="vopen">Открыть словарь</button><button class="btn wide" data-act="vtest" data-m="${V.mode}">Ещё тест</button></div>`;
}

/* --- карточки --- */
function startCards() {
  const last = w => vst(w.id).last || "";
  const items = vitems().filter(w => vst(w.id).status === "learning").sort((a, b) => last(a).localeCompare(last(b)));
  if (!items.length) { toast("В словаре нет слов на изучении."); return; }
  C = { items, i: 0, flip: false, ok: 0, again: 0, done: false }; render();
}

function cardsView() {
  const n = C.items.length;
  const head = `<div class="phead"><button class="x-btn" data-act="vclose" aria-label="Закрыть">✕</button><div class="bar"><i style="width:${(C.done ? n : C.i) / n * 100}%"></i></div><span class="small muted">${Math.min(C.i + 1, n)}/${n}</span></div>`;
  if (C.done) return `<div class="player">${head}<div class="stack center" style="padding-top:20px"><div class="big">${C.ok} знаю</div><p class="muted">Ещё раз: ${C.again}. Слова, которые ты знаешь три раза подряд, переходят в «Выучено».</p>
    <div class="stack" style="margin-top:16px"><button class="btn pri wide" data-act="vopen">К словарю</button>${C.again ? `<button class="btn wide" data-act="vcards">Повторить ещё раз</button>` : ""}</div></div></div>`;
  const w = C.items[C.i];
  const back = C.flip ? `<div class="stack" style="margin-top:16px"><div class="big" style="font-size:30px">${esc(w.ru)}</div>
    ${w.example ? `<div class="row" style="justify-content:center"><p>${esc(w.example)}</p>${spk(w.example)}</div><p class="small muted">${esc(w.example_ru)}</p>` : ""}${w.note ? `<p class="small">${esc(w.note)}</p>` : ""}</div>` : "";
  const dock = C.flip
    ? `<div class="dock"><div class="dock-in btns"><button class="btn grow" data-act="cagain">Ещё раз</button><button class="btn pri grow" data-act="cknow">Знаю</button></div></div>`
    : `<div class="dock"><div class="dock-in"><button class="btn pri wide" data-act="cflip">Показать перевод</button></div></div>`;
  return `<div class="player">${head}<div class="card center flipcard"><div class="row" style="justify-content:center"><div class="big" style="font-size:34px">${esc(w.en)}</div>${spk(w.en)}</div>
    <p class="small muted">${w.kind === "phrase" ? "Выражение" : "Слово"} · вспомни перевод</p>${back}</div>${dock}</div>`;
}

async function cardAnswer(ok) {
  const w = C.items[C.i];
  await postWord(w, ok, false, "");
  if (ok) C.ok += 1; else C.again += 1;
  C.i += 1; C.flip = false;
  if (C.i >= C.items.length) { C.i = C.items.length - 1; C.done = true; }
  render();
}

function closeVocab() { V = null; C = null; view = "vocab"; load(); }

/* ---------- действия ---------- */
async function act(a, el) {
  if (a === "tab") { view = el.dataset.v; render(); }
  else if (a === "speak") speak(el.dataset.w);
  else if (a === "vtab") { vtab = el.dataset.v; render(true); }
  else if (a === "wtoggle") { const id = el.dataset.id; if (openW.has(id)) openW.delete(id); else openW.add(id); render(true); }
  else if (a === "wstatus") { try { const r = await api("/api/vocab/status", { id: el.dataset.id, status: el.dataset.s }); D.vocab.state.words[el.dataset.id] = r.word; render(true); } catch (e) { toast(e.message); } }
  else if (a === "vtest") startVTest(el.dataset.m || "new");
  else if (a === "vcards") startCards();
  else if (a === "vcheck") vcheck();
  else if (a === "vclaim") { V.res[V.i].ok = true; V.res[V.i].claimed = true; render(); }
  else if (a === "vnext") vnext();
  else if (a === "cflip") { C.flip = true; render(); }
  else if (a === "cknow") cardAnswer(true);
  else if (a === "cagain") cardAnswer(false);
  else if (a === "vclose") closeVocab();
  else if (a === "vopen") closeVocab();
  else if (a === "newrec") { newUpload(el?.dataset?.p || null); view = "upload"; render(); }
  else if (a === "openproject") { curProject = el.dataset.p; view = "project"; render(); }
  else if (a === "openinterview") { curSession = el.dataset.s; view = "interview"; render(); }
  else if (a === "pmode") { U.mode = el.dataset.v; render(true); }
  else if (a === "src") { if (rec) return; U.source = el.dataset.v; U.err = ""; render(true); }
  else if (a === "recstart") recStart();
  else if (a === "recstop") recStop();
  else if (a === "recreset") { U.blob = null; if (U.blobUrl) URL.revokeObjectURL(U.blobUrl); U.blobUrl = null; render(true); }
  else if (a === "submit") submitUpload();
  else if (a === "copy") copyText(el.dataset.t);
  else if (a === "retry") { try { await api("/api/retry", { session_id: el.dataset.id }); await load(); } catch (e) { toast(e.message); } }
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
  if (el && !el.disabled && el.tagName !== "SELECT") act(el.dataset.act, el);
});
document.addEventListener("keydown", e => {
  if (e.key === "Enter" && e.repeat) return;
  if (e.key === "Escape" && (V || C)) closeVocab();
  else if (e.key === "Enter" && V && V.phase === "run" && V.checked && document.activeElement?.tagName !== "BUTTON") act("vnext");
  else if (e.key === "Escape" && P && P.phase !== "done") closePlayer();
  else if (e.key === "Enter" && P && P.phase === "run" && P.mode === "practice" && P.checked && document.activeElement?.tagName !== "BUTTON") act("next");
});
document.addEventListener("change", e => {
  if (e.target.id === "voice") { voiceChoice = e.target.value; try { localStorage.setItem("voice", voiceChoice); } catch { /* ок */ } speak("Hello, this is how I sound."); }
});
window.addEventListener("beforeunload", e => { if (rec || U?.uploading) { e.preventDefault(); e.returnValue = ""; } });

load();
