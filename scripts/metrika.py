#!/usr/bin/env python3
"""Выгрузка данных Яндекс.Метрики по счётчику Ducati Parfums.

Токен читается из .env.metrika (YM_TOKEN=...) или переменной окружения YM_TOKEN.

    python3 metrika.py check                      # проверить доступ и список счётчиков
    python3 metrika.py goals                      # цели счётчика (id нужны для конверсий)
    python3 metrika.py summary 2026-08-01 2026-08-14
    python3 metrika.py sources 2026-08-01 2026-08-14
    python3 metrika.py utm 2026-08-01 2026-08-14  # source / campaign / content
    python3 metrika.py pages 2026-08-01 2026-08-14
    python3 metrika.py costs 2026-08-01 2026-08-16 # расход, клики, CPC, CPA по рекламным системам
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COUNTER_ID = "111262520"
BASE = "https://api-metrika.yandex.net"
# .env.metrika лежит в корне проекта; ищем рядом со скриптом, затем на уровень выше
_HERE = Path(__file__).resolve()
ENV_FILE = next(
    (c for c in (_HERE.with_name(".env.metrika"), _HERE.parent.parent / ".env.metrika") if c.exists()),
    _HERE.parent.parent / ".env.metrika",
)


def token() -> str:
    tok = os.environ.get("YM_TOKEN")
    if not tok and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("YM_TOKEN="):
                tok = line.split("=", 1)[1].strip().strip("'\"")
    if not tok:
        sys.exit(
            f"Нет токена. Положите его в {ENV_FILE} строкой YM_TOKEN=...\n"
            "Получить: https://oauth.yandex.ru/authorize?response_type=token&client_id=<CLIENT_ID>"
        )
    return tok


def api(path: str, **params) -> dict:
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"OAuth {token()}"})
    for attempt in range(4):  # API периодически рвёт TLS-хендшейк
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            sys.exit(f"HTTP {e.code}: {body}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 3:
                sys.exit(f"Сеть недоступна после 4 попыток: {e}")
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def report(date1: str, date2: str, metrics: str, dimensions: str = "", limit: int = 50) -> dict:
    params = {
        "ids": COUNTER_ID,
        "date1": date1,
        "date2": date2,
        "metrics": metrics,
        "limit": limit,
        "accuracy": "full",
    }
    if dimensions:
        params["dimensions"] = dimensions
    return api("/stat/v1/data", **params)


def show(data: dict) -> None:
    """Печатает отчёт таблицей: измерения слева, метрики справа."""
    dim_names = [q.rsplit(":", 1)[-1] for q in data["query"].get("dimensions", [])]
    met_names = [q.rsplit(":", 1)[-1] for q in data["query"]["metrics"]]

    rows = []
    for item in data["data"]:
        dims = [(d.get("name") or d.get("id") or "—") for d in item["dimensions"]]
        rows.append(dims + [num(m) for m in item["metrics"]])

    headers = dim_names + met_names
    if not rows:
        print("Нет данных за период.")
        return
    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    line = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    print(line(headers))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))

    totals = data.get("totals")
    if totals:
        print("  ".join("-" * w for w in widths))
        label = ["ИТОГО"] + [""] * (len(dim_names) - 1) if dim_names else []
        print(line(label + [num(t) for t in totals]))


BASIC = "ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:avgVisitDurationSeconds"

# Расходы рекламных систем живут в отдельном namespace ym:ev: — метрик ym:s:expenses* не существует.
# Загружаются раз в сутки: расход за вчера появляется сегодня.
COST_METRICS = ("ym:ev:expensesRUB,ym:ev:expenseClicks,ym:ev:expenseRUBCPC,"
                "ym:ev:visits,ym:ev:sumGoalReachesAny")


def num(x, dec: int = 2) -> str:
    if x is None:
        return "—"
    s = f"{x:,.{dec}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s.replace(",", " ")


def costs(date1: str, date2: str, dimensions: str, title: str) -> None:
    """Расход, клики, визиты и конверсии по рекламным системам.

    Берём только строки с ненулевым расходом: без фильтра в визиты попадёт весь остальной
    трафик и производные колонки превращаются в кашу. CPC, цену визита и CPA считаем сами —
    таких метрик в API нет. Итоги тоже свои: в отфильтрованном отчёте API отдаёт в totals
    визиты по всему счётчику.
    """
    data = api("/stat/v1/data", ids=COUNTER_ID, date1=date1, date2=date2,
               metrics=COST_METRICS, dimensions=dimensions, filters="ym:ev:expensesRUB>0",
               sort="-ym:ev:expensesRUB", limit=100, accuracy="full")

    dim_names = [q.rsplit(":", 1)[-1].replace("lastsign", "") for q in data["query"]["dimensions"]]
    headers = dim_names + ["расход, ₽", "клики", "CPC, ₽", "визиты", "клики→визиты",
                           "цели", "цена визита, ₽", "CPA, ₽"]

    def row(dims: list, spent: float, clicks: float, visits: float, goals: float) -> list:
        return dims + [num(spent), num(clicks, 0),
                       num(spent / clicks) if clicks else "—",
                       num(visits, 0),
                       f"{visits / clicks * 100:.0f} %" if clicks else "—",
                       num(goals, 0),
                       num(spent / visits) if visits else "—",
                       num(spent / goals) if goals else "—"]

    print(title)
    rows, total = [], [0.0, 0.0, 0.0, 0.0]
    for item in data["data"]:
        spent, clicks, _cpc, visits, goals = [v or 0 for v in item["metrics"]]
        rows.append(row([(d.get("name") or d.get("id") or "—") for d in item["dimensions"]],
                        spent, clicks, visits, goals))
        total = [t + v for t, v in zip(total, (spent, clicks, visits, goals))]
    if not rows:
        print("Расходов за период не загружено.\n")
        return
    rows.append(row(["ИТОГО"] + [""] * (len(dim_names) - 1), *total))

    widths = [max(len(str(r[i])) for r in [headers] + rows) for i in range(len(headers))]
    line = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    rule = "  ".join("-" * w for w in widths)
    print(line(headers)); print(rule)
    for r in rows[:-1]:
        print(line(r))
    print(rule); print(line(rows[-1]))


def costs_missing(date1: str, date2: str) -> None:
    """Источники с UTM-разметкой, по которым расходы не загрузились, — так видно сломанную связку."""
    data = api("/stat/v1/data", ids=COUNTER_ID, date1=date1, date2=date2,
               metrics="ym:ev:expensesRUB,ym:ev:visits",
               dimensions="ym:ev:lastsignExpenseSource",
               filters="ym:ev:lastsignExpenseSource!n", limit=100, accuracy="full")
    dead = [(it["dimensions"][0].get("name") or it["dimensions"][0].get("id"), it["metrics"][1])
            for it in data["data"] if not it["metrics"][0]]
    if dead:
        print("Расходы не загружены по источникам: " +
              ", ".join(f"{s} ({num(v, 0)} визитов)" for s, v in dead))


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "check"
    dates = args[1:3] if len(args) >= 3 else ["7daysAgo", "today"]

    if cmd == "check":
        info = api("/management/v1/counters")
        for c in info["counters"]:
            print(f"{c['id']}\t{c['name']}\t{c.get('site', '')}")
        ids = {str(c["id"]) for c in info["counters"]}
        print()
        print(f"Счётчик {COUNTER_ID}: {'доступен' if COUNTER_ID in ids else 'НЕТ ДОСТУПА'}")

    elif cmd == "goals":
        goals = api(f"/management/v1/counter/{COUNTER_ID}/goals")["goals"]
        if not goals:
            print("Целей нет — конверсии считать не с чего.")
        for g in goals:
            print(f"{g['id']}\t{g['type']}\t{g['name']}")

    elif cmd == "summary":
        show(report(*dates, metrics=BASIC + ",ym:s:pageviews", dimensions="ym:s:date"))

    elif cmd == "sources":
        show(report(*dates, metrics=BASIC,
                    dimensions="ym:s:lastSignTrafficSource,ym:s:lastSignSourceEngine"))

    elif cmd == "utm":
        show(report(*dates, metrics=BASIC,
                    dimensions="ym:s:UTMSource,ym:s:UTMCampaign,ym:s:UTMContent"))

    elif cmd == "pages":
        show(report(*dates, metrics="ym:pv:pageviews,ym:pv:users",
                    dimensions="ym:pv:URLPathFull"))

    elif cmd == "direct":
        # Директ размечен yclid, а не UTM — нужны свои измерения
        show(report(*dates, metrics=BASIC,
                    dimensions="ym:s:lastSignDirectClickOrder,ym:s:lastSignDirectPlatformType"))

    elif cmd == "costs":
        costs(*dates, dimensions="ym:ev:lastsignExpenseSource,ym:ev:lastsignExpenseCampaign",
              title="Расходы по источникам и кампаниям\n")
        print()
        costs(*dates, dimensions="ym:ev:date,ym:ev:lastsignExpenseSource",
              title="Расходы по дням\n")
        print()
        costs_missing(*dates)

    elif cmd == "conv":
        # достижения всех целей за период, суммарно
        goals = api(f"/management/v1/counter/{COUNTER_ID}/goals")["goals"]
        metrics = "ym:s:visits," + ",".join(f"ym:s:goal{g['id']}reaches" for g in goals)
        data = report(*dates, metrics=metrics, dimensions="ym:s:lastSignSourceEngine")
        names = ["визиты"] + [g["name"] for g in goals]
        data["query"]["metrics"] = names  # подменяем ym:s:goalNreaches на человеческие имена
        show(data)

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
