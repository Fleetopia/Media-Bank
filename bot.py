"""Media Bank — Telegram-бот приёма заявок (v6.4).

Переменные окружения (.env), все необязательные, кроме BOT_TOKEN и GROUP_ID:
    BOT_TOKEN, GROUP_ID, REPORT_CHAT_ID, DB_FILE, ENABLE_BUTTON_PREMIUM
    ADMIN_IDS            — id админов через запятую
    MANAGERS             — менеджеры в формате "id:Имя,id:Имя"
    REPORT_TZ            — часовой пояс отчётов (по умолчанию Europe/Moscow)
    STALE_REMINDER_MINUTES — через сколько минут напомнить о не взятой заявке (0 = выключено, по умолчанию 30)
    DAILY_REPORT_HOUR / DAILY_REPORT_MINUTE
    WEEKLY_REPORT_DAY (0 = понедельник) / WEEKLY_REPORT_HOUR / WEEKLY_REPORT_MINUTE
"""
import csv
import io
import logging
import os
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, time as dtime, timedelta, timezone
from html import escape

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, MenuButtonCommands, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

# ───────────────────────────── Конфигурация ─────────────────────────────
load_dotenv()
logging.basicConfig(format='%(asctime)s | %(levelname)s | %(message)s', level=logging.INFO)
log = logging.getLogger('media_bank')

BOT_VERSION = 'MEDIA-BANK-6.4'
BOT_TOKEN = os.getenv('BOT_TOKEN', '').strip()
GROUP_ID = int(os.getenv('GROUP_ID', '0'))
REPORT_CHAT_ID = int(os.getenv('REPORT_CHAT_ID', str(GROUP_ID)))
DB_FILE = os.getenv('DB_FILE', '/data/media_bank.db' if os.path.isdir('/data') else 'media_bank.db')
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
                'reopened': 'Возвращена в работу', 'reassigned': 'Передана', 'note': 'Заметка'}

PAGE_SIZE = 8
MAX_APPS_PER_HOUR = 5       # лимит заявок от одного клиента (персонал не ограничен)
DUPLICATE_WINDOW_HOURS = 24
REPORT_CATCHUP_HOURS = 6    # если бот был выключен, отчёт уйдёт с опозданием не больше этого срока
BACKUPS_KEEP = 14
STALE_REMINDER_MINUTES = int(os.getenv('STALE_REMINDER_MINUTES', '30'))
NOTE_MAX_LEN = 500
HISTORY_LIMIT = 15
SEARCH_LIMIT = 10

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
        return '<1 мин'
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
    s = str(e)
    return 'Entity_text_invalid' in s or "can't parse entities" in s


async def _call(fn, text, **kw):
    """Вызов с HTML; если Telegram не принял premium-эмодзи, повтор с обычными."""
    kw.setdefault('parse_mode', ParseMode.HTML)
    try:
        return await fn(text, **kw)
    except BadRequest as e:
        if _entity_error(e):
            return await fn(strip_custom_emoji(text), **kw)
        raise


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
        [btn('Статистика', 'stats', 'stats'), btn('Отчёт', 'report', 'report')],
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
        [btn('Мой отчёт', 'report', 'myreport')],
        [btn('Мой профиль', 'manager_admin', 'myprofile')],
        [btn('Главное меню', 'back', 'home')]])


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
    # текст карточки заявки (с последними заметками) и сама строка заявки
    r = get_app(aid)
    return (app_text(r) + notes_block(aid), r) if r else (None, None)


def stats_text(mid=None):
    n = counts(mid)
    title = emoji('stats', '📊') + ' <b>Статистика</b>' + (f' · {escape(MANAGERS.get(mid, str(mid)))}' if mid else '')
    return (f'{title}\n\n{emoji("apps_all", "📋")} Всего: <b>{n["total"]}</b>\n'
            f'{emoji("new", "🆕")} Новых: <b>{n["new"]}</b>\n'
            f'{emoji("work", "🔄")} В работе: <b>{n["work"]}</b>\n'
            f'{emoji("done", "✅")} Завершено: <b>{n["done"]}</b>\n'
            f'❌ Отказов: <b>{n["rej"]}</b>\n'
            f'Сегодня: <b>{n["today"]}</b>')


def manager_profile(uid):
    n = counts(uid)
    return (f'{emoji("manager", "👤")} <b>Профиль менеджера</b>\n\n'
            f'<b>Имя:</b> {escape(MANAGERS.get(uid, "Менеджер"))}\n'
            f'<b>Telegram ID:</b> <code>{uid}</code>\n\n'
            f'{emoji("apps_all", "📋")} <b>Всего заявок:</b> {n["total"]}\n'
            f'{emoji("new", "🆕")} <b>Новые:</b> {n["new"]}\n'
            f'{emoji("work", "🔄")} <b>В работе:</b> {n["work"]}\n'
            f'{emoji("done", "✅")} <b>Завершено:</b> {n["done"]}\n'
            f'❌ <b>Отказов:</b> {n["rej"]}')


# ───────────────────────────── Отчёты ─────────────────────────────
async def send_report(bot, days, mid=None):
    extra = ' AND manager_id=?' if mid else ''
    args = [utc_ago(days=days)] + ([mid] if mid else [])
    with db() as c:
        rows = c.execute(
            "SELECT manager,COUNT(*),SUM(status='completed'),SUM(status='rejected') FROM applications "
            f'WHERE created_at>=?{extra} GROUP BY manager ORDER BY manager', args).fetchall()
        avg = c.execute(
            'SELECT AVG((julianday(substr(taken_at,1,19))-julianday(substr(created_at,1,19)))*1440) FROM applications '
            f'WHERE taken_at IS NOT NULL AND created_at>=?{extra}', args).fetchone()[0]
        waiting = c.execute("SELECT COUNT(*) FROM applications WHERE status='new'" + (' AND manager_id=?' if mid else ''),
                            (mid,) if mid else ()).fetchone()[0]
    period = 'за сутки' if days == 1 else f'за {days} дн.'
    if rows:
        body = ''.join(f'• {escape(m)}: {n} заявок, {done or 0} завершено' + (f', {rej} отказ' if rej else '') + '\n'
                       for m, n, done, rej in rows)
    else:
        body = 'Заявок за период нет.\n'
    total = f'\n<b>Итого:</b> {sum(r[1] for r in rows)} заявок' if len(rows) > 1 else ''
    speed = f'\n⏱ Среднее время до взятия в работу: <b>{fmt_minutes(avg)}</b>' if avg is not None else ''
    text = (f'{emoji("report", "📈")} <b>Отчёт Media Bank {period}</b>\n\n{body}{total}{speed}\n'
            f'{emoji("new", "🆕")} Ждут взятия в работу: <b>{waiting}</b>')
    await safe_send(bot, REPORT_CHAT_ID, text)


def _report_due(key, hour, minute, weekday=None):
    """Пора ли слать отчёт: время наступило, сегодня ещё не слали, опоздание не больше окна."""
    n = datetime.now(TZ)
    if weekday is not None and n.weekday() != weekday:
        return False
    target = n.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if n < target or n - target > timedelta(hours=REPORT_CATCHUP_HOURS):
        return False
    return meta_get(key) != n.strftime('%Y-%m-%d')


async def scheduled_reports(context):
    jobs = (('last_daily_report', DAILY_REPORT_HOUR, DAILY_REPORT_MINUTE, None, 1),
            ('last_weekly_report', WEEKLY_REPORT_HOUR, WEEKLY_REPORT_MINUTE, WEEKLY_REPORT_DAY, 7))
    for key, hour, minute, weekday, days in jobs:
        try:
            if _report_due(key, hour, minute, weekday):
                await send_report(context.bot, days)
                meta_set(key, datetime.now(TZ).strftime('%Y-%m-%d'))
        except Exception:
            log.exception('Не удалось отправить отчёт %s', key)


async def remind_stale(bot):
    # напоминание, если новая заявка висит без движения дольше STALE_REMINDER_MINUTES (один раз на заявку)
    if STALE_REMINDER_MINUTES <= 0:
        return
    with db() as c:
        rows = c.execute("SELECT id,manager_id,COALESCE(manager,''),COALESCE(client_name,'') FROM applications "
                         "WHERE status='new' AND reminded_at IS NULL AND created_at<=? AND created_at>=? ORDER BY id LIMIT 10",
                         (utc_ago(minutes=STALE_REMINDER_MINUTES), utc_ago(hours=24))).fetchall()
        for r in rows:
            c.execute('UPDATE applications SET reminded_at=? WHERE id=?', (now(), r[0]))
    for aid, mid, mname, cname in rows:
        text = f'⏰ <b>Заявка #{aid}</b> ({escape(cname)}) ждёт взятия в работу уже более {STALE_REMINDER_MINUTES} мин.'
        await notify(bot, mid, text, actions(aid, 'new'))
        await notify(bot, GROUP_ID, f'{text}\nМенеджер: {escape(mname)}', actions(aid, 'new'))


async def scheduled_tick(context):
    await scheduled_reports(context)
    try:
        await remind_stale(context.bot)
    except Exception:
        log.exception('Ошибка напоминаний')


async def scheduled_backup(context):
    try:
        backup_database()
    except Exception:
        log.exception('Ошибка бэкапа БД')


# ───────────────────────────── Заявка: форма ─────────────────────────────
REQUIRED = ('product', 'bank', 'client_name', 'contact', 'manager', 'manager_id')


async def start(update, context):
    u = update.effective_user
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
    if step in ('note', 'search') and not is_staff(uid):
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


async def change_status(update, context, arg, *, new, expected, event, sets, toast, client_text):
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
        await notify(context.bot, client_id, client_text.format(aid=aid, manager=escape(mgr_name)))


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


async def export_csv(update, context):
    with db() as c:
        rows = c.execute(f"SELECT {APP_COLS},COALESCE(taken_at,''),COALESCE(completed_at,'') FROM applications ORDER BY id DESC").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['ID', 'Created', 'Product', 'Bank', 'Client', 'Telegram', 'Manager', 'Status', 'Taken', 'Completed'])
    for r in rows:
        row = list(r)
        row[2] = PRODUCTS.get(row[2], row[2])
        w.writerow([csv_safe(v) for v in row])
    f = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
    f.name = 'media_bank_applications.csv'
    await update.effective_message.reply_document(f, caption='Экспорт заявок Media Bank')


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
    await edit(q, emoji(arg, PRODUCT_FALLBACK[arg]) + f' <b>{escape(PRODUCTS[arg])}</b>\n\nВыберите банк:', bank_kb(arg))


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
    await send_report(context.bot, 7)
    await edit(update.callback_query, emoji('done', '✅') + ' <b>Отчёт за 7 дней отправлен в рабочую группу.</b>', admin_kb())


async def h_myreport(update, context, arg):
    q = update.callback_query
    await send_report(context.bot, 7, q.from_user.id)
    await edit(q, emoji('done', '✅') + ' <b>Ваш отчёт за 7 дней отправлен в рабочую группу.</b>', manager_panel())


async def h_myprofile(update, context, arg):
    q = update.callback_query
    await edit(q, manager_profile(q.from_user.id), InlineKeyboardMarkup([[btn('Назад', 'back', 'manager_panel')]]))


async def h_export(update, context, arg):
    await export_csv(update, context)


async def h_managers(update, context, arg):
    kb = [[btn(n, 'manager_select', f'mstats:{m}')] for m, n in MANAGERS.items()] + [[btn('Назад', 'back', 'admin')]]
    await edit(update.callback_query, emoji('manager_admin', '🤝') + ' <b>Менеджеры</b>', InlineKeyboardMarkup(kb))


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
                        sets={'completed_at': now()}, toast='Заявка завершена',
                        client_text='✅ <b>Заявка #{aid} завершена.</b>\nСпасибо, что выбрали Media Bank!')


async def h_reject(update, context, arg):
    await change_status(update, context, arg, new='rejected', expected=('new', 'in_work'), event='rejected',
                        sets={'completed_at': now()}, toast='Заявка отмечена как отказ',
                        client_text='❌ <b>По заявке #{aid} получен отказ.</b>\nМенеджер {manager} свяжется с вами и предложит варианты.')


async def h_reopen(update, context, arg):
    await change_status(update, context, arg, new='in_work', expected=('completed', 'rejected'), event='reopened',
                        sets={'completed_at': None}, toast='Заявка возвращена в работу',
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
        ok = c.execute("UPDATE applications SET manager=?,manager_id=?,status='new',taken_at=NULL,reminded_at=NULL "
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
}
ACCESS = {
    **dict.fromkeys(('admin', 'apps', 'stats', 'report', 'export', 'managers', 'mstats'), 'admin'),
    **dict.fromkeys(('manager_panel', 'mgrapps', 'myapps', 'mystats', 'myreport', 'myprofile'), 'manager'),
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
    role = ACCESS.get(action)
    if role and not ROLE_CHECK[role](u.id):
        return await _answer(q, 'Нет доступа', True)
    if context.user_data.get('step') in ('note', 'search') and action not in ('note', 'search'):
        context.user_data.pop('step', None)
        context.user_data.pop('note_app', None)
    try:
        if action not in SELF_ANSWER:
            await _answer(q)
        await handler(update, context, arg)
    except Exception:
        log.exception('CALLBACK ERROR data=%r user=%s', data, u.id)
        await _answer(q, 'Произошла ошибка. Попробуйте ещё раз.', True)
        if q.message and q.message.chat.type == 'private':
            try:
                await safe_reply(q.message, '⚠️ <b>Произошла ошибка.</b>\n\nПопробуйте ещё раз или вернитесь в главное меню.',
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
    if is_admin(update.effective_user.id):
        await send_report(context.bot, 7)
        await update.message.reply_text('Отчёт за 7 дней отправлен в рабочую группу.')


async def on_error(update, context):
    log.error('Необработанная ошибка', exc_info=context.error)


async def setup(app):
    await app.bot.set_my_commands([
        BotCommand('start', '🏠 Главное меню'),
        BotCommand('stats', '📊 Статистика'),
        BotCommand('report', '📈 Отчёт'),
    ])
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
