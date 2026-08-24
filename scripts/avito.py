#!/usr/bin/env python3
"""Выгрузка статистики из публичного API Авито Рекламы.

Ключи создаются в кабинете Авито Рекламы: Настройки аккаунта → API (роль администратора).
Секрет показывается один раз. Кладём в .env.metrika:

    AV_CLIENT_ID=...
    AV_CLIENT_SECRET=...
    AV_ACCOUNT_ID=1132257

    python3 avito.py token                         # проверить ключи
    python3 avito.py campaigns                     # список кампаний
    python3 avito.py stats 2026-08-07 2026-08-13   # статистика по кампаниям за период
    python3 avito.py days  2026-08-07 2026-08-13   # то же, с разбивкой по дням
    python3 avito.py groups 2026-08-07 2026-08-13  # по группам внутри кампаний
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://api.avito.ru"
# .env.metrika лежит в корне проекта; ищем рядом со скриптом, затем на уровень выше
_HERE = Path(__file__).resolve()
ENV_FILE = next(
    (c for c in (_HERE.with_name(".env.metrika"), _HERE.parent.parent / ".env.metrika") if c.exists()),
    _HERE.parent.parent / ".env.metrika",
)
_tok = {}


def env(key: str, required: bool = True) -> str:
    import os
    val = os.environ.get(key, "")
    if not val and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(f"{key}="):
                val = line.split("=", 1)[1].strip().strip("'\"")
    if not val and required:
        sys.exit(f"Нет {key}. Добавьте строкой {key}=... в {ENV_FILE}")
    return val


def token() -> str:
    if _tok.get("exp", 0) > time.time():
        return _tok["t"]
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": env("AV_CLIENT_ID"),
        "client_secret": env("AV_CLIENT_SECRET"),
    }).encode()
    req = urllib.request.Request(f"{BASE}/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Авторизация не прошла — HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
    _tok["t"] = d["access_token"]
    _tok["exp"] = time.time() + int(d.get("expires_in", 3600)) - 60
    return _tok["t"]


def api(path: str, body=None, method: str = "POST", quiet: bool = False):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {token()}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:400]
        if quiet:
            return {"__error__": e.code, "__body__": msg}
        sys.exit(f"HTTP {e.code} на {method} {path}: {msg}")


def acc() -> str:
    return env("AV_ACCOUNT_ID")


def campaigns(limit: int = 100) -> list:
    # filter, limit и page — обязательные поля по спецификации
    d = api(f"/ads/v1/account/{acc()}/campaigns",
            {"filter": {}, "limit": limit, "page": 1})
    return d.get("campaigns", d if isinstance(d, list) else d.get("result", []))


def money(row: dict) -> float:
    """Расход в рублях. Копейки точнее целых рублей из spend."""
    if "spendKopeks" in row:
        return row["spendKopeks"] / 100
    return float(row.get("spend", 0))


def show(rows: list, head: list, widths_from=None) -> None:
    if not rows:
        print("Нет данных за период.")
        return
    w = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    line = lambda c: "  ".join(str(x).ljust(w[i]) for i, x in enumerate(c)).rstrip()
    print(line(head))
    print("  ".join("-" * x for x in w))
    for r in rows:
        print(line(r))


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "token"

    if cmd == "token":
        print("токен получен, длина", len(token()), "— ключи рабочие")

    elif cmd == "campaigns":
        for c in campaigns():
            print(f"{c.get('id'):>12}  {c.get('status',''):<16} {c.get('paymentModel',''):<5} "
                  f"{c.get('name','')}")

    elif cmd == "groups":
        # Показы и клики по группам. Целей здесь нет — они только в Метрике,
        # поэтому у групп должны быть разные utm, иначе конверсии не развести.
        if len(args) < 3:
            sys.exit("Нужны две даты: groups 2026-08-07 2026-08-13")
        d1, d2 = args[1], args[2]
        rows = []
        all_groups = api(f"/ads/v1/account/{acc()}/groups",
                         {"filter": {}, "limit": 100, "page": 1}).get("groups", [])
        for c in campaigns():
            # groupIDs обязателен — пустой список эндпоинт не принимает
            ids = [g["id"] for g in all_groups if g.get("campaignID") == c["id"]]
            if not ids:
                continue
            d = api(f"/ads/v1/account/{acc()}/campaigns/{c['id']}/groups/stats",
                    {"dateFrom": d1, "dateTo": d2, "groupIDs": ids})
            for g in d.get("groups", []):
                t = g.get("totalData", {})
                sp, v, cl = money(t), t.get("views", 0), t.get("clicks", 0)
                rows.append([c.get("name", "")[:22], g.get("name", "")[:22],
                             f"{sp:,.2f}".replace(",", " "), f"{v:,}".replace(",", " "),
                             str(cl), f"{cl / v * 100:.2f} %" if v else "—",
                             f"{sp / cl:.2f}" if cl else "—"])
        show(rows, ["Кампания", "Группа", "Расход, ₽", "Показы", "Клики", "CTR", "CPC, ₽"])

    elif cmd in ("stats", "days"):
        if len(args) < 3:
            sys.exit("Нужны две даты: stats 2026-08-07 2026-08-13")
        d1, d2 = args[1], args[2]
        rows, tot = [], {"views": 0, "clicks": 0, "spend": 0.0, "bonus": 0.0}

        for c in campaigns():
            # Ответ обёрнут: {"campaign": {...}, "groups": [...], "creatives": [...]}
            st = api(f"/ads/v1/account/{acc()}/campaigns/{c['id']}/stats",
                     {"dateFrom": d1, "dateTo": d2}).get("campaign", {})
            total = st.get("totalData", {})
            spend = money(total)
            views, clicks = total.get("views", 0), total.get("clicks", 0)
            tot["views"] += views
            tot["clicks"] += clicks
            tot["spend"] += spend
            tot["bonus"] += total.get("spendBonusKopeks", 0) / 100

            if cmd == "stats":
                rows.append([st.get("name") or c.get("name", ""), f"{spend:,.2f}".replace(",", " "),
                             f"{views:,}".replace(",", " "), f"{clicks:,}".replace(",", " "),
                             f"{clicks / views * 100:.2f} %" if views else "—",
                             f"{spend / clicks:.2f}" if clicks else "—",
                             f"{spend / views * 1000:.2f}" if views else "—"])
            else:
                for day in st.get("data", []):
                    v, cl = day.get("views", 0), day.get("clicks", 0)
                    rows.append([(day.get("timestamp") or "")[:10], st.get("name", "")[:28],
                                 f"{money(day):,.2f}".replace(",", " "),
                                 f"{v:,}".replace(",", " "), str(cl),
                                 f"{cl / v * 100:.2f} %" if v else "—"])

        if cmd == "stats":
            show(rows, ["Кампания", "Расход, ₽", "Показы", "Клики", "CTR", "CPC, ₽", "CPM, ₽"])
        else:
            rows.sort()
            show(rows, ["Дата", "Кампания", "Расход, ₽", "Показы", "Клики", "CTR"])

        v, cl, sp = tot["views"], tot["clicks"], tot["spend"]
        print()
        print(f"ИТОГО  расход {sp:,.2f} ₽".replace(",", " ") +
              (f" (+ бонусы {tot['bonus']:,.2f} ₽)".replace(",", " ") if tot["bonus"] else "") +
              f"  показы {v:,}  клики {cl:,}".replace(",", " ") +
              (f"  CTR {cl / v * 100:.2f}%  CPC {sp / cl:.2f} ₽  CPM {sp / v * 1000:.2f} ₽"
               if v and cl else ""))

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
