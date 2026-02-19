# Onboarding Guide

## 1. Добавление нового разработчика

### Шаг 1: GitHub — ветка и лейбл

1. В каждом репо (например `mahjong-core`) создать ветку разработчика:
   ```bash
   git checkout main && git checkout -b dev/Alice && git push -u origin dev/Alice
   ```
2. Создать лейбл `developer:Alice` в GitHub Issues (Settings > Labels)

### Шаг 2: Netlify — dev-сайт

1. Создать новый сайт в Netlify (Import existing project > GitHub repo)
2. **Имя**: `mahjong-dev-alice` (паттерн: `{game}-dev-{name}`)
3. **Branch to deploy**: `dev/Alice`
4. Netlify подхватит `netlify.toml` из репо автоматически (build command + publish dir)
5. Настроить **Deploy notifications > Outgoing webhook**:
   - URL: `https://{bot-host}/netlify/webhook`
   - Events: Deploy succeeded, Deploy failed

### Шаг 3: Railway — env variables бота

Обновить переменные окружения бота:

| Переменная | Что добавить | Формат |
|---|---|---|
| `DEVELOPER_MAP` | Маппинг TG ID → ветка → лейбл | `...,TG_USER_ID:dev/Alice:developer:Alice` |
| `NETLIFY_SITE_MAP` | Маппинг Netlify-сайта → репо | `...,mahjong-dev-alice:Owner/mahjong-core` |

Пример полной строки:
```
DEVELOPER_MAP=42692410:dev/Gleb:developer:Gleb,123456789:dev/Alice:developer:Alice
NETLIFY_SITE_MAP=mahjong-dev-gleb:Glera/mahjong-core,mahjong-dev-alice:Glera/mahjong-core,p4g-dev-gleb:Glera/play4good_test
```

### Шаг 4: Опционально — WEBAPP_URL_DEV

Если нужна кнопка в `/apps`:
```
WEBAPP_URL_DEV_2=https://mahjong-dev-alice.netlify.app
WEBAPP_DEV_2_NAME=Alice
```

### Шаг 5: Проверка

1. Разработчик пишет боту: `/repo mj` → выбирает репо
2. `/ticket тестовый тикет` → тикет создаётся в правильном репо
3. CI работает → деплой появляется на `mahjong-dev-alice.netlify.app`
4. `📦 Задача завершена` → содержит ссылку на билд

---

## 2. Добавление нового game core (пакета)

Пример: добавляем `@game/puzzle-core`.

### Шаг 1: Создать репозиторий

```bash
mkdir puzzle-core && cd puzzle-core
npm init -y
# Настроить package.json:
#   name: "@game/puzzle-core"
#   main: "dist/index.js"
#   types: "dist/index.d.ts"
#   files: ["dist"]
#   scripts: { build, test, check, playground }
```

### Шаг 2: Структура пакета

```
puzzle-core/
├── src/
│   ├── index.ts          # Экспорты + CORE_VERSION
│   ├── types.ts           # Типы
│   └── logic.ts           # Игровая логика (pure functions)
├── playground/
│   ├── index.html         # Entry point
│   ├── main.ts            # Vanilla TS UI
│   ├── style.css          # Стили
│   └── vite.config.ts     # Vite с alias на ../src/index.ts
├── tests/
│   └── core.test.ts       # Smoke tests
├── netlify.toml           # Build: playground → корень сайта
├── CLAUDE.md              # Инструкции для CI-агента
├── tsconfig.json
└── package.json
```

### Шаг 3: CORE_VERSION

В `src/index.ts`:
```typescript
export const CORE_VERSION = '0.1.0';
```
CI-агент **обязан** инкрементировать PATCH на каждый коммит.

### Шаг 4: netlify.toml

```toml
[build]
  command = "npm install && npx vite build --config playground/vite.config.ts"
  publish = "playground/dist"
```

### Шаг 5: CI workflow

Скопировать `.github/workflows/claude.yml` из `mahjong-core`, обновить:
- Секцию "Notify: changes pushed" — `grep CORE_VERSION` из `src/index.ts`
- Секреты: `BOT_WEBHOOK_URL`, `ANTHROPIC_API_KEY`

### Шаг 6: CLAUDE.md

Обязательные секции:
- Commands (build, test, check, playground)
- Architecture (файлы, экспорты)
- Core / Platform boundary (что core, что shell)
- **Versioning** — обязательный бамп CORE_VERSION на каждый коммит
- Standalone playground (структура, API таблица)

### Шаг 7: Бот — регистрация репо

В Railway env:
```
GITHUB_REPOS=...,Owner/puzzle-core:puzzle:main
NETLIFY_SITE_MAP=...,puzzle-dev-gleb:Owner/puzzle-core
```

### Шаг 8: Netlify — сайт

1. Создать сайт `puzzle-dev-gleb` → branch `dev/Gleb`
2. Добавить webhook: `https://{bot-host}/netlify/webhook`

### Шаг 9: Проверка

```bash
# Локально
cd puzzle-core
npm test && npm run build
npm run playground  # http://localhost:5173 — проверить UI

# Через бота
/repo puzzle
/ticket тестовый тикет
# Ждём CI → деплой → ссылка в "Задача завершена"
```

---

## 3. Добавление нового LiveOps события

Пример: добавляем событие "Wheel of Fortune" в `@game/liveops-shared`.

### Шаг 1: Тип события

В `liveops-shared/src/types.ts`:
```typescript
export interface WheelOfFortuneEvent {
  type: 'wheel_of_fortune';
  segments: WheelSegment[];
  spinsPerDay: number;
  // ...
}
```

### Шаг 2: Логика

В `liveops-shared/src/` создать `wheelEvent.ts`:
- Создание события (`createWheelEvent`)
- Вращение (`spinWheel`)
- Проверка доступности (`canSpin`)
- Pure functions, без DOM/UI

### Шаг 3: Экспорт

В `liveops-shared/src/index.ts`:
```typescript
export { createWheelEvent, spinWheel, canSpin } from './wheelEvent';
export type { WheelOfFortuneEvent, WheelSegment } from './types';
```

### Шаг 4: Ребилд пакета

```bash
cd liveops-shared
npm run build
```

### Шаг 5: Интеграция в shell (p4g-platform)

1. Создать `client/src/lib/liveops/wheelOfFortune/` в p4g-platform:
   - `store.ts` — Zustand store (импорт логики из `@game/liveops-shared`)
   - `WheelUI.tsx` — React компонент (UI)
   - `index.ts` — barrel export

2. Зарегистрировать в `eventRegistry.ts`:
   ```typescript
   registerEvent('wheel_of_fortune', {
     component: WheelOfFortuneUI,
     store: useWheelStore,
   });
   ```

### Шаг 6: Очистка кэша и проверка

```bash
rm -rf p4g-platform/client/node_modules/.vite-*
cd p4g-platform && npm run dev
```

### Шаг 7: Проверка через бота

```
/repo p4g
/ticket добавить wheel of fortune событие
```

---

## Справочник: все env variables бота

| Переменная | Обязательна | Формат | Назначение |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Да | string | Telegram Bot API токен |
| `OPENAI_API_KEY` | Да | string | OpenAI ключ (распознавание голоса) |
| `GITHUB_TOKEN` | Да | string | GitHub API токен |
| `GITHUB_REPO` | Нет | `owner/repo` | Fallback-репо (single-repo mode) |
| `GITHUB_REPOS` | Нет | `owner/repo:short:branch,...` | Multi-repo конфиг |
| `GITHUB_LABELS` | Нет | `label1,label2` | Лейблы на тикетах |
| `CHAT_REPO_MAP` | Нет | `chat_id:owner/repo,...` | Группа → репо |
| `DEVELOPER_MAP` | Нет | `tg_id:branch:label,...` | TG user → ветка + лейбл |
| `NETLIFY_SITE_MAP` | Нет | `site:owner/repo,...` | Netlify-сайт → репо |
| `WEBAPP_URL_PRODUCTION` | Нет | URL | Prod приложение |
| `WEBAPP_URL_DEV_1` | Нет | URL | Dev 1 приложение |
| `WEBAPP_URL_DEV_2` | Нет | URL | Dev 2 приложение |
| `WEBAPP_DEV_1_NAME` | Нет | string | Имя Dev 1 (default: "Dev 1") |
| `WEBAPP_DEV_2_NAME` | Нет | string | Имя Dev 2 (default: "Dev 2") |
| `REQUIRE_TICKET_COMMAND` | Нет | bool | В группах только через /ticket |
| `ARM_TTL_SECONDS` | Нет | int | TTL ожидания голоса после /ticket (120) |
| `PERSIST_DIR` | Нет | path | Директория для персистенции (Railway Volume) |
