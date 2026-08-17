#!/usr/bin/env python3
"""Сборка дашборда: воронка трафика Ducati Parfums по всем каналам.

Тянет четыре источника и складывает их в один HTML-файл с зашитыми данными —
открывается двойным кликом, работает без интернета, ничего не грузит со стороны.

    python3 "Аналитика и скрипты/dashboard.py"                       # с 1 августа по сегодня
    python3 "Аналитика и скрипты/dashboard.py" 2026-08-01 2026-08-17
    python3 "Аналитика и скрипты/dashboard.py" --выход путь.html
    python3 "Аналитика и скрипты/dashboard.py" --артефакт путь.html  # ещё и версия без каркаса
    python3 "Аналитика и скрипты/dashboard.py" --для-клиента         # без «что требует внимания» и подвала

Откуда что берётся:
  показы, клики, расход  — из рекламных кабинетов (Директ, Авито, ВК): только там
                           есть показы, и цифры там первичные;
  визиты, отказы, цели   — из Метрики: только она видит, что было после клика.

Связка «кампания в кабинете ↔ кампания в Метрике»:
  Директ — по имени кампании (ym:s:lastSignDirectClickOrder);
  Авито  — по utm_campaign, соответствие имён в AVITO_UTM;
  ВК     — по utm_campaign, там лежит id группы объявлений.
"""

import html
import importlib.util
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent   # рядом лежат metrika.py, direct.py, avito.py, vk.py
ROOT = SCRIPTS.parent
# В проекте шаблон лежит в папке отчёта, в репозитории публикации — рядом со скриптом.
TEMPLATE = next((c for c in (ROOT / "Отчёты" / "Дашборд" / "шаблон.html", SCRIPTS / "шаблон.html")
                 if c.exists()), ROOT / "Отчёты" / "Дашборд" / "шаблон.html")
OUTPUT = ROOT / "Отчёты" / "Дашборд" / "Дашборд — воронка трафика.html"

START = "2026-08-01"  # раньше этой даты трафика на сайте не было

# utm_campaign Авито → имя кампании в кабинете. Разметку ставим руками, поэтому
# соответствие тоже руками; всё несопоставленное дашборд покажет отдельной строкой.
AVITO_UTM = {"moto": "мотолюбители", "moto_kw": "мотолюбители - ключи"}

# Кампании, которые ведут не на сайт, а сразу на карточку маркетплейса.
# У них по определению нет визитов — считать это «потерей» нельзя.
MARKETPLACE_TRAFFIC = {"продажи на маркетплейсах от 11.08.26"}

CHANNELS = [
    {"key": "direct", "name": "Яндекс Директ", "short": "Директ"},
    {"key": "avito", "name": "Авито Реклама", "short": "Авито"},
    {"key": "vk", "name": "VK Реклама", "short": "ВК"},
    {"key": "other", "name": "Без рекламы", "short": "Без рекламы"},
]
ADV_ENGINE = {"ya_direct": "direct", "ya_undefined": "direct",
              "avitoads": "avito", "vk_ads": "vk"}


def load(name: str):
    """Импортирует соседний скрипт как модуль — переиспользуем его авторизацию."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def norm(s: str) -> str:
    """Имя кампании для сравнения: Директ отдаёт неразрывные пробелы, Метрика — обычные."""
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip().lower()


def f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


# ─────────────────────────────  Метрика: что было после клика  ─────────────────

def metrika_data(d1: str, d2: str) -> dict:
    m = load("metrika")
    goals = m.api(f"/management/v1/counter/{m.COUNTER_ID}/goals")["goals"]
    gm = ",".join(f"ym:s:goal{g['id']}reaches" for g in goals)
    base = "ym:s:visits,ym:s:users,ym:s:bounceRate,ym:s:avgVisitDurationSeconds"

    def rows(dimensions: str, metrics: str, limit: int = 10000, **extra) -> list:
        d = m.api("/stat/v1/data", ids=m.COUNTER_ID, date1=d1, date2=d2, metrics=metrics,
                  dimensions=dimensions, limit=limit, accuracy="full", **extra)
        return d["data"]

    # 1. Канал × день — основа воронки. AdvEngine отдаёт устойчивые id, а не подписи.
    days = []
    for it in rows("ym:s:date,ym:s:lastsignAdvEngine", f"{base},{gm}"):
        day, eng = it["dimensions"]
        visits, users, bounce, dur = it["metrics"][:4]
        days.append({
            "d": day.get("name"),
            "ch": ADV_ENGINE.get(eng.get("id"), "other"),
            "visits": f(visits), "users": f(users),
            "bounce": f(bounce), "dur": f(dur),
            "g": [f(v) for v in it["metrics"][4:]],
        })

    # 2. Кампания × день по Директу: yclid, а не utm, поэтому своё измерение.
    # Визиты не из Директа приходят той же выборкой с пустой кампанией — их отбрасываем,
    # иначе весь остальной трафик слипается в одну фантомную «кампанию Директа».
    site = []
    for it in rows("ym:s:date,ym:s:lastSignDirectClickOrder", f"ym:s:visits,ym:s:bounceRate,{gm}"):
        day, camp = it["dimensions"]
        if not norm(camp.get("name")):
            continue
        site.append({"d": day.get("name"), "ch": "direct", "key": norm(camp.get("name")),
                     "visits": f(it["metrics"][0]), "bounce": f(it["metrics"][1]),
                     "g": [f(v) for v in it["metrics"][2:]]})

    # 3. Кампания × день по utm — сюда попадают Авито и ВК.
    for it in rows("ym:s:date,ym:s:UTMSource,ym:s:UTMCampaign", f"ym:s:visits,ym:s:bounceRate,{gm}"):
        day, src, camp = it["dimensions"]
        s = (src.get("name") or "").lower()
        ch = "avito" if s.startswith("avito") else "vk" if s.startswith("vk") else None
        if not ch:
            continue
        raw = camp.get("name") or ""
        if not raw.strip():   # визит без utm_campaign — привязать не к чему
            continue
        key = norm(AVITO_UTM.get(raw, raw)) if ch == "avito" else raw.strip()
        site.append({"d": day.get("name"), "ch": ch, "key": key,
                     "visits": f(it["metrics"][0]), "bounce": f(it["metrics"][1]),
                     "g": [f(v) for v in it["metrics"][2:]]})

    return {"goals": [{"id": str(g["id"]), "name": g["name"]} for g in goals],
            "days": days, "site": site}


# ─────────────────────────────  Рекламные кабинеты: до клика  ──────────────────

def direct_rows(d1: str, d2: str) -> list:
    d = load("direct")
    fields = ["Date", "CampaignName", "AdNetworkType", "Impressions", "Clicks", "Cost"]
    tsv = d.fetch(d1, d2, fields)
    lines = [r.split("\t") for r in tsv.strip().splitlines() if r.strip()]
    if len(lines) < 2:
        return []
    head = lines[0]
    i = {n: head.index(n) for n in fields}
    out = []
    for r in lines[1:]:
        name = r[i["CampaignName"]]
        out.append({"d": r[i["Date"]], "ch": "direct", "camp": re.sub(r"\s+", " ", name.replace("\xa0", " ")).strip(),
                    "key": norm(name), "net": "поиск" if r[i["AdNetworkType"]] == "SEARCH" else "сети",
                    "imp": f(r[i["Impressions"]]), "clicks": f(r[i["Clicks"]]), "cost": f(r[i["Cost"]])})
    return out


def avito_rows(d1: str, d2: str) -> list:
    a = load("avito")
    out = []
    for c in a.campaigns():
        st = a.api(f"/ads/v1/account/{a.acc()}/campaigns/{c['id']}/stats",
                   {"dateFrom": d1, "dateTo": d2}).get("campaign", {})
        name = st.get("name") or c.get("name", "")
        for day in st.get("data", []):
            out.append({"d": (day.get("timestamp") or "")[:10], "ch": "avito", "camp": name,
                        "key": norm(name), "net": "", "imp": f(day.get("views")),
                        "clicks": f(day.get("clicks")), "cost": a.money(day)})
    return out


def vk_rows(d1: str, d2: str) -> list:
    v = load("vk")
    groups = v.objects("ad_groups")
    if not groups:
        return []
    names = {str(g["id"]): g.get("name", "") for g in groups}
    # В статистике ВК группы объявлений называются campaigns — уровень тот же.
    d = v.stats("campaigns", list(names), d1, d2)
    out = []
    for item in d.get("items", []):
        gid = str(item.get("id"))
        for row in item.get("rows", []):
            b = row.get("base", row)
            if not (f(b.get("shows")) or f(b.get("spent"))):
                continue
            out.append({"d": row.get("date", ""), "ch": "vk", "camp": names.get(gid, gid),
                        "key": gid, "net": "", "imp": f(b.get("shows")),
                        "clicks": f(b.get("clicks")), "cost": f(b.get("spent"))})
    return out


# ─────────────────────────────  Сборка  ────────────────────────────────────────

def collect(d1: str, d2: str, client: bool = False) -> dict:
    notes = []

    def guarded(label, fn):
        """Кабинет может отвалиться по токену — дашборд собираем из того, что есть."""
        try:
            return fn()
        except SystemExit as e:
            notes.append(f"{label}: данные не получены — {str(e)[:180]}")
            return []

    met = metrika_data(d1, d2)
    ads = (guarded("Директ", lambda: direct_rows(d1, d2))
           + guarded("Авито", lambda: avito_rows(d1, d2))
           + guarded("ВК", lambda: vk_rows(d1, d2)))

    for r in ads:
        r["mp"] = 1 if norm(r["camp"]) in MARKETPLACE_TRAFFIC else 0

    dates = sorted({r["d"] for r in ads} | {r["d"] for r in met["days"]} if ads or met["days"] else {d1})
    return {
        "meta": {
            "generated": datetime.now().strftime("%d.%m.%Y %H:%M"),
            "from": dates[0] if dates else d1, "to": dates[-1] if dates else d2,
            "counter": "111262520", "site": "ducatiparfum.ru", "notes": notes,
            # Клиентская сборка: без блока подсказок «что требует внимания» и без
            # служебного подвала — это внутренняя кухня, наружу её не показываем.
            "client": client,
        },
        "channels": CHANNELS,
        "goals": met["goals"],
        "days": met["days"],
        "site": met["site"],
        "ads": ads,
    }


CLIENT_HIDE = ("sec-insights", "sec-meta")   # внутренние секции: наружу не уходят


def build(data: dict) -> str:
    tpl = TEMPLATE.read_text(encoding="utf-8")
    if data["meta"].get("client"):
        # Вырезаем из разметки, а не прячем в браузере: в исходнике страницы их тоже быть не должно
        for sec in CLIENT_HIDE:
            tpl, n = re.subn(rf'<section id="{sec}">.*?</section>', "", tpl, flags=re.S)
            if not n:
                sys.exit(f"В шаблоне нет секции {sec} — клиентская версия собралась бы с ней.")
        # И код, который их наполняет: в нём тексты подсказок и команды сборки
        tpl, n = re.subn(r"/\*__ВНУТРЕННЕЕ__\*/.*?/\*__/ВНУТРЕННЕЕ__\*/",
                         "function renderInsights(){}\nfunction renderMeta(){}", tpl, flags=re.S)
        if not n:
            sys.exit("В шаблоне нет меток /*__ВНУТРЕННЕЕ__*/ — служебный код уехал бы клиенту.")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # </script> внутри данных разорвал бы тег — экранируем на всякий случай
    payload = payload.replace("</", "<\\/")
    if "/*__ДАННЫЕ__*/" not in tpl:
        sys.exit(f"В шаблоне {TEMPLATE} нет метки /*__ДАННЫЕ__*/ — некуда класть данные.")
    return tpl.replace("/*__ДАННЫЕ__*/", payload)


def write(text: str, out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")


def without_shell(page: str) -> str:
    """Версия без каркаса документа — для площадок, которые оборачивают страницу сами."""
    for tag in (r"<!doctype html>", r"<html[^>]*>", r"</html>", r"<head>", r"</head>",
                r"<body>", r"</body>", r"<meta[^>]*>"):
        page = re.sub(tag, "", page, flags=re.I)
    return page.strip() + "\n"


FLAGS = ("--выход", "--артефакт")   # флаги со значением: значение не считаем датой


def main() -> None:
    argv, args, opts = sys.argv[1:], [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in FLAGS and i + 1 < len(argv):
            opts[a] = argv[i + 1]; i += 2
        elif a.startswith("--"):
            i += 1
        else:
            args.append(a); i += 1

    out = Path(opts["--выход"]) if "--выход" in opts else OUTPUT
    d1 = args[0] if args else START
    d2 = args[1] if len(args) > 1 else date.today().isoformat()

    client = "--для-клиента" in sys.argv
    print(f"Собираю данные за {d1} … {d2}" + (" (версия для клиента)" if client else ""))
    data = collect(d1, d2, client=client)
    for n in data["meta"]["notes"]:
        print("  !", n)
    page = build(data)
    write(page, out)
    print(f"Готово: {out}")
    if "--артефакт" in opts:
        art = Path(opts["--артефакт"])
        write(without_shell(page), art)
        print(f"  версия без каркаса: {art}")
    print(f"  дней с данными: {len({r['d'] for r in data['days']})}, "
          f"строк по кампаниям: {len(data['ads'])}, целей: {len(data['goals'])}")


if __name__ == "__main__":
    main()
