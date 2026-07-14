# SPEC — Мультиагентная система обработки KYC-документов «Nordvik»

> Единственный источник истины по проекту. Код обязан соответствовать этой спецификации;
> при изменении решения сначала обновляется спека, затем код.

---

## 1. Цель и бизнес-контекст

**Nordvik** — финтех-компания (необанк для физлиц и малого бизнеса, ~300 сотрудников,
рост ~15% в квартал). Онбординг клиента требует KYC: клиент загружает комплект
документов, команда операционного комплаенса (12 человек) вручную извлекает поля,
сверяет их с заявленными данными и внешними реестрами и принимает решение.
На пиках очередь растягивается до 3–4 рабочих дней → клиенты бросают онбординг.

**Цель системы:** конвейер специализированных LLM-агентов на LangGraph, который
обрабатывает типовой KYC-пакет за минуты, автоматизирует 70–80% потока и отдаёт
человеку только рискованные/пограничные кейсы — с готовой сводкой. Регуляторное
ограничение: финальное решение по рискованным кейсам принимает человек
(human-in-the-loop обязателен). Каждое решение сопровождается полным audit trail.

**Оптимизируемые метрики:**
- время обработки типового пакета: дни → минуты;
- доля автоматически обработанных кейсов: 70–80%;
- recall по эскалациям (пропуск обязательной эскалации недопустим);
- точность извлечения полей (field-level accuracy) на golden-наборе.

---

## 2. Границы системы (scope)

**В зоне ответственности:**
- приём KYC-пакета через API (документы + заявленные клиентом данные);
- классификация документов, извлечение структурированных полей;
- валидация против бизнес-правил и заявленных данных, оценка риска;
- авто-решение или эскалация человеку с возобновлением процесса;
- персистентность состояния, audit trail, трейсинг, eval-контур.

**Вне зоны ответственности (см. Допущения):**
- OCR: на вход подаётся текстовое содержимое документов (результат работы
  вышестоящего OCR-сервиса);
- UI для комплаенс-аналитика (взаимодействие — через API);
- реальные интеграции с санкционными/PEP-реестрами (мокируются);
- аутентификация/авторизация API (за периметром — API-gateway компании).

---

## 3. Архитектура

### 3.1. Общая схема

```
                                  ┌────────────────────────────────────────────┐
                                  │                FastAPI                     │
                                  │  POST /cases        GET /cases/{id}       │
                                  │  POST /cases/{id}/review   GET .../audit  │
                                  └───────────────┬────────────────────────────┘
                                                  │ invoke / resume (Command)
                                                  ▼
 ┌────────────────────────────────────────────────────────────────────────────────┐
 │                              LangGraph StateGraph                              │
 │                                                                                │
 │  intake ──► router ──► orchestrator ══Send══► extract_document (worker × N)    │
 │                                │                        │                      │
 │                                └────────◄───────────────┘ (fan-in)             │
 │                                ▼                                               │
 │                            validator (evaluator: agent-checks-agent)           │
 │                                ▼                                               │
 │                            risk_scorer ──► tools: sanctions_mock, pep_mock     │
 │                                ▼                                               │
 │                          decision_gate ──┬──► auto_decision ──► finalize       │
 │                                          └──► human_review (interrupt) ─► finalize
 │                                                                                │
 │   любой узел ──(устойчивый сбой)──► handle_error ──► human_review (деградация) │
 └────────────────────────────────┬───────────────────────────────────────────────┘
                                  │ checkpoints + audit events
                                  ▼
                            PostgreSQL (checkpointer, audit_events)
```

### 3.2. Слои

| Слой | Модули | Ответственность |
|---|---|---|
| API | `kyc_agent/api/` | приём пакета, статус, SSE-поток событий, HITL-решение |
| Граф | `kyc_agent/graph/` | состояние, узлы-агенты, сборка графа, checkpointer |
| Бизнес-правила | `kyc_agent/rules/` | детерминированные правила валидации и риска (чистые функции) |
| LLM-слой | `kyc_agent/llm/` | абстракция провайдера, right-sizing моделей, retry/fallback |
| Инструменты | `kyc_agent/tools/` | мок-реестры санкций и PEP с реалистичным интерфейсом |
| Персистентность | `kyc_agent/persistence/` | пул Postgres, checkpointer, audit sink |
| Наблюдаемость | `kyc_agent/observability/` | structured-логирование траекторий, LangSmith |
| Схемы | `kyc_agent/schemas/` | Pydantic-модели документов, кейса, решений |

---

## 4. Применяемые паттерны и зачем они здесь

### 4.1. Routing (маршрутизация)

**Проблема:** документы физлиц и бизнес-клиентов требуют разных схем извлечения и
разных наборов правил; прогонять всё через один «универсальный» промпт — дорого и
менее точно.

**Решение:** узел `router` дешёвой моделью классифицирует каждый документ
(`id_document | proof_of_address | business_registration | ubo_declaration |
unknown`) и подтверждает тип клиента (`individual | business`). Условное ребро
направляет кейс в соответствующий суб-путь: набор схем извлечения и правило
полноты комплекта зависят от типа клиента. Нечитаемый/неопознанный документ
помечается `unknown` и обрабатывается веткой ошибок.

### 4.2. Orchestrator–workers

**Проблема:** в пакете несколько документов; извлечение по ним независимо, а
конвейер в целом должен управляться из одной точки (контроль этапов, ретраи,
деградация).

**Решение:** узел `orchestrator` планирует обработку и через **Send API** LangGraph
раздаёт по одному воркеру `extract_document` на каждый классифицированный документ
(fan-out). Результаты сливаются в состояние редьюсером (fan-in), после чего
оркестратор передаёт управление валидатору. Оркестратор же владеет счётчиками
ретраев и решением о деградации к ручной обработке.

### 4.3. Structured output при temperature=0

**Проблема:** извлечённые поля идут в сверку с реестрами и в решение — свободный
текст недопустим; извлечение должно быть воспроизводимым (одинаковый документ →
одинаковый результат), это требование и комплаенса, и тестируемости.

**Решение:** экстрактор вызывает LLM через `with_structured_output` на строгие
Pydantic-схемы (`IndividualIdFields`, `ProofOfAddressFields`,
`BusinessRegistrationFields`, `UboDeclarationFields`). Температуры по шагам:

| Шаг | T | Обоснование |
|---|---|---|
| router | 0.0 | классификация — детерминированная задача с закрытым множеством классов |
| extractor | 0.0 | воспроизводимость извлечения, никакой «креативности» |
| validator | 0.0 | сверка фактов; расхождения должны находиться стабильно |
| risk_scorer | 0.0 | рисковые триггеры — детерминированные правила; LLM только формирует связное обоснование, и оно тоже должно быть воспроизводимым для audit trail |

### 4.4. Evaluator — агент проверяет агента

**Проблема:** экстрактор может галлюцинировать или молча терять поля; авто-одобрение
на основе непроверенного извлечения — прямой комплаенс-риск (ложные авто-одобрения).

**Решение:** отдельный агент `validator` получает исходный текст документа,
результат экстрактора и заявленные клиентом данные и независимо: (а) перепроверяет,
что каждое извлечённое значение действительно присутствует в документе,
(б) сверяет поля с заявленными данными, (в) прогоняет детерминированные
бизнес-правила (срок действия, полнота комплекта), (г) выставляет итоговый
confidence. Расхождение «экстрактор ↔ документ» снижает confidence и может само
по себе отправить кейс человеку. Влияние валидатора на долю ложных авто-одобрений
замеряется в eval-контуре (ablation `--no-validator`).

### 4.5. Human-in-the-loop

**Проблема:** регулятор запрещает полностью автоматическое решение по рискованным
кейсам; при этом процесс не должен терять контекст, пока ждёт человека (часы/дни).

**Решение:** узел `human_review` вызывает `interrupt(payload)` LangGraph со сводкой
кейса (извлечённые поля, флаги, сработавшие правила, рекомендация системы).
Выполнение графа останавливается, состояние зафиксировано чекпоинтером. Аналитик
отдаёт решение через `POST /cases/{id}/review`; API возобновляет граф
`Command(resume=...)` с того же места. Триггеры эскалации — раздел 7.3.

### 4.6. State persistence (checkpointer)

**Проблема:** процесс долгоживущий (ожидание человека), приложение может
рестартовать; терять прогресс и контекст нельзя.

**Решение:** `AsyncPostgresSaver` из `langgraph-checkpoint-postgres`;
`thread_id = case_id`. Любой инстанс приложения может возобновить кейс после
рестарта. В тестах — `MemorySaver` (та же семантика, без внешней зависимости).
Схема состояния — раздел 6.

### 4.7. Bounded execution + error handling + fallback

**Проблема:** LLM-вызовы и внешние инструменты нестабильны; зацикливание или
бесконечные ретраи в комплаенс-конвейере недопустимы.

**Решение:**
- жёсткий `recursion_limit` на прогон графа (константа `GRAPH_RECURSION_LIMIT = 25`);
- на каждый LLM-шаг — не более `MAX_STEP_RETRIES = 2` повторов;
- вызовы LLM и инструментов обёрнуты в try/except с классификацией ошибки;
- стратегия fallback: повтор той же моделью → повтор fallback-моделью
  (`MODEL_FALLBACK`) → деградация: кейс уходит в `human_review` со статусом
  `degraded_to_manual` и описанием сбоя (система никогда не «роняет» кейс молча);
- сбой мок-реестра (санкции/PEP) — это **обязательная эскалация**, а не пропуск
  проверки: отсутствие ответа реестра нельзя трактовать как «чисто».

### 4.8. Trajectory logging / observability

**Проблема:** комплаенсу нужен полный ответ на вопрос «почему система решила именно
так» по каждому кейсу; инженерам — отладка многошагового графа.

**Решение:** два контура.
1. **Audit trail (доменный):** каждый узел пишет событие в таблицу
   `audit_events` (Postgres): вход/выход узла, извлечённые значения, сработавшие
   правила, confidence, принятое решение, решение человека. API отдаёт полный
   trail по кейсу.
2. **Трейсинг (инженерный):** structured-логи (structlog, JSON) каждого шага +
   нативная интеграция LangSmith (включается переменными окружения
   `LANGSMITH_TRACING=true`, `LANGSMITH_API_KEY`), т.к. LangGraph трейсит в
   LangSmith из коробки.

### 4.9. Right-sizing моделей по шагам

**Проблема:** прогонять классификацию документов через флагманскую модель — платить
×10 за задачу, которую решает малая модель; извлечение и рисковый анализ малой
моделью — терять точность там, где она критична.

**Решение:** модель каждого шага задаётся конфигурацией (env). Дефолт кода —
`fake:*` (оффлайн-режим: система обязана подниматься и работать без API-ключей,
см. DoD); рекомендованная продовая конфигурация задаётся в `.env` и приведена
в `.env.example`:

| Шаг | Переменная | Прод-рекомендация | Обоснование |
|---|---|---|---|
| router | `MODEL_ROUTER` | `anthropic:claude-haiku-4-5` | классификация с закрытым множеством классов — задача малой модели |
| extractor | `MODEL_EXTRACTOR` | `anthropic:claude-sonnet-5` | точность извлечения → прямо влияет на решение |
| validator | `MODEL_VALIDATOR` | `anthropic:claude-sonnet-5` | перепроверка фактов не должна быть слабее извлечения |
| risk_scorer | `MODEL_RISK` | `anthropic:claude-sonnet-5` | формулировка обоснования для audit trail |
| fallback | `MODEL_FALLBACK` | `anthropic:claude-opus-4-8` | последняя попытка перед деградацией к человеку |

Провайдер не захардкожен: строки вида `provider:model` разбираются
`init_chat_model` (LangChain), поддерживаются любые провайдеры LangChain.
Специальное значение `fake:*` включает детерминированную оффлайн-реализацию
(раздел 9).

---

## 5. Технологический стек

| Компонент | Выбор |
|---|---|
| Язык | Python 3.12, строгая типизация (mypy), async |
| Оркестрация | LangGraph ≥ 1.0 (StateGraph, Send, interrupt, checkpointer) |
| LLM-слой | LangChain `init_chat_model` + собственная фабрика по шагам |
| Валидация | Pydantic v2 |
| API | FastAPI + uvicorn, SSE-стрим статусов |
| БД | PostgreSQL 16 (checkpointer + audit trail) |
| Контейнеризация | Docker + docker-compose (app + postgres) |
| Наблюдаемость | structlog (JSON) + LangSmith (opt-in) |
| Тесты | pytest, pytest-asyncio |
| Инструменты | uv (зависимости), ruff (линт), mypy (типы) |

---

## 6. Модель данных

### 6.1. Входные данные (API)

```
KYCPackage
├── customer_type: "individual" | "business"
├── applicant: ApplicantDeclared        # то, что клиент ввёл при регистрации
│   ├── full_name: str
│   ├── date_of_birth: date | None      # для физлица
│   ├── address: str
│   ├── company_name: str | None        # для бизнеса
│   ├── registration_number: str | None
│   └── expected_monthly_volume_eur: Decimal
└── documents: list[InputDocument]
    ├── document_id: str
    ├── file_name: str
    └── text_content: str               # результат OCR (допущение 12.1)
```

### 6.2. Состояние графа (`KYCState`)

Состояние — единый объект, персистится чекпоинтером на каждом суперешаге.

```
KYCState (TypedDict, total=False)
├── case_id: str                        # = thread_id чекпоинтера
├── package: KYCPackage                 # неизменяемый вход
├── status: CaseStatus                  # см. 6.4
├── classified_documents: list[ClassifiedDocument]
│       {document_id, doc_type, classifier_confidence}
├── extractions: Annotated[list[ExtractionResult], operator.add]   # fan-in редьюсер
│       {document_id, doc_type, fields: dict, field_confidence: dict,
│        extraction_error: str | None}
├── validation: ValidationReport
│       {field_checks: list[FieldCheck{field, declared, extracted, match, critical}],
│        rule_flags: list[RuleFlag{rule_id, severity, details}],
│        overall_confidence: float, evaluator_notes: str}
├── risk: RiskAssessment
│       {level: "low"|"medium"|"high", triggered_rules: list[RuleFlag],
│        sanctions_hits: list[RegistryHit], pep_hits: list[RegistryHit],
│        rationale: str}
├── decision: Decision | None
│       {outcome: "approve"|"reject"|"escalate", decided_by: "system"|"human",
│        reason_codes: list[str], rationale: str, reviewer: str | None}
├── errors: Annotated[list[ProcessingError], operator.add]
│       {node, error_type, message, attempt, timestamp}
├── retry_counts: Annotated[dict[str, int], merge]   # bounded execution, по узлам
└── degraded_reasons: Annotated[list[str], operator.add]
        # кейс деградирован ⇔ список непуст; additive-редьюсер, т.к.
        # воркеры извлечения пишут конкурентно в одном суперешаге
```

### 6.3. Схемы извлечения (structured output)

- `IndividualIdFields`: full_name, date_of_birth, document_number,
  expiry_date, nationality.
- `ProofOfAddressFields`: full_name, address, issue_date, issuer.
- `BusinessRegistrationFields`: company_name, registration_number,
  registration_date, legal_form, registered_address.
- `UboDeclarationFields`: company_name, beneficial_owners:
  list[{full_name, date_of_birth, ownership_percent}].

Каждое поле сопровождается confidence ∈ [0,1] от экстрактора.

### 6.4. Статусы кейса

`received → processing → awaiting_human_review → completed`
плюс терминальный `failed` (только при невозможности даже деградации — например,
битое состояние). Деградация при сбоях — это `awaiting_human_review` с
`degraded=true`, а не `failed`.

### 6.5. Audit trail (Postgres)

```sql
CREATE TABLE audit_events (
  id           BIGSERIAL PRIMARY KEY,
  case_id      TEXT NOT NULL,
  ts           TIMESTAMPTZ NOT NULL DEFAULT now(),
  node         TEXT NOT NULL,          -- узел графа или "api"
  event_type   TEXT NOT NULL,          -- node_started / node_completed / rule_triggered /
                                       -- registry_checked / decision_made / human_decision / error
  payload      JSONB NOT NULL
);
CREATE INDEX idx_audit_case ON audit_events (case_id, ts);
```

Audit sink — интерфейс с двумя реализациями: `PostgresAuditSink` (прод) и
`InMemoryAuditSink` (тесты). Запись событий не должна ронять конвейер: сбой
записи логируется и добавляется в `errors`, кейс продолжает обработку, но
финализация без хотя бы одного записанного `decision_made` события невозможна.

---

## 7. Бизнес-правила

### 7.1. Полнота комплекта (`package_completeness`)

| Тип клиента | Обязательные документы |
|---|---|
| individual | `id_document`, `proof_of_address` |
| business | `business_registration`, `ubo_declaration` |

Неполный комплект → `reject` c reason code `INCOMPLETE_PACKAGE` (клиент может
подать заново); это авто-решение, эскалация не требуется.

### 7.2. Правила валидации (детерминированные, unit-тестируемые)

| rule_id | Проверка | Severity |
|---|---|---|
| `DOC_EXPIRED` | expiry_date < today | critical |
| `NAME_MISMATCH` | нормализованное ФИО (casefold, схлопывание пробелов) не совпадает; fuzzy ratio (difflib) < 0.85 → critical, 0.85–0.95 → warning | critical/warning |
| `DOB_MISMATCH` | дата рождения не совпадает с заявленной | critical |
| `ADDRESS_MISMATCH` | нормализованный адрес: fuzzy ratio < 0.7 | warning |
| `COMPANY_NAME_MISMATCH` | аналогично NAME_MISMATCH для бизнеса | critical/warning |
| `REG_NUMBER_MISMATCH` | регистрационный номер не совпадает точно | critical |
| `EXTRACTION_INCOMPLETE` | обязательное поле схемы не извлечено | warning |
| `EVALUATOR_DISCREPANCY` | валидатор не нашёл извлечённое значение в тексте документа | critical |

### 7.3. Рисковые правила — обязательные триггеры эскалации человеку

| rule_id | Триггер |
|---|---|
| `SANCTIONS_HIT` | совпадение в санкционном реестре (fuzzy-поиск по имени, порог мок-реестра) |
| `PEP_MATCH` | совпадение в PEP-реестре |
| `HIGH_VOLUME` | expected_monthly_volume_eur > 10 000 (individual) / 50 000 (business) |
| `LOW_CONFIDENCE` | overall_confidence < 0.75 |
| `CRITICAL_MISMATCH` | любой critical-флаг из 7.2, кроме DOC_EXPIRED и INCOMPLETE_PACKAGE |
| `REGISTRY_UNAVAILABLE` | реестр не ответил после ретраев — «нет ответа» ≠ «чисто» |
| `UBO_SANCTIONS_OR_PEP` | санкции/PEP по любому бенефициару |

**Инвариант recall-а эскалаций:** сработал хотя бы один триггер из 7.3 →
авто-одобрение невозможно ни при каких условиях.

### 7.4. Матрица решений (`decision_gate`)

| Условие (проверяется сверху вниз) | Решение |
|---|---|
| комплект неполный **и кейс не деградирован** | auto `reject` (`INCOMPLETE_PACKAGE`) |
| `DOC_EXPIRED`, нет триггеров 7.3, кейс не деградирован | auto `reject` (`DOCUMENT_EXPIRED`) — детерминированный проверяемый факт |
| любой триггер 7.3 или кейс деградирован | `escalate` → human_review |
| иначе (confidence ≥ 0.75, флагов нет) | auto `approve` |

Деградированный кейс (нечитаемый документ, устойчивый сбой шага) никогда не
авто-реджектится по неполноте: нечитаемый файл может оказаться как раз
недостающим документом — решает человек.

Человек на эскалации выбирает `approve` или `reject` (+ комментарий);
его решение записывается в audit trail c `decided_by="human"`.

---

## 8. Граф LangGraph

### 8.1. Узлы

| Узел | Модель | Что делает |
|---|---|---|
| `intake` | — | инициализация состояния, проверка структуры пакета, audit-событие |
| `router` | MODEL_ROUTER | классификация типа каждого документа + подтверждение типа клиента |
| `orchestrator` | — | полнота комплекта; fan-out `Send("extract_document", …)` по документам; контроль деградации |
| `extract_document` | MODEL_EXTRACTOR | воркер: structured output по схеме типа документа, per-field confidence |
| `validator` | MODEL_VALIDATOR | evaluator: перепроверка извлечения по тексту, сверка с заявленным, детерминированные правила 7.2, overall_confidence |
| `risk_scorer` | MODEL_RISK | вызов мок-реестров (tools), правила 7.3, уровень риска, rationale |
| `decision_gate` | — | детерминированная матрица 7.4 (условное ребро) |
| `auto_decision` | — | фиксация авто-решения + audit |
| `human_review` | — | `interrupt(summary)`; после resume — применение решения человека |
| `finalize` | — | финальный статус, итоговое audit-событие `decision_made` |
| `handle_error` | — | классификация сбоя, `degraded=true`, маршрут в human_review |

### 8.2. Рёбра

```
START → intake → router → orchestrator
orchestrator ══ Send × N ══► extract_document → validator      # fan-out/fan-in
orchestrator ──(неполный комплект)──► decision_gate            # мимо извлечения
validator → risk_scorer → decision_gate
decision_gate ─┬─► auto_decision → finalize → END
               └─► human_review → finalize → END
{router, validator} ──(устойчивый сбой)──► handle_error → decision_gate
```

Инвариант: **все** решения, включая деградацию при сбоях, проходят через
единственную точку — `decision_gate` (матрица 7.4), поэтому `handle_error`
ведёт в `decision_gate`, а не напрямую в `human_review`. Сбой отдельного
воркера `extract_document` не убивает кейс: воркер фиксирует
`extraction_error` + причину деградации, и кейс уходит человеку через ту же
матрицу. Признак деградации — непустой список `degraded_reasons` в состоянии
(воркеры пишут его конкурентно через additive-редьюсер).

### 8.3. Контракт HITL

`interrupt()` получает payload:

```json
{
  "case_id": "...",
  "reason_codes": ["SANCTIONS_HIT", "..."],
  "summary": {"extracted": {...}, "flags": [...], "risk": {...}},
  "system_recommendation": "reject",
  "degraded": false
}
```

Resume-значение от API: `{"outcome": "approve"|"reject", "reviewer": str,
"comment": str}`. Невалидное resume-значение → повторный interrupt с ошибкой в
payload (кейс не может быть закрыт некорректным решением).

---

## 9. LLM-слой и оффлайн-режим

`llm/factory.py` — фабрика `get_chat_model(step: PipelineStep) -> BaseChatModel`:
- читает `MODEL_<STEP>` из конфигурации (`provider:model_name`);
- `init_chat_model(...)` с температурой шага (раздел 4.3);
- retry/fallback-обёртка (раздел 4.7).

**Fake-провайдер** (`fake:extractor` и т.п.) — детерминированные реализации
`BaseChatModel`-совместимого интерфейса, которые решают те же задачи
правилами/регэкспами по синтетическим документам:
- используется в тестах, CI и оффлайн-демо (docker-compose поднимается и работает
  без единого API-ключа);
- интерфейсы и путь исполнения графа идентичны реальному LLM-режиму — переключение
  на реальные модели — вопрос конфигурации `.env`;
- «нечитаемый документ» стабильно даёт ошибку извлечения → тест error-handling.

Секреты — только через переменные окружения (`.env`, не в git).

---

## 10. API

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/api/v1/cases` | приём KYC-пакета; 202 + `case_id`; запуск графа в фоне |
| GET | `/api/v1/cases/{case_id}` | статус + сводка состояния + решение |
| GET | `/api/v1/cases/{case_id}/events` | SSE-стрим audit-событий (прогресс в реальном времени) |
| POST | `/api/v1/cases/{case_id}/review` | решение человека; resume графа `Command(resume=…)` |
| GET | `/api/v1/cases/{case_id}/audit` | полный audit trail кейса |
| GET | `/health` | liveness: приложение + БД |

Ошибки: 404 — неизвестный кейс; 409 — review для кейса не в статусе
`awaiting_human_review`; 422 — невалидный вход (Pydantic).

---

## 11. Eval-контур

`eval/golden_set.json` — размеченный синтетический набор (≥ 12 кейсов: чистые,
несовпадения, санкции/PEP, high-volume, битые документы, неполные комплекты).
Для каждого кейса заданы: эталонные значения полей, ожидаемое решение, ожидание
эскалации.

`eval/run_eval.py` прогоняет набор через граф (fake-провайдер по умолчанию;
реальные модели — если заданы ключи) и считает:

| Метрика | Определение | Цель |
|---|---|---|
| field_accuracy | доля точно извлечённых полей (нормализованное сравнение) | ≥ 0.95 (fake: 1.0) |
| auto_rate | доля кейсов без эскалации на «типовом» подмножестве | 0.70–0.80 |
| escalation_recall | доля обязательных эскалаций, отправленных человеку | **1.00** (жёсткий инвариант) |
| escalation_precision | доля эскалаций, которые действительно были нужны | ≥ 0.8 |
| decision_accuracy | совпадение итогового решения с эталоном | ≥ 0.9 |

Ablation `--no-validator` (валидатор пропускается, confidence берётся от
экстрактора «на веру») — для измерения вклада evaluator-паттерна в
предотвращение ложных авто-одобрений. Результаты пишутся в `eval/results.json`
и фиксируются в README.

---

## 12. Допущения

1. **OCR вне scope.** Вход — текстовое содержимое документа (`text_content`),
   как если бы его выдал вышестоящий OCR-сервис. «Битый» документ = нечитаемый/
   мусорный текст.
2. **Реестры санкций и PEP — моки** с реалистичным tool-интерфейсом
   (fuzzy-поиск по имени, латентность, инъекция сбоев для тестов). Данные
   полностью синтетические.
3. **Fake-LLM-провайдер** — легитимный первоклассный режим работы (тесты, CI,
   оффлайн-демо), а не заглушка «на потом»: он проходит через те же интерфейсы
   и тот же граф. Реальные модели включаются конфигурацией без изменения кода.
   Числа eval в README зафиксированы на fake-провайдере (детерминированны и
   воспроизводимы); прогон на реальных моделях — той же командой при наличии
   ключей.
4. **Один процесс приложения** в docker-compose; горизонтальное масштабирование
   (несколько воркеров, очередь) — вне scope, но архитектурно не заблокировано:
   состояние в Postgres, кейс возобновляем с любого инстанса.
5. **Аутентификация API** — за периметром (gateway); в демо эндпоинты открыты,
   reviewer передаётся полем запроса.
6. **Персональные данные** — только синтетические; в реальном проде audit trail
   потребует шифрования полей и политики retention (отмечено, вне scope).
7. **Валюта порогов** — EUR; пороги 7.3 — конфигурация с дефолтами из спеки.
8. **Хранение файлов документов** (blob storage) — вне scope; храним только
   текст и результаты обработки.

---

## 13. План реализации

| Этап | Содержание | Артефакты |
|---|---|---|
| 1 | Спецификация | `SPEC.md` |
| 2 | Каркас проекта: pyproject (uv), конфигурация, схемы Pydantic | `src/kyc_agent/{config,schemas}` |
| 3 | Бизнес-правила (чистые функции) + юнит-тесты | `rules/`, `tests/unit` |
| 4 | Мок-реестры (tools) + LLM-фабрика с fake-провайдером и retry/fallback | `tools/`, `llm/` |
| 5 | Граф: состояние, узлы, сборка, checkpointer (Memory для тестов) | `graph/` |
| 6 | HITL: interrupt/resume; audit trail; observability | `audit/`, `observability/` |
| 7 | Интеграционные тесты: 4+ синтетических сценария через граф | `tests/integration` |
| 8 | FastAPI + Postgres checkpointer + docker-compose | `api/`, `persistence/`, Docker |
| 9 | Eval-контур: golden set, метрики, ablation валидатора | `eval/` |
| 10 | README (англ.) со схемой, метриками, инструкцией запуска | `README.md` |

Каждый этап — отдельный осмысленный коммит (Conventional Commits, англ.), push
в remote по мере продвижения.

---

## 14. Критерии готовности (Definition of Done)

- [x] `SPEC.md` синхронизирован с кодом.
- [x] Граф реализует все паттерны раздела 4; все узлы раздела 8 существуют и покрыты тестами.
- [x] Синтетические кейсы проходят с ожидаемым поведением: чистый → авто-одобрение; несовпадение → эскалация; санкции/PEP/high-volume → эскалация; битый документ → graceful-деградация к человеку; неполный комплект → авто-reject (87 тестов: unit + integration).
- [x] Checkpointer (Postgres в проде, Memory в тестах), HITL-прерывание и возобновление работают, включая рестарт процесса между interrupt и resume (проверено рестартом контейнера в docker-compose).
- [x] Лимиты итераций и retry/fallback реализованы и протестированы.
- [x] Audit trail пишется по каждому узлу и отдаётся через API.
- [x] Eval-метрики раздела 11 посчитаны, числа зафиксированы в README (`eval/results.json`); escalation_recall = 1.0; auto_rate = 0.714; ablation без валидатора: recall 0.857, одно ложное авто-одобрение.
- [x] `docker compose up` поднимает систему одной командой без API-ключей; секреты через `.env`; README (англ.) описывает запуск, архитектуру, трейсы.
- [x] Всё закоммичено и запушено в remote.
