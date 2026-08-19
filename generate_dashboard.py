#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
generate_dashboard.py — серверная сборка дашборда активности менеджеров Preparty.

Полная замена клиентской версии (WH был вшит в index.html — виден всем в
интернете). Теперь: вебхук читается только из окружения (GitHub Actions
secret), все данные считаются здесь и запекаются в статический HTML.
Браузер посетителя больше НИКОГДА не обращается к Bitrix напрямую.

Логика метрик 1:1 перенесена из прежнего index.html (см. CLAUDE.md репозитория
dashboard) — часовой пояс, MOVED_TIME, комиссии, пагинация, backoff не менялись.

Новое: недельные отказы по причинам и зависшие контакты (компании без
действий N+ дней) — только для группы Дарьи. Это первая версия этих двух
блоков: поле "причина отказа" ищется автоматически по названию, здесь может
понадобиться калибровка после первого живого запуска.
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

MSK = timezone(timedelta(hours=3))
PAGE_PAUSE = 0.15
RETRY_PAUSES = [1, 2, 4, 8, 16]

CATEGORY_ID = 4
CONFIRMED = "C4:UC_3KB83W"
REALIZED = "C4:WON"
EVENT_DATE_FIELD = "UF_CRM_1594025532470"
COMMISSION_FIELDS = ["UF_CRM_1675687802829", "UF_CRM_1675689049773"]

MANAGERS = [
    {"name": "Кирасирова Анна", "id": "12752"},
    {"name": "Воронежцева Алина", "id": "9174"},
    {"name": "Айсина Динара", "id": "14262"},
    {"name": "Федотова Дарья", "id": "160"},
]

# Планы продаж по менеджерам: id -> { месяц: сумма } (перенесено как есть из index.html)
PLANS = {
    "12752": {4: 2982000, 5: 2070000, 6: 2802000, 7: 3048000, 8: 2520000, 9: 3108000},
    "9174": {4: 3314000, 5: 2250000, 6: 3104000, 7: 3556000, 8: 2940000, 9: 3626000},
    "14262": {4: 2048000, 5: 1440000, 6: 1928000, 7: 2032000, 8: 1680000, 9: 2072000},
    "160": {4: 1896000, 5: 1440000, 6: 1806000, 7: 1524000, 8: 1260000, 9: 1554000},
}

# Только группа Дарьи — для новых блоков (отказы, зависшие контакты)
DARYA_GROUP = [m for m in MANAGERS if m["id"] != "160"]  # Кирасирова, Воронежцева, Айсина
STALE_CONTACT_DAYS = 45


# --------------------------------------------------------------------------
# Bitrix24 REST helpers (тот же проверенный паттерн, что в stall_detector.py)
# --------------------------------------------------------------------------

def bx_call(webhook: str, method: str, params: dict) -> dict:
    url = f"{webhook.rstrip('/')}/{method}.json"
    for pause in [0] + RETRY_PAUSES:
        if pause:
            time.sleep(pause)
        resp = requests.post(url, json=params, timeout=30)
        if resp.status_code in (429, 503):
            continue
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"{method} → {data.get('error')}: {data.get('error_description')}")
        return data
    raise RuntimeError(f"{method}: превышено число ретраев (429/503)")


def bx_paginate(webhook: str, method: str, params: dict) -> list:
    items, start = [], 0
    while True:
        data = bx_call(webhook, method, dict(params, start=start))
        page = data.get("result")
        if isinstance(page, dict):
            page = page.get("tasks") or page.get("items") or []
        if not page:
            break
        items.extend(page)
        nxt = data.get("next")
        if nxt is None:
            break
        start = nxt
        time.sleep(PAGE_PAUSE)
    return items


def parse_money(value) -> float:
    """Bitrix отдаёт денежные поля то как '10233.9', то как '10233.9|RUB'
    (с кодом валюты через |). Берём только числовую часть перед |."""
    if value is None or value == "":
        return 0.0
    s = str(value).split("|")[0].strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def net_opportunity(deal: dict) -> float:
    opp = parse_money(deal.get("OPPORTUNITY"))
    comm = sum(parse_money(deal.get(f)) for f in COMMISSION_FIELDS)
    return opp - comm


# --------------------------------------------------------------------------
# Даты (МСК)
# --------------------------------------------------------------------------

def now_msk() -> datetime:
    return datetime.now(MSK)


def ymd(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def tomorrow_of(date_str: str) -> str:
    d = datetime.fromisoformat(date_str) + timedelta(days=1)
    return ymd(d)


def month_bounds(base: datetime, offset: int):
    """offset=0 текущий месяц, -1 прошлый, +1 следующий. Возвращает (start_iso, end_iso, month_num)."""
    y, m = base.year, base.month + offset
    while m < 1:
        m += 12
        y -= 1
    while m > 12:
        m -= 12
        y += 1
    start = f"{y}-{m:02d}-01T00:00:00+03:00"
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    end = f"{ny}-{nm:02d}-01T00:00:00+03:00"
    return start, end, m


# --------------------------------------------------------------------------
# Метрики за день (перенесено из index.html)
# --------------------------------------------------------------------------

def get_calls(webhook, user_id, from_time, date_str) -> int:
    tmr = tomorrow_of(date_str)
    items = bx_paginate(webhook, "voximplant.statistic.get", {
        "filter": {"PORTAL_USER_ID": user_id, ">=CALL_START_DATE": date_str, "<CALL_START_DATE": tmr},
        "select": ["CALL_DURATION", "CALL_START_DATE"],
    })
    return sum(1 for i in items if i.get("CALL_START_DATE", "") >= from_time and int(i.get("CALL_DURATION") or 0) >= 40)


def get_new_deals(webhook, user_id, from_time, date_str) -> int:
    tmr = tomorrow_of(date_str)
    deals = bx_paginate(webhook, "crm.deal.list", {
        "filter": {"CATEGORY_ID": CATEGORY_ID, "ASSIGNED_BY_ID": user_id, ">=DATE_CREATE": date_str, "<DATE_CREATE": tmr},
        "select": ["ID", "DATE_CREATE"],
    })
    return sum(1 for d in deals if d.get("DATE_CREATE", "") >= from_time)


def get_daily_confirmed(webhook, user_id, date_str) -> float:
    from_time = f"{date_str}T00:00:00+03:00"
    to_time = f"{tomorrow_of(date_str)}T00:00:00+03:00"
    deals = bx_paginate(webhook, "crm.deal.list", {
        "filter": {"ASSIGNED_BY_ID": user_id, "CATEGORY_ID": CATEGORY_ID, "STAGE_ID": CONFIRMED, ">=DATE_MODIFY": from_time},
        "select": ["ID", "OPPORTUNITY", "MOVED_TIME"] + COMMISSION_FIELDS,
    })
    return sum(net_opportunity(d) for d in deals if from_time <= d.get("MOVED_TIME", "") < to_time)


def get_daily_realized(webhook, user_id, date_str) -> float:
    tmr = tomorrow_of(date_str)
    deals = bx_paginate(webhook, "crm.deal.list", {
        "filter": {"ASSIGNED_BY_ID": user_id, "CATEGORY_ID": CATEGORY_ID, "STAGE_ID": REALIZED,
                   f">={EVENT_DATE_FIELD}": date_str, f"<{EVENT_DATE_FIELD}": tmr},
        "select": ["ID", "OPPORTUNITY"] + COMMISSION_FIELDS,
    })
    return sum(net_opportunity(d) for d in deals)


def get_tasks(webhook, user_id, from_time, date_str) -> int:
    tmr = tomorrow_of(date_str)
    to_time = f"{tmr}T00:00:00+03:00"
    changed = bx_paginate(webhook, "tasks.task.list", {
        "filter": {"RESPONSIBLE_ID": user_id, ">=CHANGED_DATE": date_str, "<CHANGED_DATE": tmr},
        "select": ["ID"],
    })
    count = 0
    for t in changed:
        tid = t.get("id") or t.get("ID")
        if not tid:
            continue
        resp = bx_call(webhook, "tasks.task.get", {"taskId": tid})
        task = (resp.get("result") or {}).get("task") or {}
        closed = task.get("closedDate") or ""
        if task.get("status") == "5" and from_time <= closed < to_time and str(task.get("closedBy") or "") == str(user_id):
            count += 1
    return count


def collect_day_metrics(webhook, user_id, date_str) -> dict:
    from_time = f"{date_str}T00:00:00+03:00"
    return {
        "calls": get_calls(webhook, user_id, from_time, date_str),
        "newDeals": get_new_deals(webhook, user_id, from_time, date_str),
        "confirmedSum": get_daily_confirmed(webhook, user_id, date_str),
        "realizedSum": get_daily_realized(webhook, user_id, date_str),
        "tasks": get_tasks(webhook, user_id, from_time, date_str),
    }


# --------------------------------------------------------------------------
# Метрики за месяц (перенесено из index.html)
# --------------------------------------------------------------------------

def get_month_realized(webhook, user_id, month_start, month_end) -> float:
    in_confirmed = bx_paginate(webhook, "crm.deal.list", {
        "filter": {"ASSIGNED_BY_ID": user_id, "CATEGORY_ID": CATEGORY_ID, "STAGE_ID": CONFIRMED,
                   f">={EVENT_DATE_FIELD}": month_start, f"<{EVENT_DATE_FIELD}": month_end},
        "select": ["ID", "OPPORTUNITY"] + COMMISSION_FIELDS,
    })
    won = bx_paginate(webhook, "crm.deal.list", {
        "filter": {"ASSIGNED_BY_ID": user_id, "CATEGORY_ID": CATEGORY_ID, "STAGE_ID": "C4:WON", ">=DATE_MODIFY": month_start},
        "select": ["ID", "OPPORTUNITY", "PREVIOUS_STAGE_ID", "MOVED_TIME"] + COMMISSION_FIELDS,
    })
    won_from_confirmed = [d for d in won if d.get("PREVIOUS_STAGE_ID") == CONFIRMED and d.get("MOVED_TIME", "") >= month_start]
    seen, total = set(), 0.0
    for d in in_confirmed + won_from_confirmed:
        if d["ID"] in seen:
            continue
        seen.add(d["ID"])
        total += net_opportunity(d)
    return total


def get_prev_month_realized(webhook, user_id, prev_start, prev_end) -> float:
    won = bx_paginate(webhook, "crm.deal.list", {
        "filter": {"ASSIGNED_BY_ID": user_id, "CATEGORY_ID": CATEGORY_ID, "STAGE_ID": "C4:WON", ">=DATE_MODIFY": prev_start},
        "select": ["ID", "OPPORTUNITY", "MOVED_TIME"] + COMMISSION_FIELDS,
    })
    return sum(net_opportunity(d) for d in won if prev_start <= d.get("MOVED_TIME", "") < prev_end)


def current_plan(user_id: str, month_num: int) -> float:
    return PLANS.get(user_id, {}).get(month_num, 0)


# --------------------------------------------------------------------------
# НОВОЕ: недельные отказы по причинам (только группа Дарьи)
# --------------------------------------------------------------------------

def find_reason_field(webhook: str):
    """Ищет кастомное поле «причина отказа» по названию. Возвращает (код, {id_значения: текст})."""
    fields = bx_call(webhook, "crm.deal.fields", {})["result"]
    for code, meta in fields.items():
        title = (meta.get("title") or "").lower()
        if "причин" in title and code.startswith("UF_"):
            items = meta.get("items") or []
            labels = {str(it.get("ID")): it.get("VALUE") for it in items} if items else {}
            return code, labels
    return None, {}


def get_weekly_rejections(webhook: str, week_start: str):
    """Отказы группы Дарьи за последние 7 дней. STAGE_SEMANTIC_ID='F' — стандартный
    признак Bitrix для проигранной сделки, есть в любой воронке."""
    reason_field, labels = find_reason_field(webhook)
    manager_ids = [m["id"] for m in DARYA_GROUP]
    select = ["ID", "TITLE", "ASSIGNED_BY_ID", "DATE_MODIFY"]
    if reason_field:
        select.append(reason_field)

    deals = bx_paginate(webhook, "crm.deal.list", {
        "filter": {"CATEGORY_ID": CATEGORY_ID, "ASSIGNED_BY_ID": manager_ids,
                   "STAGE_SEMANTIC_ID": "F", ">=DATE_MODIFY": week_start},
        "select": select,
    })

    by_reason, by_manager = {}, {m["id"]: 0 for m in DARYA_GROUP}
    for d in deals:
        mid = str(d.get("ASSIGNED_BY_ID"))
        if mid in by_manager:
            by_manager[mid] += 1
        raw = d.get(reason_field) if reason_field else None
        reason = labels.get(str(raw), raw) if raw else "Не указано"
        by_reason[reason] = by_reason.get(reason, 0) + 1

    return {
        "total": len(deals),
        "by_reason": sorted(by_reason.items(), key=lambda x: -x[1]),
        "by_manager": [(next(m["name"] for m in DARYA_GROUP if m["id"] == mid), c) for mid, c in by_manager.items()],
        "reason_field_found": reason_field is not None,
    }


# --------------------------------------------------------------------------
# НОВОЕ: зависшие контакты (только группа Дарьи)
# --------------------------------------------------------------------------

def get_stale_contacts(webhook: str, threshold_days: int):
    manager_ids = [m["id"] for m in DARYA_GROUP]
    companies = bx_paginate(webhook, "crm.company.list", {
        "filter": {"ASSIGNED_BY_ID": manager_ids},
        "select": ["ID", "TITLE", "ASSIGNED_BY_ID", "DATE_MODIFY"],
    })
    if not companies:
        return []

    # Последняя активность по компании — из crm.activity.list (звонки, письма,
    # сообщения, задачи), OWNER_TYPE_ID=4 — компания.
    activities = bx_paginate(webhook, "crm.activity.list", {
        "filter": {"OWNER_TYPE_ID": 4, "RESPONSIBLE_ID": manager_ids},
        "select": ["OWNER_ID", "CREATED"],
        "order": {"CREATED": "DESC"},
    })
    last_activity = {}
    for a in activities:
        oid = str(a.get("OWNER_ID"))
        if oid not in last_activity:
            last_activity[oid] = a.get("CREATED")

    now = now_msk()
    stale = []
    for c in companies:
        cid = str(c["ID"])
        last = last_activity.get(cid) or c.get("DATE_MODIFY")
        if not last:
            continue
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            continue
        days = (now - last_dt).days
        if days >= threshold_days:
            manager_name = next((m["name"] for m in DARYA_GROUP if m["id"] == str(c.get("ASSIGNED_BY_ID"))), "?")
            stale.append({"name": c.get("TITLE") or f"Компания #{cid}", "manager": manager_name, "days": days})

    stale.sort(key=lambda x: -x["days"])
    return stale


# --------------------------------------------------------------------------
# Сборка HTML
# --------------------------------------------------------------------------

def fmt_money(n):
    if not n:
        return "—"
    return f"{round(n):,}".replace(",", " ") + " ₽"


def fmt_num(n):
    return str(n) if n else "—"


MONTH_NAMES = {1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель", 5: "Май", 6: "Июнь",
               7: "Июль", 8: "Август", 9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"}


def build_month_table_rows(managers_data, key_real, key_plan):
    rows = []
    for name, m in sorted(managers_data, key=lambda x: -(x[1].get("realizedSum") or 0)):
        plan = m.get(key_plan, 0)
        real = m.get(key_real, 0)
        rem = plan - real
        rem_html = "—" if plan <= 0 else ("✓ Выполнен" if rem <= 0 else fmt_money(rem))
        rows.append(f"<tr><td class='mname'>{name}</td><td class='money'>{fmt_money(plan) if plan else '—'}</td>"
                     f"<td class='money'>{fmt_money(real)}</td><td>{rem_html}</td></tr>")
    return "".join(rows)


def build_day_table_rows(managers_data, day_key):
    rows = []
    for name, m in sorted(managers_data, key=lambda x: -(x[1].get("realizedSum") or 0)):
        d = m[day_key]
        rows.append(f"<tr><td class='mname'>{name}</td><td>{fmt_num(d['calls'])}</td><td>{fmt_num(d['newDeals'])}</td>"
                     f"<td class='money'>{fmt_money(d['confirmedSum'])}</td><td class='money'>{fmt_money(d['realizedSum'])}</td>"
                     f"<td>{fmt_num(d['tasks'])}</td></tr>")
    return "".join(rows)


def build_html(data: dict, generated_at: str) -> str:
    with open(os.path.join(os.path.dirname(__file__), "template.html"), encoding="utf-8") as f:
        template = f.read()
    return template.replace("__DATA_JSON__", json.dumps(data, ensure_ascii=False)).replace("__GENERATED_AT__", generated_at)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--webhook", default=os.environ.get("BITRIX_WEBHOOK"))
    p.add_argument("--output", default="index.html")
    p.add_argument("--stale-days", type=int, default=STALE_CONTACT_DAYS)
    args = p.parse_args()

    if not args.webhook:
        print("Нужен --webhook или переменная окружения BITRIX_WEBHOOK", file=sys.stderr)
        sys.exit(1)

    now = now_msk()
    today = ymd(now)
    yesterday = ymd(now - timedelta(days=1))
    week_ago = ymd(now - timedelta(days=7)) + "T00:00:00+03:00"

    cur_start, cur_end, cur_m = month_bounds(now, 0)
    prev_start, prev_end, prev_m = month_bounds(now, -1)
    next_start, next_end, next_m = month_bounds(now, 1)

    managers_data = []
    for mgr in MANAGERS:
        uid = mgr["id"]
        print(f"Считаю: {mgr['name']}...", file=sys.stderr)
        today_m = collect_day_metrics(args.webhook, uid, today)
        yest_m = collect_day_metrics(args.webhook, uid, yesterday)
        month_real = get_month_realized(args.webhook, uid, cur_start, cur_end)
        prev_real = get_prev_month_realized(args.webhook, uid, prev_start, prev_end)
        next_real = get_month_realized(args.webhook, uid, next_start, next_end)

        managers_data.append((mgr["name"], {
            "today": today_m,
            "yesterday": yest_m,
            "realizedSum": today_m["realizedSum"],
            "monthlyRealized": month_real,
            "plan": current_plan(uid, cur_m),
            "prevMonthlyRealized": prev_real,
            "prevPlan": current_plan(uid, prev_m),
            "nextMonthlyRealized": next_real,
            "nextPlan": current_plan(uid, next_m),
        }))

    print("Считаю отказы за неделю...", file=sys.stderr)
    rejections = get_weekly_rejections(args.webhook, week_ago)

    print("Считаю зависшие контакты...", file=sys.stderr)
    stale_contacts = get_stale_contacts(args.webhook, args.stale_days)

    data = {
        "generatedAt": now.strftime("%Y-%m-%d %H:%M"),
        "months": {
            "current": {"label": MONTH_NAMES[cur_m], "rows": build_month_table_rows(managers_data, "monthlyRealized", "plan")},
            "prev": {"label": MONTH_NAMES[prev_m], "rows": build_month_table_rows(managers_data, "prevMonthlyRealized", "prevPlan")},
            "next": {"label": MONTH_NAMES[next_m], "rows": build_month_table_rows(managers_data, "nextMonthlyRealized", "nextPlan")},
        },
        "days": {
            "today": {"label": f"Сегодня: {today}", "rows": build_day_table_rows(managers_data, "today")},
            "yesterday": {"label": f"Вчера: {yesterday}", "rows": build_day_table_rows(managers_data, "yesterday")},
        },
        "rejections": rejections,
        "staleContacts": stale_contacts,
        "staleDays": args.stale_days,
    }

    html = build_html(data, data["generatedAt"])
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Готово: {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
