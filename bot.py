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

# Premium/custom emoji support.
# Confirmed Telegram custom emoji IDs supplied by the user.
# Environment variables can override these defaults if needed.
CUSTOM_EMOJI = {
    "new": os.getenv("EMOJI_NEW", "5382357040008021292").strip(),
    "card": os.getenv("EMOJI_CARD", "5445353829304387411").strip(),
    "money": os.getenv("EMOJI_MONEY", "5287231198098117669").strip(),
    "building": os.getenv("EMOJI_BUILDING", "5278702045883292456").strip(),
    "user": os.getenv("EMOJI_USER", "5190498849440931467").strip(),
    "manager": os.getenv("EMOJI_MANAGER", "5373012449597335010").strip(),
    "chart": os.getenv("EMOJI_CHART", "5231200819986047254").strip(),
    "report": os.getenv("EMOJI_REPORT", "5244837092042750681").strip(),
    "ok": os.getenv("EMOJI_OK", "5206607081334906820").strip(),
    "work": os.getenv("EMOJI_WORK", "5386367538735104399").strip(),
}

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
        [InlineKeyboardButton("💳 Дебетовая карта", callback_data="product:debit")],
        [InlineKeyboardButton("💰 Кредитная карта", callback_data="product:credit")],
        [InlineKeyboardButton("🏢 Регистрация бизнеса + РКО", callback_data="product:rko")],
        [InlineKeyboardButton("📝 Оставить заявку", callback_data="start_form")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")])
    elif is_manager(uid):
        rows.append([InlineKeyboardButton("👨‍💼 Панель менеджера", callback_data="manager")])
    return mk(rows)


def product_kb():
    return mk([
        [InlineKeyboardButton("📝 Оставить заявку", callback_data="start_form")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main")]
    ])


def manager_kb():
    return mk([
        [InlineKeyboardButton("🗂 Мои заявки", callback_data="my_apps")],
        [InlineKeyboardButton("📋 Все заявки", callback_data="all_apps")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📈 Отчёт за сегодня", callback_data="today_report")],
        [InlineKeyboardButton("◀️ В меню", callback_data="main")]
    ])


def admin_kb():
    return mk([
        [InlineKeyboardButton("📋 Все заявки", callback_data="all_apps")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("📈 Отчёт за сегодня", callback_data="today_report")],
        [InlineKeyboardButton("👥 Менеджеры", callback_data="managers")],
        [InlineKeyboardButton("📤 Экспорт CSV", callback_data="export")],
        [InlineKeyboardButton("◀️ В меню", callback_data="main")]
    ])


def app_actions(app_id, status):
    rows = []
    if status == "new":
        rows.append([InlineKeyboardButton("👤 Взять в работу", callback_data=f"take:{app_id}")])
    elif status == "in_work":
        rows.append([InlineKeyboardButton("✅ Завершить", callback_data=f"complete:{app_id}")])
    rows.append([InlineKeyboardButton("🔎 Открыть", callback_data=f"view:{app_id}")])
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
        mk([[InlineKeyboardButton("◀️ В меню", callback_data="main")]])
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
            [InlineKeyboardButton("Эдуард", callback_data="manager:6045840701")],
            [InlineKeyboardButton("Александр", callback_data="manager:8923153510")],
            [InlineKeyboardButton("◀️ В меню", callback_data="main")]
        ])
    )


async def create_application(q, context):
    uid = q.from_user.id
    name = context.user_data.get("name", "").strip()
    product = context.user_data.get("product", "Не указано")
    manager_id = context.user_data.get("manager_id")
    manager_name = MANAGERS.get(manager_id, "")
    username = q.from_user.username or ""
    now = datetime.now().isoformat(timespec="seconds")

    with db() as c:
        cur = c.execute("""
        INSERT INTO applications
        (user_id,username,name,product,manager_id,manager_name,status,created_at)
        VALUES(?,?,?,?,?,?,?,?)
        """, (
            uid, username, name, product, manager_id,
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
        f"{custom_emoji('manager', '👨‍💼')} Менеджер: <b>{escape(manager_name)}</b>\n"
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

    return total, new, work, done, manager_rows, product_rows


def format_report(title, start=None, end=None):
    total, new, work, done, managers, products = report_data(start, end)
    conversion = done / total * 100 if total else 0

    text = (
        f"{custom_emoji('report', '📈')} <b>{escape(title)}</b>\n\n"
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
    SELECT id,name,product,manager_name,status
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
            mk([[InlineKeyboardButton(
                "◀️ Назад",
                callback_data="admin" if is_admin(q.from_user.id) else "manager"
            )]])
        )
        return

    symbols = {"new": custom_emoji("new", "🆕"), "in_work": custom_emoji("work", "🔄"), "completed": custom_emoji("ok", "✅")}
    text = "📋 <b>Заявки</b>\n\n"
    buttons = []

    for aid, name, product, manager_name, status in rows:
        text += (
            f"{symbols.get(status, '•')} <b>#{aid}</b> — "
            f"{escape(name)} — {escape(product)}\n"
        )
        buttons.append([
            InlineKeyboardButton(
                f"🔎 Открыть #{aid}",
                callback_data=f"view:{aid}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "◀️ Назад",
            callback_data="admin" if is_admin(q.from_user.id) else "manager"
        )
    ])
    await safe_edit(q, text, mk(buttons))


async def view_app(q, app_id):
    if not is_manager(q.from_user.id) and not is_admin(q.from_user.id):
        await q.answer("⛔ Доступ запрещён.", show_alert=True)
        return

    with db() as c:
        row = c.execute("""
        SELECT id,user_id,username,name,product,manager_name,status,
               created_at,taken_at,completed_at
        FROM applications WHERE id=?
        """, (app_id,)).fetchone()

    if not row:
        await q.answer("Заявка не найдена.", show_alert=True)
        return

    aid, uid, username, name, product, manager_name, status, created, taken, completed = row
    status_text = {
        "new": f"{custom_emoji('new', '🆕')} Новая",
        "in_work": f"{custom_emoji('work', '🔄')} В работе",
        "completed": f"{custom_emoji('ok', '✅')} Завершена"
    }.get(status, status)

    text = (
        f"📄 <b>Заявка #{aid}</b>\n\n"
        f"👤 Имя: {escape(name)}\n"
        f"📱 Telegram: @{escape(username) if username else 'нет'}\n"
        f"🛍 Услуга: {escape(product)}\n"
        f"{custom_emoji('manager', '👨‍💼')} Менеджер: {escape(manager_name or 'не назначен')}\n"
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
        SELECT id,user_id,username,name,product,manager_name,status,
               created_at,taken_at,completed_at
        FROM applications ORDER BY id DESC
        """).fetchall()

    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([
            "id","user_id","username","name","product","manager",
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
        await safe_edit(
            q,
            f"✨ <b>{escape(title)}</b>\n\n{escape(description)}",
            product_kb()
        )

    elif d == "start_form":
        context.user_data.setdefault("product", "Не указано")
        await ask_name(q, context)

    elif d.startswith("manager:"):
        mid = int(d.split(":")[1])
        context.user_data["manager_id"] = mid
        name = escape(context.user_data.get("name", ""))
        product = escape(context.user_data.get("product", ""))
        username = q.from_user.username or ""
        await safe_edit(
            q,
            f"🔎 <b>Проверьте заявку</b>\n\n"
            f"👤 Имя: {name}\n"
            f"📱 Telegram: @{escape(username) if username else 'нет'}\n"
            f"🛍 Услуга: {product}\n"
            f"👨‍💼 Менеджер: {escape(MANAGERS[mid])}\n\n"
            "Всё верно?",
            mk([
                [InlineKeyboardButton("✅ Отправить", callback_data="send_app")],
                [InlineKeyboardButton("◀️ Назад", callback_data="start_form")]
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
        await list_apps(q)

    elif d == "my_apps":
        await list_apps(q, True)

    elif d == "stats":
        await show_stats(q)

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
            mk([[InlineKeyboardButton(
                "◀️ Назад",
                callback_data="admin" if is_admin(uid) else "manager"
            )]])
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
        await safe_edit(q, text, mk([[InlineKeyboardButton("◀️ Назад", callback_data="admin")]]))

    elif d == "export":
        if is_admin(uid):
            # Callback cannot directly use reply_document conveniently; use a private temporary message.
            path = "applications_export.csv"
            import csv
            with db() as c:
                rows = c.execute("""
                SELECT id,user_id,username,name,product,manager_name,status,
                       created_at,taken_at,completed_at
                FROM applications ORDER BY id DESC
                """).fetchall()
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["id","user_id","username","name","product","manager","status","created_at","taken_at","completed_at"])
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
            c.execute("""
            UPDATE applications
            SET status='in_work', manager_id=?, manager_name=?, taken_at=?
            WHERE id=? AND status='new'
            """, (uid, MANAGERS.get(uid, "Администратор"), now, aid))
        await view_app(q, aid)

    elif d.startswith("complete:"):
        if not is_manager(uid) and not is_admin(uid):
            return
        aid = int(d.split(":")[1])
        now = datetime.now().isoformat(timespec="seconds")
        with db() as c:
            c.execute("""
            UPDATE applications SET status='completed', completed_at=?
            WHERE id=? AND status='in_work'
            """, (now, aid))
        await view_app(q, aid)


async def show_stats(q):
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        new = c.execute("SELECT COUNT(*) FROM applications WHERE status='new'").fetchone()[0]
        work = c.execute("SELECT COUNT(*) FROM applications WHERE status='in_work'").fetchone()[0]
        done = c.execute("SELECT COUNT(*) FROM applications WHERE status='completed'").fetchone()[0]
    conversion = done / total * 100 if total else 0
    await safe_edit(
        q,
        f"{custom_emoji('chart', '📊')} <b>Статистика</b>\n\n"
        f"Всего заявок: <b>{total}</b>\n"
        f"🆕 Новых: <b>{new}</b>\n"
        f"🔄 В работе: <b>{work}</b>\n"
        f"✅ Завершённых: <b>{done}</b>\n"
        f"📈 Конверсия: <b>{conversion:.1f}%</b>",
        mk([[InlineKeyboardButton(
            "◀️ Назад",
            callback_data="admin" if is_admin(q.from_user.id) else "manager"
        )]])
    )


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
