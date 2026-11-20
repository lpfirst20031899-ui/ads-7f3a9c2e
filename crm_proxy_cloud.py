#!/usr/bin/env python3
"""
ADS DASHBOARD — CRM Sync Server (Cloud / Render.com)
──────────────────────────────────────────────────────
Це хмарна версія crm_proxy.py.
Запускається на Render.com і доступна з будь-якого пристрою.

Локальна версія (crm_proxy.py) залишається без змін.
"""

import hashlib
import hmac
import http.server
import re
import urllib.request
import urllib.error
import urllib.parse
import json
import os
import time
import threading
import socket
import socketserver
from datetime import datetime, timedelta, timezone

# ══════════════════════════════════════════════
#  НАЛАШТУВАННЯ  (ті самі, що в crm_proxy.py)
# ══════════════════════════════════════════════
API_KEY  = "1PtL9c3Qc1S8iiRDgazx2Yv1"
CRM_BASE = "https://api.keepincrm.com/v1"
DELAY_MS = 650   # Ліміт CRM ~100 запитів/хв = 600 мс/запит. 650 мс тримає нас під лімітом
                 # (200 мс давало ~300/хв → 429 Too Many Requests, ще й разом із роботами дублів/коментарів).
INC_DAYS = 5
PORT     = int(os.environ.get('PORT', 8765))   # Render задає PORT сам

# Ворота. PROXY_SECRET — той самий рядок, що в змінних Netlify: там функція /dash
# підписує токен, тут ми перевіряємо підпис. Без спільного секрету /api/* відкритий
# усьому інтернету разом з усіма угодами CRM, тому за відсутності змінної
# віддаємо 503, а не працюємо далі тихо.
PROXY_SECRET = os.environ.get('PROXY_SECRET', '')
# Домен сайту на Netlify. '*' лишається тільки на час переїзду.
ALLOW_ORIGIN = os.environ.get('ALLOW_ORIGIN', '*')
# Логіни входу в дашборд: 'artem:пароль1,olga:пароль2'. Токен підписує САМ проксі
# (POST /api/login), бо на GitHub Pages немає серверної функції: підписати на клієнті
# не можна — секрет приїхав би у браузер разом зі сторінкою, і ворота перестали б бути
# воротами. Логіни, а не один спільний пароль, бо інакше відкликати доступ одній людині
# можна тільки змінивши пароль усім.
DASH_USERS_RAW = os.environ.get('DASH_USERS', '')
TOKEN_DAYS = int(os.environ.get('TOKEN_DAYS', '30'))


def parse_users(raw):
    """'a:1,b:2' → {'a': '1', 'b': '2'}. Ділимо по ПЕРШОМУ ':' — у паролі він дозволений."""
    out = {}
    for part in str(raw or '').split(','):
        part = part.strip()
        if ':' not in part:
            continue
        login, pw = part.split(':', 1)
        login, pw = login.strip(), pw.strip()
        # Логін їде в токені як окреме поле, тому кома й двокрапка в ньому зламали б формат.
        if login and pw and re.fullmatch(r'[A-Za-z0-9_.-]+', login):
            out[login] = pw
    return out


DASH_USERS = parse_users(DASH_USERS_RAW)
# ══════════════════════════════════════════════

# На Render файлова система тимчасова — використовуємо /tmp
DATA_FILE = '/tmp/crm_data.json'

CATMAP = {'А': 'A', 'В': 'B', 'С': 'C', 'A': 'A', 'B': 'B', 'C': 'C'}


# Київський зсув живе в ua_time.py — одне правило на обидва проксі.
from ua_time import dmy_to_iso, iso_to_dmy


def parse_crm_date(s):
    if not s: return ''
    s = str(s).strip()
    if len(s) >= 10 and s[2] == '.' and s[5] == '.': return s[:10]
    return iso_to_dmy(s)


def lead_status(item):
    # CRM має чотири значення result: null / failed / archived / successful.
    # Тягнули лише перші два — і 67 угод, закритих кнопкою «Архівувати»
    # (result='archived', усі з 07.09.2026), зникали з дашборда цілком:
    # ні в лідах, ні в програних, а в localStorage висіли «в роботі».
    return 'active' if item.get('result') is None else 'lost'


def crm_item_to_row(item, status, is_realizatsiya=False):
    cf = item.get('custom_fields') or {}
    cat_raw = str(cf.get('Категорія клієнта') or '').strip()
    archive_status = item.get('archive_status') or {}
    stage = item.get('stage') or {}
    responsible = item.get('main_responsible') or {}

    lead_date = iso_to_dmy(item.get('ordered_at') or item.get('created_at') or '')
    sale_date = parse_crm_date(str(cf.get('Дата продажу') or '').strip())
    if is_realizatsiya:
        # Продаж відносимо до дати ЗАЯВКИ (коли лід прийшов), а не до дати оплати.
        # Оплата могла прийти через місяць — але в аналітику вона йде за датою заявки.
        date_val = lead_date or sale_date
    else:
        date_val = lead_date

    # Угода — за тим, хто її першим опрацював. Кваліфікатор ставить себе в «Кваліфікатор»,
    # а відповідальним після передачі стає менеджер; порожнє поле — опрацював сам відповідальний.
    qualifier = str(cf.get('Кваліфікатор') or '').strip()

    return {
        'id':            item.get('id'),
        'manager':       qualifier or str(responsible.get('name') or '').strip(),
        'date':          date_val,
        'sale_date':     sale_date if is_realizatsiya else '',
        'creo':          str(cf.get('Крео')     or '').strip() or 'Не вказано',
        'adset':         str(cf.get('Адсет')    or '').strip() or 'Не вказано',
        'campaign':      str(cf.get('Кампанія') or '').strip() or 'Не вказано',
        'ad_funnel':     str(cf.get('Воронка')  or '').strip() or 'Не вказано',
        'category':      CATMAP.get(cat_raw),
        'status':        status,
        'stage':         str(stage.get('name')          or '').strip(),
        'last_note':     str(item.get('last_note')       or '').strip(),
        'reason':        str(archive_status.get('name') or '').strip(),
        # Сире значення, без нормалізації: довідник областей живе в дашборді,
        # щоб правити його без деплою проксі.
        'region':        str(cf.get("Місцезнаходження об'єкту") or '').strip(),
        'form_purpose':  str(cf.get('Мета встановлення')   or '').strip(),
        'form_budget':   str(cf.get('Бюджет')               or '').strip(),
        'form_timeline': str(cf.get('Строки встановлення')  or '').strip(),
    }


def fetch_page(endpoint, page):
    sep = '&' if '?' in endpoint else '?'
    url = f"{CRM_BASE}{endpoint}{sep}page={page}"
    req = urllib.request.Request(url, headers={
        'X-Auth-Token': API_KEY,
        'Accept':       'application/json',
        'User-Agent':   'AdsDashboard/2.0',
    })
    # До 6 спроб. На 429 (перевищено ліміт CRM) — чекаємо (Retry-After або наростаючу паузу)
    # і пробуємо ще, а не валимо весь синк. На інші помилки — коротка пауза.
    backoff = 5
    last_err = None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                ra = e.headers.get('Retry-After') if e.headers else None
                wait = int(ra) if (ra and str(ra).isdigit()) else backoff
                time.sleep(min(wait, 60))
                backoff = min(backoff * 2, 60)
                continue
            if attempt == 5: raise
            time.sleep(2)
        except Exception as e:
            last_err = e
            if attempt == 5: raise
            time.sleep(2)
    if last_err: raise last_err


def inc_window(path):
    """Скільки днів тягнути в режимі inc: кнопка «3 місяці» просить days=90.

    Межа 365 — вище сенсу немає (дані з 2026-01-01), а підставлений days=99999
    перетворив би inc на повний прохід на 346 сторінок без попередження.
    """
    m = re.search(r'[?&]days=(\d{1,6})', path)
    return min(max(int(m.group(1)), 1), 365) if m else INC_DAYS


def sync_crm(mode, send_event, inc_days=INC_DAYS):
    date_filter = ''
    active_rows, lost_rows, won_rows = [], [], []
    FB_SOURCE_ID = 6

    # Inc mode: fetch only last INC_DAYS days (client localStorage holds full history)
    # Full mode: fetch all 2026 data
    if mode == 'inc':
        since = datetime.now(tz=timezone.utc) - timedelta(days=inc_days)
        CUT_DATE = since.strftime('%Y-%m-%d')
    else:
        CUT_DATE = '2026-01-01'

    def process_item(item):
        funnel_id    = (item.get('funnel') or {}).get('id')
        funnel_title = (item.get('funnel') or {}).get('title', '')
        cf           = item.get('custom_fields') or {}
        ordered_at   = item.get('ordered_at') or item.get('created_at') or ''
        year         = int(ordered_at[:4]) if len(ordered_at) >= 4 else 0
        status       = lead_status(item)

        is_b2c          = (funnel_id == 1 or 'B2C' in funnel_title or 'В2С' in funnel_title or 'Кінцевий' in funnel_title)
        is_realizatsiya = (funnel_id == 3 or 'Реаліз' in funnel_title)

        if not is_b2c and not is_realizatsiya: return []
        if year < 2026: return []

        source_id   = (item.get('source') or {}).get('id')
        source_name = str((item.get('source') or {}).get('name') or '').strip()
        custom_src  = str(cf.get('Джерело') or '').strip()
        is_fb = (source_id == FB_SOURCE_ID or source_name == 'FB Ads' or custom_src == 'FB Ads')
        if not is_fb: return []

        if is_realizatsiya and status == 'lost': return []

        out = []
        if is_realizatsiya:
            lead_row = crm_item_to_row(item, 'active', is_realizatsiya=False)
            if lead_row.get('date'): out.append((lead_row, 'active'))
            sale_date = str(cf.get('Дата продажу') or '').strip()
            if sale_date:
                won_row = crm_item_to_row(item, 'won', is_realizatsiya=True)
                if won_row.get('date'): out.append((won_row, 'won'))
        else:
            row = crm_item_to_row(item, status)
            if row.get('date'): out.append((row, status))
        return out

    def fetch_segment(label, endpoint, phase_start, phase_len):
        page = 1
        total_pages = 1
        while page <= total_pages:
            try:
                data = fetch_page(endpoint, page)
            except urllib.error.HTTPError as e:
                send_event({'type': 'error', 'message': f"CRM API {e.code}: {e.reason}"}); return False
            except Exception as e:
                send_event({'type': 'error', 'message': f"Помилка: {str(e)}"}); return False

            items = data.get('items') or []
            pg    = data.get('pagination') or {}
            total_pages = int(pg.get('total_pages') or 1)

            for item in items:
                for row, kind in process_item(item):
                    if kind == 'won':    won_rows.append(row)
                    elif kind == 'active': active_rows.append(row)
                    elif kind == 'lost':   lost_rows.append(row)

            pct = phase_start + (page / total_pages) * phase_len
            send_event({
                'type': 'progress',
                'label': f"{label}: {page}/{total_pages}  ·  лідів:{len(active_rows)+len(lost_rows)}  програних:{len(lost_rows)}  оплат:{len(won_rows)}",
                'pct': round(min(pct, 99), 1),
            })
            page += 1
            if page <= total_pages: time.sleep(DELAY_MS / 1000)
        return True

    df = date_filter
    # Один прохід без фільтра по result: статус угоди рахуємо з поля result у process_item.
    # Окремі запити result_blank=1 / result_eq=failed губили archived і successful.
    if mode == 'inc':
        # updated_at_gteq — ловимо ВСІ зміни за останні inc_days днів:
        # нові ліди, зміни статусів, оплати по лютневих заявках тощо.
        seg = f"/agreements?q%5Bupdated_at_gteq%5D={CUT_DATE}{df}"
        if not fetch_segment("Оновлені", seg, 0, 99): return
    elif mode == 'comments':
        # Тільки активні угоди (усі 2026) — оновлюємо коментарі, без програних/оплат
        seg = f"/agreements?q%5Bcreated_at_gteq%5D=2026-01-01&q%5Bresult_blank%5D=1{df}"
        if not fetch_segment("Коментарі", seg, 0, 99): return
    else:
        seg = f"/agreements?q%5Bcreated_at_gteq%5D=2026-01-01{df}"
        if not fetch_segment("Угоди 2026+", seg, 0, 99): return

    # Incremental merge (in-memory on cloud)
    if mode == 'inc' and os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, encoding='utf-8') as f:
                existing = json.load(f)
            # Видалена в CRM угода з мерджу по id сама не зникає, і кеш /tmp тримав
            # її вічно — звідси вона знову приїжджала в браузер на старті (/api/data).
            # У межах вікна відповідь повна (дата заявки ≤ updated_at), тому все,
            # чого в ній немає, з кешу знімаємо. Старіше вікна не чіпаємо.
            fresh = {r['id'] for r in active_rows + lost_rows + won_rows if r.get('id')}
            def _alive(r):
                return r['id'] in fresh or dmy_to_iso(r.get('date')) < CUT_DATE
            for k in ('active', 'lost', 'won'):
                existing[k] = [r for r in existing.get(k, []) if not r.get('id') or _alive(r)]
            active_map = {r['id']: r for r in existing.get('active', []) if r.get('id')}
            lost_map   = {r['id']: r for r in existing.get('lost',   []) if r.get('id')}
            won_map    = {r['id']: r for r in existing.get('won',    []) if r.get('id')}
            active_noid = [r for r in existing.get('active', []) if not r.get('id')]
            lost_noid   = [r for r in existing.get('lost',   []) if not r.get('id')]
            won_noid    = [r for r in existing.get('won',    []) if not r.get('id')]
            for r in active_rows:
                if r.get('id'): active_map[r['id']] = r; lost_map.pop(r['id'], None)
                else: active_noid.append(r)
            for r in lost_rows:
                if r.get('id'): lost_map[r['id']] = r; active_map.pop(r['id'], None)
                else: lost_noid.append(r)
            for r in won_rows:
                if r.get('id'): won_map[r['id']] = r
                else: won_noid.append(r)
            active_rows[:] = list(active_map.values()) + active_noid
            lost_rows[:]   = list(lost_map.values())   + lost_noid
            won_rows[:]    = list(won_map.values())     + won_noid
        except Exception as e:
            print(f"Merge failed: {e}")

    payload = {
        'active':    active_rows,
        'lost':      lost_rows,
        'won':       won_rows,
        'total':     len(active_rows) + len(lost_rows) + len(won_rows),
        'synced_at': datetime.now(tz=timezone.utc).isoformat(),
        # Межа, у якій знімок повний: усе з датою заявки >= cache_since у ньому
        # є обов'язково, тому чого немає — видалено. Клієнт чистить localStorage
        # по цьому ж правилу на старті (bootRestoreCRM), а не лише після синку:
        # інкремент тягне вікно, і за його межами знімок нічого не доказує.
        # Без цього поля видалена вчора заявка жила в браузері до наступного
        # синку — живьём 19.11.2026 дашборд показував 20 заявок при 18 у CRM.
        'cache_since': CUT_DATE,
    }
    # comments-режим не чіпає кеш /tmp (там повний знімок для boot-restore)
    if mode != 'comments':
        try:
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            print(f"Save failed: {e}")

    # Стрімимо дані через SSE-чанки → клієнт не робить окремий /api/data запит
    _CH = 300
    def _chunks(lst):
        for i in range(0, max(1, len(lst)), _CH):
            yield lst[i:i+_CH]
    for c in _chunks(active_rows): send_event({'type': 'data_active', 'rows': c})
    for c in _chunks(lost_rows):   send_event({'type': 'data_lost',   'rows': c})
    for c in _chunks(won_rows):    send_event({'type': 'data_won',    'rows': c})

    send_event({
        'type': 'done_signal',
        'total': payload['total'],
        'active_count': len(active_rows),
        'lost_count':   len(lost_rows),
        'won_count':    len(won_rows),
        # Межа, з якої відповідь повна — по ній клієнт знімає видалені з localStorage.
        # comments-режим тягне тільки активні, тому межі не дає: інакше клієнт
        # зняв би всі програні й оплати як «видалені».
        'since': '' if mode == 'comments' else CUT_DATE,
    })


def sign(exp, login):
    """Підпис токена. Логін входить у підписуваний рядок, інакше його можна підмінити."""
    return hmac.new(PROXY_SECRET.encode(), f'{exp}:{login}'.encode(), hashlib.sha256).hexdigest()


def mint_token(login, now=None):
    exp = int((now if now is not None else time.time()) + TOKEN_DAYS * 86400)
    return f'{exp}.{login}.{sign(exp, login)}'


def token_ok(path):
    """Токен у вигляді exp.логін.hmac(exp:логін).

    Логін у токені не для прав — права тут не потрібні, /api/data віддає лише дані CRM
    (угоди, менеджери, категорії), грошей у них немає: spend і CPL приходять у браузер
    прямо з Facebook за токеном, який людина вписує сама. Логін потрібен, щоб доступ
    можна було відкликати одній людині, а не всім одразу зміною секрету. Відкликання
    залежить від того, хто підписав, — див. коментар біля перевірки нижче.

    Логін із «@» приїжджає з браузера як `%40`, тому шлях спершу розкодовується:
    без цього токен воркера не збігався б із шаблоном і давав 401 на все.
    """
    path = urllib.parse.unquote(path)
    m = re.search(r'[?&]t=(\d+)\.([A-Za-z0-9_.+@-]+)\.([0-9a-f]{64})(?=&|$)', path)
    if not m:
        return False
    exp, login, sig = int(m.group(1)), m.group(2), m.group(3)
    if exp < time.time():
        return False
    # Логін із «@» підписує воркер go.green-partners.com.ua (src/dash.js): список
    # людей дашборда лежить у нього в D1, і відкликання робить він — прибрали пошту,
    # і наступний обмін сесії на токен закритий. Тут лишається тільки підпис і строк,
    # тому строк тих токенів короткий (12 годин): рівно на стільки відкликання і
    # відстає. Логін без «@» — друга хвіртка, DASH_USERS у змінних Render, і для неї
    # відкликання, як і було, через список. Перетнутися вони не можуть: parse_users
    # «@» у логіні не приймає.
    if '@' not in login and login not in DASH_USERS:
        return False
    return hmac.compare_digest(sig, sign(exp, login))


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # Silent on cloud

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path.split('?')[0] != '/api/login':
            self.send_response(404); self.end_headers(); return
        if not PROXY_SECRET or not DASH_USERS:
            self._json({'ok': False, 'message': 'PROXY_SECRET або DASH_USERS не задані у змінних Render'}, 503); return
        # Тіло обмежуємо: інакше один запит може з'їсти пам'ять інстансу.
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            n = 0
        if n <= 0 or n > 4096:
            self._json({'ok': False, 'message': 'Порожній або завеликий запит'}, 400); return
        try:
            body = json.loads(self.rfile.read(n).decode('utf-8')) or {}
            login = str(body.get('login') or '').strip()
            got = str(body.get('password') or '')
        except Exception:
            self._json({'ok': False, 'message': 'Очікується JSON {"login": "...", "password": "..."}'}, 400); return
        # Пароль беремо навіть для невідомого логіна — порівняння мусить коштувати
        # однаково, інакше час відповіді розрізняє «логіна немає» і «пароль не той».
        want = DASH_USERS.get(login, '')
        ok = bool(want) and hmac.compare_digest(got.encode(), want.encode())
        if not ok:
            time.sleep(1)   # найдешевший стоп онлайн-перебору: один інстанс, запити послідовні
            self._json({'ok': False, 'message': 'Невірний логін або пароль'}, 401); return
        self._json({'ok': True, 't': mint_token(login), 'days': TOKEN_DAYS, 'login': login})

    def do_GET(self):
        # Health check Render дивиться на '/' і токена не має — його лишаємо відкритим.
        if self.path.startswith('/api/'):
            if not PROXY_SECRET:
                self._json({'ok': False, 'message': 'PROXY_SECRET не заданий у змінних Render'}, 503); return
            if not token_ok(self.path):
                self._json({'ok': False, 'message': 'Потрібен вхід'}, 401); return
        if self.path.startswith('/api/sync'):
            self._serve_sync(); return
        if self.path == '/api/data':
            self._serve_data(); return
        if self.path == '/api/ping':
            self._json({'ok': True, 'server': 'cloud'}); return
        # Root — health check для Render
        if self.path in ('/', '/health'):
            self._json({'ok': True, 'service': 'ads-dashboard-crm-proxy'}); return
        self.send_response(404); self.end_headers()

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',  ALLOW_ORIGIN)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, ngrok-skip-browser-warning')

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_data(self):
        if not os.path.exists(DATA_FILE):
            self._json({'ok': False, 'message': 'Немає даних. Виконай синхронізацію.'}); return
        with open(DATA_FILE, 'rb') as f:
            raw = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._cors()
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_sync(self):
        if   'mode=full'     in self.path: mode = 'full'
        elif 'mode=comments' in self.path: mode = 'comments'
        else:                              mode = 'inc'
        inc_days = inc_window(self.path)
        self.send_response(200)
        self.send_header('Content-Type',      'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control',     'no-cache')
        self.send_header('X-Accel-Buffering', 'no')
        self._cors()
        self.end_headers()

        _done = threading.Event()

        def send_event(obj):
            try:
                line = 'data: ' + json.dumps(obj, ensure_ascii=False) + '\n\n'
                self.wfile.write(line.encode('utf-8'))
                self.wfile.flush()
            except BrokenPipeError:
                pass

        def _keepalive():
            # Кожні 15 сек — Render/Netlify proxy не обриває SSE з'єднання
            while not _done.wait(15):
                try:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                except Exception:
                    break

        threading.Thread(target=_keepalive, daemon=True).start()

        try:
            sync_crm(mode, send_event, inc_days)
        except Exception as e:
            send_event({'type': 'error', 'message': str(e)})
        finally:
            _done.set()


class _ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    print(f'CRM Proxy Cloud running on port {PORT}')
    server = _ThreadedServer(('0.0.0.0', PORT), Handler)
    server.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('Stopped.')


if __name__ == '__main__':
    main()
