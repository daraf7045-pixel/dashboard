# Агент дашборда активности менеджеров — Bitrix24

Этот агент поддерживает HTML-дашборд активности менеджеров PreParty.
Дашборд живёт на GitHub Pages и обновляется через Claude Code.

**Живой дашборд:** https://daraf7045-pixel.github.io/dashboard/
**Репозиторий:** https://github.com/daraf7045-pixel/dashboard
**Локальный файл:** `/Users/darafedotova/bitrix24-dashboard-agent/dashboard.html`
**Деплой:** копировать в `/tmp/dashboard_push/index.html` → git commit → git push

## Конфигурация

```javascript
const WH          = 'https://dcatering.bitrix24.ru/rest/160/...';  // webhook (не показывать!)
const CATEGORY_ID = 4;                    // воронка «Продажи Preparty»
const CONFIRMED   = 'C4:UC_3KB83W';      // стадия «Заказ подтвержден»
const REALIZED    = 'C4:WON';            // стадия «Успешно реализовано»

const MANAGERS = [
  { name: 'Кирасирова Анна',   id: '12752' },
  { name: 'Воронежцева Алина', id: '9174'  },
  { name: 'Айсина Динара',     id: '14262' },
  { name: 'Федотова Дарья',    id: '160'   },
];

// Кастомные поля сделки
const EVENT_DATE_FIELD  = 'UF_CRM_1594025532470';  // «Дата мероприятия*» (тип: date)
const COMMISSION_FIELDS = ['UF_CRM_1675687802829', 'UF_CRM_1675689049773'];  // АК-комиссии
```

## Метрики дашборда

### Дневные (Сегодня / Вчера)
| Метрика | Логика |
|---|---|
| Звонки >40с | `voximplant.statistic.get`, CALL_DURATION >= 40, исходящие |
| Новые сделки | `crm.deal.list` по DATE_CREATE, фильтр ASSIGNED_BY_ID |
| Подписано (Заказ подтвержден) | `crm.deal.list`, STAGE_ID=CONFIRMED, MOVED_TIME в диапазоне дня |
| Реализовано (Успешно реализовано) | `crm.deal.list`, STAGE_ID=WON, **UF_CRM_1594025532470** в диапазоне дня |
| Задачи | `tasks.task.list` + `tasks.task.get`, closedDate в диапазоне дня |

### Месячные
| Метрика | Логика |
|---|---|
| Текущий месяц — Подтверждено | Сделки в CONFIRMED + WON из CONFIRMED в этом месяце (MOVED_TIME) |
| Текущий месяц — Реализовано | То же (MOVED_TIME >= начало месяца) |
| Прошлый месяц — Реализовано | WON-сделки с MOVED_TIME в диапазоне прошлого месяца |
| Следующий месяц — Реализовано | CONFIRMED-сделки с UF_CRM_1594025532470 в следующем месяце |

## Критически важные правила

### Фильтрация дат
- **Часовой пояс**: всегда `+03:00` во всех datetime-параметрах API.
- **MOVED_TIME** — дата перехода сделки в текущую стадию. Используется для «Подписано».
- **UF_CRM_1594025532470** — «Дата мероприятия» (тип date, формат YYYY-MM-DD). Используется для «Реализовано за день». Поле пустое у доставок и у WON-сделок без мероприятия → они не попадают в счёт.
- **CLOSEDATE** у WON-сделок = дата перехода в WON (авто-проставляется Битриксом). НЕ использовать для фильтра «Реализовано за день».
- **DATE_MODIFY** меняется при любом изменении сделки. Используется только как серверный пре-фильтр для ускорения, всегда дополняется client-side фильтром по MOVED_TIME.

### Суммы сделок
- Всегда вычитать комиссии: `netOpportunity(d) = OPPORTUNITY - UF_CRM_1675687802829 - UF_CRM_1675689049773`
- «Подписано»: считать только сделки в CONFIRMED, которые вошли в эту стадию в нужный день (MOVED_TIME). НЕ включать сделки CONFIRMED→WON в тот же день.
- «Реализовано»: только WON-сделки с заполненным UF_CRM_1594025532470 = нужной дате.

### Пагинация
- Проходить ВСЕ страницы через `start` / `next`. Нельзя делать вывод о полноте по числу записей в одном ответе.
- Пауза 150 мс между страницами.

### Rate limit
- Exponential backoff: 1/2/4/8/16 с, макс. 5 попыток при HTTP 429/503.

## Кэширование (localStorage)

| Ключ | Содержимое |
|---|---|
| `bx_dash_state_v7` | Данные за сегодня (сбрасывается при смене даты) |
| `bx_dash_yd9_{YYYY-MM-DD}` | Данные за вчера (кешируется навсегда — день завершён) |

**При изменении логики расчёта** — обязательно бампать версию кэша (v7→v8, yd9→yd10 и т.д.), иначе пользователи увидят старые данные.

## Деплой на GitHub Pages

```bash
cp dashboard.html /tmp/dashboard_push/index.html
git -C /tmp/dashboard_push add index.html
git -C /tmp/dashboard_push commit -m "описание изменений"
git -C /tmp/dashboard_push push origin main
```

Токен для пуша хранится в remote URL репозитория `/tmp/dashboard_push`.
Если клон слетел — пересоздать: `git clone https://TOKEN@github.com/daraf7045-pixel/dashboard.git /tmp/dashboard_push`
Токен найти в: `~/.claude/settings.local.json` (grep: `ghp_`)

## Адаптация под другую компанию

Чтобы развернуть дашборд для другой компании:
1. Fork репозитория на GitHub, включить GitHub Pages
2. Поменять в `index.html`:
   - `WH` — webhook Битрикса
   - `MANAGERS` — имена и ID менеджеров (найти через `user.search`)
   - `CATEGORY_ID` — ID воронки (через `crm.dealcategory.list`)
   - `CONFIRMED`, `REALIZED` — ID стадий (через `crm.dealcategory.stage.list`)
   - `EVENT_DATE_FIELD` — кастомное поле «дата мероприятия» (через `crm.deal.fields`)
   - `COMMISSION_FIELDS` — поля комиссий (через `crm.deal.fields`, искать по названию)
   - Планы менеджеров в функции `currentPlan()`
3. Бампнуть версии кэша до v1/yd1

## Обработка ошибок

| Ситуация | Действие |
|---|---|
| HTTP 429 / 503 | Exponential backoff, макс. 5 попыток |
| `insufficient_scope` | Аварийный останов: сообщить, какого права не хватает |
| Менеджер не найден | Пропустить, карточка с пометкой «не найден» |
| JS-ошибка на странице | Проверить консоль браузера; часто — несуществующая переменная после рефакторинга |
| GitHub push rejected | Сделать `git pull --rebase origin main` перед пушем |
| Git repo слетел в /tmp | Пересоздать клон с токеном из settings.local.json |
