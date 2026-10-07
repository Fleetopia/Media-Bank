"""Media Bank — Telegram-бот приёма заявок (v7.0).

Переменные окружения (.env), все необязательные, кроме BOT_TOKEN и GROUP_ID:
    BOT_TOKEN, GROUP_ID, REPORT_CHAT_ID, DB_FILE, ENABLE_BUTTON_PREMIUM
    ADMIN_IDS            — id админов через запятую
    MANAGERS             — менеджеры в формате "id:Имя,id:Имя"
    REPORT_TZ            — часовой пояс отчётов (по умолчанию Europe/Moscow)
    STALE_REMINDER_MINUTES — через сколько минут напомнить о не взятой заявке (0 = выключено, по умолчанию 30)
    ESCALATE_MINUTES     — через сколько минут сообщить админам, что заявка не взята (по умолчанию 60, 0 = выкл.)
    STUCK_HOURS          — через сколько часов напомнить о заявке «в работе» без движения (по умолчанию 72, 0 = выкл.)
    MONTHLY_REPORT_HOUR  — час автоотчёта за прошлый месяц 1-го числа (по умолчанию 10)
    BACKUP_TO_ADMIN      — 1/0: присылать ежедневный бэкап БД админам в Telegram (по умолчанию 1)
    LOG_FILE             — путь к файлу логов (по умолчанию bot.log рядом с БД)
    MANAGER_PERCENT      — доля менеджера от суммы оффера, % (по умолчанию 100)
    DEFAULT_HOLD_DAYS    — холд для продуктов без оффера в OFFERS (по умолчанию 0)
    DAILY_REPORT_HOUR / DAILY_REPORT_MINUTE
    WEEKLY_REPORT_DAY (0 = понедельник) / WEEKLY_REPORT_HOUR / WEEKLY_REPORT_MINUTE
"""
import csv
import io
import json
import logging
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, time as dtime, timedelta, timezone
from html import escape, unescape

from dotenv import load_dotenv
from telegram import (BotCommand, BotCommandScopeChat, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands,
                      Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# ───────────────────────────── Конфигурация ─────────────────────────────
load_dotenv()
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
log = logging.getLogger('media_bank')

BOT_VERSION = 'MEDIA-BANK-7.2'
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
GROUP_ID = int(os.getenv('GROUP_ID', '0'))
REPORT_CHAT_ID = int(os.getenv('REPORT_CHAT_ID', str(GROUP_ID)))
DB_FILE = os.getenv('DB_FILE', '/data/media_bank.db' if os.path.isdir('/data') else 'media_bank.db')
LOG_FILE = os.getenv('LOG_FILE', os.path.join(os.path.dirname(os.path.abspath(DB_FILE)), 'bot.log'))
try:
    from logging.handlers import RotatingFileHandler
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    _fh = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    _fh.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(message)s'))
    logging.getLogger().addHandler(_fh)
except OSError as _e:
    log.warning('Файл логов недоступен: %s', _e)
ENABLE_BUTTON_PREMIUM = os.getenv('ENABLE_BUTTON_PREMIUM', '1') == '1'

DAILY_REPORT_HOUR = int(os.getenv('DAILY_REPORT_HOUR', '21'))
DAILY_REPORT_MINUTE = int(os.getenv('DAILY_REPORT_MINUTE', '0'))
WEEKLY_REPORT_DAY = int(os.getenv('WEEKLY_REPORT_DAY', '0'))
WEEKLY_REPORT_HOUR = int(os.getenv('WEEKLY_REPORT_HOUR', '21'))
WEEKLY_REPORT_MINUTE = int(os.getenv('WEEKLY_REPORT_MINUTE', '5'))

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(os.getenv('REPORT_TZ', 'Europe/Moscow'))
except Exception:  # нет tzdata / неверное имя пояса
    log.warning('REPORT_TZ недоступен, отчёты идут по UTC (pip install tzdata)')
    TZ = timezone.utc


def _parse_managers(raw):
    out = {}
    for part in raw.split(','):
        uid, _, name = part.partition(':')
        uid = uid.strip()
        if uid.isdigit():
            out[int(uid)] = name.strip() or uid
    return out


# Значения по умолчанию = прежние захардкоженные; лучше вынести в .env.
ADMIN_IDS = {int(x) for x in re.findall(r'\d+', os.getenv('ADMIN_IDS', '8431733595'))}
MANAGERS = _parse_managers(os.getenv('MANAGERS', '8431733595:Эдуард,8923153510:Александр'))

PRODUCTS = {'debit': 'Дебетовая карта', 'credit': 'Кредитная карта', 'rko': 'Регистрация бизнеса + РКО'}
PRODUCT_FALLBACK = {'debit': '💳', 'credit': '💰', 'rko': '🏢'}
BANKS = {
    'debit': ['Т-Банк', 'Альфа-Банк', 'ВТБ-Банк', 'Промсвязьбанк', 'Ак Барс Банк', 'ОТП банк'],
    'credit': ['Т-Банк', 'ВТБ', 'Уралсиб', 'ОТП-Банк', 'Яндекс — Кредитная карта супер Сплит', 'Альфа-Банк'],
    'rko': ['Альфа-Банк', 'Промсвязьбанк', 'РКО от Санкт-Петербург Банка', 'УБРиР Банк'],
}
STATUS_TITLES = {'all': 'Все', 'new': 'Новые', 'in_work': 'В работе', 'completed': 'Завершённые', 'rejected': 'Отказ'}
EVENT_TITLES = {'created': 'Создана', 'taken': 'Взята в работу', 'completed': 'Завершена', 'rejected': 'Отказ',
                'reopened': 'Возвращена в работу', 'reassigned': 'Передана', 'note': 'Заметка', 'rated': 'Оценка клиента'}

PAGE_SIZE = 8
MAX_APPS_PER_HOUR = 5       # лимит заявок от одного клиента (персонал не ограничен)
DUPLICATE_WINDOW_HOURS = 24
REPORT_CATCHUP_HOURS = 6    # если бот был выключен, отчёт уйдёт с опозданием не больше этого срока
BACKUPS_KEEP = 14
STALE_REMINDER_MINUTES = int(os.getenv('STALE_REMINDER_MINUTES', '30'))
NOTE_MAX_LEN = 500
HISTORY_LIMIT = 15
SEARCH_LIMIT = 10
ESCALATE_MINUTES = int(os.getenv('ESCALATE_MINUTES', '60'))
STUCK_HOURS = int(os.getenv('STUCK_HOURS', '72'))
MONTHLY_REPORT_HOUR = int(os.getenv('MONTHLY_REPORT_HOUR', '10'))
BACKUP_TO_ADMIN = os.getenv('BACKUP_TO_ADMIN', '1') == '1'
MAX_BACKUP_SEND_MB = 45
MONTHS_RU = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь']
SHORT_PRODUCTS = {'debit': 'Дебет', 'credit': 'Кредит', 'rko': 'РКО'}
BLOCKED = set()  # заблокированные клиенты (загружается из БД)

# Офферы партнёрской сети: (продукт, банк) → (сумма оффера ₽, холд в днях, ЦД — целевое действие).
# Названия банков должны совпадать с BANKS. Суммы попадают в «Ставки выплат» при первом запуске,
# дальше их можно менять в админ-панели.
OFFERS = {
    ('debit', 'Т-Банк'): (1311, 30, 'от 500'),
    ('debit', 'Альфа-Банк'): (1194, 30, 'от 500'),
    ('debit', 'ВТБ-Банк'): (2165, 30, 'от 500'),
    ('debit', 'Промсвязьбанк'): (1522, 30, 'от 1000'),
    ('debit', 'Ак Барс Банк'): (1760, 30, 'от 1000'),
    ('debit', 'ОТП банк'): (1720, 30, 'от 1000'),
    ('rko', 'Альфа-Банк'): (16000, 45, 'Открытие счёта'),
    ('rko', 'Промсвязьбанк'): (11000, 30, 'Открытие счёта на платном тарифе'),
    ('rko', 'РКО от Санкт-Петербург Банка'): (11250, 30, 'от 5000'),
    ('rko', 'УБРиР Банк'): (11250, 30, 'на тарифах Бизнес и Премиум-класс от 4500'),
    ('credit', 'Т-Банк'): (4050, 30, 'от 1000'),
    ('credit', 'ВТБ'): (3100, 30, 'от 2000'),
    ('credit', 'Уралсиб'): (4500, 30, 'от 500'),
    ('credit', 'ОТП-Банк'): (4310, 30, 'от 1000'),
    ('credit', 'Яндекс — Кредитная карта супер Сплит'): (5098, 30, 'от 1000'),
    ('credit', 'Альфа-Банк'): (3680, 30, 'от 1000'),
}
# прежние значения ставок, которые нужно обновить, если админ их не менял (старое → новое берётся из OFFERS)
OFFERS_LEGACY = {('credit', 'ОТП-Банк'): 4775}
# суммы, которые засевала версия 7.1: нужны, чтобы отличить «не правили вручную» от «изменено админом»
PREV_SEED_71 = {('rko', 'Альфа-Банк'): 16000, ('rko', 'Промсвязьбанк'): 11000, ('rko', 'РКО от Санкт-Петербург Банка'): 11250,
                ('rko', 'УБРиР Банк'): 11250, ('credit', 'Т-Банк'): 4050, ('credit', 'ВТБ'): 3100, ('credit', 'Уралсиб'): 4500,
                ('credit', 'ОТП-Банк'): 4775, ('credit', 'Яндекс — Кредитная карта супер Сплит'): 5098, ('credit', 'Альфа-Банк'): 3680}
MANAGER_PERCENT = min(max(float(os.getenv('MANAGER_PERCENT', '100')), 0), 100)
DEFAULT_HOLD_DAYS = int(os.getenv('DEFAULT_HOLD_DAYS', '0'))

# Premium-эмодзи: имя → custom emoji id
EMOJI = {
    'welcome': '5438496463044752972',
    'debit': '5445353829304387411',
    'credit': '5287231198098117669',
    'rko': '5278702045883292456',
    'form': '5197269100878907942', 'apps': '5197269100878907942',
    'apps_all': '5197269100878907942', 'edit': '5197269100878907942',
    'back': '5416117059207572332', 'cancel': '5416117059207572332',
    'send': '5206607081334906820', 'done': '5206607081334906820',
    'user': '5373012449597335010',
    'manager': '5190498849440931467', 'manager_select': '5190498849440931467',
    'manager_admin': '5190498849440931467',
    'new': '5382357040008021292',
    'work': '5386367538735104399',
    'open': '5444856076954520455', 'export': '5444856076954520455',
    'admin': '5217822164362739968',
    'stats': '5231200819986047254',
    'report': '5244837092042750681',
    'bank': '5332455502917949981',
    'search': '5379999674193172777',
}


# ───────────────────────────── Утилиты ─────────────────────────────
def emoji(key, fallback='•'):
    eid = EMOJI.get(key)
    if not eid:
        return escape(fallback)
    return f'<tg-emoji emoji-id="{eid}">{escape(fallback)}</tg-emoji>'


def strip_custom_emoji(text):
    return re.sub(r'<tg-emoji\s+emoji-id="[^"]+">(.*?)</tg-emoji>', r'\1', text, flags=re.S)


def btn(text, key=None, data=None):
    icon = EMOJI.get(key) if (ENABLE_BUTTON_PREMIUM and key) else None
    return InlineKeyboardButton(text, callback_data=data, icon_custom_emoji_id=icon)


TS_FMT = '%Y-%m-%d %H:%M:%S'


def now():
    return datetime.now(timezone.utc).strftime(TS_FMT + ' UTC')


def utc_ago(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime(TS_FMT)


def local_midnight_utc():
    n = datetime.now(TZ)
    return n.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime(TS_FMT)


def fmt_money(x):
    s = f'{int(x):,}' if x == int(x) else f'{x:,.2f}'.rstrip('0').rstrip('.')
    return s.replace(',', ' ') + ' ₽'


def parse_money(value):
    t = re.sub(r'(?i)(руб(лей|\.)?|р\.?|₽|rub)', '', value).replace('\xa0', '').replace(' ', '').replace(',', '.')
    try:
        x = float(t)
    except ValueError:
        return None
    return x if 0 <= x <= 10_000_000 else None


def period_days(days):
    return (utc_ago(days=days), None, 'за сутки' if days == 1 else f'за {days} дн.')


def period_month(offset=0):
    # календарный месяц в часовом поясе отчётов; offset 0 = текущий, -1 = прошлый
    n = datetime.now(TZ)
    y, mo = n.year, n.month + offset
    while mo < 1:
        mo += 12
        y -= 1
    ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
    to_utc = lambda d: d.astimezone(timezone.utc).strftime(TS_FMT)
    return (to_utc(datetime(y, mo, 1, tzinfo=TZ)), to_utc(datetime(ny, nm, 1, tzinfo=TZ)), f'за {MONTHS_RU[mo - 1]} {y}')


PERIOD_ALIASES = {'month': 'm0', 'месяц': 'm0', 'm0': 'm0', 'prev': 'm1', 'прошлый': 'm1', 'm1': 'm1'}


def parse_period(arg):
    arg = PERIOD_ALIASES.get((arg or '').lower(), arg)
    if arg == 'm0':
        return period_month(0)
    if arg == 'm1':
        return period_month(-1)
    d = to_int(arg)
    return period_days(d if d and 1 <= d <= 366 else 7)


def _range(col, period):
    since, until, _label = period
    sql, args = f'{col}>=?', [since]
    if until:
        sql += f' AND {col}<?'
        args.append(until)
    return sql, args


def fmt_dt(s):
    """'2026-10-04 12:00:00 UTC' → '04.10.2026 15:00' в часовом поясе отчётов."""
    try:
        dt = datetime.strptime(s[:19], TS_FMT).replace(tzinfo=timezone.utc).astimezone(TZ)
        return dt.strftime('%d.%m.%Y %H:%M')
    except (ValueError, TypeError):
        return s or ''


def fmt_minutes(m):
    m = max(m, 0)
    if m < 1:
        return 'меньше минуты'
    return f'{round(m)} мин' if m < 60 else f'{m / 60:.1f} ч'


def to_int(s):
    return int(s) if s and s.lstrip('-').isdigit() else None


def is_admin(uid):
    return uid in ADMIN_IDS


def is_manager(uid):
    return uid in MANAGERS


def is_staff(uid):
    return is_admin(uid) or is_manager(uid)


def can_manage(uid, manager_id):
    return is_admin(uid) or uid == manager_id


def actor_name(uid):
    return MANAGERS.get(uid) or ('Админ' if is_admin(uid) else str(uid))


USERNAME_RE = re.compile(r'^[A-Za-z][A-Za-z0-9_]{4,31}$')
PHONE_RE = re.compile(r'^\+?\d{10,15}$')
CONTACT_SAFE_RE = re.compile(r'^(@[A-Za-z0-9_]{5,32}|\+?\d{10,15})$')


def normalize_contact(value):
    """@username / t.me/username / телефон → нормализованная строка или None."""
    v = re.sub(r'^(?:https?://)?(?:t\.me|telegram\.me)/', '', value.strip(), flags=re.I).lstrip('@').strip('/')
    if USERNAME_RE.fullmatch(v):
        return '@' + v
    phone = re.sub(r'[\s\-()]', '', v)
    return phone if PHONE_RE.fullmatch(phone) else None


def csv_safe(value):
    """Защита от формул в Excel (CSV-инъекция), контакты не трогаем."""
    s = '' if value is None else str(value)
    if s[:1] in ('=', '+', '-', '@', '\t', '\r') and not CONTACT_SAFE_RE.fullmatch(s):
        return "'" + s
    return s


# ───────────────────────────── База данных ─────────────────────────────
HAS_LEGACY_NAME = False  # в старых БД есть обязательная колонка `name`


@contextmanager
def db():
    """Соединение с коммитом при успехе, откатом при ошибке и обязательным закрытием."""
    conn = sqlite3.connect(DB_FILE, timeout=10)
    # SQLite lower()/LIKE не понимают кириллицу — регистр для поиска считаем сами
    conn.create_function('LOWERU', 1, lambda v: v.casefold() if isinstance(v, str) else v)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


APP_MIGRATIONS = {
    'user_id': 'INTEGER', 'username': 'TEXT', 'product': 'TEXT', 'bank': 'TEXT',
    'client_name': 'TEXT', 'contact': 'TEXT', 'manager': 'TEXT', 'manager_id': 'INTEGER',
    'status': "TEXT DEFAULT 'new'", 'taken_at': 'TEXT', 'completed_at': 'TEXT',
    'group_message_id': 'INTEGER', 'reminded_at': 'TEXT',
    'rating': 'INTEGER', 'escalated_at': 'TEXT', 'stuck_reminded_at': 'TEXT',
}


def init_db():
    global HAS_LEGACY_NAME
    os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS applications(
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, user_id INTEGER NOT NULL, username TEXT,
            product TEXT NOT NULL, bank TEXT NOT NULL, client_name TEXT NOT NULL, contact TEXT NOT NULL,
            manager TEXT NOT NULL, manager_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new',
            taken_at TEXT, completed_at TEXT, group_message_id INTEGER, reminded_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS app_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT, app_id INTEGER NOT NULL, ts TEXT NOT NULL,
            actor_id INTEGER, actor TEXT, event TEXT NOT NULL, details TEXT)''')
        c.execute('CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT, first_seen TEXT NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)')
        c.execute('CREATE TABLE IF NOT EXISTS rates(product TEXT NOT NULL, bank TEXT NOT NULL, '
                  'amount REAL NOT NULL DEFAULT 0, PRIMARY KEY(product,bank))')
        c.execute('CREATE TABLE IF NOT EXISTS staff_managers(user_id INTEGER PRIMARY KEY, name TEXT NOT NULL, '
                  'active INTEGER NOT NULL DEFAULT 1)')
        c.execute('CREATE TABLE IF NOT EXISTS blocked(user_id INTEGER PRIMARY KEY, reason TEXT, ts TEXT)')
        seed_rates(c)
        for (p, b), old in OFFERS_LEGACY.items():
            c.execute('UPDATE rates SET amount=? WHERE product=? AND bank=? AND amount=?', (OFFERS[(p, b)][0], p, b, old))

        # Миграции старых БД без потери заявок
        cols = {r[1] for r in c.execute('PRAGMA table_info(applications)')}
        for col, ddl in APP_MIGRATIONS.items():
            if col not in cols:
                c.execute(f'ALTER TABLE applications ADD COLUMN {col} {ddl}')
        HAS_LEGACY_NAME = 'name' in cols
        ucols = {r[1] for r in c.execute('PRAGMA table_info(users)')}
        if 'username' not in ucols:
            c.execute('ALTER TABLE users ADD COLUMN username TEXT')
        if 'first_seen' not in ucols:
            c.execute('ALTER TABLE users ADD COLUMN first_seen TEXT')
            c.execute('UPDATE users SET first_seen=? WHERE first_seen IS NULL', (now(),))
        c.execute("UPDATE applications SET status='new' WHERE status IS NULL OR status=''")

        c.execute('CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_apps_manager ON applications(manager_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_apps_created ON applications(created_at)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_apps_user ON applications(user_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_events_app ON app_events(app_id)')
    load_managers()
    load_blocked()


# COALESCE: в старых заявках после миграции часть полей может быть NULL
APP_COLS = ("id,COALESCE(created_at,''),COALESCE(product,''),COALESCE(bank,''),COALESCE(client_name,''),"
            "COALESCE(contact,''),COALESCE(manager,''),COALESCE(status,'new')")


def get_app(aid):
    with db() as c:
        return c.execute(f'SELECT {APP_COLS} FROM applications WHERE id=?', (aid,)).fetchone()


def counts(mid=None):
    where = ' WHERE manager_id=?' if mid else ''
    args = [local_midnight_utc()] + ([mid] if mid else [])
    with db() as c:
        r = c.execute(
            "SELECT COUNT(*),SUM(status='new'),SUM(status='in_work'),SUM(status='completed'),SUM(status='rejected'),"
            f'SUM(created_at>=?) FROM applications{where}', args).fetchone()
    return dict(total=r[0] or 0, new=r[1] or 0, work=r[2] or 0, done=r[3] or 0, rej=r[4] or 0, today=r[5] or 0)


def meta_get(key):
    with db() as c:
        row = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else None


def meta_set(key, value):
    with db() as c:
        c.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))


def log_event(c, aid, event, actor_id, actor=None, details=''):
    # история заявки: кто, когда и что сделал
    c.execute('INSERT INTO app_events(app_id,ts,actor_id,actor,event,details) VALUES(?,?,?,?,?,?)',
              (aid, now(), actor_id, actor or actor_name(actor_id), event, details))


def load_managers():
    # менеджеры из .env + добавленные/удалённые командами /addmanager и /delmanager (БД главнее)
    with db() as c:
        rows = c.execute('SELECT user_id,name,active FROM staff_managers').fetchall()
    for uid, name, active in rows:
        if active:
            MANAGERS[uid] = name
        else:
            MANAGERS.pop(uid, None)


def load_blocked():
    with db() as c:
        BLOCKED.clear()
        BLOCKED.update(r[0] for r in c.execute('SELECT user_id FROM blocked'))


def load_rates():
    with db() as c:
        return {(p, b): a for p, b, a in c.execute('SELECT product,bank,amount FROM rates')}


def seed_rates(c):
    # стартовые ставки из OFFERS: новые добавляются; прежние обновляются, только если их не меняли вручную
    row = c.execute("SELECT value FROM meta WHERE key='offers_seed'").fetchone()
    prev = {tuple(k.split('|', 1)): v for k, v in json.loads(row[0]).items()} if row else PREV_SEED_71
    for (p, b), offer in OFFERS.items():
        cur = c.execute('SELECT amount FROM rates WHERE product=? AND bank=?', (p, b)).fetchone()
        if cur is None:
            c.execute('INSERT INTO rates(product,bank,amount) VALUES(?,?,?)', (p, b, offer[0]))
        elif cur[0] == prev.get((p, b)) and cur[0] != offer[0]:
            c.execute('UPDATE rates SET amount=? WHERE product=? AND bank=?', (offer[0], p, b))
    c.execute('INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
              ('offers_seed', json.dumps({f'{p}|{b}': o[0] for (p, b), o in OFFERS.items()}, ensure_ascii=False)))


def rating_summary(mid=None):
    with db() as c:
        r = c.execute('SELECT AVG(rating),COUNT(rating) FROM applications WHERE rating IS NOT NULL' + (' AND manager_id=?' if mid else ''),
                      (mid,) if mid else ()).fetchone()
    return (r[0], r[1]) if r and r[1] else (None, 0)


def backup_database():
    """Консистентный бэкап SQLite в папку backups рядом с БД."""
    if not os.path.exists(DB_FILE):
        return None
    backup_dir = os.path.join(os.path.dirname(os.path.abspath(DB_FILE)), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_UTC')
    target = os.path.join(backup_dir, f'media_bank_{stamp}.db')
    with closing(sqlite3.connect(DB_FILE)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)
    old = sorted((os.path.join(backup_dir, n) for n in os.listdir(backup_dir) if n.endswith('.db')),
                 key=os.path.getmtime, reverse=True)
    for path in old[BACKUPS_KEEP:]:
        try:
            os.remove(path)
        except OSError:
            log.warning('Не удалось удалить старый бэкап: %s', path)
    log.info('Бэкап БД создан: %s', target)
    return target


# ───────────────────────────── Отправка сообщений ─────────────────────────────
def _entity_error(e):
    s = str(e).lower()
    return 'entity_text_invalid' in s or "can't parse entities" in s


async def _call(fn, text, **kw):
    """Вызов с HTML; если Telegram не принял premium-эмодзи, повтор с обычными."""
    kw.setdefault('parse_mode', ParseMode.HTML)
    try:
        return await fn(text, **kw)
    except BadRequest as e:
        if not _entity_error(e):
            raise
        log.warning('Ошибка разметки, повтор без premium-эмодзи: %s', e)
    try:
        return await fn(strip_custom_emoji(text), **kw)
    except BadRequest as e:
        if not _entity_error(e):
            raise
        log.warning('Ошибка разметки, отправляю обычным текстом: %s', e)
    kw['parse_mode'] = None
    return await fn(unescape(re.sub(r'<[^>]+>', '', strip_custom_emoji(text))), **kw)


async def edit(q, text, kb=None):
    try:
        await _call(q.edit_message_text, text, reply_markup=kb)
    except BadRequest as e:
        if 'not modified' not in str(e).lower():
            raise


async def safe_reply(message, text, **kw):
    return await _call(message.reply_text, text, **kw)


async def safe_send(bot, chat_id, text, **kw):
    return await _call(lambda t, **k: bot.send_message(chat_id, t, **k), text, **kw)


async def notify(bot, chat_id, text, kb=None):
    """Личное уведомление; не падает, если человек не запускал бота."""
    try:
        await safe_send(bot, chat_id, text, reply_markup=kb)
        return True
    except TelegramError as e:
        log.warning('Уведомление %s не доставлено: %s', chat_id, e)
        return False


async def _answer(q, text=None, alert=False):
    try:
        await q.answer(text, show_alert=alert)
    except TelegramError as e:
        log.debug('answer failed: %s', e)


# ───────────────────────────── Клавиатуры и тексты ─────────────────────────────
def status_label(s):
    return {
        'new': emoji('new', '🆕') + ' <b>Новая</b>',
        'in_work': emoji('work', '🔄') + ' <b>В работе</b>',
        'completed': emoji('done', '✅') + ' <b>Завершена</b>',
        'rejected': '❌ <b>Отказ</b>',
    }.get(s, escape(str(s)))


def home_kb(uid):
    rows = [[btn('Дебетовая карта', 'debit', 'product:debit')],
            [btn('Кредитная карта', 'credit', 'product:credit')],
            [btn('Регистрация бизнеса + РКО', 'rko', 'product:rko')],
            [btn('Оставить заявку', 'form', 'form')],
            [btn('Статус моих заявок', 'apps', 'client_apps')]]
    if is_admin(uid):
        rows.append([btn('Админ-панель', 'admin', 'admin')])
    if is_manager(uid):
        rows.append([btn('Панель менеджера', 'manager', 'manager_panel')])
    return InlineKeyboardMarkup(rows)


def product_kb():
    return InlineKeyboardMarkup([[btn(PRODUCTS[p], p, f'product:{p}')] for p in PRODUCTS] + [[btn('Назад', 'back', 'home')]])


def bank_kb(p):
    return InlineKeyboardMarkup([[btn(b, 'bank', f'bank:{p}:{i}')] for i, b in enumerate(BANKS[p])] + [[btn('Назад', 'back', 'home')]])


def manager_kb():
    return InlineKeyboardMarkup([[btn(n, 'manager_select', f'mgr:{m}')] for m, n in MANAGERS.items()] + [[btn('Назад', 'back', 'home')]])


def form_kb():
    return InlineKeyboardMarkup([[btn('Назад', 'back', 'home')]])


def review_kb():
    return InlineKeyboardMarkup([[btn('Отправить заявку', 'send', 'submit')], [btn('Изменить', 'edit', 'edit')], [btn('Отмена', 'cancel', 'cancel')]])


def actions(aid, st):
    rows = []
    if st == 'new':
        rows.append([btn('Взять в работу', 'work', f'take:{aid}')])
    elif st == 'in_work':
        rows.append([btn('Завершить', 'done', f'complete:{aid}'), btn('Отказ', None, f'reject:{aid}')])
    elif st in ('completed', 'rejected'):
        rows.append([btn('Вернуть в работу', 'work', f'reopen:{aid}')])
    tools = [btn('Заметка', 'edit', f'note:{aid}')]
    if st in ('new', 'in_work'):
        tools.append(btn('Передать', 'manager', f'reassign:{aid}'))
    rows.append(tools)
    rows.append([btn('Открыть', 'open', f'view:{aid}'), btn('История', None, f'history:{aid}')])
    return InlineKeyboardMarkup(rows)


def reassign_kb(aid, current):
    rows = [[btn(n, 'manager_select', f'reassign_to:{aid}:{m}')] for m, n in MANAGERS.items() if m != current]
    return InlineKeyboardMarkup(rows + [[btn('Назад', 'back', f'view:{aid}')]])


def admin_kb():
    return InlineKeyboardMarkup([
        [btn('Все заявки', 'apps_all', 'apps:all')],
        [btn('Новые', 'new', 'apps:new'), btn('В работе', 'work', 'apps:in_work'), btn('Завершённые', 'done', 'apps:completed')],
        [btn('Статистика', 'stats', 'stats')],
        [btn('Отчёт 7 дн.', 'report', 'report:7'), btn('Отчёт 30 дн.', 'report', 'report:30')],
        [btn('Отчёт: этот месяц', 'report', 'report:m0'), btn('Прошлый месяц', 'report', 'report:m1')],
        [btn('Выплаты: этот месяц', 'report', 'payouts:m0'), btn('Прошлый месяц', 'report', 'payouts:m1')],
        [btn('Ставки выплат', 'stats', 'rates')],
        [btn('Менеджеры', 'manager_admin', 'managers'), btn('Поиск', 'search', 'search')],
        [btn('Экспорт CSV', 'export', 'export')],
        [btn('Главное меню', 'back', 'home')]])


def manager_panel():
    return InlineKeyboardMarkup([
        [btn('Все заявки', 'apps_all', 'mgrapps:all')],
        [btn('Новые', 'new', 'mgrapps:new'), btn('В работе', 'work', 'mgrapps:in_work')],
        [btn('Завершённые', 'done', 'mgrapps:completed')],
        [btn('Мои заявки', 'apps', 'myapps'), btn('Поиск', 'search', 'search')],
        [btn('Моя статистика', 'stats', 'mystats')],
        [btn('Мой отчёт 7 дн.', 'report', 'myreport:7'), btn('30 дн.', 'report', 'myreport:30')],
        [btn('Мои выплаты: месяц', 'report', 'mypayout:m0'), btn('Прошлый месяц', 'report', 'mypayout:m1')],
        [btn('Мой профиль', 'manager_admin', 'myprofile')],
        [btn('Главное меню', 'back', 'home')]])


def rate_kb(aid):
    return InlineKeyboardMarkup([[btn(f'{n}⭐', None, f'rv:{aid}:{n}') for n in range(1, 6)]])


def app_text(r):
    aid, created, product, bank, name, contact, manager, st = r
    return (f'{emoji("open", "📂")} <b>Заявка #{aid}</b>\n\n'
            f'{emoji(product, PRODUCT_FALLBACK.get(product, "•"))} <b>Продукт:</b> {escape(PRODUCTS.get(product, str(product)))}\n'
            f'{emoji("bank", "🏦")} <b>Банк:</b> {escape(bank)}\n'
            f'{emoji("user", "👤")} <b>Имя:</b> {escape(name)}\n'
            f'{emoji("user", "👤")} <b>Telegram:</b> {escape(contact)}\n'
            f'{emoji("manager", "🤝")} <b>Менеджер:</b> {escape(manager)}\n'
            f'{status_label(st)}\n\n<i>Создана: {escape(fmt_dt(created))}</i>')


def notes_block(aid):
    with db() as c:
        rows = c.execute("SELECT ts,actor,details FROM app_events WHERE app_id=? AND event='note' "
                         'ORDER BY id DESC LIMIT 3', (aid,)).fetchall()
    if not rows:
        return ''
    lines = '\n'.join(f'• <i>{escape(fmt_dt(ts))}</i> {escape(actor or "")}: {escape(details or "")}' for ts, actor, details in reversed(rows))
    return f'\n\n{emoji("edit", "📝")} <b>Заметки:</b>\n{lines}'


def render_app(aid):
    # текст карточки: данные заявки, отправитель, оценка клиента и последние заметки
    r = get_app(aid)
    if not r:
        return None, None
    with db() as c:
        src = c.execute("SELECT user_id,COALESCE(username,''),rating FROM applications WHERE id=?", (aid,)).fetchone()
    text = app_text(r)
    if src:
        who = f'@{src[1]} · ' if src[1] else ''
        text += f'\n<i>Отправил: {escape(who)}id <code>{src[0]}</code></i>'
        if src[2]:
            text += f'\n⭐ Оценка клиента: <b>{src[2]}/5</b>'
    return text + notes_block(aid), r


def stats_text(mid=None):
    n = counts(mid)
    avg, cnt = rating_summary(mid)
    title = emoji('stats', '📊') + ' <b>Статистика</b>' + (f' · {escape(MANAGERS.get(mid, str(mid)))}' if mid else '')
    rating = f'\n⭐ Средняя оценка: <b>{avg:.1f}</b> ({cnt})' if cnt else ''
    return (f'{title}\n\n{emoji("apps_all", "📋")} Всего: <b>{n["total"]}</b>\n'
            f'{emoji("new", "🆕")} Новых: <b>{n["new"]}</b>\n'
            f'{emoji("work", "🔄")} В работе: <b>{n["work"]}</b>\n'
            f'{emoji("done", "✅")} Завершено: <b>{n["done"]}</b>\n'
            f'❌ Отказов: <b>{n["rej"]}</b>\n'
            f'Сегодня: <b>{n["today"]}</b>{rating}')


def manager_profile(uid):
    n = counts(uid)
    avg, cnt = rating_summary(uid)
    rating = f'\n⭐ <b>Средняя оценка:</b> {avg:.1f} ({cnt})' if cnt else ''
    return (f'{emoji("manager", "👤")} <b>Профиль менеджера</b>\n\n'
            f'<b>Имя:</b> {escape(MANAGERS.get(uid, "Менеджер"))}\n'
            f'<b>Telegram ID:</b> <code>{uid}</code>\n\n'
            f'{emoji("apps_all", "📋")} <b>Всего заявок:</b> {n["total"]}\n'
            f'{emoji("new", "🆕")} <b>Новые:</b> {n["new"]}\n'
            f'{emoji("work", "🔄")} <b>В работе:</b> {n["work"]}\n'
            f'{emoji("done", "✅")} <b>Завершено:</b> {n["done"]}\n'
            f'❌ <b>Отказов:</b> {n["rej"]}{rating}')


# ───────────────────────────── Отчёты ─────────────────────────────
def completed_rows(period, mid=None):
    # оформлено продуктов (по дате завершения): менеджер → банк → продукт → штуки
    sql, args = _range('COALESCE(completed_at,created_at)', period)
    extra = ' AND manager_id=?' if mid else ''
    with db() as c:
        return c.execute("SELECT COALESCE(manager,''),COALESCE(bank,''),COALESCE(product,''),COUNT(*) FROM applications "
                         f"WHERE status='completed' AND {sql}{extra} GROUP BY 1,2,3 ORDER BY 1,4 DESC,2",
                         args + ([mid] if mid else [])).fetchall()


def build_report(period, mid=None):
    extra = ' AND manager_id=?' if mid else ''
    xa = [mid] if mid else []
    c_sql, c_args = _range('created_at', period)
    d_sql, d_args = _range('COALESCE(completed_at,created_at)', period)
    with db() as c:
        rows = c.execute("SELECT COALESCE(manager,''),COUNT(*),SUM(status='rejected') FROM applications "
                         f'WHERE {c_sql}{extra} GROUP BY 1 ORDER BY 1', c_args + xa).fetchall()
        avg = c.execute('SELECT AVG((julianday(substr(taken_at,1,19))-julianday(substr(created_at,1,19)))*1440) FROM applications '
                        f'WHERE taken_at IS NOT NULL AND {c_sql}{extra}', c_args + xa).fetchone()[0]
        waiting = c.execute("SELECT COUNT(*) FROM applications WHERE status='new'" + extra, xa).fetchone()[0]
        ratings = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT COALESCE(manager,''),AVG(rating),COUNT(rating) FROM applications WHERE status='completed' "
            f'AND rating IS NOT NULL AND {d_sql}{extra} GROUP BY 1', d_args + xa)}
    done_rows = completed_rows(period, mid)

    out = [f'{emoji("report", "📈")} <b>Отчёт Media Bank {period[2]}</b>', '']
    if rows:
        out += [f'• {escape(m)}: поступило {n}' + (f', отказов {rej}' if rej else '') for m, n, rej in rows]
        if len(rows) > 1:
            out.append(f'<b>Итого поступило:</b> {sum(r[1] for r in rows)}')
    else:
        out.append('Новых заявок за период нет.')

    if done_rows:
        by_manager = {}
        for m, bank, prod, n in done_rows:
            by_manager.setdefault(m, []).append((bank, prod, n))
        out += ['', f'{emoji("done", "✅")} <b>Оформлено продуктов</b> (по дате завершения):']
        for m, items in by_manager.items():
            star = f' · ⭐ {ratings[m][0]:.1f} ({ratings[m][1]})' if m in ratings else ''
            out += ['', f'{emoji("manager", "👤")} <b>{escape(m)}</b> — {sum(i[2] for i in items)} шт.{star}']
            out += [f'   • {escape(b)} · {escape(PRODUCTS.get(p, p))}: <b>{n}</b>' for b, p, n in items]
        if not mid:
            by_bank = {}
            for _m, bank, _p, n in done_rows:
                by_bank[bank] = by_bank.get(bank, 0) + n
            out += ['', f'{emoji("bank", "🏦")} <b>По банкам (все менеджеры):</b>']
            out += [f'   • {escape(b)}: <b>{n}</b>' for b, n in sorted(by_bank.items(), key=lambda x: (-x[1], x[0]))]
        out += ['', f'<b>Итого оформлено:</b> {sum(r[3] for r in done_rows)} шт.']
    else:
        out += ['', f'{emoji("done", "✅")} За период оформленных продуктов нет.']

    if avg is not None:
        out.append(f'\n⏱ Среднее время до взятия в работу: <b>{fmt_minutes(avg)}</b>')
    out.append(f'{emoji("new", "🆕")} Ждут взятия в работу: <b>{waiting}</b>')
    return '\n'.join(out)


def hold_days(prod, bank):
    offer = OFFERS.get((prod, bank))
    return offer[1] if offer else DEFAULT_HOLD_DAYS


def build_payouts(period, mid=None):
    # выплаты = оформленные продукты × сумма оффера × доля менеджера; учитывается холд партнёрской сети.
    # Суммы видят только админ и сам менеджер.
    sql, args = _range('COALESCE(completed_at,created_at)', period)
    extra = ' AND manager_id=?' if mid else ''
    with db() as c:
        apps = c.execute("SELECT COALESCE(manager,''),COALESCE(bank,''),COALESCE(product,''),COALESCE(completed_at,created_at) "
                         f"FROM applications WHERE status='completed' AND {sql}{extra}", args + ([mid] if mid else [])).fetchall()
    rates = load_rates()
    share = MANAGER_PERCENT / 100
    out = [f'{emoji("stats", "💰")} <b>Выплаты {period[2]}</b>']
    if MANAGER_PERCENT < 100:
        out.append(f'<i>Доля менеджера: {MANAGER_PERCENT:g}% от суммы оффера</i>')
    if not apps:
        return '\n'.join(out + ['', 'Оформленных продуктов за период нет.'])

    now_dt = datetime.now(timezone.utc)
    groups = {}  # менеджер → {(банк, продукт): [доступно, всего]}
    for m, bank, prod, done in apps:
        try:
            done_dt = datetime.strptime(done[:19], TS_FMT).replace(tzinfo=timezone.utc)
        except ValueError:
            done_dt = now_dt - timedelta(days=3650)  # непонятную дату считаем давней
        g = groups.setdefault(m, {}).setdefault((bank, prod), [0, 0])
        g[1] += 1
        g[0] += 1 if done_dt + timedelta(days=hold_days(prod, bank)) <= now_dt else 0

    grand = grand_free = 0
    missing = {}
    for m in sorted(groups):
        total = free_total = 0
        lines = []
        for (bank, prod), (free, n) in sorted(groups[m].items(), key=lambda x: (-x[1][1], x[0])):
            rate = rates.get((prod, bank), 0) * share
            name = f'{escape(bank)} · {SHORT_PRODUCTS.get(prod, escape(prod))}'
            if not rate:
                missing[(bank, prod)] = missing.get((bank, prod), 0) + n
                lines.append(f'   • {name}: {n} шт. — ставка не задана')
                continue
            total += n * rate
            free_total += free * rate
            hold = hold_days(prod, bank)
            suffix = ''
            if hold:
                suffix = f' · холд {hold} дн.' + (f', в холде: {n - free}' if n > free else '')
            lines.append(f'   • {name}: {n} × {fmt_money(rate)} = {fmt_money(n * rate)}{suffix}')
        grand += total
        grand_free += free_total
        split = f' (доступно {fmt_money(free_total)}, в холде {fmt_money(total - free_total)})' if total > free_total else ''
        out += ['', f'{emoji("manager", "👤")} <b>{escape(m)}</b> — <b>{fmt_money(total)}</b>{split}'] + lines
    if len(groups) > 1:
        split = f' (доступно {fmt_money(grand_free)}, в холде {fmt_money(grand - grand_free)})' if grand > grand_free else ''
        out += ['', f'<b>Итого к выплате:</b> {fmt_money(grand)}{split}']
    if missing:
        names = ', '.join(f'{escape(b)} · {SHORT_PRODUCTS.get(p, escape(p))} ({n} шт.)' for (b, p), n in missing.items())
        out += ['', f'⚠️ Нет ставки: {names}. Задайте в «Ставки выплат».']
    return '\n'.join(out)


async def send_long(bot, chat_id, text, limit=3800):
    # лимит Telegram 4096 символов: режем по строкам, каждая строка самодостаточна по разметке
    chunk = ''
    for line in text.split('\n'):
        if chunk and len(chunk) + len(line) + 1 > limit:
            await safe_send(bot, chat_id, chunk)
            chunk = line
        else:
            chunk = f'{chunk}\n{line}' if chunk else line
    if chunk:
        await safe_send(bot, chat_id, chunk)


async def send_report(bot, period, mid=None, chat_id=None):
    await send_long(bot, chat_id or REPORT_CHAT_ID, build_report(period, mid))


async def deliver_report(bot, requester_id, period, mid=None):
    # ручной отчёт: в рабочий чат; если не вышло, присылаем запросившему и возвращаем причину
    try:
        await send_report(bot, period, mid)
        return None
    except TelegramError as e:
        log.exception('Отчёт не доставлен в чат %s', REPORT_CHAT_ID)
        await send_report(bot, period, mid, chat_id=requester_id)
        return f'{type(e).__name__}: {e}'


# (период отчёта разбирается в parse_period)


def report_result_text(err, label='за 7 дн.'):
    if not err:
        return emoji('done', '✅') + f' <b>Отчёт {label} отправлен в рабочую группу.</b>'
    return (f'⚠️ <b>Не удалось отправить отчёт в рабочий чат</b> (<code>{REPORT_CHAT_ID}</code>).\n'
            f'Причина: <code>{escape(err)}</code>\n\nОтчёт прислан вам. Проверьте <code>/check</code>.')


def _report_due(key, hour, minute, weekday=None, monthday=None, window=REPORT_CATCHUP_HOURS):
    # пора ли слать отчёт: время наступило, сегодня ещё не слали, опоздание не больше окна
    n = datetime.now(TZ)
    if weekday is not None and n.weekday() != weekday:
        return False
    if monthday is not None and n.day != monthday:
        return False
    target = n.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if n < target or n - target > timedelta(hours=window):
        return False
    return meta_get(key) != n.strftime('%Y-%m-%d')


async def scheduled_reports(context):
    bot = context.bot

    async def daily():
        await send_report(bot, period_days(1))

    async def weekly():
        await send_report(bot, period_days(7))

    async def monthly():
        # 1-го числа: отчёт за прошлый месяц в группу + выплаты каждому админу в личку
        period = period_month(-1)
        await send_report(bot, period)
        for admin_id in ADMIN_IDS:
            try:
                await send_long(bot, admin_id, build_payouts(period))
            except TelegramError as e:
                log.warning('Выплаты не доставлены админу %s: %s', admin_id, e)

    jobs = (('last_daily_report', DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE, None, None, REPORT_CATCHUP_HOURS, daily),
            ('last_weekly_report', WEEKLY_REPORT_HOUR, WEEKLY_REPORT_MINUTE, WEEKLY_REPORT_DAY, None, REPORT_CATCHUP_HOURS, weekly),
            ('last_monthly_report', MONTHLY_REPORT_HOUR, 0, None, 1, 20, monthly))
    for key, hour, minute, weekday, monthday, window, fn in jobs:
        try:
            if _report_due(key, hour, minute, weekday, monthday, window):
                await fn()
                meta_set(key, datetime.now(TZ).strftime('%Y-%m-%d'))
        except Exception:
            log.exception('Не удалось отправить отчёт %s', key)


async def remind_stale(bot):
    # напоминания (каждое срабатывает один раз на заявку):
    # 1) менеджеру и в группу: новая заявка не взята; 2) админам: не взята слишком долго; 3) менеджеру: «в работе» без движения
    stamp = now()
    first, escalate, stuck = [], [], []
    new_sql = ("SELECT id,manager_id,COALESCE(manager,''),COALESCE(client_name,'') FROM applications "
               "WHERE status='new' AND {col} IS NULL AND created_at<=? AND created_at>=? ORDER BY id LIMIT 10")
    with db() as c:
        if STALE_REMINDER_MINUTES > 0:
            first = c.execute(new_sql.format(col='reminded_at'), (utc_ago(minutes=STALE_REMINDER_MINUTES), utc_ago(hours=24))).fetchall()
            c.executemany('UPDATE applications SET reminded_at=? WHERE id=?', [(stamp, r[0]) for r in first])
        if ESCALATE_MINUTES > 0:
            escalate = c.execute(new_sql.format(col='escalated_at'), (utc_ago(minutes=ESCALATE_MINUTES), utc_ago(hours=24))).fetchall()
            c.executemany('UPDATE applications SET escalated_at=? WHERE id=?', [(stamp, r[0]) for r in escalate])
        if STUCK_HOURS > 0:
            stuck = c.execute("SELECT id,manager_id,COALESCE(manager,''),COALESCE(client_name,'') FROM applications "
                              "WHERE status='in_work' AND stuck_reminded_at IS NULL AND taken_at<=? AND taken_at>=? ORDER BY id LIMIT 10",
                              (utc_ago(hours=STUCK_HOURS), utc_ago(days=30))).fetchall()
            c.executemany('UPDATE applications SET stuck_reminded_at=? WHERE id=?', [(stamp, r[0]) for r in stuck])
    for aid, mid, mname, cname in first:
        text = f'⏰ <b>Заявка #{aid}</b> ({escape(cname)}) ждёт взятия в работу уже более {STALE_REMINDER_MINUTES} мин.'
        await notify(bot, mid, text, actions(aid, 'new'))
        await notify(bot, GROUP_ID, f'{text}\nМенеджер: {escape(mname)}', actions(aid, 'new'))
    for aid, mid, mname, cname in escalate:
        for admin_id in ADMIN_IDS:
            await notify(bot, admin_id, f'🚨 <b>Заявка #{aid}</b> ({escape(cname)}) не взята в работу уже более {ESCALATE_MINUTES} мин.\n'
                                        f'Менеджер: {escape(mname)}', actions(aid, 'new'))
    hours = f'{STUCK_HOURS // 24} дн.' if STUCK_HOURS % 24 == 0 else f'{STUCK_HOURS} ч'
    for aid, mid, mname, cname in stuck:
        await notify(bot, mid, f'⌛ <b>Заявка #{aid}</b> ({escape(cname)}) в работе уже более {hours}.\n'
                               'Завершите её, оформите отказ или добавьте заметку.', actions(aid, 'in_work'))


async def scheduled_tick(context):
    await scheduled_reports(context)
    try:
        await remind_stale(context.bot)
    except Exception:
        log.exception('Ошибка напоминаний')


async def send_backup(bot, path, chat_ids):
    if os.path.getsize(path) > MAX_BACKUP_SEND_MB * 1024 * 1024:
        log.warning('Бэкап слишком большой для отправки в Telegram: %s', path)
        return 0
    sent = 0
    for cid in chat_ids:
        try:
            with open(path, 'rb') as f:
                await bot.send_document(cid, document=f, filename=os.path.basename(path),
                                        caption='🗄 Резервная копия БД Media Bank (в ней данные клиентов — храните аккуратно)')
            sent += 1
        except TelegramError as e:
            log.warning('Бэкап не доставлен %s: %s', cid, e)
    return sent


async def scheduled_backup(context):
    try:
        path = backup_database()
        if path and BACKUP_TO_ADMIN:
            await send_backup(context.bot, path, ADMIN_IDS)
    except Exception:
        log.exception('Ошибка бэкапа БД')


# ───────────────────────────── Заявка: форма ─────────────────────────────
REQUIRED = ('product', 'bank', 'client_name', 'contact', 'manager', 'manager_id')


async def start(update, context):
    u = update.effective_user
    if u.id in BLOCKED and not is_staff(u.id):
        return
    context.user_data.clear()
    with db() as c:
        c.execute('INSERT INTO users(user_id,username,first_seen) VALUES(?,?,?) '
                  'ON CONFLICT(user_id) DO UPDATE SET username=excluded.username', (u.id, u.username or '', now()))
    text = emoji('welcome', '⭐') + ' <b>Media Bank</b>\n\nВыберите интересующую вас услугу:'
    if update.callback_query:
        await edit(update.callback_query, text, home_kb(u.id))
    else:
        await safe_reply(update.message, text, reply_markup=home_kb(u.id))


async def text_input(update, context):
    step = context.user_data.get('step')
    value = ' '.join((update.message.text or '').split())
    if not step or not value:
        return
    uid = update.effective_user.id
    if uid in BLOCKED and not is_staff(uid):
        return
    if step in ('note', 'search') and not is_staff(uid):
        context.user_data.clear()
        return
    if step == 'rate' and not is_admin(uid):
        context.user_data.clear()
        return
    if step == 'note':
        aid = context.user_data.get('note_app')
        if len(value) > NOTE_MAX_LEN:
            return await safe_reply(update.message, f'⚠️ <b>Заметка слишком длинная</b> (максимум {NOTE_MAX_LEN} символов).')
        if not aid or not get_app(aid):
            context.user_data.clear()
            return await safe_reply(update.message, '⚠️ <b>Заявка не найдена.</b>')
        with db() as c:
            log_event(c, aid, 'note', uid, details=value)
        context.user_data.pop('step', None)
        context.user_data.pop('note_app', None)
        await safe_reply(update.message, f'{emoji("done", "✅")} <b>Заметка добавлена к заявке #{aid}.</b>',
                         reply_markup=InlineKeyboardMarkup([[btn('Открыть заявку', 'open', f'view:{aid}')]]))
        await refresh_group_message(context.bot, aid)
    elif step == 'rate':
        key = context.user_data.get('rate_key')
        amount = parse_money(value)
        if not key or amount is None:
            return await safe_reply(update.message, '⚠️ <b>Введите сумму числом</b>, например 1500 (0 — без выплаты).')
        with db() as c:
            c.execute('INSERT INTO rates(product,bank,amount) VALUES(?,?,?) '
                      'ON CONFLICT(product,bank) DO UPDATE SET amount=excluded.amount', (key[0], key[1], amount))
        context.user_data.pop('step', None)
        context.user_data.pop('rate_key', None)
        await safe_reply(update.message, f'{emoji("done", "✅")} Ставка <b>{escape(key[1])} · {SHORT_PRODUCTS.get(key[0], key[0])}</b>: {fmt_money(amount)}',
                         reply_markup=InlineKeyboardMarkup([[btn('К ставкам', 'stats', 'rates')]]))
    elif step == 'search':
        context.user_data.pop('step', None)
        await do_search(update.message, uid, value)
    elif step == 'name':
        if not 2 <= len(value) <= 60:
            return await safe_reply(update.message, '⚠️ <b>Имя должно быть от 2 до 60 символов.</b> Введите ещё раз:', reply_markup=form_kb())
        context.user_data.update(client_name=value, step='contact')
        await safe_reply(update.message, emoji('user', '👤') + ' <b>Введите Telegram клиента:</b>\nНапример: @username или +79991234567',
                         reply_markup=form_kb())
    elif step == 'contact':
        contact = normalize_contact(value)
        if not contact:
            return await safe_reply(update.message, '⚠️ <b>Не похоже на Telegram или телефон.</b>\nПример: @username или +79991234567',
                                    reply_markup=form_kb())
        context.user_data.update(contact=contact, step='manager')
        await safe_reply(update.message, emoji('manager', '🤝') + ' <b>Выберите менеджера:</b>', reply_markup=manager_kb())


async def do_search(message, uid, query):
    # поиск по номеру (до 9 цифр) или по части имени / Telegram / телефона клиента, без учёта регистра
    q = query.strip().lstrip('#')
    cols = "id,COALESCE(client_name,''),COALESCE(product,''),COALESCE(bank,''),COALESCE(status,'new')"
    with db() as c:
        if q.isdigit() and len(q) <= 9:
            rows = c.execute(f'SELECT {cols} FROM applications WHERE id=?', (int(q),)).fetchall()
        else:
            like = '%' + q.casefold().replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
            rows = c.execute(
                f"SELECT {cols} FROM applications WHERE LOWERU(client_name) LIKE ? ESCAPE '\\' "
                "OR LOWERU(contact) LIKE ? ESCAPE '\\' OR LOWERU(username) LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
                (like, like, like, SEARCH_LIMIT)).fetchall()
    if rows:
        body = '\n\n'.join(f'<b>#{r[0]}</b> · {escape(r[1])}\n{escape(PRODUCTS.get(r[2], r[2]))} · {escape(r[3])} · {status_label(r[4])}'
                            for r in rows)
        text = f'{emoji("search", "🔎")} <b>Найдено: {len(rows)}</b>\n\n{body}'
    else:
        text = f'{emoji("search", "🔎")} <b>Ничего не найдено.</b>'
    kb = [[btn(f'#{r[0]} · {r[1][:24]}', 'open', f'view:{r[0]}')] for r in rows]
    kb += [[btn('Новый поиск', 'search', 'search')], [btn('Назад', 'back', 'admin' if is_admin(uid) else 'manager_panel')]]
    await safe_reply(message, text, reply_markup=InlineKeyboardMarkup(kb))


async def submit(update, context):
    q = update.callback_query
    u = q.from_user
    d = context.user_data
    missing = [k for k in REQUIRED if not d.get(k)]
    if missing:
        log.error('SUBMIT VALIDATION: missing=%s keys=%s', missing, list(d))
        return await edit(q, '⚠️ <b>Данные заявки заполнены не полностью.</b>\n\nНачните заявку заново.', home_kb(u.id))

    with db() as c:
        recent = 0
        if not is_staff(u.id):
            recent = c.execute('SELECT COUNT(*) FROM applications WHERE user_id=? AND created_at>=?',
                               (u.id, utc_ago(hours=1))).fetchone()[0]
        dup = c.execute('SELECT id FROM applications WHERE user_id=? AND product=? AND bank=? AND contact=? AND created_at>=?',
                        (u.id, d['product'], d['bank'], d['contact'], utc_ago(hours=DUPLICATE_WINDOW_HOURS))).fetchone()
    if recent >= MAX_APPS_PER_HOUR:
        context.user_data.clear()
        return await edit(q, '⚠️ <b>Слишком много заявок за час.</b>\n\nПопробуйте позже.', home_kb(u.id))
    if dup:
        context.user_data.clear()
        return await edit(q, f'⚠️ <b>Такая заявка уже отправлена</b> (#{dup[0]}).\n\nМенеджер скоро свяжется с вами.', home_kb(u.id))

    with db() as c:
        active = c.execute("SELECT id,COALESCE(manager,'') FROM applications WHERE contact=? AND product=? AND bank=? "
                           "AND status IN ('new','in_work') ORDER BY id LIMIT 1", (d['contact'], d['product'], d['bank'])).fetchone()
    if active:
        context.user_data.clear()
        who = f' (#{active[0]}, менеджер {escape(active[1])})' if is_staff(u.id) else ''
        return await edit(q, f'⚠️ <b>По этому клиенту уже есть активная заявка{who}.</b>\n\nПовторно оформлять не нужно.', home_kb(u.id))

    cols = ['created_at', 'user_id', 'username', 'product', 'bank', 'client_name', 'contact', 'manager', 'manager_id', 'status']
    vals = [now(), u.id, u.username or '', d['product'], d['bank'], d['client_name'], d['contact'], d['manager'], d['manager_id'], 'new']
    if HAS_LEGACY_NAME:
        cols.append('name')
        vals.append(d['client_name'])
    with db() as c:
        aid = c.execute(f'INSERT INTO applications({",".join(cols)}) VALUES({",".join("?" * len(vals))})', vals).lastrowid
        log_event(c, aid, 'created', u.id, ('@' + u.username) if u.username else 'Клиент', f'{PRODUCTS[d["product"]]} · {d["bank"]}')

    manager_id = d['manager_id']
    context.user_data.clear()
    text, _ = render_app(aid)
    group_ok = True
    try:
        msg = await safe_send(context.bot, GROUP_ID, text, reply_markup=actions(aid, 'new'))
        with db() as c:
            c.execute('UPDATE applications SET group_message_id=? WHERE id=?', (msg.message_id, aid))
    except Exception:
        group_ok = False
        log.exception('GROUP SEND ERROR for application #%s', aid)
    if manager_id != u.id:
        await notify(context.bot, manager_id, text, actions(aid, 'new'))

    if group_ok:
        result = f'{emoji("done", "✅")} <b>Заявка отправлена!</b>\n\nНомер заявки: <b>#{aid}</b>\nМенеджер свяжется с вами.'
    else:
        result = (f'⚠️ <b>Заявка #{aid} сохранена</b>\n\nНе удалось отправить уведомление в рабочую группу. '
                  'Заявка не потеряна и доступна в панелях менеджера/администратора.')
    await edit(q, result, home_kb(u.id))


# ───────────────────────────── Работа с заявками ─────────────────────────────
async def refresh_group_message(bot, aid):
    # обновляет карточку заявки в рабочей группе после любого изменения
    text, r = render_app(aid)
    with db() as c:
        row = c.execute('SELECT group_message_id FROM applications WHERE id=?', (aid,)).fetchone()
    if not r or not row or not row[0]:
        return
    try:
        await _call(lambda t, **k: bot.edit_message_text(t, chat_id=GROUP_ID, message_id=row[0], **k),
                    text, reply_markup=actions(aid, r[7]))
    except BadRequest as e:
        if 'not modified' not in str(e).lower():
            log.warning('Не удалось обновить сообщение группы для #%s: %s', aid, e)


async def change_status(update, context, arg, *, new, expected, event, sets, toast, client_text, client_kb=None):
    q = update.callback_query
    u = q.from_user
    aid = to_int(arg)
    pre = None
    if aid is not None:
        with db() as c:
            pre = c.execute("SELECT manager_id,user_id,COALESCE(manager,'') FROM applications WHERE id=?", (aid,)).fetchone()
    if not pre:
        return await _answer(q, 'Заявка не найдена', True)
    mgr_id, client_id, mgr_name = pre
    if not can_manage(u.id, mgr_id):
        return await _answer(q, 'Заявка назначена не вам', True)
    marks = ','.join('?' * len(expected))
    assigns = ''.join(f',{col}=?' for col in sets)
    with db() as c:
        ok = c.execute(f'UPDATE applications SET status=?{assigns} WHERE id=? AND manager_id=? AND status IN ({marks})',
                       (new, *sets.values(), aid, mgr_id, *expected)).rowcount
        if ok:
            log_event(c, aid, event, u.id)
    if not ok:
        return await _answer(q, 'Статус заявки уже изменён', True)
    await _answer(q, toast)
    await show_app(q, aid, context)
    await refresh_group_message(context.bot, aid)
    if client_id and client_id != u.id:
        await notify(context.bot, client_id, client_text.format(aid=aid, manager=escape(mgr_name)),
                     client_kb(aid) if client_kb else None)


async def show_app(q, aid, context=None):
    text, r = render_app(aid)
    if not r:
        return await edit(q, '⚠️ <b>Заявка не найдена.</b>', manager_panel() if is_manager(q.from_user.id) else admin_kb())
    kb = actions(r[0], r[7])
    last = context.user_data.get('last_list') if context else None
    if last and q.message and q.message.chat.type == 'private':
        kb = InlineKeyboardMarkup(list(kb.inline_keyboard) + [[btn('К списку', 'back', last)]])
    await edit(q, text, kb)


async def show_apps(update, context, scope, arg):
    """Список заявок с фильтром по статусу и постраничной навигацией."""
    q = update.callback_query
    st, _, pg = arg.partition(':')
    st = st if st in STATUS_TITLES else 'all'
    page = int(pg) if pg.isdigit() else 0
    title, back = {'apps': ('Все заявки', 'admin'), 'mgrapps': ('Все заявки', 'manager_panel'), 'myapps': ('Мои заявки', 'manager_panel')}[scope]

    cond, args = [], []
    if st != 'all':
        cond.append('status=?')
        args.append(st)
    if scope == 'myapps':
        cond.append('manager_id=?')
        args.append(q.from_user.id)
    where = (' WHERE ' + ' AND '.join(cond)) if cond else ''
    with db() as c:
        total = c.execute(f'SELECT COUNT(*) FROM applications{where}', args).fetchone()[0]
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(page, 0), pages - 1)
        rows = c.execute(f"SELECT id,COALESCE(product,''),COALESCE(bank,''),COALESCE(client_name,''),COALESCE(manager,''),COALESCE(status,'new') "
                         f'FROM applications{where} '
                         'ORDER BY id DESC LIMIT ? OFFSET ?', args + [PAGE_SIZE, page * PAGE_SIZE]).fetchall()

    context.user_data['last_list'] = f'{scope}:{st}:{page}'
    kb = [[btn(f'#{r[0]} · {r[3][:24]}', 'open', f'view:{r[0]}')] for r in rows]
    nav = []
    if page > 0:
        nav.append(btn('◀', None, f'{scope}:{st}:{page - 1}'))
    if page < pages - 1:
        nav.append(btn('▶', None, f'{scope}:{st}:{page + 1}'))
    if nav:
        kb.append(nav)
    kb += [[btn('Все', 'apps_all', f'{scope}:all')],
           [btn('Новые', 'new', f'{scope}:new'), btn('В работе', 'work', f'{scope}:in_work')],
           [btn('Завершённые', 'done', f'{scope}:completed'), btn('Отказ', None, f'{scope}:rejected')],
           [btn('Назад', 'back', back)]]

    head = f'{emoji("apps_all", "📋")} <b>{title}</b>' + (f' · {STATUS_TITLES[st]}' if st != 'all' else '')
    if pages > 1:
        head += f'  <i>(стр. {page + 1}/{pages})</i>'
    body = '\n\n'.join(f'<b>#{r[0]}</b> · {escape(r[3])}\n{escape(PRODUCTS.get(r[1], r[1]))} · {escape(r[2])}\n{escape(r[4])} · {status_label(r[5])}'
                       for r in rows) if rows else 'Заявок пока нет.'
    await edit(q, f'{head}\n\n{body}', InlineKeyboardMarkup(kb))


async def export_csv(update, context, period=None):
    where, args = '', []
    if period:
        sql, args = _range('created_at', period)
        where = f' WHERE {sql}'
    rates = load_rates()
    with db() as c:
        rows = c.execute(f"SELECT {APP_COLS},COALESCE(taken_at,''),COALESCE(completed_at,''),COALESCE(rating,'') "
                         f'FROM applications{where} ORDER BY id DESC', args).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['ID', 'Created', 'Product', 'Bank', 'Client', 'Telegram', 'Manager', 'Status', 'Taken', 'Completed', 'Rating', 'Payout'])
    for r in rows:
        row = list(r)
        payout = rates.get((row[2], row[3]), 0) if row[7] == 'completed' else ''
        row[2] = PRODUCTS.get(row[2], row[2])
        w.writerow([csv_safe(v) for v in row] + [payout])
    f = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
    f.name = 'media_bank_applications.csv'
    await update.effective_message.reply_document(f, caption='Экспорт заявок Media Bank' + (f' {period[2]}' if period else ''))


# ───────────────────────────── Обработчики кнопок ─────────────────────────────
async def respond(update, context, text, kb):
    # в личке правим сообщение; в группе присылаем в личку, чтобы не ломать карточку заявки
    q = update.callback_query
    if not q.message or q.message.chat.type == 'private':
        await _answer(q)
        return await edit(q, text, kb)
    ok = await notify(context.bot, q.from_user.id, text, kb)
    await _answer(q, 'Отправил вам в личные сообщения' if ok else 'Сначала откройте бота и нажмите /start', not ok)


async def h_home(update, context, arg):
    await start(update, context)


async def h_form(update, context, arg):
    context.user_data.clear()
    await edit(update.callback_query, emoji('form', '📝') + ' <b>Выберите продукт:</b>', product_kb())


async def h_product(update, context, arg):
    q = update.callback_query
    if arg not in PRODUCTS:
        return await edit(q, '⚠️ <b>Неизвестный продукт.</b>', home_kb(q.from_user.id))
    context.user_data['product'] = arg
    text = emoji(arg, PRODUCT_FALLBACK[arg]) + f' <b>{escape(PRODUCTS[arg])}</b>\n\nВыберите банк:'
    if is_staff(q.from_user.id):
        terms = [f'• {escape(b)} — ЦД: {escape(OFFERS[(arg, b)][2])}, холд {OFFERS[(arg, b)][1]} дн.' for b in BANKS[arg] if (arg, b) in OFFERS]
        if terms:
            text += '\n\n<i>Условия офферов:</i>\n' + '\n'.join(terms)
    await edit(q, text, bank_kb(arg))


async def h_bank(update, context, arg):
    q = update.callback_query
    p, _, i = arg.partition(':')
    try:
        bank = BANKS[p][int(i)]
    except (KeyError, ValueError, IndexError):
        return await edit(q, '⚠️ <b>Банк не найден.</b>', home_kb(q.from_user.id))
    context.user_data.update(product=p, bank=bank, step='name')
    await edit(q, emoji('user', '👤') + ' <b>Введите имя клиента:</b>', form_kb())


async def h_edit(update, context, arg):
    context.user_data['step'] = 'name'
    await edit(update.callback_query, emoji('edit', '✏️') + ' <b>Введите имя клиента заново:</b>', form_kb())


async def h_manager_pick(update, context, arg):
    q = update.callback_query
    mid = to_int(arg)
    if mid not in MANAGERS:
        return await edit(q, '⚠️ <b>Менеджер не найден.</b>', home_kb(q.from_user.id))
    x = context.user_data
    if any(k not in x for k in ('product', 'bank', 'client_name', 'contact')):
        x.clear()
        return await edit(q, '⚠️ <b>Сессия заявки устарела. Начните заявку заново.</b>', home_kb(q.from_user.id))
    x.update(manager_id=mid, manager=MANAGERS[mid], step='review')
    text = (f'{emoji("form", "📝")} <b>Проверьте заявку</b>\n\n'
            f'{emoji(x["product"], PRODUCT_FALLBACK[x["product"]])} <b>Продукт:</b> {escape(PRODUCTS[x["product"]])}\n'
            f'{emoji("bank", "🏦")} <b>Банк:</b> {escape(x["bank"])}\n'
            f'{emoji("user", "👤")} <b>Имя:</b> {escape(x["client_name"])}\n'
            f'{emoji("user", "👤")} <b>Telegram:</b> {escape(x["contact"])}\n'
            f'{emoji("manager", "🤝")} <b>Менеджер:</b> {escape(x["manager"])}')
    await edit(q, text, review_kb())


async def h_submit(update, context, arg):
    await submit(update, context)


async def h_admin(update, context, arg):
    await edit(update.callback_query, emoji('admin', '⚙️') + ' <b>Админ-панель</b>', admin_kb())


async def h_manager_panel(update, context, arg):
    await edit(update.callback_query, emoji('manager', '🤝') + ' <b>Панель менеджера</b>', manager_panel())


def _apps_handler(scope):
    async def handler(update, context, arg):
        await show_apps(update, context, scope, arg)
    return handler


async def h_stats(update, context, arg):
    await edit(update.callback_query, stats_text(), admin_kb())


async def h_mystats(update, context, arg):
    q = update.callback_query
    await edit(q, stats_text(q.from_user.id), manager_panel())


async def h_report(update, context, arg):
    q = update.callback_query
    period = parse_period(arg)
    err = await deliver_report(context.bot, q.from_user.id, period)
    await edit(q, report_result_text(err, period[2]), admin_kb())


async def h_myreport(update, context, arg):
    q = update.callback_query
    period = parse_period(arg)
    err = await deliver_report(context.bot, q.from_user.id, period, q.from_user.id)
    await edit(q, report_result_text(err, period[2]), manager_panel())


async def h_myprofile(update, context, arg):
    q = update.callback_query
    await edit(q, manager_profile(q.from_user.id), InlineKeyboardMarkup([[btn('Назад', 'back', 'manager_panel')]]))


async def h_export(update, context, arg):
    await export_csv(update, context, parse_period(arg) if arg else None)


async def h_managers(update, context, arg):
    kb = [[btn(n, 'manager_select', f'mstats:{m}')] for m, n in MANAGERS.items()] + [[btn('Назад', 'back', 'admin')]]
    text = (emoji('manager_admin', '🤝') + ' <b>Менеджеры</b>\n\nДобавить: <code>/addmanager ID Имя</code>\n'
            'Удалить: <code>/delmanager ID</code>\n<i>Новый менеджер должен один раз нажать /start у бота.</i>')
    await edit(update.callback_query, text, InlineKeyboardMarkup(kb))


async def h_mstats(update, context, arg):
    q = update.callback_query
    mid = to_int(arg)
    if mid not in MANAGERS:
        return await edit(q, '⚠️ <b>Менеджер не найден.</b>', admin_kb())
    await edit(q, stats_text(mid), InlineKeyboardMarkup([[btn('Назад', 'back', 'managers')]]))


async def h_view(update, context, arg):
    q = update.callback_query
    aid = to_int(arg)
    if aid is None:
        return await _answer(q, 'Некорректный номер заявки', True)
    await _answer(q)
    await show_app(q, aid, context)


async def h_take(update, context, arg):
    await change_status(update, context, arg, new='in_work', expected=('new',), event='taken', sets={'taken_at': now()},
                        toast='Заявка взята в работу',
                        client_text='🔄 <b>Заявка #{aid} принята в работу.</b>\nМенеджер {manager} свяжется с вами.')


async def h_complete(update, context, arg):
    await change_status(update, context, arg, new='completed', expected=('in_work',), event='completed',
                        sets={'completed_at': now()}, toast='Заявка завершена', client_kb=rate_kb,
                        client_text='✅ <b>Заявка #{aid} завершена.</b>\nСпасибо, что выбрали Media Bank!\n\nОцените работу менеджера:')


async def h_reject(update, context, arg):
    await change_status(update, context, arg, new='rejected', expected=('new', 'in_work'), event='rejected',
                        sets={'completed_at': now()}, toast='Заявка отмечена как отказ',
                        client_text='❌ <b>По заявке #{aid} получен отказ.</b>\nМенеджер {manager} свяжется с вами и предложит варианты.')


async def h_reopen(update, context, arg):
    await change_status(update, context, arg, new='in_work', expected=('completed', 'rejected'), event='reopened',
                        sets={'completed_at': None, 'stuck_reminded_at': None}, toast='Заявка возвращена в работу',
                        client_text='🔄 <b>Заявка #{aid} снова в работе.</b>\nМенеджер {manager} на связи.')


async def h_client_apps(update, context, arg):
    q = update.callback_query
    with db() as c:
        rows = c.execute("SELECT id,COALESCE(product,''),COALESCE(bank,''),COALESCE(status,'new'),COALESCE(manager,'') "
                         'FROM applications WHERE user_id=? ORDER BY id DESC LIMIT 10', (q.from_user.id,)).fetchall()
    body = '\n\n'.join(f'<b>#{r[0]}</b> · {escape(PRODUCTS.get(r[1], r[1]))} · {escape(r[2])}\n{status_label(r[3])} · {escape(r[4])}'
                        for r in rows) if rows else 'У вас пока нет заявок.'
    await edit(q, f'{emoji("apps_all", "📋")} <b>Мои заявки</b>\n\n{body}',
               InlineKeyboardMarkup([[btn('Оставить заявку', 'form', 'form')], [btn('Главное меню', 'back', 'home')]]))


async def h_search(update, context, arg):
    context.user_data['step'] = 'search'
    back = 'admin' if is_admin(update.callback_query.from_user.id) else 'manager_panel'
    await respond(update, context, f'{emoji("search", "🔎")} <b>Поиск заявки</b>\n\nВведите номер заявки или часть имени / Telegram / телефона клиента:',
                  InlineKeyboardMarkup([[btn('Назад', 'back', back)]]))


async def h_note(update, context, arg):
    q = update.callback_query
    aid = to_int(arg)
    if aid is None or not get_app(aid):
        return await _answer(q, 'Заявка не найдена', True)
    context.user_data.update(step='note', note_app=aid)
    await respond(update, context, f'{emoji("edit", "📝")} <b>Заметка к заявке #{aid}</b>\n\nНапишите текст заметки (до {NOTE_MAX_LEN} символов):',
                  InlineKeyboardMarkup([[btn('Назад', 'back', f'view:{aid}')]]))


async def h_history(update, context, arg):
    q = update.callback_query
    aid = to_int(arg)
    if aid is None or not get_app(aid):
        return await _answer(q, 'Заявка не найдена', True)
    with db() as c:
        rows = c.execute('SELECT ts,actor,event,details FROM app_events WHERE app_id=? ORDER BY id DESC LIMIT ?',
                         (aid, HISTORY_LIMIT)).fetchall()
    lines = []
    for ts, actor, event, details in reversed(rows):
        line = f'<i>{escape(fmt_dt(ts))}</i> · {EVENT_TITLES.get(event, escape(event))} · {escape(actor or "")}'
        if details:
            line += f'\n   {escape(details[:150])}'
        lines.append(line)
    text = f'{emoji("open", "📂")} <b>История заявки #{aid}</b>\n\n' + ('\n'.join(lines) if lines else 'Событий пока нет.')
    await respond(update, context, text, InlineKeyboardMarkup([[btn('Назад', 'back', f'view:{aid}')]]))


async def h_reassign(update, context, arg):
    q = update.callback_query
    aid = to_int(arg)
    with db() as c:
        pre = c.execute('SELECT manager_id,status FROM applications WHERE id=?', (aid,)).fetchone() if aid is not None else None
    if not pre:
        return await _answer(q, 'Заявка не найдена', True)
    if not can_manage(q.from_user.id, pre[0]):
        return await _answer(q, 'Заявка назначена не вам', True)
    if pre[1] not in ('new', 'in_work'):
        return await _answer(q, 'Передать можно только активную заявку', True)
    await respond(update, context, f'{emoji("manager", "🤝")} <b>Кому передать заявку #{aid}?</b>', reassign_kb(aid, pre[0]))


async def h_reassign_to(update, context, arg):
    q = update.callback_query
    u = q.from_user
    a, _, m = arg.partition(':')
    aid, new_mid = to_int(a), to_int(m)
    if new_mid not in MANAGERS:
        return await _answer(q, 'Менеджер не найден', True)
    with db() as c:
        pre = c.execute("SELECT manager_id,COALESCE(manager,'') FROM applications WHERE id=?", (aid,)).fetchone() if aid is not None else None
    if not pre:
        return await _answer(q, 'Заявка не найдена', True)
    old_mid, old_name = pre
    if not can_manage(u.id, old_mid):
        return await _answer(q, 'Заявка назначена не вам', True)
    if old_mid == new_mid:
        return await _answer(q, 'Заявка уже у этого менеджера', True)
    with db() as c:
        ok = c.execute("UPDATE applications SET manager=?,manager_id=?,status='new',taken_at=NULL,reminded_at=NULL,escalated_at=NULL,stuck_reminded_at=NULL "
                       "WHERE id=? AND manager_id=? AND status IN ('new','in_work')",
                       (MANAGERS[new_mid], new_mid, aid, old_mid)).rowcount
        if ok:
            log_event(c, aid, 'reassigned', u.id, details=f'{old_name} → {MANAGERS[new_mid]}')
    if not ok:
        return await _answer(q, 'Заявку уже нельзя передать', True)
    await _answer(q, 'Заявка передана')
    await show_app(q, aid, context)
    await refresh_group_message(context.bot, aid)
    text, _r = render_app(aid)
    if new_mid != u.id:
        await notify(context.bot, new_mid, '🔁 <b>Вам передана заявка</b>\n\n' + text, actions(aid, 'new'))
    if old_mid not in (u.id, new_mid):
        await notify(context.bot, old_mid, f'🔁 Заявка #{aid} передана менеджеру {escape(MANAGERS[new_mid])}.')


async def h_payouts(update, context, arg):
    # выплаты уходят только запросившему в личку, не в рабочую группу
    await send_long(context.bot, update.callback_query.from_user.id, build_payouts(parse_period(arg)))


async def h_mypayout(update, context, arg):
    uid = update.callback_query.from_user.id
    await send_long(context.bot, uid, build_payouts(parse_period(arg), uid))


async def h_rates(update, context, arg):
    rates = load_rates()
    kb = []
    for p, banks in BANKS.items():
        for i, b in enumerate(banks):
            amount = rates.get((p, b), 0)
            kb.append([btn(f'{b} · {SHORT_PRODUCTS[p]} — {fmt_money(amount) if amount else "не задана"}', None, f'setrate:{p}:{i}')])
    kb.append([btn('Назад', 'back', 'admin')])
    await edit(update.callback_query, emoji('stats', '💰') + ' <b>Ставки выплат</b>\n\nСумма менеджеру за один оформленный продукт. '
               'Нажмите строку, чтобы изменить:', InlineKeyboardMarkup(kb))


async def h_setrate(update, context, arg):
    q = update.callback_query
    p, _, i = arg.partition(':')
    try:
        bank = BANKS[p][int(i)]
    except (KeyError, ValueError, IndexError):
        return await edit(q, '⚠️ <b>Банк не найден.</b>', admin_kb())
    context.user_data.update(step='rate', rate_key=(p, bank))
    await edit(q, f'{emoji("stats", "💰")} <b>{escape(bank)} · {escape(PRODUCTS[p])}</b>\n\nВведите ставку в рублях за один оформленный продукт '
                  '(число, например 1500; 0 — без выплаты):', InlineKeyboardMarkup([[btn('Назад', 'back', 'rates')]]))


async def h_review(update, context, arg):
    # оценка клиента 1-5 после завершения заявки; одна оценка на заявку
    q = update.callback_query
    a, _, n = arg.partition(':')
    aid, score = to_int(a), to_int(n)
    if aid is None or score not in (1, 2, 3, 4, 5):
        return
    with db() as c:
        ok = c.execute("UPDATE applications SET rating=? WHERE id=? AND user_id=? AND status='completed' AND rating IS NULL",
                       (score, aid, q.from_user.id)).rowcount
        if ok:
            log_event(c, aid, 'rated', q.from_user.id, 'Клиент', f'{score}/5')
        mgr = c.execute("SELECT COALESCE(manager,'') FROM applications WHERE id=?", (aid,)).fetchone()
    if not ok:
        return await edit(q, 'Спасибо! Оценка уже учтена.')
    await edit(q, f'Спасибо за оценку {"⭐" * score}! Мы стараемся стать лучше.')
    await refresh_group_message(context.bot, aid)
    if score <= 3:
        for admin_id in ADMIN_IDS:
            await notify(context.bot, admin_id, f'⚠️ <b>Низкая оценка {score}/5</b> по заявке #{aid} (менеджер {escape(mgr[0] if mgr else "")}).',
                         actions(aid, 'completed'))


HANDLERS = {
    'home': h_home, 'form': h_form, 'product': h_product, 'bank': h_bank, 'edit': h_edit,
    'cancel': h_home, 'mgr': h_manager_pick, 'submit': h_submit,
    'admin': h_admin, 'manager_panel': h_manager_panel,
    'apps': _apps_handler('apps'), 'mgrapps': _apps_handler('mgrapps'), 'myapps': _apps_handler('myapps'),
    'stats': h_stats, 'mystats': h_mystats, 'report': h_report, 'myreport': h_myreport,
    'myprofile': h_myprofile, 'export': h_export, 'managers': h_managers, 'mstats': h_mstats,
    'view': h_view, 'take': h_take, 'complete': h_complete, 'reject': h_reject, 'reopen': h_reopen,
    'client_apps': h_client_apps, 'search': h_search, 'note': h_note, 'history': h_history,
    'reassign': h_reassign, 'reassign_to': h_reassign_to,
    'payouts': h_payouts, 'mypayout': h_mypayout, 'rates': h_rates, 'setrate': h_setrate, 'rv': h_review,
}
ACCESS = {
    **dict.fromkeys(('admin', 'apps', 'stats', 'report', 'export', 'managers', 'mstats', 'payouts', 'rates', 'setrate'), 'admin'),
    **dict.fromkeys(('manager_panel', 'mgrapps', 'myapps', 'mystats', 'myreport', 'myprofile', 'mypayout'), 'manager'),
    **dict.fromkeys(('view', 'take', 'complete', 'reject', 'reopen', 'reassign', 'reassign_to', 'note', 'history', 'search'), 'staff'),
}
ROLE_CHECK = {'admin': is_admin, 'manager': is_manager, 'staff': is_staff}
# эти действия сами отвечают на callback (с alert при ошибке)
SELF_ANSWER = {'take', 'complete', 'reject', 'reopen', 'view', 'reassign', 'reassign_to', 'note', 'history', 'search'}


async def callback(update, context):
    q = update.callback_query
    u = q.from_user
    data = q.data or ''
    action, _, arg = data.partition(':')
    handler = HANDLERS.get(action)
    if handler is None:
        return await _answer(q)
    if u.id in BLOCKED and not is_staff(u.id):
        return await _answer(q, 'Доступ ограничен', True)
    role = ACCESS.get(action)
    if role and not ROLE_CHECK[role](u.id):
        return await _answer(q, 'Нет доступа', True)
    if context.user_data.get('step') in ('note', 'search', 'rate') and action not in ('note', 'search', 'setrate'):
        context.user_data.pop('step', None)
        context.user_data.pop('note_app', None)
        context.user_data.pop('rate_key', None)
    try:
        if action not in SELF_ANSWER:
            await _answer(q)
        await handler(update, context, arg)
    except Exception as err:
        log.exception('CALLBACK ERROR data=%r user=%s', data, u.id)
        await _answer(q, 'Произошла ошибка. Попробуйте ещё раз.', True)
        if q.message and q.message.chat.type == 'private':
            try:
                detail = f'\n\n<code>{escape(type(err).__name__ + ": " + str(err))[:300]}</code>' if is_admin(u.id) else ''
                await safe_reply(q.message, '⚠️ <b>Произошла ошибка.</b>\n\nПопробуйте ещё раз или вернитесь в главное меню.' + detail,
                                 reply_markup=home_kb(u.id))
            except Exception:
                log.exception('Не удалось отправить сообщение об ошибке')


# ───────────────────────────── Команды ─────────────────────────────
async def stats_cmd(update, context):
    u = update.effective_user
    if is_admin(u.id):
        await safe_reply(update.message, stats_text(), reply_markup=admin_kb())
    elif is_manager(u.id):
        await safe_reply(update.message, stats_text(u.id), reply_markup=manager_panel())


async def find_cmd(update, context):
    u = update.effective_user
    if not is_staff(u.id) or update.effective_chat.type != 'private':
        return
    query = ' '.join(context.args or [])
    if not query:
        return await safe_reply(update.message, 'Использование: <code>/find номер или часть имени/контакта</code>')
    await do_search(update.message, u.id, query)


async def myid_cmd(update, context):
    await safe_reply(update.message, f'Ваш Telegram ID: <code>{update.effective_user.id}</code>')


async def report_cmd(update, context):
    u = update.effective_user
    if not is_admin(u.id):
        return
    try:
        period = parse_period(context.args[0] if context.args else None)
        err = await deliver_report(context.bot, u.id, period)
        await safe_reply(update.message, report_result_text(err, period[2]))
    except Exception as e:
        log.exception('Ошибка /report')
        await safe_reply(update.message, f'⚠️ <b>Ошибка отчёта:</b> <code>{escape(type(e).__name__ + ": " + str(e))}</code>')


async def payouts_cmd(update, context):
    u = update.effective_user
    if is_admin(u.id):
        period = parse_period(context.args[0] if context.args else 'm0')
        await send_long(context.bot, u.id, build_payouts(period))
    elif is_manager(u.id):
        period = parse_period(context.args[0] if context.args else 'm0')
        await send_long(context.bot, u.id, build_payouts(period, u.id))


async def export_cmd(update, context):
    if is_admin(update.effective_user.id):
        await export_csv(update, context, parse_period(context.args[0]) if context.args else None)


async def backup_cmd(update, context):
    u = update.effective_user
    if not is_admin(u.id):
        return
    path = backup_database()
    sent = await send_backup(context.bot, path, [u.id]) if path else 0
    await safe_reply(update.message, '🗄 Бэкап создан и отправлен.' if sent else '⚠️ Бэкап создан на сервере, но отправить файл не удалось.')


async def addmanager_cmd(update, context):
    u = update.effective_user
    if not is_admin(u.id):
        return
    uid = to_int(context.args[0]) if context.args else None
    name = ' '.join(context.args[1:]).strip()[:40]
    if not uid or uid < 0 or not name:
        return await safe_reply(update.message, 'Использование: <code>/addmanager ID Имя</code>\nНапример: <code>/addmanager 123456789 Иван</code>\n'
                                                'ID человек узнаёт командой /myid.')
    with db() as c:
        c.execute('INSERT INTO staff_managers(user_id,name,active) VALUES(?,?,1) '
                  'ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,active=1', (uid, name))
    MANAGERS[uid] = name
    try:
        await context.bot.set_my_commands(STAFF_COMMANDS if uid not in ADMIN_IDS else ADMIN_COMMANDS, scope=BotCommandScopeChat(uid))
    except TelegramError:
        pass
    await safe_reply(update.message, f'✅ Менеджер <b>{escape(name)}</b> добавлен. Пусть он нажмёт /start у бота, чтобы получать уведомления.\n'
                                     'Не забудьте задать ставки в админ-панели.')


async def delmanager_cmd(update, context):
    u = update.effective_user
    if not is_admin(u.id):
        return
    uid = to_int(context.args[0]) if context.args else None
    if uid not in MANAGERS:
        return await safe_reply(update.message, 'Использование: <code>/delmanager ID</code> (ID из списка менеджеров)')
    if len(MANAGERS) <= 1:
        return await safe_reply(update.message, '⚠️ Нельзя удалить последнего менеджера.')
    with db() as c:
        active = c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status IN ('new','in_work')", (uid,)).fetchone()[0]
        c.execute('INSERT INTO staff_managers(user_id,name,active) VALUES(?,?,0) ON CONFLICT(user_id) DO UPDATE SET active=0', (uid, MANAGERS[uid]))
    name = MANAGERS.pop(uid)
    extra = f'\n⚠️ У него осталось активных заявок: <b>{active}</b> — передайте их другому менеджеру (кнопка «Передать»).' if active else ''
    await safe_reply(update.message, f'Менеджер <b>{escape(name)}</b> удалён. История и выплаты по его заявкам сохранены.{extra}')


async def ban_cmd(update, context):
    u = update.effective_user
    if not is_admin(u.id):
        return
    uid = to_int(context.args[0]) if context.args else None
    if not uid or uid < 0:
        return await safe_reply(update.message, 'Использование: <code>/ban ID причина</code> (ID клиента виден в карточке заявки)')
    if is_staff(uid):
        return await safe_reply(update.message, '⚠️ Нельзя заблокировать сотрудника.')
    with db() as c:
        c.execute('INSERT INTO blocked(user_id,reason,ts) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET reason=excluded.reason',
                  (uid, ' '.join(context.args[1:])[:100], now()))
    BLOCKED.add(uid)
    await safe_reply(update.message, f'⛔ Пользователь <code>{uid}</code> заблокирован: бот ему больше не отвечает.')


async def unban_cmd(update, context):
    u = update.effective_user
    if not is_admin(u.id):
        return
    uid = to_int(context.args[0]) if context.args else None
    if not uid:
        return await safe_reply(update.message, 'Использование: <code>/unban ID</code>')
    with db() as c:
        c.execute('DELETE FROM blocked WHERE user_id=?', (uid,))
    BLOCKED.discard(uid)
    await safe_reply(update.message, f'✅ Пользователь <code>{uid}</code> разблокирован.')


async def help_cmd(update, context):
    u = update.effective_user
    text = ('ℹ️ <b>Помощь</b>\n\n/start — главное меню\n'
            '«Оставить заявку» — выберите продукт и банк, укажите имя и Telegram клиента, затем менеджера.\n'
            '«Статус моих заявок» — ход ваших заявок.')
    if is_staff(u.id):
        text += ('\n\n<b>Сотрудникам</b>\n/find текст — поиск заявки по номеру, имени или контакту\n/stats — статистика\n'
                 '/payouts [month|prev|N] — ваши выплаты за период')
    if is_admin(u.id):
        text += ('\n\n<b>Админу</b>\n/report [N|month|prev] — отчёт в рабочую группу\n/payouts [month|prev|N] — выплаты всем менеджерам\n'
                 '/export [N|month|prev] — CSV\n/backup — бэкап БД\n/check — диагностика\n'
                 '/addmanager ID Имя, /delmanager ID — менеджеры\n/ban ID, /unban ID — блокировка клиента')
    await safe_reply(update.message, text)


async def check_cmd(update, context):
    # диагностика для админа: БД, часовой пояс, планировщик, доступ бота к чатам
    u = update.effective_user
    if not is_admin(u.id):
        return
    lines = [f'<b>{BOT_VERSION}</b>',
             f'БД: <code>{escape(DB_FILE)}</code> ({"есть" if os.path.exists(DB_FILE) else "нет"})',
             f'Часовой пояс отчётов: {escape(str(TZ))}',
             f'Планировщик: {"работает" if context.job_queue else "❌ не установлен (pip install python-telegram-bot[job-queue])"}',
             f'Последний суточный отчёт: {escape(meta_get("last_daily_report") or "—")}, недельный: {escape(meta_get("last_weekly_report") or "—")}']
    for title, cid in (('GROUP_ID', GROUP_ID), ('REPORT_CHAT_ID', REPORT_CHAT_ID)):
        try:
            chat = await context.bot.get_chat(cid)
            me = await context.bot.get_chat_member(cid, context.bot.id)
            lines.append(f'{title} <code>{cid}</code>: «{escape(chat.title or "")}», статус бота: {escape(str(me.status))}')
        except TelegramError as e:
            lines.append(f'{title} <code>{cid}</code>: ❌ {escape(str(e))}')
    await safe_reply(update.message, '\n'.join(lines))


async def on_error(update, context):
    log.error('Необработанная ошибка', exc_info=context.error)


CLIENT_COMMANDS = [BotCommand('start', '🏠 Главное меню'), BotCommand('help', 'ℹ️ Помощь')]
STAFF_COMMANDS = CLIENT_COMMANDS + [BotCommand('find', '🔎 Поиск заявки'), BotCommand('stats', '📊 Статистика'),
                                    BotCommand('payouts', '💰 Мои выплаты')]
ADMIN_COMMANDS = STAFF_COMMANDS + [BotCommand('report', '📈 Отчёт'), BotCommand('export', '📄 Экспорт CSV'),
                                   BotCommand('backup', '🗄 Бэкап БД'), BotCommand('check', '🩺 Диагностика'),
                                   BotCommand('addmanager', '➕ Добавить менеджера'), BotCommand('delmanager', '➖ Удалить менеджера'),
                                   BotCommand('ban', '⛔ Заблокировать клиента'), BotCommand('unban', '✅ Разблокировать')]


async def setup(app):
    await app.bot.set_my_commands(CLIENT_COMMANDS)
    for uid in ADMIN_IDS | set(MANAGERS):
        try:
            await app.bot.set_my_commands(ADMIN_COMMANDS if uid in ADMIN_IDS else STAFF_COMMANDS, scope=BotCommandScopeChat(uid))
        except TelegramError as e:
            log.debug('Личное меню команд для %s не задано: %s', uid, e)
    await app.bot.set_chat_menu_button(menu_button=MenuButtonCommands())


def main():
    if not BOT_TOKEN:
        raise RuntimeError('BOT_TOKEN is missing in .env')
    if not GROUP_ID:
        raise RuntimeError('GROUP_ID is missing in .env')
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(setup).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('stats', stats_cmd))
    app.add_handler(CommandHandler('report', report_cmd))
    app.add_handler(CommandHandler('myid', myid_cmd))
    app.add_handler(CommandHandler('find', find_cmd))
    app.add_handler(CommandHandler('check', check_cmd))
    for name, fn in (('help', help_cmd), ('payouts', payouts_cmd), ('export', export_cmd), ('backup', backup_cmd),
                     ('addmanager', addmanager_cmd), ('delmanager', delmanager_cmd), ('ban', ban_cmd), ('unban', unban_cmd)):
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, text_input))
    app.add_error_handler(on_error)
    if app.job_queue:
        app.job_queue.run_repeating(scheduled_tick, interval=60, first=10)
        app.job_queue.run_daily(scheduled_backup, time=dtime(hour=3, minute=15, tzinfo=timezone.utc), name='database_backup')
    else:
        log.warning('JobQueue недоступен: pip install "python-telegram-bot[job-queue]" — отчёты и бэкапы отключены')
    log.info('%s | DB: %s | TZ: %s | кнопки premium: %s | админов: %d, менеджеров: %d',
             BOT_VERSION, DB_FILE, TZ, ENABLE_BUTTON_PREMIUM, len(ADMIN_IDS), len(MANAGERS))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
