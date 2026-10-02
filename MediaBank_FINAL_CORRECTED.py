import logging
import os
import sqlite3
from datetime import datetime, timedelta, time
from html import escape

from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, BotCommandScopeAllPrivateChats, InputFile
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)
from telegram.request import HTTPXRequest

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "-1004381547715"))
REPORT_CHAT_ID = int(os.getenv("REPORT_CHAT_ID", str(GROUP_ID)))
PROXY = os.getenv("PROXY", "").strip()
DB_FILE = "media_bank.db"

# Manager IDs used in the project.
MANAGERS = {
    6045840701: "Эдуард",
    8923153510: "Александр",
}
ADMIN_IDS = {6045840701}

# Report schedule. Times are local to the machine running the bot.
DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "21"))
DAILY_REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
WEEKLY_REPORT_DAY = int(os.getenv("WEEKLY_REPORT_DAY", "0"))  # Monday = 0
WEEKLY_REPORT_HOUR = int(os.getenv("WEEKLY_REPORT_HOUR", "21"))
WEEKLY_REPORT_MINUTE = int(os.getenv("WEEKLY_REPORT_MINUTE", "5"))

# Premium/custom emoji support:
# Put Telegram custom emoji IDs in .env. If left blank, normal emoji are used.
# Example:
# EMOJI_NEW=5368324170671202286
# EMOJI_OK=5368324170671202287
CUSTOM_EMOJI = {
    "new": os.getenv("EMOJI_NEW", "").strip(),
    "ok": os.getenv("EMOJI_OK", "").strip(),
    "work": os.getenv("EMOJI_WORK", "").strip(),
    "money": os.getenv("EMOJI_MONEY", "").strip(),
    "chart": os.getenv("EMOJI_CHART", "").strip(),
    "user": os.getenv("EMOJI_USER", "").strip(),
    "building": os.getenv("EMOJI_BUILDING", "").strip(),
    "card": os.getenv("EMOJI_CARD", "").strip(),
}

BUTTON_EMOJI = {
    "debit": "5445353829304387411", "credit": "5287231198098117669",
    "rko": "5278702045883292456", "form": "5210952531676504517",
    "back": "5197269100878907942", "admin": "5217822164362739968",
    "manager": "5373012449597335010", "user": "5190498849440931467",
    "apps": "5447410659077661506", "stats": "5231200819986047254",
    "report": "5244837092042750681", "export": "5444856076954520455",
    "new": "5382357040008021292", "work": "5386367538735104399",
    "done": "5206607081334906820", "open": "5193177581888755275",
    "search": "5379999674193172777", "bank": "5332455502917949981",
    "send": "5206607081334906820",
}

def pb(text, callback_data, kind=None):
    prefixes = ("💳 ","💰 ","🏢 ","📝 ","◀️ ","⚙️ ","👨‍💼 ","👤 ",
                "🗂 ","📊 ","📋 ","📈 ","👥 ","📤 ","🏦 ","🆕 ",
                "🔄 ","✅ ","🔎 ","📌 ","✏️ ","🚀 ")
    for prefix in prefixes:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return InlineKeyboardButton(text, callback_data=callback_data,
                                icon_custom_emoji_id=BUTTON_EMOJI.get(kind or ""))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("media_bank")

PRODUCTS = {
    "debit": ("💳 Дебетовая карта", "Подберём подходящую дебетовую карту и передадим заявку специалисту."),
    "credit": ("💰 Кредитная карта", "Поможем подобрать кредитную карту и передадим заявку специалисту."),
    "rko": ("🏢 Регистрация бизнеса + РКО", "Поможем оформить заявку на регистрацию бизнеса и РКО."),
}

# Banks/offers used by Media Bank, grouped by the service shown in the
# user's offer lists. The names intentionally preserve the source wording.
BANKS_BY_PRODUCT = {
    "debit": [
        ("tbank", "Т-Банк"),
        ("alfa", "Альфа-Банк"),
        ("vtb", "ВТБ-Банк"),
        ("psb", "Промсвязьбанк"),
        ("akbars", "Ак Барс Банк"),
        ("otp", "ОТП банк"),
    ],
    "credit": [
        ("tbank", "Т-Банк"),
        ("vtb", "ВТБ"),
        ("uralsib", "Уралсиб"),
        ("otp", "ОТП-Банк"),
        ("yandex_split", "Яндекс - Кредитная карта супер Сплит"),
        ("alfa", "Альфа-Банк"),
    ],
    "rko": [
        ("alfa", "Альфа-Банк"),
        ("psb", "Промсвязьбанк"),
        ("spb", "РКО от Санкт-Петербург Банка"),
        ("ubrir", "УБРиР Банк"),
    ],
}

ALL_BANK_NAMES = {
    key: name
    for items in BANKS_BY_PRODUCT.values()
    for key, name in items
}


def db():
    return sqlite3.connect(DB_FILE)


def init_db():
    with db() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            name TEXT NOT NULL,
            product TEXT NOT NULL,
            manager_id INTEGER,
            manager_name TEXT,
            status TEXT NOT NULL DEFAULT 'new',
            created_at TEXT NOT NULL,
            taken_at TEXT,
            completed_at TEXT
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
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS report_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """)
        # Safe migration for existing databases created before bank tracking.
        columns = {row[1] for row in c.execute("PRAGMA table_info(applications)").fetchall()}
        if "bank" not in columns:
            c.execute("ALTER TABLE applications ADD COLUMN bank TEXT DEFAULT 'Другой / не указан'")


def is_admin(uid):
    return uid in ADMIN_IDS


def is_manager(uid):
    return uid in MANAGERS


def mk(rows):
    return InlineKeyboardMarkup(rows)


def custom_emoji(kind, fallback):
    # HTML custom emoji syntax. Telegram ignores this only if an invalid ID is used,
    # so the helper falls back to ordinary emoji when no ID is configured.
    eid = CUSTOM_EMOJI.get(kind, "")
    if eid:
        return f'<tg-emoji emoji-id="{escape(eid)}">{fallback}</tg-emoji>'
    return fallback


def main_kb(uid):
    rows = [
        [pb("💳 Дебетовая карта", "product:debit", "debit")],
        [pb("💰 Кредитная карта", "product:credit", "credit")],
        [pb("🏢 Регистрация бизнеса + РКО", "product:rko", "rko")],
        [pb("📝 Оставить заявку", "start_form", "form")],
    ]
    if is_admin(uid):
        rows.append([pb("⚙️ Админ-панель", "admin", "admin")])
    elif is_manager(uid):
        rows.append([pb("👨‍💼 Панель менеджера", "manager", "manager")])
    return mk(rows)


def product_kb():
    return mk([
        [pb("📝 Оставить заявку", "start_form", "form")],
        [pb("🏦 Выбрать банк", "choose_bank", "bank")],
        [pb("◀️ Назад", "main", "back")]
    ])


def manager_kb():
    return mk([
        [pb("🗂 Мои заявки", "my_apps", "apps")],
        [pb("📊 Моя статистика", "my_stats", "stats")],
        [pb("📋 Все заявки", "all_apps", "apps")],
        [pb("📈 Отчёт за сегодня", "today_report", "report")],
        [pb("👤 Мой профиль", "my_profile", "user")],
        [pb("◀️ В меню", "main", "back")]
    ])


def admin_kb():
    return mk([
        [pb("📋 Все заявки", "all_apps", "apps")],
        [pb("📊 Статистика", "stats", "stats")],
        [pb("🏦 Банки", "bank_stats", "bank")],
        [pb("📈 Отчёт за сегодня", "today_report", "report")],
        [pb("👥 Менеджеры", "managers", "manager")],
        [pb("📤 Экспорт CSV", "export", "export")],
        [pb("◀️ В меню", "main", "back")]
    ])


def apps_filter_kb(back):
    return mk([
        [pb("📋 Все", "apps:all", "apps"),
         pb("🆕 Новые", "apps:new", "new")],
        [pb("🔄 В работе", "apps:in_work", "work"),
         pb("✅ Завершённые", "apps:completed", "done")],
        [pb("◀️ Назад", back, "back")]
    ])


def manager_profile_kb():
    return mk([
        [pb("🗂 Мои заявки", "my_apps", "apps")],
        [pb("📊 Моя статистика", "my_stats", "stats")],
        [pb("◀️ Назад", "manager", "back")]
    ])


def app_actions(app_id, status):
    rows = []
    if status == "new":
        rows.append([pb("👤 Взять в работу", f"take:{app_id}", "work")])
    elif status == "in_work":
        rows.append([pb("✅ Завершить", f"complete:{app_id}", "done")])
    rows.append([pb("🔎 Открыть", f"view:{app_id}", "open")])
    return mk(rows)


async def safe_edit(q, text, reply_markup=None):
    try:
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML, reply_markup=reply_markup
        )
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            raise


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    u = update.effective_user
    with db() as c:
        c.execute("""
        INSERT INTO users(user_id,username,first_name,last_seen)
        VALUES(?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        username=excluded.username, first_name=excluded.first_name,
        last_seen=excluded.last_seen
        """, (
            u.id, u.username, u.first_name,
            datetime.now().isoformat(timespec="seconds")
        ))
    await update.message.reply_text(
        "👋 <b>Добро пожаловать в Media Bank!</b>\n\n"
        "Выберите интересующую вас услугу:",
        parse_mode=ParseMode.HTML,
        reply_markup=main_kb(u.id)
    )


async def help_cmd(update, context):
    await update.message.reply_text(
        "Используйте /start для открытия меню.\n"
        "Сотрудники: /panel\n"
        "Администратор: /admin"
    )


async def myid(update, context):
    await update.message.reply_text(
        f"Ваш Telegram ID: <code>{update.effective_user.id}</code>",
        parse_mode=ParseMode.HTML
    )


async def panel_cmd(update, context):
    if not is_manager(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только для сотрудников.")
        return
    await update.message.reply_text("👨‍💼 <b>Панель менеджера</b>", parse_mode=ParseMode.HTML, reply_markup=manager_kb())


async def admin_cmd(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только администратору.")
        return
    await update.message.reply_text("⚙️ <b>Админ-панель</b>", parse_mode=ParseMode.HTML, reply_markup=admin_kb())


async def ask_name(q, context):
    context.user_data["state"] = "name"
    await safe_edit(
        q,
        "📝 <b>Заявка</b>\n\nВведите ваше имя:",
        mk([[pb("◀️ В меню", "main", "back")]])
    )


async def text_input(update, context):
    if context.user_data.get("state") != "name":
        return
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("Введите имя текстом.")
        return
    context.user_data["name"] = name
    context.user_data["state"] = "manager"
    await update.message.reply_text(
        "👨‍💼 <b>Выберите менеджера:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=mk([
            [pb("Эдуард", "manager:6045840701", "manager")],
            [pb("Александр", "manager:8923153510", "manager")],
            [pb("◀️ В меню", "main", "back")]
        ])
    )


async def create_application(q, context):
    uid = q.from_user.id
    name = context.user_data.get("name", "").strip()
    product = context.user_data.get("product", "Не указано")
    bank = context.user_data.get("bank", "Другой / не указан")
    manager_id = context.user_data.get("manager_id")
    manager_name = MANAGERS.get(manager_id, "")
    username = q.from_user.username or ""
    now = datetime.now().isoformat(timespec="seconds")

    with db() as c:
        cur = c.execute("""
        INSERT INTO applications
        (user_id,username,name,product,bank,manager_id,manager_name,status,created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            uid, username, name, product, bank, manager_id,
            manager_name, "new", now
        ))
        app_id = cur.lastrowid

    new_icon = custom_emoji("new", "🆕")
    user_icon = custom_emoji("user", "👤")
    card_icon = custom_emoji("card", "💳")

    notification = (
        f"{new_icon} <b>Новая заявка #{app_id}</b>\n\n"
        f"{user_icon} Имя: <b>{escape(name)}</b>\n"
        f"📱 Telegram: @{escape(username) if username else 'нет username'}\n"
        f"{card_icon} Услуга: <b>{escape(product)}</b>\n"
        f"🏦 Банк: <b>{escape(bank)}</b>\n"
        f"👨‍💼 Менеджер: <b>{escape(manager_name)}</b>\n"
        f"🕐 {now}"
    )
    try:
        await context.bot.send_message(
            GROUP_ID,
            notification,
            parse_mode=ParseMode.HTML,
            reply_markup=app_actions(app_id, "new")
        )
    except TelegramError:
        log.exception("Could not send application to group")

    context.user_data.clear()
    await safe_edit(
        q,
        f"{custom_emoji('ok','✅')} <b>Заявка отправлена!</b>\n\n"
        "Специалист свяжется с вами в Telegram.",
        main_kb(uid)
    )


def report_data(period_start=None, period_end=None):
    where = ""
    args = []
    if period_start:
        where = " WHERE created_at >= ?"
        args.append(period_start)
    if period_end:
        where += (" AND " if where else " WHERE ") + "created_at < ?"
        args.append(period_end)

    with db() as c:
        total = c.execute(f"SELECT COUNT(*) FROM applications{where}", args).fetchone()[0]
        new = c.execute(f"SELECT COUNT(*) FROM applications{where + (' AND' if where else ' WHERE')} status='new'", args).fetchone()[0]
        work = c.execute(f"SELECT COUNT(*) FROM applications{where + (' AND' if where else ' WHERE')} status='in_work'", args).fetchone()[0]
        done = c.execute(f"SELECT COUNT(*) FROM applications{where + (' AND' if where else ' WHERE')} status='completed'", args).fetchone()[0]

        manager_rows = c.execute(
            f"""SELECT COALESCE(manager_name,'Не назначен'), COUNT(*)
                FROM applications{where}
                GROUP BY manager_name ORDER BY COUNT(*) DESC""", args
        ).fetchall()

        product_rows = c.execute(
            f"""SELECT product, COUNT(*) FROM applications{where}
                GROUP BY product ORDER BY COUNT(*) DESC""", args
        ).fetchall()

        bank_rows = c.execute(
            f"""SELECT COALESCE(bank,'Другой / не указан'),
                       COUNT(*),
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                FROM applications{where}
                GROUP BY COALESCE(bank,'Другой / не указан')
                ORDER BY COUNT(*) DESC""", args
        ).fetchall()

        product_bank_rows = c.execute(
            f"""SELECT COALESCE(bank,'Другой / не указан'), product,
                       COUNT(*),
                       SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                FROM applications{where}
                GROUP BY COALESCE(bank,'Другой / не указан'), product
                ORDER BY COUNT(*) DESC""", args
        ).fetchall()

    return total, new, work, done, manager_rows, product_rows, bank_rows, product_bank_rows


def format_report(title, start=None, end=None):
    total, new, work, done, managers, products, banks, product_banks = report_data(start, end)
    conversion = done / total * 100 if total else 0

    text = (
        f"📈 <b>{escape(title)}</b>\n\n"
        f"📥 Всего заявок: <b>{total}</b>\n"
        f"🆕 Новых: <b>{new}</b>\n"
        f"🔄 В работе: <b>{work}</b>\n"
        f"✅ Завершено: <b>{done}</b>\n"
        f"📊 Конверсия: <b>{conversion:.1f}%</b>\n"
    )

    if products:
        text += "\n<b>По услугам:</b>\n"
        for product, count in products:
            text += f"• {escape(product)} — <b>{count}</b>\n"

    if banks:
        text += "\n<b>🏦 По банкам:</b>\n"
        for bank, count, completed in banks:
            text += f"• {escape(bank)} — <b>{count}</b> заявок / <b>{completed or 0}</b> завершено\n"

    if product_banks:
        text += "\n<b>🏦 Банк → продукт:</b>\n"
        current_bank = None
        for bank, product, count, completed in product_banks:
            if bank != current_bank:
                text += f"\n<b>{escape(bank)}</b>\n"
                current_bank = bank
            text += f"  • {escape(product)} — <b>{count}</b> / <b>{completed or 0}</b> завершено\n"

    if managers:
        text += "\n<b>По менеджерам:</b>\n"
        for manager_name, count in managers:
            text += f"• {escape(manager_name or 'Не назначен')} — <b>{count}</b>\n"

    return text


async def send_daily_report(app, manual=False):
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    text = format_report(
        f"Отчёт за {now:%d.%m.%Y}",
        start.isoformat(timespec="seconds"),
        (start + timedelta(days=1)).isoformat(timespec="seconds")
    )
    try:
        await app.bot.send_message(REPORT_CHAT_ID, text, parse_mode=ParseMode.HTML)
        with db() as c:
            c.execute(
                "INSERT INTO report_log(kind,created_at) VALUES(?,?)",
                ("daily_manual" if manual else "daily_auto", now.isoformat(timespec="seconds"))
            )
    except TelegramError:
        log.exception("Daily report failed")


async def send_weekly_report(app):
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    text = format_report(
        f"Отчёт за неделю {monday:%d.%m}–{now:%d.%m.%Y}",
        monday.isoformat(timespec="seconds"),
        (now + timedelta(days=1)).isoformat(timespec="seconds")
    )
    try:
        await app.bot.send_message(REPORT_CHAT_ID, text, parse_mode=ParseMode.HTML)
        with db() as c:
            c.execute(
                "INSERT INTO report_log(kind,created_at) VALUES(?,?)",
                ("weekly_auto", now.isoformat(timespec="seconds"))
            )
    except TelegramError:
        log.exception("Weekly report failed")


async def scheduled_reports(context):
    now = datetime.now()
    if now.hour == DAILY_REPORT_HOUR and now.minute == DAILY_REPORT_MINUTE:
        with db() as c:
            exists = c.execute(
                "SELECT COUNT(*) FROM report_log WHERE kind='daily_auto' AND created_at LIKE ?",
                (now.strftime("%Y-%m-%d") + "%",)
            ).fetchone()[0]
        if not exists:
            await send_daily_report(context.application)

    if now.weekday() == WEEKLY_REPORT_DAY and now.hour == WEEKLY_REPORT_HOUR and now.minute == WEEKLY_REPORT_MINUTE:
        with db() as c:
            exists = c.execute(
                "SELECT COUNT(*) FROM report_log WHERE kind='weekly_auto' AND created_at LIKE ?",
                (now.strftime("%Y-%m-%d") + "%",)
            ).fetchone()[0]
        if not exists:
            await send_weekly_report(context.application)


async def list_apps(q, mine=False):
    if not is_manager(q.from_user.id) and not is_admin(q.from_user.id):
        await q.answer("⛔ Доступ только для сотрудников.", show_alert=True)
        return

    sql = """
    SELECT id,name,product,bank,manager_name,status
    FROM applications
    """
    args = []
    if mine:
        sql += " WHERE manager_id=?"
        args.append(q.from_user.id)
    sql += " ORDER BY id DESC LIMIT 50"

    with db() as c:
        rows = c.execute(sql, args).fetchall()

    if not rows:
        await safe_edit(
            q, "📋 <b>Заявок пока нет.</b>",
            mk([[pb("◀️ Назад", "admin" if is_admin(q.from_user.id) else "manager", "back")]])
        )
        return

    symbols = {"new": "🆕", "in_work": "🔄", "completed": "✅"}
    text = "📋 <b>Заявки</b>\n\n"
    buttons = []

    for aid, name, product, bank, manager_name, status in rows:
        text += (
            f"{symbols.get(status, '•')} <b>#{aid}</b> — "
            f"{escape(name)} — {escape(product)} — 🏦 {escape(bank or 'Другой / не указан')}\n"
        )
        buttons.append([
            pb(f"🔎 Открыть #{aid}", f"view:{aid}", "open")
        ])

    buttons.append([
        pb("◀️ Назад", "admin" if is_admin(q.from_user.id) else "manager", "back")
    ])
    await safe_edit(q, text, mk(buttons))


async def view_app(q, app_id):
    if not is_manager(q.from_user.id) and not is_admin(q.from_user.id):
        await q.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    with db() as c:
        row = c.execute("""
        SELECT id,user_id,username,name,product,bank,manager_name,status,
               created_at,taken_at,completed_at
        FROM applications WHERE id=?
        """, (app_id,)).fetchone()

    if not row:
        await q.answer("Заявка не найдена.", show_alert=True)
        return

    aid, uid, username, name, product, bank, manager_name, status, created, taken, completed = row
    status_text = {
        "new": "🆕 Новая",
        "in_work": "🔄 В работе",
        "completed": "✅ Завершена"
    }.get(status, status)

    text = (
        f"📄 <b>Заявка #{aid}</b>\n\n"
        f"👤 Имя: {escape(name)}\n"
        f"📱 Telegram: @{escape(username) if username else 'нет'}\n"
        f"🛍 Услуга: {escape(product)}\n"
        f"🏦 Банк: {escape(bank or 'Другой / не указан')}\n"
        f"👨‍💼 Менеджер: {escape(manager_name or 'не назначен')}\n"
        f"📌 Статус: {status_text}\n"
        f"🕐 Создана: {created}"
    )
    if taken:
        text += f"\n🔄 Взята: {taken}"
    if completed:
        text += f"\n✅ Завершена: {completed}"

    await safe_edit(q, text, app_actions(aid, status))


async def export_csv(update, context):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ только администратору.")
        return

    path = "applications_export.csv"
    with db() as c:
        rows = c.execute("""
        SELECT id,user_id,username,name,product,bank,manager_name,status,
               created_at,taken_at,completed_at
        FROM applications ORDER BY id DESC
        """).fetchall()

    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "id","user_id","username","name","product","bank","manager",
            "status","created_at","taken_at","completed_at"
        ])
        w.writerows(rows)

    with open(path, "rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename="media_bank_applications.csv"),
            caption="📤 Экспорт заявок"
        )
    try:
        os.remove(path)
    except OSError:
        pass


async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data
    uid = q.from_user.id

    if d == "main":
        context.user_data.clear()
        await safe_edit(q, "👋 <b>Media Bank</b>\n\nВыберите услугу:", main_kb(uid))

    elif d.startswith("product:"):
        key = d.split(":", 1)[1]
        title, description = PRODUCTS[key]
        context.user_data["product"] = title
        context.user_data["product_key"] = key
        context.user_data.pop("bank", None)
        await safe_edit(
            q,
            f"✨ <b>{escape(title)}</b>\n\n{escape(description)}\n\n🏦 <b>Выберите банк:</b>",
            bank_kb(key, "main")
        )

    elif d == "choose_bank":
        product_key = context.user_data.get("product_key")
        await safe_edit(q, "🏦 <b>Выберите банк</b>", bank_kb(product_key, "main"))

    elif d.startswith("bank:"):
        key = d.split(":", 1)[1]
        product_key = context.user_data.get("product_key")
        allowed = dict(BANKS_BY_PRODUCT.get(product_key, []))
        if key not in allowed:
            await q.answer("Этот банк недоступен для выбранного продукта.", show_alert=True)
            return
        context.user_data["bank"] = allowed[key]
        if context.user_data.get("product"):
            await safe_edit(
                q,
                f"🏦 Банк: <b>{escape(context.user_data['bank'])}</b>\n\n📝 Теперь оставьте заявку.",
                mk([
                    [pb("📝 Продолжить заявку", "start_form", "form")],
                    [pb("🏦 Сменить банк", "choose_bank", "bank")],
                    [pb("◀️ В меню", "main", "back")]
                ])
            )
        else:
            await safe_edit(
                q,
                f"🏦 Банк: <b>{escape(context.user_data['bank'])}</b>\n\nСначала выберите услугу.",
                mk([[pb("◀️ В меню", "main", "back")]])
            )

    elif d == "start_form":
        if not context.user_data.get("product") or context.user_data.get("product") == "Не указано":
            await safe_edit(q, "📝 <b>Заявка</b>\n\nСначала выберите услугу:", mk([
                [pb("💳 Дебетовая карта", "product:debit", "debit")],
                [pb("💰 Кредитная карта", "product:credit", "credit")],
                [pb("🏢 Регистрация бизнеса + РКО", "product:rko", "rko")],
                [pb("◀️ В меню", "main", "back")]
            ]))
            return
        if not context.user_data.get("bank"):
            await safe_edit(q, "🏦 <b>Выберите банк</b>", bank_kb(context.user_data.get("product_key"), "main"))
            return
        await ask_name(q, context)

    elif d.startswith("manager:"):
        mid = int(d.split(":")[1])
        context.user_data["manager_id"] = mid
        name = escape(context.user_data.get("name", ""))
        product = escape(context.user_data.get("product", ""))
        bank = escape(context.user_data.get("bank", "Другой / не указан"))
        username = q.from_user.username or ""
        await safe_edit(
            q,
            f"🔎 <b>Проверьте заявку</b>\n\n"
            f"👤 Имя: {name}\n"
            f"📱 Telegram: @{escape(username) if username else 'нет'}\n"
            f"🛍 Услуга: {product}\n"
            f"🏦 Банк: {bank}\n"
            f"👨‍💼 Менеджер: {escape(MANAGERS[mid])}\n\n"
            "Всё верно?",
            mk([
                [pb("✅ Отправить", "send_app", "send")],
                [pb("◀️ Назад", "start_form", "back")]
            ])
        )

    elif d == "send_app":
        await create_application(q, context)

    elif d == "manager":
        if not is_manager(uid):
            await q.answer("⛔ Доступ запрещён.", show_alert=True)
            return
        await safe_edit(q, "👨‍💼 <b>Панель менеджера</b>\n\nВыберите раздел:", manager_kb())

    elif d == "admin":
        if not is_admin(uid):
            await q.answer("⛔ Доступ запрещён.", show_alert=True)
            return
        await safe_edit(q, "⚙️ <b>Админ-панель</b>\n\nВыберите раздел:", admin_kb())

    elif d == "all_apps":
        if not is_manager(uid) and not is_admin(uid):
            await q.answer("⛔ Доступ запрещён.", show_alert=True); return
        back = "admin" if is_admin(uid) else "manager"
        await safe_edit(q, "📋 <b>Фильтр заявок</b>\n\nВыберите статус:", apps_filter_kb(back))

    elif d.startswith("apps:"):
        if not is_manager(uid) and not is_admin(uid):
            await q.answer("⛔ Доступ запрещён.", show_alert=True); return
        status=d.split(":",1)[1]
        if status == "all":
            await list_apps(q)
        else:
            # Same list UI, filtered by status.
            sql="SELECT id,name,product,bank,manager_name,status FROM applications WHERE status=? ORDER BY id DESC LIMIT 50"
            with db() as c: rows=c.execute(sql,(status,)).fetchall()
            if not rows:
                await safe_edit(q,"📋 <b>Заявок нет.</b>",mk([[pb("◀️ Назад", "admin" if is_admin(uid) else "manager", "back")]])); return
            symbols={"new":"🆕","in_work":"🔄","completed":"✅"}
            text="📋 <b>Заявки</b>\n\n"; buttons=[]
            for aid,name,product,bank,mgr,st in rows:
                text += f"{symbols.get(st,'•')} <b>#{aid}</b> — {escape(name)} — {escape(product)} — 🏦 {escape(bank or 'Другой / не указан')}\n"
                buttons.append([pb(f"🔎 Открыть #{aid}", f"view:{aid}", "open")])
            buttons.append([pb("◀️ Назад", "admin" if is_admin(uid) else "manager", "back")])
            await safe_edit(q,text,mk(buttons))

    elif d == "my_apps":
        await list_apps(q, True)

    elif d == "my_stats":
        await show_manager_stats(q)

    elif d == "my_profile":
        await show_manager_profile(q)

    elif d == "stats":
        await show_stats(q)

    elif d == "bank_stats":
        await show_bank_stats(q)

    elif d == "today_report":
        now = datetime.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        text = format_report(
            f"Отчёт за {now:%d.%m.%Y}",
            start.isoformat(timespec="seconds"),
            (start + timedelta(days=1)).isoformat(timespec="seconds")
        )
        await safe_edit(
            q, text,
            mk([[pb("◀️ Назад", "admin" if is_admin(uid) else "manager", "back")]])
        )

    elif d == "managers":
        if not is_admin(uid):
            return
        text = "👥 <b>Менеджеры</b>\n\n"
        for mid, name in MANAGERS.items():
            with db() as c:
                count = c.execute(
                    "SELECT COUNT(*) FROM applications WHERE manager_id=?",
                    (mid,)
                ).fetchone()[0]
            text += f"👨‍💼 {escape(name)} — заявок: <b>{count}</b>\n"
        buttons=[]
        for mid,name in MANAGERS.items():
            buttons.append([pb(f"📊 {name}", f"manager_stats:{mid}", "stats")])
        buttons.append([pb("◀️ Назад", "admin", "back")])
        await safe_edit(q, text, mk(buttons))

    elif d.startswith("manager_stats:"):
        if not is_admin(uid):
            await q.answer("⛔ Доступ запрещён.", show_alert=True); return
        await show_manager_stats(q, int(d.split(":",1)[1]))

    elif d == "export":
        if is_admin(uid):
            # Callback cannot directly use reply_document conveniently; use a private temporary message.
            path = "applications_export.csv"
            import csv
            with db() as c:
                rows = c.execute("""
                SELECT id,user_id,username,name,product,bank,manager_name,status,
                       created_at,taken_at,completed_at
                FROM applications ORDER BY id DESC
                """).fetchall()
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["id","user_id","username","name","product","bank","manager","status","created_at","taken_at","completed_at"])
                w.writerows(rows)
            with open(path, "rb") as f:
                await q.message.reply_document(InputFile(f, filename="media_bank_applications.csv"), caption="📤 Экспорт заявок")
            try:
                os.remove(path)
            except OSError:
                pass

    elif d.startswith("view:"):
        await view_app(q, int(d.split(":")[1]))

    elif d.startswith("take:"):
        if not is_manager(uid) and not is_admin(uid):
            return
        aid = int(d.split(":")[1])
        now = datetime.now().isoformat(timespec="seconds")
        with db() as c:
            row=c.execute("SELECT user_id FROM applications WHERE id=? AND status='new'",(aid,)).fetchone()
            c.execute("""
            UPDATE applications
            SET status='in_work', manager_id=?, manager_name=?, taken_at=?
            WHERE id=? AND status='new'
            """, (uid, MANAGERS.get(uid, "Администратор"), now, aid))
        if row:
            try:
                await context.bot.send_message(row[0], f"🔄 <b>Заявка #{aid} принята в работу</b>\n\nМенеджер уже начал обработку вашей заявки.", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
        await view_app(q, aid)

    elif d.startswith("complete:"):
        if not is_manager(uid) and not is_admin(uid):
            return
        aid = int(d.split(":")[1])
        now = datetime.now().isoformat(timespec="seconds")
        with db() as c:
            row=c.execute("SELECT user_id FROM applications WHERE id=? AND status='in_work'",(aid,)).fetchone()
            c.execute("""
            UPDATE applications SET status='completed', completed_at=?
            WHERE id=? AND status='in_work'
            """, (now, aid))
        if row:
            try:
                await context.bot.send_message(row[0], f"✅ <b>Заявка #{aid} завершена</b>\n\nСпасибо за обращение в Media Bank.", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
        await view_app(q, aid)


async def show_manager_stats(q, manager_id=None):
    uid=q.from_user.id
    if not is_manager(uid) and not is_admin(uid):
        await q.answer("⛔ Доступ запрещён.", show_alert=True); return
    target=manager_id or uid
    name=MANAGERS.get(target, "Администратор")
    with db() as c:
        total=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=?",(target,)).fetchone()[0]
        new=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='new'",(target,)).fetchone()[0]
        work=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='in_work'",(target,)).fetchone()[0]
        done=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='completed'",(target,)).fetchone()[0]
        banks=c.execute("""SELECT COALESCE(bank,'Другой / не указан'), COUNT(*),
                                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                         FROM applications WHERE manager_id=?
                         GROUP BY COALESCE(bank,'Другой / не указан') ORDER BY COUNT(*) DESC""",(target,)).fetchall()
    conv=done/total*100 if total else 0
    text=f"👤 <b>Статистика: {escape(name)}</b>\n\n📥 Всего: <b>{total}</b>\n🆕 Новых: <b>{new}</b>\n🔄 В работе: <b>{work}</b>\n✅ Завершено: <b>{done}</b>\n📈 Конверсия: <b>{conv:.1f}%</b>"
    if banks:
        text += "\n\n<b>🏦 По банкам:</b>\n" + "".join(f"• {escape(b)} — <b>{n}</b> / <b>{d or 0}</b> завершено\n" for b,n,d in banks)
    back='admin' if is_admin(uid) else 'manager'
    await safe_edit(q, text, mk([[pb("◀️ Назад", back, "back")]]))


async def show_manager_profile(q):
    uid=q.from_user.id
    if not is_manager(uid):
        await q.answer("⛔ Доступ запрещён.", show_alert=True); return
    name=MANAGERS.get(uid,"Менеджер")
    username=q.from_user.username
    with db() as c:
        total=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=?",(uid,)).fetchone()[0]
        work=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='in_work'",(uid,)).fetchone()[0]
        done=c.execute("SELECT COUNT(*) FROM applications WHERE manager_id=? AND status='completed'",(uid,)).fetchone()[0]
        banks=c.execute("""SELECT COALESCE(bank,'Другой / не указан'), COUNT(*),
                                SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                         FROM applications WHERE manager_id=?
                         GROUP BY COALESCE(bank,'Другой / не указан') ORDER BY COUNT(*) DESC""",(uid,)).fetchall()
    text=f"👤 <b>Профиль менеджера</b>\n\nИмя: <b>{escape(name)}</b>\nTelegram: @{escape(username) if username else 'нет username'}\n📥 Всего заявок: <b>{total}</b>\n🔄 В работе: <b>{work}</b>\n✅ Завершено: <b>{done}</b>"
    if banks:
        text += "\n\n<b>🏦 По банкам:</b>\n" + "".join(f"• {escape(b)} — <b>{n}</b> / <b>{d or 0}</b> завершено\n" for b,n,d in banks)
    await safe_edit(q, text, manager_profile_kb())




async def show_bank_stats(q):
    if not is_admin(q.from_user.id):
        await q.answer("⛔ Доступ только администратору.", show_alert=True)
        return
    with db() as c:
        rows = c.execute("""
            SELECT COALESCE(bank,'Другой / не указан'),
                   product,
                   COUNT(*),
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='new' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN status='in_work' THEN 1 ELSE 0 END)
            FROM applications
            GROUP BY COALESCE(bank,'Другой / не указан'), product
            ORDER BY COALESCE(bank,'Другой / не указан'), COUNT(*) DESC
        """).fetchall()
    if not rows:
        text="🏦 <b>Банки</b>\n\nЗаявок пока нет."
    else:
        text="🏦 <b>Статистика по банкам</b>\n\n"
        current=None
        for bank,product,total,done,new,work in rows:
            if bank != current:
                if current is not None:
                    text += "\n"
                current=bank
                text += f"<b>🏦 {escape(bank)}</b>\n"
            text += f"  • {escape(product)} — <b>{total}</b> всего | 🆕 {new or 0} | 🔄 {work or 0} | ✅ <b>{done or 0}</b>\n"
    await safe_edit(q,text,mk([[pb("◀️ Назад", "admin", "back")]]))


async def show_stats(q):
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        new = c.execute("SELECT COUNT(*) FROM applications WHERE status='new'").fetchone()[0]
        work = c.execute("SELECT COUNT(*) FROM applications WHERE status='in_work'").fetchone()[0]
        done = c.execute("SELECT COUNT(*) FROM applications WHERE status='completed'").fetchone()[0]
        products = c.execute("SELECT product,COUNT(*) FROM applications GROUP BY product ORDER BY COUNT(*) DESC").fetchall()
        managers = c.execute("SELECT COALESCE(manager_name,'Не назначен'),COUNT(*) FROM applications GROUP BY manager_name ORDER BY COUNT(*) DESC").fetchall()
        banks = c.execute("""SELECT COALESCE(bank,'Другой / не указан'), COUNT(*),
                                  SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
                           FROM applications GROUP BY COALESCE(bank,'Другой / не указан') ORDER BY COUNT(*) DESC""").fetchall()
    conversion = done / total * 100 if total else 0
    text=(f"📊 <b>Статистика</b>\n\nВсего заявок: <b>{total}</b>\n🆕 Новых: <b>{new}</b>\n🔄 В работе: <b>{work}</b>\n✅ Завершённых: <b>{done}</b>\n📈 Конверсия: <b>{conversion:.1f}%</b>")
    if products:
        text += "\n\n<b>По услугам:</b>\n" + "".join(f"• {escape(p)} — <b>{n}</b>\n" for p,n in products)
    if banks:
        text += "\n<b>🏦 По банкам:</b>\n" + "".join(f"• {escape(b)} — <b>{n}</b> заявок / <b>{d or 0}</b> завершено\n" for b,n,d in banks)
    if managers:
        text += "\n<b>По менеджерам:</b>\n" + "".join(f"• {escape(m)} — <b>{n}</b>\n" for m,n in managers)
    await safe_edit(q,text,mk([
        [pb("🏦 Детально по банкам", "bank_stats", "bank")],
        [pb("◀️ Назад", "admin" if is_admin(q.from_user.id) else "manager", "back")]
    ]))


async def report_cmd(update, context):
    if not is_manager(update.effective_user.id) and not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    await update.message.reply_text(
        format_report(
            f"Отчёт за {now:%d.%m.%Y}",
            start.isoformat(timespec="seconds"),
            (start + timedelta(days=1)).isoformat(timespec="seconds")
        ),
        parse_mode=ParseMode.HTML
    )


async def scheduled_reports(context):
    now = datetime.now()
    if now.hour == DAILY_REPORT_HOUR and now.minute == DAILY_REPORT_MINUTE:
        with db() as c:
            exists = c.execute(
                "SELECT COUNT(*) FROM report_log WHERE kind='daily_auto' AND created_at LIKE ?",
                (now.strftime("%Y-%m-%d") + "%",)
            ).fetchone()[0]
        if not exists:
            await send_daily_report(context.application)

    if now.weekday() == WEEKLY_REPORT_DAY and now.hour == WEEKLY_REPORT_HOUR and now.minute == WEEKLY_REPORT_MINUTE:
        with db() as c:
            exists = c.execute(
                "SELECT COUNT(*) FROM report_log WHERE kind='weekly_auto' AND created_at LIKE ?",
                (now.strftime("%Y-%m-%d") + "%",)
            ).fetchone()[0]
        if not exists:
            await send_weekly_report(context.application)


async def send_daily_report(app):
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    text = format_report(
        f"Автоотчёт за {now:%d.%m.%Y}",
        start.isoformat(timespec="seconds"),
        (start + timedelta(days=1)).isoformat(timespec="seconds")
    )
    await app.bot.send_message(REPORT_CHAT_ID, text, parse_mode=ParseMode.HTML)
    with db() as c:
        c.execute("INSERT INTO report_log(kind,created_at) VALUES(?,?)", ("daily_auto", now.isoformat(timespec="seconds")))


async def send_weekly_report(app):
    now = datetime.now()
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    text = format_report(
        f"Автоотчёт за неделю {start:%d.%m}–{now:%d.%m.%Y}",
        start.isoformat(timespec="seconds"),
        (now + timedelta(days=1)).isoformat(timespec="seconds")
    )
    await app.bot.send_message(REPORT_CHAT_ID, text, parse_mode=ParseMode.HTML)
    with db() as c:
        c.execute("INSERT INTO report_log(kind,created_at) VALUES(?,?)", ("weekly_auto", now.isoformat(timespec="seconds")))


async def error_handler(update, context):
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return
    log.error("Unhandled error", exc_info=context.error)


async def post_init(app):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Главное меню"),
            BotCommand("help", "Помощь"),
            BotCommand("panel", "Панель менеджера"),
            BotCommand("admin", "Админ-панель"),
            BotCommand("report", "Отчёт за сегодня"),
            BotCommand("myid", "Мой Telegram ID"),
        ],
        scope=BotCommandScopeAllPrivateChats()
    )
    if app.job_queue:
        app.job_queue.run_repeating(scheduled_reports, interval=60, first=10)


def build_app():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN отсутствует в .env")
    kwargs = dict(
        connect_timeout=30, read_timeout=30,
        write_timeout=30, pool_timeout=30
    )
    if PROXY:
        kwargs["proxy"] = PROXY
    request = HTTPXRequest(**kwargs)
    return (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(request)
        .post_init(post_init)
        .build()
    )


def main():
    init_db()
    app = build_app()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CommandHandler("export", export_csv))
    app.add_handler(CallbackQueryHandler(button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input))
    app.add_error_handler(error_handler)

    print("======================================")
    print(" MEDIA BANK — ULTIMATE FRESH BUILD")
    print(" Auto reports: enabled")
    print(" Premium/custom emoji: configurable")
    print("======================================")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
