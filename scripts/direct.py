#!/usr/bin/env python3
"""Выгрузка расходов и показов из API Яндекс.Директа (Reports, JSON v5).

Метрика не отдаёт показы, CTR и CPM — их можно взять только здесь.
Токен читается из .env.metrika (YD_TOKEN=...) или переменной окружения YD_TOKEN.
Если кабинет агентский — там же YD_CLIENT_LOGIN=логин-клиента.

    python3 direct.py 2026-08-07 2026-08-13            # по кампаниям
    python3 direct.py 2026-08-07 2026-08-13 --by-day   # по дням
"""

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.direct.yandex.com/json/v5/reports"
# .env.metrika лежит в корне проекта; ищем рядом со скриптом, затем на уровень выше
_HERE = Path(__file__).resolve()
ENV_FILE = next(
    (c for c in (_HERE.with_name(".env.metrika"), _HERE.parent.parent / ".env.metrika") if c.exists()),
    _HERE.parent.parent / ".env.metrika",
)

FIELDS = ["CampaignName", "AdNetworkType", "Impressions", "Clicks", "Ctr", "Cost", "AvgCpc"]


def env(key: str, required: bool = True) -> str:
    import os
    val = os.environ.get(key, "")
    if not val and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip().strip("'\"")
    if not val and required:
        sys.exit(
            f"Нет {key}. Добавьте строкой {key}=... в {ENV_FILE}\n"
            "Токен нужен со scope direct:api — это отдельный токен, не тот, что для Метрики."
        )
    return val


def fetch(date1: str, date2: str, fields: list, vat: bool = True) -> str:
    body = json.dumps({
        "params": {
            "SelectionCriteria": {"DateFrom": date1, "DateTo": date2},
            "FieldNames": fields,
            "ReportName": f"ducati {date1}..{date2} {int(time.time())}",
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES" if vat else "NO",
            "IncludeDiscount": "NO",
        }
    }, ensure_ascii=False).encode("utf-8")

    headers = {
        "Authorization": f"Bearer {env('YD_TOKEN')}",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "processingMode": "auto",
        "returnMoneyInMicros": "false",   # иначе суммы приходят в микрорублях
        "skipReportHeader": "true",
        "skipReportSummary": "true",
    }
    client_login = env("YD_CLIENT_LOGIN", required=False)
    if client_login:
        headers["Client-Login"] = client_login

    for attempt in range(10):
        req = urllib.request.Request(ENDPOINT, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if resp.status in (201, 202):  # отчёт встал в очередь
                    time.sleep(int(resp.headers.get("retryIn", 5)))
                    continue
                text = resp.read().decode("utf-8")
                # Директ отдаёт отказ с кодом 200, положив ошибку в тело ответа
                if text.lstrip().startswith("{"):
                    err = json.loads(text).get("error", {})
                    sys.exit(f"Директ отказал: код {err.get('error_code')} — "
                             f"{err.get('error_string')}. {err.get('error_detail', '')}")
                return text
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:600]}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 9:
                sys.exit(f"Сеть недоступна: {e}")
            time.sleep(2 ** min(attempt, 4))
    sys.exit("Отчёт так и не собрался — попробуйте позже.")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 2:
        sys.exit(__doc__)

    fields = list(FIELDS)
    if "--by-day" in sys.argv:
        fields.insert(0, "Date")

    tsv = fetch(args[0], args[1], fields, vat="--no-vat" not in sys.argv)
    rows = [r.split("\t") for r in tsv.strip().splitlines() if r.strip()]
    if len(rows) < 2:
        print("Данных за период нет.")
        return

    head, data = rows[0], rows[1:]
    # CPM Директ не отдаёт — считаем сами
    head = head + ["Cpm"]
    for r in data:
        cost = float(r[head.index("Cost")])
        impr = float(r[head.index("Impressions")])
        r.append(f"{cost / impr * 1000:.2f}" if impr else "—")

    widths = [max(len(str(r[i])) for r in [head] + data) for i in range(len(head))]
    line = lambda cells: "  ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)).rstrip()
    print(line(head))
    print("  ".join("-" * w for w in widths))
    for r in data:
        print(line(r))

    idx = {n: head.index(n) for n in ("Impressions", "Clicks", "Cost")}
    impr = sum(int(r[idx["Impressions"]]) for r in data)
    clicks = sum(int(r[idx["Clicks"]]) for r in data)
    cost = sum(float(r[idx["Cost"]]) for r in data)
    print("  ".join("-" * w for w in widths))
    print(f"ИТОГО  показы {impr:,}  клики {clicks:,}  расход {cost:,.2f} ₽  "
          f"CTR {clicks / impr * 100:.2f}%  CPC {cost / clicks:.2f} ₽  CPM {cost / impr * 1000:.2f} ₽"
          .replace(",", " "))


if __name__ == "__main__":
    main()
