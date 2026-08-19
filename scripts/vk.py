#!/usr/bin/env python3
"""API ВК Рекламы (VK Ads): чтение статистики и управление кампаниями.

Способы авторизации. Кладём в .env.metrika:

  1. Постоянный токен eLama для конкретного клиентского кабинета:
     VK_TOKEN=...                 # также поддерживаются VK_ADS_TOKEN и access_token
     agency_client_name=...       # логин вида 0000000000@agency_client

     Токен уже выпущен eLama по Agency Client Credentials Grant. Для API-запросов
     достаточно токена; agency_client_name нужен команде check для сверки кабинета.

  2. Агентские ключи + id кабинета клиента:
     VK_CLIENT_ID=...
     VK_CLIENT_SECRET=...
     VK_AGENCY_CLIENT_ID=...        (или VK_AGENCY_CLIENT_NAME=...)

  3. Собственный кабинет без агентства:
     VK_CLIENT_ID=...
     VK_CLIENT_SECRET=...

Чтение:
    python3 vk.py check                          # проверить токен и кабинет
    python3 vk.py me                             # чей кабинет и что токену разрешено
    python3 vk.py clients                        # кабинеты клиентов (только агентские ключи)
    python3 vk.py plans                          # кампании (ad_plans)
    python3 vk.py groups                         # группы объявлений
    python3 vk.py packages                       # пакеты размещения (нужны, чтобы создать группу)
    python3 vk.py stats 2026-08-07 2026-08-13    # сводка за период
    python3 vk.py days  2026-08-07 2026-08-13    # разбивка по дням

Списки поисковых фраз (контекстный таргетинг), ресурс /api/v3/search_phrases:
    python3 vk.py phrases                        # какие списки есть
    python3 vk.py phrases 7454077                # фразы и стоп-фразы списка
    python3 vk.py phrases 7454077 --csv файл.csv # выгрузить в файл
    python3 vk.py phrases-new "Имя" фразы.json   # черновик, с --да создаст
    python3 vk.py phrases-set 7454077 фразы.json # заменить содержимое, тоже с --да
    python3 vk.py phrases-rm 7454077 --да        # удалить список

Аудиторию поверх списка фраз API не собирает: id списка не совпадает с object_id
в relations сегмента. Сегмент заводится в кабинете, в скрипты передаётся его id.

Произвольные запросы и создание объектов:
    python3 vk.py get  /api/v2/ad_plans.json limit=5
    python3 vk.py post /api/v2/ad_plans.json кампания.json          # только показать, что уйдёт
    python3 vk.py post /api/v2/ad_plans.json кампания.json --да     # реально создать

Без --да ничего не отправляется: post печатает тело запроса и выходит.
Создание идёт от лица кабинета и тратит его бюджет — сверяйте тело перед --да.
"""

import gzip
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://ads.vk.ru"
# .env.metrika лежит в корне проекта; ищем рядом со скриптом, затем на уровень выше
_HERE = Path(__file__).resolve()
ENV_FILE = next(
    (c for c in (_HERE.with_name(".env.metrika"), _HERE.parent.parent / ".env.metrika") if c.exists()),
    _HERE.parent.parent / ".env.metrika",
)
_tok = {}
READY_TOKEN_KEYS = ("VK_TOKEN", "VK_ADS_TOKEN", "access_token")
CLIENT_NAME_KEYS = ("agency_client_name", "VK_AGENCY_CLIENT_NAME")


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


def env_any(keys, required: bool = True) -> str:
    """Возвращает первое заданное значение из нескольких имён переменной."""
    for key in keys:
        value = env(key, required=False)
        if value:
            return value
    if required:
        sys.exit(f"Нет {', '.join(keys)}. Добавьте токен в {ENV_FILE}")
    return ""


def token(plain: bool = False) -> str:
    """plain=True — токен уровня агентства, без привязки к кабинету клиента.
    Нужен только команде clients: список кабинетов клиентским токеном не отдаётся."""
    key = "plain" if plain else "client"
    if _tok.get(key, {}).get("exp", 0) > time.time():
        return _tok[key]["t"]

    # Токен eLama уже привязан к кабинету клиента, для plain не годится.
    ready = "" if plain else env_any(READY_TOKEN_KEYS, required=False)
    if ready:
        if ready.lower().startswith("bearer "):
            ready = ready.split(None, 1)[1]
        _tok[key] = {"t": ready, "exp": time.time() + 3600}
        return ready

    params = {
        "grant_type": "client_credentials",
        "client_id": env("VK_CLIENT_ID"),
        "client_secret": env("VK_CLIENT_SECRET"),
    }
    # Агентская схема: ключи принадлежат агентству, кабинет клиента указывается отдельно.
    agency_id = env("VK_AGENCY_CLIENT_ID", required=False)
    agency_name = env("VK_AGENCY_CLIENT_NAME", required=False)
    if (agency_id or agency_name) and not plain:
        params["grant_type"] = "agency_client_credentials"
        params["agency_client_id" if agency_id else "agency_client_name"] = agency_id or agency_name

    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{BASE}/api/v2/oauth2/token.json", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Авторизация не прошла — HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}")
    _tok[key] = {"t": d["access_token"], "exp": time.time() + int(d.get("expires_in", 86400)) - 60}
    return _tok[key]["t"]


def unpack(raw: bytes):
    """Ресурс /api/v3/ отвечает gzip даже без Accept-Encoding — json.load на нём падает."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    text = raw.decode("utf-8", "replace")
    return json.loads(text) if text.strip() else {}


def api(path: str, quiet: bool = False, plain: bool = False, **params):
    url = f"{BASE}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token(plain)}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return unpack(r.read())
    except urllib.error.HTTPError as e:
        msg = e.read().decode("utf-8", "replace")[:400]
        if quiet:
            return {"__error__": e.code, "__body__": msg}
        sys.exit(f"HTTP {e.code} на {path}: {msg}{hint(e.code)}")


def upload(path) -> dict:
    """Заливает картинку в контент кабинета. Возвращает объект с id — его и кладём в баннер."""
    import mimetypes
    import os
    import uuid

    data = open(path, "rb").read()
    name = os.path.basename(path)
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    boundary = uuid.uuid4().hex

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{BASE}/api/v2/content/static.json", data=body, method="POST",
        headers={"Authorization": f"Bearer {token()}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"Не залилась {name} — HTTP {e.code}: "
                 f"{e.read().decode('utf-8', 'replace')[:600]}{hint(e.code)}")


def hint(code: int) -> str:
    if code == 401:
        return ("\n\nТокен не принят. Проверьте VK_TOKEN (или access_token). "
                "Постоянный токен eLama не требует обновления, но его может отозвать агентство.")
    if code == 403:
        return ("\n\nДоступ есть, но не к этому действию. Похоже, токен выдан только на чтение — "
                "на создание и правку объектов нужны отдельные права от агентства.")
    return ""


def post(path: str, payload, method: str = "POST"):
    """Создание/изменение объекта. Вызывать только после явного подтверждения."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method,
                                 headers={"Authorization": f"Bearer {token()}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return unpack(r.read()) or {"status": r.status}
    except urllib.error.HTTPError as e:
        # ВК кладёт разбор ошибки по полям в тело ответа — печатаем целиком, оно полезное
        sys.exit(f"HTTP {e.code} на {method} {path}:\n"
                 f"{e.read().decode('utf-8', 'replace')[:1500]}{hint(e.code)}")


def objects(kind: str) -> list:
    """kind: ad_plans (кампании) или ad_groups (группы)."""
    out, offset = [], 0
    while True:
        d = api(f"/api/v2/{kind}.json", limit=50, offset=offset,
                fields="id,name,status,budget_limit")
        items = d.get("items", [])
        out += items
        offset += len(items)
        if len(items) < 50 or offset >= d.get("count", 0):
            break
    return out


def stats(level: str, ids: list, d1: str, d2: str, period: str = "day") -> dict:
    return api(f"/api/v2/statistics/{level}/{period}.json",
               id=",".join(str(i) for i in ids), date_from=d1, date_to=d2, metrics="base")


def num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def show(rows: list, head: list) -> None:
    if not rows:
        print("Нет данных за период.")
        return
    w = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    line = lambda c: "  ".join(str(x).ljust(w[i]) for i, x in enumerate(c)).rstrip()
    print(line(head))
    print("  ".join("-" * x for x in w))
    for r in rows:
        print(line(r))


def fmt(x: float, dec: int = 2) -> str:
    return f"{x:,.{dec}f}".replace(",", " ")


# ─── списки поисковых фраз (контекстный таргетинг) ──────────────────────────
# Живут отдельно от остального ремаркетинга: ресурс /api/v3/search_phrases.json,
# не /api/v2/remarketing/*. Требуют заголовок X-Trg-User-Id и отвечают gzip.

SP = "/api/v3/search_phrases"
_uid = {}


def user_id() -> int:
    if not _uid:
        _uid["id"] = api("/api/v2/user.json")["id"]
    return _uid["id"]


def sp_send(path: str, method: str, body: bytes = b"", ctype: str = "application/json"):
    headers = {"Authorization": f"Bearer {token()}", "X-Trg-User-Id": str(user_id())}
    if body:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(f"{BASE}{path}", data=body or None, method=method,
                                 headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return unpack(r.read()) or {"status": r.status}
    except urllib.error.HTTPError as e:
        raw = e.read()
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        sys.exit(f"HTTP {e.code} на {method} {path}:\n"
                 f"{raw.decode('utf-8', 'replace')[:1200]}{hint(e.code)}")


def sp_read(src) -> dict:
    """Читает список фраз из файла.

    .json — {"phrases": [...], "stop_phrases": [...], "expires": "12d"}.
    Текст — фразы построчно или через запятую; всё после строки «стоп:» или «---»
    считается стоп-фразами. Строки, начинающиеся с #, пропускаются.
    """
    text = Path(src).read_text(encoding="utf-8")
    if str(src).endswith(".json"):
        d = json.loads(text)
        return {"phrases": d.get("phrases", []),
                "stop_phrases": d.get("stop_phrases", []),
                "expires": d.get("expires", "12d")}

    phrases, stops, cur = [], [], None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().rstrip(":") in ("стоп", "стоп-фразы", "---"):
            cur = stops
            continue
        (stops if cur is stops else phrases).extend(
            x.strip() for x in line.split(",") if x.strip())
    return {"phrases": phrases, "stop_phrases": stops, "expires": "12d"}


def sp_body(d: dict) -> bytes:
    return json.dumps({"phrases": d["phrases"], "stop_phrases": d["stop_phrases"],
                       "expires": d.get("expires", "12d")}, ensure_ascii=False).encode("utf-8")


def sp_preview(name: str, d: dict) -> None:
    print(f"Список «{name}»: {len(d['phrases'])} фраз, {len(d['stop_phrases'])} стоп-фраз, "
          f"срок {d.get('expires', '12d')}")
    print("  фразы:", ", ".join(d["phrases"][:8]) + (" …" if len(d["phrases"]) > 8 else ""))
    if d["stop_phrases"]:
        print("  стоп: ", ", ".join(d["stop_phrases"][:8]) +
              (" …" if len(d["stop_phrases"]) > 8 else ""))


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0] if args else "check"

    if cmd == "check":
        account = api("/api/v2/user.json")
        username = account.get("username", "")
        configured_name = env_any(CLIENT_NAME_KEYS, required=False)
        if configured_name and configured_name != username:
            sys.exit("Токен относится к кабинету " + username +
                     ", но agency_client_name содержит " + configured_name + ".")
        print(f"Доступ подтверждён: {username} (id {account.get('id')}, {account.get('currency', '')}).")
        print("Типы кабинета:", ", ".join(account.get("types", [])))

    elif cmd == "token":
        print("токен найден, длина", len(token()), "— для проверки доступа используйте check")

    elif cmd == "me":
        # Показывает, к какому кабинету привязан токен и какие права у него есть.
        # Если тут пусто по правам — создавать кампании токен не сможет, нужен другой.
        print(json.dumps(api("/api/v2/user.json"), ensure_ascii=False, indent=2))

    elif cmd == "packages":
        # Пакет = формат и место размещения. Без его id группу объявлений не создать.
        d = api("/api/v2/packages.json", limit=50)   # больше 50 ВК не отдаёт
        for p in d.get("items", []):
            print(f"{p.get('id'):>8}  {p.get('name','')}")
        print(f"\nвсего {d.get('count', len(d.get('items', [])))}")

    elif cmd == "get":
        if len(args) < 2:
            sys.exit("Нужен путь: get /api/v2/ad_plans.json limit=5")
        params = dict(a.split("=", 1) for a in args[2:] if "=" in a)
        print(json.dumps(api(args[1], **params), ensure_ascii=False, indent=2))

    elif cmd == "post":
        if len(args) < 3:
            sys.exit("Нужен путь и файл с телом: post /api/v2/ad_plans.json кампания.json [--да]")
        path, src = args[1], args[2]
        payload = json.loads(sys.stdin.read() if src == "-" else Path(src).read_text(encoding="utf-8"))
        print(f"{'POST'} {BASE}{path}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if "--да" not in sys.argv:
            print("\nЭто черновик — ничего не отправлено. Повторите с --да, чтобы создать.")
            return
        print("\nОтвет:")
        print(json.dumps(post(path, payload), ensure_ascii=False, indent=2))

    elif cmd == "clients":
        # Работает только с агентскими ключами: показывает кабинеты клиентов и их id.
        # По готовой строке VK_TOKEN список не отдаётся — она уже привязана к одному кабинету.
        if not env("VK_CLIENT_ID", required=False):
            sys.exit("Список кабинетов доступен только по агентской паре VK_CLIENT_ID/VK_CLIENT_SECRET.\n"
                     "С постоянным токеном eLama он не нужен: токен уже привязан к кабинету, "
                     "смотрите его через  python3 vk.py me")
        d = api("/api/v2/agency/clients.json", plain=True, limit=100)
        for c in d.get("items", []):
            u = c.get("user", {})
            print(f"{u.get('id', c.get('id')):>12}  {u.get('username', '')}  {u.get('client_username', '')}")

    elif cmd in ("plans", "groups"):
        kind = "ad_plans" if cmd == "plans" else "ad_groups"
        for o in objects(kind):
            print(f"{o.get('id'):>12}  {str(o.get('status','')):<12} {o.get('name','')}")

    elif cmd in ("stats", "days"):
        if len(args) < 3:
            sys.exit("Нужны две даты: stats 2026-08-07 2026-08-13")
        d1, d2 = args[1], args[2]

        groups = objects("ad_groups")
        if not groups:
            print("Групп объявлений в кабинете нет.")
            return
        names = {str(g["id"]): g.get("name", "") for g in groups}
        d = stats("campaigns", list(names), d1, d2)   # в статистике группы зовутся campaigns

        rows, tot = [], {"shows": 0.0, "clicks": 0.0, "spent": 0.0}
        for item in d.get("items", []):
            gid = str(item.get("id"))
            if cmd == "stats":
                b = item.get("total", {}).get("base", item.get("total", {}))
                sh, cl, sp = num(b.get("shows")), num(b.get("clicks")), num(b.get("spent"))
                tot["shows"] += sh; tot["clicks"] += cl; tot["spent"] += sp
                rows.append([names.get(gid, gid), fmt(sp), fmt(sh, 0), fmt(cl, 0),
                             f"{cl / sh * 100:.2f} %" if sh else "—",
                             fmt(sp / cl) if cl else "—",
                             fmt(sp / sh * 1000) if sh else "—"])
            else:
                for row in item.get("rows", []):
                    b = row.get("base", row)
                    sh, cl, sp = num(b.get("shows")), num(b.get("clicks")), num(b.get("spent"))
                    tot["shows"] += sh; tot["clicks"] += cl; tot["spent"] += sp
                    rows.append([row.get("date", ""), names.get(gid, gid)[:28],
                                 fmt(sp), fmt(sh, 0), fmt(cl, 0),
                                 f"{cl / sh * 100:.2f} %" if sh else "—"])

        if cmd == "stats":
            show(rows, ["Группа", "Расход, ₽", "Показы", "Клики", "CTR", "CPC, ₽", "CPM, ₽"])
        else:
            rows.sort()
            show(rows, ["Дата", "Группа", "Расход, ₽", "Показы", "Клики", "CTR"])

        sh, cl, sp = tot["shows"], tot["clicks"], tot["spent"]
        print()
        print(f"ИТОГО  расход {fmt(sp)} ₽  показы {fmt(sh, 0)}  клики {fmt(cl, 0)}" +
              (f"  CTR {cl / sh * 100:.2f}%  CPC {fmt(sp / cl)} ₽  CPM {fmt(sp / sh * 1000)} ₽"
               if sh and cl else ""))

    elif cmd == "phrases":
        if len(args) < 2:
            d = api(f"{SP}.json")
            rows = [[i.get("id"), i.get("status", ""), i.get("phrases_cnt", 0),
                     i.get("stop_phrases_cnt", 0), i.get("errors_cnt", 0), i.get("name", "")]
                    for i in d.get("items", [])]
            show(rows, ["id", "статус", "фраз", "стоп", "ошибок", "имя"])
            return
        sid = args[1]
        d = api(f"{SP}/{sid}.json")
        items = d.get("items", [])
        if "--csv" in args:
            dst = args[args.index("--csv") + 1]
            Path(dst).write_text(
                "phrase;expires\n" + "\n".join(f'{i["phrase"]};{i.get("expires", "")}'
                                                for i in items), encoding="utf-8")
            print(f"{len(items)} фраз выгружено в {dst}")
            return
        for i in items:
            print(i["phrase"])
        stops = d.get("common_stop_phrases") or []
        if stops:
            print("\nстоп:")
            print(", ".join(stops))
        print(f"\nвсего {len(items)} фраз, {len(stops)} стоп-фраз, "
              f"ошибок разбора {d.get('errors_cnt', 0)}")

    elif cmd in ("phrases-new", "phrases-set"):
        if len(args) < 3:
            sys.exit('Нужны имя/id и файл: phrases-new "Ключи — мужчины" фразы.json [--да]')
        d = sp_read(args[2])
        if not d["phrases"]:
            sys.exit(f"В {args[2]} не нашлось ни одной фразы.")
        sp_preview(args[1], d)
        if "--да" not in args:
            print("\nЭто черновик — ничего не отправлено. Повторите с --да.")
            return
        if cmd == "phrases-new":
            path = f"{SP}.json?" + urllib.parse.urlencode({"name": args[1]})
            r = sp_send(path, "POST", sp_body(d))
        else:
            r = sp_send(f"{SP}/{int(args[1])}.json", "PUT", sp_body(d))
        print("\nОтвет:", json.dumps(r, ensure_ascii=False, indent=2))

    elif cmd == "phrases-rm":
        if len(args) < 2:
            sys.exit("Нужен id: phrases-rm 7454077 [--да]")
        if "--да" not in args:
            print(f"Удалить список {args[1]}? Повторите с --да.")
            return
        print(json.dumps(sp_send(f"{SP}/{int(args[1])}.json", "DELETE"),
                         ensure_ascii=False, indent=2))

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
