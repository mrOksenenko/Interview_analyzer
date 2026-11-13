# Схема `progress/sessions/<id>.analysis.json`

Обязательные поля: `session_id`, `project`, `date`, `level`, `errors`, `answers`, `topics`.
`project` — id проекта (из `meta.json` записи или `progress.py project list`); проект должен существовать.
Остальное — по возможности. Проверяет `scripts/progress.py ingest`.

```json
{
  "session_id": "2026-11-12-acme-1",
  "project": "acme",
  "date": "2026-11-12",
  "role": "Backend Developer, Acme",
  "level": {
    "cefr": "B1",
    "scores": {"grammar": 3, "vocabulary": 3, "fluency": 2, "coherence": 3, "relevance": 4},
    "summary": "Понятен, но времена и структура предложений мешают раскрыть опыт."
  },
  "candidate_word_count": 812,
  "recruiter_feedback": "опционально — текст фидбека как есть",
  "errors": [
    {
      "category": "tenses",
      "subtopic": "present perfect vs past simple",
      "quote": "I have worked there in 2019",
      "correction": "I worked there in 2019",
      "rule": "С конкретной датой в прошлом — Past Simple, не Present Perfect.",
      "timestamp": "03:12"
    }
  ],
  "answers": [
    {
      "question": "What is the difference between a process and a thread?",
      "timestamp": "08:40",
      "answer_summary": "Сказал, что треды «быстрее», про общую память не упомянул.",
      "score_content": 4,
      "score_english": 7,
      "verdict": "partial",
      "cause": "knowledge",
      "evidence": "Фразы грамматически чистые, но нет ни одного термина (shared memory, context switch).",
      "knowledge_gap_topic": "Processes vs threads"
    }
  ],
  "topics": [
    {"type": "grammar", "name": "Present Perfect vs Past Simple", "category": "tenses"},
    {"type": "knowledge", "name": "Processes vs threads", "category": "operating_systems"}
  ],
  "overall": {
    "summary": "Содержание 4/10: ... Английский 7/10: ... Выборка: 5 вопросов."
  },
  "strengths": ["Хорошо структурирует рассказ о проекте", "Самоисправляется"]
}
```

## Поля

- `errors[].category` — из `grammar-taxonomy.md`. `quote` — **дословно** из транскрипта.
- `answers[].score_content` и `score_english` — **обязательны**, числа 1–10 (шкалы в SKILL.md, шаг 4);
  `smalltalk: true` исключает ответ из итога; `verdict` — `good|partial|poor`;
  `cause` — `language|question_understanding|knowledge|mixed|none`.
- `overall` — итог интервью. `content`/`english` = среднее по содержательным ответам, `total` = среднее
  этих двух. Скрипт досчитывает недостающее сам; `summary` пишет Claude после анализа всего блока.
- `topics[].type` — `grammar` (языковая тема) или `knowledge` (содержательная). Тема = 1 понятие, которое можно выучить и проверить; не «грамматика вообще».
  Название темы держи **стабильным между интервью одного проекта** (тот же текст → скрипт распознает повтор и возврат ошибки;
  ключ темы = `<project>/<type>:<slug названия>`). Одна и та же тема в разных проектах — разные темы.
- `candidate_word_count` — если не указан, скрипт считает по строкам `Candidate:` в
  `progress/transcripts/<id>.labeled.md`.
