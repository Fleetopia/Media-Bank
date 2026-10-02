
import os
import csv
import io
import html
import sqlite3
import logging
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeAllPrivateChats
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1004381547715"))
REPORT_CHAT_ID = int(os.getenv("REPORT_CHAT_ID", str(GROUP_ID)))
DB_FILE = os.getenv("DB_FILE", "/data/media_bank.db")
if not os.path.isdir(os.path.dirname(DB_FILE)):
    DB_FILE = os.getenv("DB_FILE", "media_bank.db")

# FadeHost does not need a proxy. For local Windows testing you may set:
# PROXY_URL=socks5://127.0.0.1:10808
PROXY_URL = os.getenv("PROXY_URL", "").strip()

MANAGERS = {
    6045840701: "Эдуард",
    8923153510: "Александр",
}
ADMIN_IDS = {6045840701}

# The IDs below are the Premium Emoji IDs supplied by the owner.
# The most important semantic IDs are kept fixed and reused consistently.
PE = {
    "brand": "5438496463044752972",
    "debit": "5445353829304387411",
    "credit": "5287231198098117669",
    "rko": "5278702045883292456",
    "user": "5190498849440931467",
    "manager": "5373012449597335010",
    "stats": "5231200819986047254",
    "report": "5244837092042750681",
    "done": "5206607081334906820",
    "work": "5386367538735104399",
    "new": "5382357040008021292",
    "form": "5210952531676504517",
    "back": "5197269100878907942",
    "cancel": "5253742260054409879",
    "open": "5193177581888755275",
    "search": "5379999674193172777",
    "export": "5444856076954520455",
    "admin": "5217822164362739968",
    "send": "5206607081334906820",
    "bank": "5332455502917949981",
    "refresh": "5210952531676504517",
}

# All supplied IDs are retained here for future role expansion.
PREMIUM_IDS = [
"5438496463044752972","5445353829304387411","5287231198098117669",
"5278702045883292456","5197269100878907942","5416117059207572332",
"5210952531676504517","5206607081334906820","5373012449597335010",
"5190498849440931467","5447410659077661506","5373012449597335010",
"5382194935057372936","5210956306952758910","5197269100878907942",
"5332455502917949981","5445353829304387411","5287231198098117669",
"5278702045883292456","5253742260054409879","5382357040008021292",
"5386367538735104399","5206607081334906820","5444856076954520455",
"5193177581888755275","5379999674193172777","5206607081334906820",
"5210952531676504517","5190498849440931467","5231200819986047254",
"5197269100878907942","5244837092042750681","5190498849440931467",
"5386367538735104399","5206607081334906820","5253742260054409879",
"5217822164362739968","5341715473882955310","5231200819986047254",
"5244837092042750681","5197269100878907942","5382357040008021292",
"5386367538735104399","5206607081334906820","5190498849440931467",
"5445355530111437729","5443127283898405358","5231012545799666522",
"5231200819986047254","5244837092042750681","5246762912428603768",
"5303214794336125778","5274055917766202507","5413879192267805083",
"5382194935057372936","5382194935057372936","5287231198098117669",
"5310278924616356636","5440539497383087970","5424972470023104089",
"5244837092042750681","5231200819986047254","5413879192267805083",
"5274055917766202507","5274055917766202507","5444856076954520455",
"5382357040008021292","5386367538735104399","5206607081334906820",
"5190498849440931467","5445353829304387411","5287231198098117669",
"5278702045883292456","5332455502917949981","5458603043203327669",
"5424818078833715060","5395695537687123235","5461117441612462242",
"5456140674028019486","5424972470023104089","5341715473882955310",
"5197371802136892976","5447644880824181073","5445267414562389170",
"5395444784611480792","5206607081334906820","5197288647275071607",
"5251203410396458957","5197288647275071607","5271604874419647061"
]

USE_BUTTON_PREMIUM = os.getenv("ENABLE_BUTTON_PREMIUM", "1").lower() in {"1","true","yes","on"}

PRODUCTS = {
    "debit": {
        "name": "Дебетовая карта",
        "emoji": "💳",
        "icon": PE["debit"],
        "banks": [
            "Т-Банк", "Альфа-Банк", "ВТБ-Банк",
            "Промсвязьбанк", "Ак Барс Банк", "ОТП банк"
        ],
    },
    "credit": {
        "name": "Кредитная карта",
        "emoji": "💰",
        "icon": PE["credit"],
        "banks": [
            "Т-Банк", "ВТБ", "Уралсиб",
            "ОТП-Банк", "Яндекс — Кредитная карта супер Сплит",
            "Альфа-Банк"
        ],
    },
    "rko": {
        "name": "Регистрация бизнеса + РКО",
        "emoji": "🏢",
        "icon": PE["rko"],
        "banks": [
            "Альфа-Банк", "Промсвязьбанк",
            "РКО от Санкт-Петербург Банка", "УБРиР Банк"
        ],
    },
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
log = logging.getLogger("media_bank")


def now():
    return datetime.now().isoformat(timespec="seconds")


def db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    folder = os.path.dirname(DB_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT,
            client_name TEXT,
            contact TEXT,
            product TEXT NOT NULL,
            bank TEXT,
            manager_id INTEGER,
            manager TEXT,
            status TEXT NOT NULL DEFAULT 'new'
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_seen TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        # Safe migration for an older database.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(applications)").fetchall()}
        for name, typ in {
            "bank": "TEXT",
            "manager_id": "INTEGER",
            "manager": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'new'",
        }.items():
            if name not in cols:
                c.execute(f"ALTER TABLE applications ADD COLUMN {name} {typ}")


def is_admin(uid):
    return uid in ADMIN_IDS


def is_manager(uid):
    return uid in MANAGERS


def tg_emoji(eid, fallback="•"):
    # Custom emoji in message text. Telegram Premium/custom emoji IDs are
    # delivered as message entities via this HTML syntax.
    return f'<tg-emoji emoji-id="{html.escape(str(eid))}">{html.escape(fallback)}</tg-emoji>'


def icon_button(text, icon_id=None, callback_data=None):
    # Bot API/PTB 22.8 supports icon_custom_emoji_id on InlineKeyboardButton.
    # If the platform rejects it, the send/edit helper retries without icons.
    return InlineKeyboardButton(
        text=text,
        callback_data=callback_data,
        icon_custom_emoji_id=(icon_id if USE_BUTTON_PREMIUM else None),
    )


def clean_button(label):
    # Remove legacy ordinary emoji from button text.
    return re.sub(
        r"^[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\s]+", "", label
    ).strip()


def btn(text, icon, callback):
    return icon_button(clean_button(text), icon, callback)


def main_kb(uid):
    rows = [
        [btn("💳 Дебетовая карта", PE["debit"], "product:debit")],
        [btn("💰 Кредитная карта", PE["credit"], "product:credit")],
        [btn("🏢 Регистрация бизнеса + РКО", PE["rko"], "product:rko")],
        [btn("📝 Оставить заявку", PE["form"], "start_form")],
    ]
    if is_admin(uid):
        rows.append([btn("⚙️ Админ-панель", PE["admin"], "admin")])
    elif is_manager(uid):
        rows.append([btn("👨‍💼 Панель менеджера", PE["manager"], "manager")])
    return InlineKeyboardMarkup(rows)


def product_kb():
    return InlineKeyboardMarkup([
        [btn("📝 Оставить заявку", PE["form"], "start_form")],
        [btn("◀️ Назад", PE["back"], "main")],
    ])


def bank_kb(product):
    p = PRODUCTS[product]
    rows = []
    for i, bank in enumerate(p["banks"]):
        rows.append([btn(bank, PE["bank"], f"bank:{i}")])
    rows.append([btn("◀️ Назад", PE["back"], f"product:{product}")])
    return InlineKeyboardMarkup(rows)


def manager_choice_kb():
    return InlineKeyboardMarkup([
        [btn("Эдуард", PE["manager"], "manager:6045840701")],
        [btn("Александр", PE["manager"], "manager:8923153510")],
        [btn("◀️ Назад", PE["back"], "back_bank")],
    ])


def confirm_kb():
    return InlineKeyboardMarkup([
        [btn("Отправить заявку", PE["send"], "send")],
        [btn("Изменить", PE["form"], "edit_form")],
        [btn("Отмена", PE["cancel"], "main")],
    ])


def manager_kb():
    return InlineKeyboardMarkup([
        [btn("Мои заявки", PE["manager"], "my_apps")],
        [btn("Все заявки", PE["open"], "all_apps")],
        [btn("Статистика", PE["stats"], "stats")],
        [btn("Отчёт за сегодня", PE["report"], "today_report")],
        [btn("Мой профиль", PE["user"], "my_profile")],
        [btn("В меню", PE["back"], "main")],
    ])


def admin_kb():
    return InlineKeyboardMarkup([
        [btn("Все заявки", PE["open"], "all_apps")],
        [btn("Новые", PE["new"], "apps:new")],
        [btn("В работе", PE["work"], "apps:in_work")],
        [btn("Завершённые", PE["done"], "apps:completed")],
        [btn("Статистика", PE["stats"], "stats")],
        [btn("Отчёт за сегодня", PE["report"], "today_report")],
        [btn("Менеджеры", PE["manager"], "managers")],
        [btn("Экспорт CSV", PE["export"], "export")],
        [btn("В меню", PE["back"], "main")],
    ])


def app_actions(app_id, status):
    rows = []
    if status == "new":
        rows.append([btn("Взять в работу", PE["work"], f"take:{app_id}")])
    elif status == "in_work":
        rows.append([btn("Завершить", PE["done"], f"complete:{app_id}")])
    rows.append([btn("Открыть", PE["open"], f"view:{app_id}")])
    return InlineKeyboardMarkup(rows)


async def safe_send(target, text, reply_markup=None):
    try:
        return await target.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except BadRequest as e:
        if reply_markup and "icon_custom_emoji_id" in str(e).lower():
            # Retry once with the exact same buttons without custom icons.
            log.warning("Button custom emoji rejected; retrying without button icons")
            return await target.reply_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=strip_button_icons(reply_markup)
            )
        raise


async def safe_edit(query, text, reply_markup=None):
    try:
        return await query.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except BadRequest as e:
        msg = str(e)
        if "Message is not modified" in msg:
            return None
        if reply_markup and "icon_custom_emoji_id" in msg.lower():
            log.warning("Button custom emoji rejected on edit; retrying without icons")
            return await query.edit_message_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=strip_button_icons(reply_markup)
            )
        raise


def strip_button_icons(markup):
    if not markup:
        return None
    rows = []
    for row in markup.inline_keyboard:
        rows.append([
            InlineKeyboardButton(
                text=b.text,
                url=b.url,
                callback_data=b.callback_data,
                web_app=b.web_app,
                login_url=b.login_url,
                switch_inline_query=b.switch_inline_query,
                switch_inline_query_current_chat=b.switch_inline_query_current_chat,
                callback_game=b.callback_game,
                pay=b.pay,
            ) for b in row
        ])
    return InlineKeyboardMarkup(rows)


def status_name(s):
    return {
        "new": "Новая",
        "in_work": "В работе",
        "completed": "Завершена",
    }.get(s, s)


def user_line(u):
    username = f"@{u.username}" if u.username else "не указан"
    return f"{tg_emoji(PE['user'], '•')} <b>Telegram:</b> {html.escape(username)}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    u = update.effective_user
    with db() as c:
        c.execute("""
        INSERT INTO users(user_id,username,first_name,last_seen)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username,
        first_name=excluded.first_name,
        last_seen=excluded.last_seen
        """, (u.id, u.username, u.first_name, now()))
    text = (
        f"{tg_emoji(PE['brand'], '★')} <b>Media Bank</b>\n\n"
        "Выберите интересующую вас услугу:"
    )
    await safe_send(update.message, text, main_kb(u.id))


async def help_cmd(update, context):
    await safe_send(
        update.message,
        f"{tg_emoji(PE['brand'], '•')} <b>Media Bank</b>\n\n"
        "Для подачи заявки нажмите /start.\n"
        "Сотрудникам доступна панель менеджера."
    )


async def myid(update, context):
    await safe_send(
        update.message,
        f"{tg_emoji(PE['user'], '•')} Ваш Telegram ID: "
        f"<code>{update.effective_user.id}</code>"
    )


async def panel_cmd(update, context):
    if not is_manager(update.effective_user.id):
        return await safe_send(update.message, "Доступ только для менеджеров.")
    await safe_send(update.message, f"{tg_emoji(PE['manager'], '•')} <b>Панель менеджера</b>", manager_kb())


async def admin_cmd(update, context):
    if not is_admin(update.effective_user.id):
        return await safe_send(update.message, "Доступ запрещён.")
    await safe_send(update.message, f"{tg_emoji(PE['admin'], '•')} <b>Админ-панель</b>", admin_kb())


async def ask_name(update, context):
    context.user_data["state"] = "name"
    await safe_send(
        update.message,
        f"{tg_emoji(PE['user'], '•')} <b>Введите ваше имя:</b>",
        InlineKeyboardMarkup([[btn("Отмена", PE["cancel"], "main")]])
    )


async def text_input(update, context):
    text = (update.message.text or "").strip()
    state = context.user_data.get("state")
    if not state:
        return
    if text.startswith("/"):
        return

    if state == "name":
        context.user_data["client_name"] = text[:100]
        context.user_data["state"] = "contact"
        await safe_send(
            update.message,
            f"{tg_emoji(PE['user'], '•')} <b>Введите ваш Telegram @username</b>\n"
            "Если username нет — напишите «нет».",
            InlineKeyboardMarkup([[btn("Отмена", PE["cancel"], "main")]])
        )
    elif state == "contact":
        context.user_data["contact"] = text[:100]
        context.user_data["state"] = None
        await show_review(update.message, context)


async def show_review(target, context):
    d = context.user_data
    p = PRODUCTS[d["product"]]
    text = (
        f"{tg_emoji(PE['form'], '•')} <b>Проверьте заявку</b>\n\n"
        f"{tg_emoji(PE['debit'] if d['product']=='debit' else PE['credit'] if d['product']=='credit' else PE['rko'], '•')} "
        f"<b>Услуга:</b> {html.escape(p['name'])}\n"
        f"{tg_emoji(PE['bank'], '•')} <b>Банк:</b> {html.escape(d['bank'])}\n"
        f"{tg_emoji(PE['user'], '•')} <b>Имя:</b> {html.escape(d['client_name'])}\n"
        f"{tg_emoji(PE['user'], '•')} <b>Контакт:</b> {html.escape(d['contact'])}\n"
        f"{tg_emoji(PE['manager'], '•')} <b>Менеджер:</b> {html.escape(d['manager'])}"
    )
    await safe_send(target, text, confirm_kb())


async def create_application(update, context):
    d = context.user_data
    u = update.effective_user
    with db() as c:
        cur = c.execute("""
        INSERT INTO applications
        (created_at,user_id,username,client_name,contact,product,bank,manager_id,manager,status)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            now(), u.id, u.username, d["client_name"], d["contact"],
            d["product"], d["bank"], d["manager_id"], d["manager"], "new"
        ))
        app_id = cur.lastrowid

    p = PRODUCTS[d["product"]]
    username = f"@{u.username}" if u.username else "не указан"
    group_text = (
        f"{tg_emoji(PE['new'], '•')} <b>Новая заявка #{app_id}</b>\n\n"
        f"{tg_emoji(PE['user'], '•')} <b>Имя:</b> {html.escape(d['client_name'])}\n"
        f"{tg_emoji(PE['user'], '•')} <b>Telegram:</b> {html.escape(username)}\n"
        f"{tg_emoji(PE['debit'] if d['product']=='debit' else PE['credit'] if d['product']=='credit' else PE['rko'], '•')} "
        f"<b>Услуга:</b> {html.escape(p['name'])}\n"
        f"{tg_emoji(PE['bank'], '•')} <b>Банк:</b> {html.escape(d['bank'])}\n"
        f"{tg_emoji(PE['manager'], '•')} <b>Менеджер:</b> {html.escape(d['manager'])}\n"
        f"{tg_emoji(PE['new'], '•')} <b>Статус:</b> Новая\n"
        f"<i>{html.escape(now())}</i>"
    )
    try:
        await update.get_bot().send_message(
            GROUP_ID, group_text, parse_mode=ParseMode.HTML,
            reply_markup=app_actions(app_id, "new")
        )
    except BadRequest as e:
        if "icon_custom_emoji_id" in str(e).lower():
            await update.get_bot().send_message(
                GROUP_ID, group_text, parse_mode=ParseMode.HTML,
                reply_markup=strip_button_icons(app_actions(app_id, "new"))
            )
        else:
            raise

    context.user_data.clear()
    await safe_send(
        update.message,
        f"{tg_emoji(PE['done'], '✓')} <b>Заявка отправлена!</b>\n\n"
        "Менеджер получил вашу заявку и свяжется с вами."
    )


def application_row(row):
    return (
        f"#{row['id']} • {status_name(row['status'])}\n"
        f"{row['client_name']} • {row['product']} • {row['bank'] or '—'} • {row['manager'] or '—'}"
    )


async def list_apps(target, status_filter=None, manager_id=None):
    q = "SELECT * FROM applications"
    params = []
    where = []
    if status_filter:
        where.append("status=?"); params.append(status_filter)
    if manager_id:
        where.append("manager_id=?"); params.append(manager_id)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT 30"
    with db() as c:
        rows = c.execute(q, params).fetchall()
    if not rows:
        text = f"{tg_emoji(PE['open'], '•')} <b>Заявок пока нет.</b>"
        return await safe_send(target, text, admin_kb() if is_admin(target.from_user.id) else manager_kb())
    text = f"{tg_emoji(PE['open'], '•')} <b>Заявки</b>\n\n"
    for r in rows:
        text += html.escape(application_row(r)) + "\n\n"
    await safe_send(target, text, admin_kb() if is_admin(target.from_user.id) else manager_kb())


async def view_app(query, app_id):
    with db() as c:
        r = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    if not r:
        return await query.answer("Заявка не найдена", show_alert=True)
    username = f"@{r['username']}" if r["username"] else "не указан"
    text = (
        f"{tg_emoji(PE['open'], '•')} <b>Заявка #{r['id']}</b>\n\n"
        f"{tg_emoji(PE['user'], '•')} <b>Имя:</b> {html.escape(r['client_name'])}\n"
        f"{tg_emoji(PE['user'], '•')} <b>Telegram:</b> {html.escape(username)}\n"
        f"{tg_emoji(PE['debit'] if r['product']=='Дебетовая карта' else PE['credit'] if r['product']=='Кредитная карта' else PE['rko'], '•')} "
        f"<b>Услуга:</b> {html.escape(r['product'])}\n"
        f"{tg_emoji(PE['bank'], '•')} <b>Банк:</b> {html.escape(r['bank'] or '—')}\n"
        f"{tg_emoji(PE['manager'], '•')} <b>Менеджер:</b> {html.escape(r['manager'] or '—')}\n"
        f"<b>Статус:</b> {status_name(r['status'])}\n"
        f"<b>Создана:</b> {html.escape(r['created_at'])}"
    )
    await safe_edit(query, text, app_actions(r["id"], r["status"]))


async def stats_text(manager_id=None):
    with db() as c:
        total = c.execute(
            "SELECT COUNT(*) n FROM applications" + (" WHERE manager_id=?" if manager_id else ""),
            (manager_id,) if manager_id else ()
        ).fetchone()["n"]
        new = c.execute(
            "SELECT COUNT(*) n FROM applications WHERE status='new'" + (" AND manager_id=?" if manager_id else ""),
            (manager_id,) if manager_id else ()
        ).fetchone()["n"]
        work = c.execute(
            "SELECT COUNT(*) n FROM applications WHERE status='in_work'" + (" AND manager_id=?" if manager_id else ""),
            (manager_id,) if manager_id else ()
        ).fetchone()["n"]
        done = c.execute(
            "SELECT COUNT(*) n FROM applications WHERE status='completed'" + (" AND manager_id=?" if manager_id else ""),
            (manager_id,) if manager_id else ()
        ).fetchone()["n"]
    return (
        f"{tg_emoji(PE['stats'], '•')} <b>Статистика</b>\n\n"
        f"Всего: <b>{total}</b>\n"
        f"{tg_emoji(PE['new'], '•')} Новых: <b>{new}</b>\n"
        f"{tg_emoji(PE['work'], '•')} В работе: <b>{work}</b>\n"
        f"{tg_emoji(PE['done'], '•')} Завершено: <b>{done}</b>"
    )


async def show_stats(query, manager_id=None):
    await safe_edit(query, await stats_text(manager_id), manager_kb() if manager_id else admin_kb())


async def report_text(days=1, manager_id=None):
    start = datetime.now() - timedelta(days=days-1)
    with db() as c:
        params = [start.isoformat(timespec="seconds")]
        where = "created_at >= ?"
        if manager_id:
            where += " AND manager_id=?"; params.append(manager_id)
        total = c.execute(f"SELECT COUNT(*) n FROM applications WHERE {where}", params).fetchone()["n"]
        done = c.execute(f"SELECT COUNT(*) n FROM applications WHERE {where} AND status='completed'", params).fetchone()["n"]
        banks = c.execute(
            f"SELECT bank,COUNT(*) n FROM applications WHERE {where} GROUP BY bank ORDER BY n DESC",
            params
        ).fetchall()
    text = (
        f"{tg_emoji(PE['report'], '•')} <b>Отчёт</b>\n\n"
        f"Заявок: <b>{total}</b>\n"
        f"Завершено: <b>{done}</b>\n\n"
        f"{tg_emoji(PE['bank'], '•')} <b>По банкам:</b>\n"
    )
    text += "\n".join(f"• {html.escape(r['bank'] or '—')}: {r['n']}" for r in banks) or "—"
    return text


async def report_cmd(update, context):
    if not is_manager(update.effective_user.id):
        return
    await safe_send(update.message, await report_text(1, update.effective_user.id), manager_kb())


async def export_csv(query):
    if not is_admin(query.from_user.id):
        return await query.answer("Нет доступа", show_alert=True)
    with db() as c:
        rows = c.execute("SELECT * FROM applications ORDER BY id DESC").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id","created_at","user_id","username","client_name","contact","product","bank","manager","status"])
    for r in rows:
        w.writerow([r["id"],r["created_at"],r["user_id"],r["username"],r["client_name"],r["contact"],r["product"],r["bank"],r["manager"],r["status"]])
    data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    data.name = "media_bank_applications.csv"
    await query.message.reply_document(data, caption=f"{tg_emoji(PE['export'], '•')} Экспорт заявок")
    await query.answer()


async def button(update: Update, context):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id

    if data == "main":
        context.user_data.clear()
        return await safe_edit(q, f"{tg_emoji(PE['brand'], '★')} <b>Media Bank</b>\n\nВыберите услугу:", main_kb(uid))

    if data.startswith("product:"):
        product = data.split(":",1)[1]
        if product not in PRODUCTS:
            return
        context.user_data["product"] = product
        p = PRODUCTS[product]
        text = (
            f"{tg_emoji(p['icon'], p['emoji'])} <b>{html.escape(p['name'])}</b>\n\n"
            f"{tg_emoji(PE['bank'], '•')} <b>Выберите банк:</b>"
        )
        return await safe_edit(q, text, bank_kb(product))

    if data == "start_form":
        return await ask_name_callback(q, context)

    if data == "edit_form":
        return await ask_name_callback(q, context)

    if data == "back_bank":
        product = context.user_data.get("product")
        if product:
            return await safe_edit(q, f"{tg_emoji(PRODUCTS[product]['icon'], PRODUCTS[product]['emoji'])} <b>Выберите банк:</b>", bank_kb(product))
        return await safe_edit(q, f"{tg_emoji(PE['brand'], '★')} <b>Media Bank</b>", main_kb(uid))

    if data.startswith("bank:"):
        product = context.user_data.get("product")
        if not product:
            return await safe_edit(q, "Сначала выберите продукт.", main_kb(uid))
        idx = int(data.split(":")[1])
        banks = PRODUCTS[product]["banks"]
        if idx >= len(banks):
            return
        context.user_data["bank"] = banks[idx]
        context.user_data["state"] = "manager"
        return await safe_edit(q, f"{tg_emoji(PE['manager'], '•')} <b>Выберите менеджера:</b>", manager_choice_kb())

    if data.startswith("manager:"):
        mid = int(data.split(":")[1])
        context.user_data["manager_id"] = mid
        context.user_data["manager"] = MANAGERS[mid]
        context.user_data["state"] = None
        return await ask_name_callback(q, context)

    if data == "send":
        return await create_application_callback(q, context)

    if data == "admin" and is_admin(uid):
        return await safe_edit(q, f"{tg_emoji(PE['admin'], '•')} <b>Админ-панель</b>", admin_kb())

    if data == "manager" and is_manager(uid):
        return await safe_edit(q, f"{tg_emoji(PE['manager'], '•')} <b>Панель менеджера</b>", manager_kb())

    if data == "all_apps":
        return await list_apps(q, None, None)

    if data.startswith("apps:"):
        if not (is_admin(uid) or is_manager(uid)):
            return
        return await list_apps(q, data.split(":")[1], uid if is_manager(uid) and not is_admin(uid) else None)

    if data == "my_apps":
        return await list_apps(q, None, uid)

    if data == "stats":
        return await show_stats(q, uid if is_manager(uid) and not is_admin(uid) else None)

    if data == "today_report":
        return await safe_edit(q, await report_text(1, uid if is_manager(uid) and not is_admin(uid) else None), manager_kb() if is_manager(uid) and not is_admin(uid) else admin_kb())

    if data == "managers" and is_admin(uid):
        text = f"{tg_emoji(PE['manager'], '•')} <b>Менеджеры</b>\n\n"
        for mid, name in MANAGERS.items():
            with db() as c:
                total = c.execute("SELECT COUNT(*) n FROM applications WHERE manager_id=?", (mid,)).fetchone()["n"]
                done = c.execute("SELECT COUNT(*) n FROM applications WHERE manager_id=? AND status='completed'", (mid,)).fetchone()["n"]
            text += f"<b>{html.escape(name)}</b>: {total} заявок / {done} завершено\n"
        return await safe_edit(q, text, admin_kb())

    if data == "export":
        return await export_csv(q)

    if data.startswith("view:"):
        return await view_app(q, int(data.split(":")[1]))

    if data.startswith("take:"):
        app_id = int(data.split(":")[1])
        if not is_manager(uid):
            return await q.answer("Нет доступа", show_alert=True)
        with db() as c:
            c.execute("UPDATE applications SET status='in_work',manager_id=?,manager=? WHERE id=? AND status='new'", (uid, MANAGERS[uid], app_id))
            r = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if r:
            try:
                await context.bot.send_message(
                    r["user_id"],
                    f"{tg_emoji(PE['work'], '•')} <b>Заявка #{app_id} взята в работу.</b>\n"
                    f"Менеджер: {html.escape(MANAGERS[uid])}",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        return await view_app(q, app_id)

    if data.startswith("complete:"):
        app_id = int(data.split(":")[1])
        if not is_manager(uid):
            return await q.answer("Нет доступа", show_alert=True)
        with db() as c:
            c.execute("UPDATE applications SET status='completed' WHERE id=? AND status='in_work'", (app_id,))
            r = c.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
        if r:
            try:
                await context.bot.send_message(
                    r["user_id"],
                    f"{tg_emoji(PE['done'], '✓')} <b>Заявка #{app_id} завершена.</b>",
                    parse_mode=ParseMode.HTML
                )
            except Exception:
                pass
        return await view_app(q, app_id)

    if data == "my_profile" and is_manager(uid):
        return await safe_edit(
            q,
            f"{tg_emoji(PE['user'], '•')} <b>Мой профиль</b>\n\n"
            f"Имя: <b>{html.escape(MANAGERS[uid])}</b>\n"
            f"Telegram ID: <code>{uid}</code>",
            manager_kb()
        )


async def ask_name_callback(q, context):
    context.user_data["state"] = "name"
    return await safe_edit(
        q,
        f"{tg_emoji(PE['user'], '•')} <b>Введите ваше имя:</b>",
        InlineKeyboardMarkup([[btn("Отмена", PE["cancel"], "main")]])
    )


async def create_application_callback(q, context):
    d = context.user_data
    required = ["product","bank","manager_id","manager","client_name","contact"]
    if not all(d.get(k) for k in required):
        return await q.answer("Заполните все поля.", show_alert=True)
    u = q.from_user
    with db() as c:
        cur = c.execute("""
        INSERT INTO applications
        (created_at,user_id,username,client_name,contact,product,bank,manager_id,manager,status)
        VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (now(),u.id,u.username,d["client_name"],d["contact"],
              PRODUCTS[d["product"]]["name"],d["bank"],d["manager_id"],d["manager"],"new"))
        app_id = cur.lastrowid

    p = PRODUCTS[d["product"]]
    username = f"@{u.username}" if u.username else "не указан"
    text = (
        f"{tg_emoji(PE['new'], '•')} <b>Новая заявка #{app_id}</b>\n\n"
        f"{tg_emoji(PE['user'], '•')} <b>Имя:</b> {html.escape(d['client_name'])}\n"
        f"{tg_emoji(PE['user'], '•')} <b>Telegram:</b> {html.escape(username)}\n"
        f"{tg_emoji(p['icon'], p['emoji'])} <b>Услуга:</b> {html.escape(p['name'])}\n"
        f"{tg_emoji(PE['bank'], '•')} <b>Банк:</b> {html.escape(d['bank'])}\n"
        f"{tg_emoji(PE['manager'], '•')} <b>Менеджер:</b> {html.escape(d['manager'])}\n"
        f"{tg_emoji(PE['new'], '•')} <b>Статус:</b> Новая"
    )
    markup = app_actions(app_id, "new")
    try:
        await context.bot.send_message(GROUP_ID, text, parse_mode=ParseMode.HTML, reply_markup=markup)
    except BadRequest as e:
        if "icon_custom_emoji_id" in str(e).lower():
            await context.bot.send_message(GROUP_ID, text, parse_mode=ParseMode.HTML, reply_markup=strip_button_icons(markup))
        else:
            raise

    context.user_data.clear()
    return await safe_edit(
        q,
        f"{tg_emoji(PE['done'], '✓')} <b>Заявка отправлена!</b>\n\n"
        "Менеджер получил заявку."
    )


async def scheduled_reports(app):
    # Lightweight daily/weekly scheduler. It checks once a minute.
    now_dt = datetime.now()
    if now_dt.hour == int(os.getenv("DAILY_REPORT_HOUR", "21")) and now_dt.minute == int(os.getenv("DAILY_REPORT_MINUTE", "0")):
        key = now_dt.strftime("%Y-%m-%d")
        with db() as c:
            exists = c.execute("SELECT 1 FROM report_log WHERE kind=? AND created_at LIKE ?", ("daily", key+"%")).fetchone()
        if not exists:
            try:
                await app.bot.send_message(REPORT_CHAT_ID, await report_text(1), parse_mode=ParseMode.HTML)
                with db() as c:
                    c.execute("INSERT INTO report_log(kind,created_at) VALUES(?,?)", ("daily", now()))
            except Exception:
                log.exception("Daily report failed")


async def post_init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Главное меню"),
        BotCommand("help", "Помощь"),
        BotCommand("panel", "Панель менеджера"),
        BotCommand("admin", "Админ-панель"),
        BotCommand("report", "Отчёт"),
        BotCommand("myid", "Мой Telegram ID"),
    ], scope=BotCommandScopeAllPrivateChats())
    if app.job_queue:
        app.job_queue.run_repeating(scheduled_reports, interval=60, first=10)


async def error_handler(update, context):
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return
    log.exception("Unhandled error", exc_info=context.error)


def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN отсутствует в environment variables")
    builder = Application.builder().token(BOT_TOKEN)
    # Optional proxy is for local use only.
    if PROXY_URL:
        from telegram.request import HTTPXRequest
        req = HTTPXRequest(
            proxy=PROXY_URL,
            connect_timeout=30, read_timeout=30,
            write_timeout=30, pool_timeout=30
        )
        builder = builder.request(req).get_updates_request(req)
    return builder.post_init(post_init).build()


def main():
    init_db()
    app = build_app()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    app.add_error_handler(error_handler)
    print("MEDIA BANK — CLEAN FINAL")
    print("DB:", DB_FILE)
    print("Premium message emoji: enabled")
    print("Premium button icons:", USE_BUTTON_PREMIUM)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
