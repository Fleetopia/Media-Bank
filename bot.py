import os
import csv
import io
import sqlite3
import logging
import shutil
from datetime import datetime, timezone
from html import escape

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    MenuButtonCommands,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# MEDIA BANK — CLEAN REBUILD
# Telegram bot / заявки / менеджеры / статистика / отчёты
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
REPORT_CHAT_ID = int(os.getenv("REPORT_CHAT_ID", str(GROUP_ID)))
DB_FILE = os.getenv("DB_FILE", "media_bank.db")

# Premium button icons:
# 1 / true / yes / on = enabled
ENABLE_BUTTON_PREMIUM = os.getenv(
    "ENABLE_BUTTON_PREMIUM", "1"
).strip().lower() in {"1", "true", "yes", "on"}

DAILY_REPORT_HOUR = int(os.getenv("DAILY_REPORT_HOUR", "21"))
DAILY_REPORT_MINUTE = int(os.getenv("DAILY_REPORT_MINUTE", "0"))
WEEKLY_REPORT_DAY = int(os.getenv("WEEKLY_REPORT_DAY", "0"))
WEEKLY_REPORT_HOUR = int(os.getenv("WEEKLY_REPORT_HOUR", "21"))
WEEKLY_REPORT_MINUTE = int(os.getenv("WEEKLY_REPORT_MINUTE", "5"))

ADMIN_IDS = {6045840701}

MANAGERS = {
    6045840701: "Эдуард",
    8923153510: "Александр",
}

PRODUCTS = {
    "debit": "Дебетовая карта",
    "credit": "Кредитная карта",
    "rko": "Регистрация бизнеса + РКО",
}

BANKS = {
    "debit": [
        "Т-Банк",
        "Альфа-Банк",
        "ВТБ-Банк",
        "Промсвязьбанк",
        "Ак Барс Банк",
        "ОТП банк",
    ],
    "credit": [
        "Т-Банк",
        "ВТБ",
        "Уралсиб",
        "ОТП-Банк",
        "Яндекс — Кредитная карта супер Сплит",
        "Альфа-Банк",
    ],
    "rko": [
        "Альфа-Банк",
        "Промсвязьбанк",
        "РКО от Санкт-Петербург Банка",
        "УБРиР Банк",
    ],
}

# ============================================================
# ВАЖНЫЕ CUSTOM EMOJI ID
# ============================================================

EMOJI = {
    # Review screen — закреплено по запросу
    "review": "5197269100878907942",
    "debit": "5445353829304387411",
    "credit": "5287231198098117669",
    "rko": "5278702045883292456",
    "bank": "5332455502917949981",

    # Имя / Telegram
    "user": "5373012449597335010",

    # Менеджер
    "manager": "5190498849440931467",

    # Остальные уже использовавшиеся ID
    "welcome": "5438496463044752972",
    "back": "5206607081334906820",
    "send": "5206607081334906820",
    "edit": "5382194935057372936",
    "cancel": "5253742260054409879",
    "new": "5382357040008021292",
    "work": "5386367538735104399",
    "done": "5206607081334906820",
    "open": "5190498849440931467",
    "apps": "5444856076954520455",
    "admin": "5190498849440931467",
    "stats": "5206607081334906820",
    "report": "5190498849440931467",
    "export": "5274055917766202507",
}

# Отдельная карта именно для INLINE-КНОПОК.
# Здесь больше нет зависимости от старого массива индексов.
BUTTON_ICONS = {
    "debit": EMOJI["debit"],
    "credit": EMOJI["credit"],
    "rko": EMOJI["rko"],
    "form": EMOJI["review"],
    "review": EMOJI["review"],
    "bank": EMOJI["bank"],
    "manager": EMOJI["manager"],
    "user": EMOJI["user"],
    "back": EMOJI["back"],
    "send": EMOJI["send"],
    "edit": EMOJI["edit"],
    "cancel": EMOJI["cancel"],
    "new": EMOJI["new"],
    "work": EMOJI["work"],
    "done": EMOJI["done"],
    "open": EMOJI["open"],
    "apps": EMOJI["apps"],
    "admin": EMOJI["admin"],
    "stats": EMOJI["stats"],
    "report": EMOJI["report"],
    "export": EMOJI["export"],
}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("media_bank")


# ============================================================
# HELPERS
# ============================================================

def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def emoji(key, fallback="•"):
    eid = EMOJI.get(key)
    if not eid:
        return escape(fallback)
    return f'<tg-emoji emoji-id="{eid}">{escape(fallback)}</tg-emoji>'


def btn(text, key=None, data=None):
    kwargs = {
        "text": text,
        "callback_data": data,
    }

    # Premium icon перед текстом кнопки.
    # Если Telegram не даёт показать иконку из-за ограничений
    # аккаунта владельца бота, сама кнопка всё равно работает.
    if ENABLE_BUTTON_PREMIUM and key in BUTTON_ICONS:
        kwargs["icon_custom_emoji_id"] = BUTTON_ICONS[key]

    return InlineKeyboardButton(**kwargs)


def db():
    return sqlite3.connect(DB_FILE)


# ============================================================
# DATABASE
# ============================================================

def init_db():
    directory = os.path.dirname(os.path.abspath(DB_FILE))
    os.makedirs(directory, exist_ok=True)

    with db() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS applications(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT,
                product TEXT NOT NULL,
                bank TEXT NOT NULL,
                client_name TEXT NOT NULL,
                contact TEXT NOT NULL,
                manager TEXT NOT NULL,
                manager_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                taken_at TEXT,
                completed_at TEXT
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_seen TEXT NOT NULL
            )
            """
        )

        # Безопасные миграции старых баз.
        app_cols = {
            row[1]
            for row in c.execute("PRAGMA table_info(applications)").fetchall()
        }

        migrations = {
            "manager_id": "ALTER TABLE applications ADD COLUMN manager_id INTEGER DEFAULT 0",
            "status": "ALTER TABLE applications ADD COLUMN status TEXT DEFAULT 'new'",
            "taken_at": "ALTER TABLE applications ADD COLUMN taken_at TEXT",
            "completed_at": "ALTER TABLE applications ADD COLUMN completed_at TEXT",
        }

        for col, sql in migrations.items():
            if col not in app_cols:
                c.execute(sql)

        user_cols = {
            row[1]
            for row in c.execute("PRAGMA table_info(users)").fetchall()
        }

        if "first_seen" not in user_cols:
            c.execute("ALTER TABLE users ADD COLUMN first_seen TEXT")
            c.execute(
                "UPDATE users SET first_seen=? WHERE first_seen IS NULL",
                (now(),),
            )

        c.commit()


def backup_database():
    if not os.path.exists(DB_FILE):
        return

    backup_dir = os.path.join(
        os.path.dirname(os.path.abspath(DB_FILE)),
        "backups",
    )
    os.makedirs(backup_dir, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    destination = os.path.join(
        backup_dir,
        f"media_bank_{stamp}.db",
    )

    try:
        shutil.copy2(DB_FILE, destination)

        files = sorted(
            [
                os.path.join(backup_dir, x)
                for x in os.listdir(backup_dir)
                if x.endswith(".db")
            ],
            key=os.path.getmtime,
            reverse=True,
        )

        for old in files[14:]:
            try:
                os.remove(old)
            except OSError:
                pass

        log.info("Database backup created: %s", destination)
    except Exception:
        log.exception("Database backup failed")


# ============================================================
# KEYBOARDS
# ============================================================

def home_kb(uid):
    rows = [
        [btn("Дебетовая карта", "debit", "product:debit")],
        [btn("Кредитная карта", "credit", "product:credit")],
        [btn("Регистрация бизнеса + РКО", "rko", "product:rko")],
        [btn("Оставить заявку", "form", "form")],
    ]

    # ВАЖНО:
    # Эдуард одновременно администратор и менеджер,
    # поэтому ему показываются обе панели.
    if uid in ADMIN_IDS:
        rows.append([btn("Админ-панель", "admin", "admin")])

    if uid in MANAGERS:
        rows.append([btn("Панель менеджера", "manager", "manager_panel")])

    return InlineKeyboardMarkup(rows)


def product_kb():
    return InlineKeyboardMarkup(
        [
            [btn("Дебетовая карта", "debit", "product:debit")],
            [btn("Кредитная карта", "credit", "product:credit")],
            [btn("Регистрация бизнеса + РКО", "rko", "product:rko")],
            [btn("Назад", "back", "home")],
        ]
    )


def bank_kb(product):
    rows = [
        [btn(bank, "bank", f"bank:{product}:{i}")]
        for i, bank in enumerate(BANKS[product])
    ]
    rows.append([btn("Назад", "back", "home")])
    return InlineKeyboardMarkup(rows)


def manager_kb():
    return InlineKeyboardMarkup(
        [
            [btn("Эдуард", "manager", "mgr:6045840701")],
            [btn("Александр", "manager", "mgr:8923153510")],
            [btn("Назад", "back", "home")],
        ]
    )


def form_kb():
    return InlineKeyboardMarkup(
        [[btn("Назад", "back", "home")]]
    )


def review_kb():
    return InlineKeyboardMarkup(
        [
            [btn("Отправить заявку", "send", "submit")],
            [btn("Изменить", "edit", "edit")],
            [btn("Отмена", "cancel", "cancel")],
        ]
    )


def application_actions(app_id, status_value):
    rows = []

    if status_value == "new":
        rows.append(
            [btn("Взять в работу", "work", f"take:{app_id}")]
        )
    elif status_value == "in_work":
        rows.append(
            [btn("Завершить", "done", f"complete:{app_id}")]
        )

    rows.append(
        [btn("Открыть", "open", f"view:{app_id}")]
    )

    return InlineKeyboardMarkup(rows)


def admin_kb():
    return InlineKeyboardMarkup(
        [
            [btn("Все заявки", "apps", "apps:all")],
            [
                btn("Новые", "new", "apps:new"),
                btn("В работе", "work", "apps:in_work"),
                btn("Завершённые", "done", "apps:completed"),
            ],
            [
                btn("Статистика", "stats", "stats"),
                btn("Отчёт", "report", "report"),
            ],
            [btn("Менеджеры", "manager", "managers")],
            [btn("Экспорт CSV", "export", "export")],
            [btn("Главное меню", "back", "home")],
        ]
    )


def manager_panel_kb():
    return InlineKeyboardMarkup(
        [
            [btn("Мои заявки", "apps", "myapps")],
            [btn("Моя статистика", "stats", "mystats")],
            [btn("Мой отчёт", "report", "myreport")],
            [btn("Главное меню", "back", "home")],
        ]
    )


# ============================================================
# UI
# ============================================================

async def edit_message(query, text, keyboard):
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    except Exception as exc:
        if "Message is not modified" not in str(exc):
            raise


async def start(update, context):
    user = update.effective_user
    context.user_data.clear()

    with db() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO users(
                user_id, username, first_seen
            )
            VALUES(
                ?, ?,
                COALESCE(
                    (SELECT first_seen FROM users WHERE user_id=?),
                    ?
                )
            )
            """,
            (
                user.id,
                user.username or "",
                user.id,
                now(),
            ),
        )
        c.commit()

    text = (
        emoji("welcome", "⭐")
        + " <b>Media Bank</b>\n\n"
        + "Выберите интересующую вас услугу:"
    )

    if update.callback_query:
        await edit_message(
            update.callback_query,
            text,
            home_kb(user.id),
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=home_kb(user.id),
        )


def review_text(data):
    product = data["product"]

    # КЛЮЧЕВОЕ МЕСТО:
    # продукт выбирает СВОЙ Premium custom emoji ID.
    product_icon = emoji(product, "•")

    return (
        emoji("review", "📝")
        + " <b>Проверьте заявку</b>\n\n"
        + f"{product_icon} <b>Продукт:</b> "
        + escape(PRODUCTS[product])
        + "\n"
        + f'{emoji("bank", "🏦")} <b>Банк:</b> '
        + escape(data["bank"])
        + "\n"
        + f'{emoji("user", "👤")} <b>Имя:</b> '
        + escape(data["client_name"])
        + "\n"
        + f'{emoji("user", "👤")} <b>Telegram:</b> '
        + escape(data["contact"])
        + "\n"
        + f'{emoji("manager", "🤝")} <b>Менеджер:</b> '
        + escape(data["manager"])
    )


# ============================================================
# STATS / REPORTS
# ============================================================

async def stats_text(manager_id=None):
    where = ""
    args = ()

    if manager_id is not None:
        where = " AND manager_id=?"
        args = (manager_id,)

    with db() as c:
        def count(condition):
            return c.execute(
                "SELECT COUNT(*) FROM applications "
                "WHERE " + condition + where,
                args,
            ).fetchone()[0]

        total = count("1=1")
        new_count = count("status='new'")
        work_count = count("status='in_work'")
        done_count = count("status='completed'")

        # Счётчик "Сегодня" оставлен отдельным и не меняется.
        today_count = count(
            "date(created_at)=date('now')"
        )

    title = emoji("stats", "📊") + " <b>Статистика</b>"

    if manager_id is not None:
        title += f" · {escape(MANAGERS.get(manager_id, 'Менеджер'))}"

    return (
        f"{title}\n\n"
        f"📋 Всего: <b>{total}</b>\n"
        f'{emoji("new", "🆕")} Новых: <b>{new_count}</b>\n'
        f'{emoji("work", "🔄")} В работе: <b>{work_count}</b>\n'
        f'{emoji("done", "✅")} Завершено: <b>{done_count}</b>\n'
        f"Сегодня: <b>{today_count}</b>"
    )


async def send_report(bot, manager_id=None, destination=None):
    destination = destination or REPORT_CHAT_ID

    where = 'created_at >= datetime("now","-7 day")'
    args = []

    if manager_id is not None:
        where += " AND manager_id=?"
        args.append(manager_id)

    with db() as c:
        rows = c.execute(
            f"""
            SELECT manager, COUNT(*),
                   SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END)
            FROM applications
            WHERE {where}
            GROUP BY manager
            ORDER BY manager
            """,
            args,
        ).fetchall()

    title = emoji("report", "📈") + " <b>Отчёт Media Bank за 7 дней</b>\n\n"

    if rows:
        body = "".join(
            f"• <b>{escape(row[0])}</b>: "
            f"{row[1]} заявок, "
            f"{row[2] or 0} завершено\n"
            for row in rows
        )
    else:
        body = "Заявок за период нет."

    await bot.send_message(
        destination,
        title + body,
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# APPLICATION SUBMISSION
# ============================================================

async def submit_application(update, context):
    data = context.user_data
    user = update.effective_user
    created = now()

    with db() as c:
        cur = c.execute(
            """
            INSERT INTO applications(
                created_at,
                user_id,
                username,
                product,
                bank,
                client_name,
                contact,
                manager,
                manager_id,
                status
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                created,
                user.id,
                user.username or "",
                data["product"],
                data["bank"],
                data["client_name"],
                data["contact"],
                data["manager"],
                data["manager_id"],
                "new",
            ),
        )

        application_id = cur.lastrowid
        c.commit()

    group_text = (
        emoji("new", "🆕")
        + f" <b>Новая заявка #{application_id}</b>\n\n"
        + f'{emoji("review", "📝")} <b>Продукт:</b> '
        + escape(PRODUCTS[data["product"]])
        + "\n"
        + f'{emoji("bank", "🏦")} <b>Банк:</b> '
        + escape(data["bank"])
        + "\n"
        + f'{emoji("user", "👤")} <b>Имя:</b> '
        + escape(data["client_name"])
        + "\n"
        + f'{emoji("user", "👤")} <b>Telegram:</b> '
        + escape(data["contact"])
        + "\n"
        + f'{emoji("manager", "🤝")} <b>Менеджер:</b> '
        + escape(data["manager"])
        + "\n"
        + f'{emoji("new", "🆕")} <b>Статус:</b> Новая\n'
        + f"<i>{escape(created)}</i>"
    )

    await context.bot.send_message(
        GROUP_ID,
        group_text,
        parse_mode=ParseMode.HTML,
        reply_markup=application_actions(
            application_id,
            "new",
        ),
    )

    context.user_data.clear()

    await update.effective_message.reply_text(
        emoji("done", "✅")
        + " <b>Заявка отправлена!</b>\n\n"
        + f"Номер заявки: <b>#{application_id}</b>\n"
        + "Менеджер свяжется с вами.",
        parse_mode=ParseMode.HTML,
        reply_markup=home_kb(user.id),
    )


# ============================================================
# USER FORM
# ============================================================

async def text_input(update, context):
    step = context.user_data.get("step")
    value = (update.message.text or "").strip()

    if not step or not value:
        return

    if step == "name":
        context.user_data["client_name"] = value
        context.user_data["step"] = "contact"

        await update.message.reply_text(
            emoji("user", "👤")
            + " <b>Введите Telegram клиента:</b>\n"
            + "Например: @username",
            parse_mode=ParseMode.HTML,
            reply_markup=form_kb(),
        )

    elif step == "contact":
        context.user_data["contact"] = value
        context.user_data["step"] = "manager"

        await update.message.reply_text(
            emoji("manager", "🤝")
            + " <b>Выберите менеджера:</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=manager_kb(),
        )


# ============================================================
# APPLICATION LIST / VIEW
# ============================================================

async def applications_list(
    update,
    context,
    status_filter="all",
    manager_id=None,
):
    user_id = update.effective_user.id

    if manager_id is None and user_id not in ADMIN_IDS:
        manager_id = user_id

    conditions = []
    args = []

    if status_filter != "all":
        conditions.append("status=?")
        args.append(status_filter)

    if manager_id is not None:
        conditions.append("manager_id=?")
        args.append(manager_id)

    where = ""
    if conditions:
        where = " WHERE " + " AND ".join(conditions)

    with db() as c:
        rows = c.execute(
            f"""
            SELECT id, product, bank, client_name, manager, status
            FROM applications
            {where}
            ORDER BY id DESC
            LIMIT 30
            """,
            args,
        ).fetchall()

    back_callback = (
        "admin"
        if user_id in ADMIN_IDS
        else "manager_panel"
    )

    keyboard = [
        [
            btn(
                f"Открыть #{row[0]}",
                "open",
                f"view:{row[0]}",
            )
        ]
        for row in rows
    ]

    keyboard.append(
        [btn("Назад", "back", back_callback)]
    )

    if rows:
        body = "\n\n".join(
            f"<b>#{row[0]}</b> · {escape(row[3])}\n"
            f"{escape(PRODUCTS.get(row[1], row[1]))} · "
            f"{escape(row[2])}\n"
            f"{escape(row[4])} · {escape(row[5])}"
            for row in rows
        )
    else:
        body = "Заявок пока нет."

    text = (
        emoji("apps", "📋")
        + " <b>Заявки</b>\n\n"
        + body
    )

    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await edit_message(
            update.callback_query,
            text,
            markup,
        )
    else:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


async def view_application(query, application_id):
    with db() as c:
        row = c.execute(
            """
            SELECT
                id,
                created_at,
                product,
                bank,
                client_name,
                contact,
                manager,
                status
            FROM applications
            WHERE id=?
            """,
            (application_id,),
        ).fetchone()

    if not row:
        await query.answer(
            "Заявка не найдена",
            show_alert=True,
        )
        return

    text = (
        emoji("open", "📂")
        + f" <b>Заявка #{row[0]}</b>\n\n"
        + f'{emoji("review", "📝")} <b>Продукт:</b> '
        + escape(PRODUCTS.get(row[2], row[2]))
        + "\n"
        + f'{emoji("bank", "🏦")} <b>Банк:</b> '
        + escape(row[3])
        + "\n"
        + f'{emoji("user", "👤")} <b>Имя:</b> '
        + escape(row[4])
        + "\n"
        + f'{emoji("user", "👤")} <b>Telegram:</b> '
        + escape(row[5])
        + "\n"
        + f'{emoji("manager", "🤝")} <b>Менеджер:</b> '
        + escape(row[6])
        + "\n\n"
        + f"<b>Статус:</b> {escape(row[7])}\n"
        + f"<b>Создана:</b> {escape(row[1])}"
    )

    await edit_message(
        query,
        text,
        application_actions(
            row[0],
            row[7],
        ),
    )


# ============================================================
# CSV
# ============================================================

async def export_csv(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return

    with db() as c:
        rows = c.execute(
            """
            SELECT
                id,
                created_at,
                product,
                bank,
                client_name,
                contact,
                manager,
                status
            FROM applications
            ORDER BY id DESC
            """
        ).fetchall()

    stream = io.StringIO()
    writer = csv.writer(stream)

    writer.writerow(
        [
            "ID",
            "Created",
            "Product",
            "Bank",
            "Client",
            "Telegram",
            "Manager",
            "Status",
        ]
    )

    writer.writerows(rows)

    document = io.BytesIO(
        stream.getvalue().encode("utf-8-sig")
    )
    document.name = "media_bank_applications.csv"

    await update.effective_message.reply_document(
        document,
        caption="Экспорт заявок Media Bank",
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback(update, context):
    query = update.callback_query
    await query.answer()

    data = query.data
    user = query.from_user

    if data == "home":
        return await start(update, context)

    if data == "form":
        context.user_data.clear()

        return await edit_message(
            query,
            emoji("review", "📝")
            + " <b>Выберите продукт:</b>",
            product_kb(),
        )

    if data.startswith("product:"):
        product = data.split(":", 1)[1]

        if product not in PRODUCTS:
            return

        context.user_data["product"] = product

        return await edit_message(
            query,
            emoji(product, "•")
            + f" <b>{escape(PRODUCTS[product])}</b>\n\n"
            + "Выберите банк:",
            bank_kb(product),
        )

    if data.startswith("bank:"):
        _, product, index = data.split(":", 2)

        if product not in BANKS:
            return

        try:
            bank = BANKS[product][int(index)]
        except (ValueError, IndexError):
            return

        context.user_data.update(
            {
                "product": product,
                "bank": bank,
                "step": "name",
            }
        )

        return await edit_message(
            query,
            emoji("user", "👤")
            + " <b>Введите имя клиента:</b>",
            form_kb(),
        )

    if data == "edit":
        context.user_data["step"] = "name"

        return await edit_message(
            query,
            emoji("user", "👤")
            + " <b>Введите имя клиента заново:</b>",
            form_kb(),
        )

    if data == "cancel":
        context.user_data.clear()
        return await start(update, context)

    if data.startswith("mgr:"):
        manager_id = int(data.split(":", 1)[1])

        if manager_id not in MANAGERS:
            return

        context.user_data.update(
            {
                "manager_id": manager_id,
                "manager": MANAGERS[manager_id],
                "step": "review",
            }
        )

        return await edit_message(
            query,
            review_text(context.user_data),
            review_kb(),
        )

    if data == "submit":
        return await submit_application(
            update,
            context,
        )

    # ---------------- ADMIN ----------------

    if data == "admin" and user.id in ADMIN_IDS:
        return await edit_message(
            query,
            emoji("admin", "⚙️")
            + " <b>Админ-панель</b>",
            admin_kb(),
        )

    if data == "stats" and user.id in ADMIN_IDS:
        return await edit_message(
            query,
            await stats_text(),
            admin_kb(),
        )

    if data == "report" and user.id in ADMIN_IDS:
        await send_report(context.bot)
        return await query.answer(
            "Отчёт отправлен",
            show_alert=True,
        )

    if data == "managers" and user.id in ADMIN_IDS:
        keyboard = [
            [
                btn(
                    manager_name,
                    "manager",
                    f"mstats:{manager_id}",
                )
            ]
            for manager_id, manager_name in MANAGERS.items()
        ]

        keyboard.append(
            [btn("Назад", "back", "admin")]
        )

        return await edit_message(
            query,
            emoji("manager", "🤝")
            + " <b>Менеджеры</b>",
            InlineKeyboardMarkup(keyboard),
        )

    if data.startswith("mstats:") and user.id in ADMIN_IDS:
        manager_id = int(data.split(":", 1)[1])

        return await edit_message(
            query,
            await stats_text(manager_id),
            InlineKeyboardMarkup(
                [[btn("Назад", "back", "managers")]]
            ),
        )

    if data == "export" and user.id in ADMIN_IDS:
        return await export_csv(update, context)

    # ---------------- MANAGER ----------------

    if data == "manager_panel" and user.id in MANAGERS:
        return await edit_message(
            query,
            emoji("manager", "🤝")
            + " <b>Панель менеджера</b>",
            manager_panel_kb(),
        )

    if data == "mystats" and user.id in MANAGERS:
        return await edit_message(
            query,
            await stats_text(user.id),
            manager_panel_kb(),
        )

    if data == "myreport" and user.id in MANAGERS:
        await send_report(
            context.bot,
            manager_id=user.id,
        )

        return await query.answer(
            "Ваш отчёт отправлен",
            show_alert=True,
        )

    if data == "myapps" and user.id in MANAGERS:
        return await applications_list(
            update,
            context,
            "all",
            user.id,
        )

    # ---------------- APPLICATIONS ----------------

    if data.startswith("apps:"):
        status_filter = data.split(":", 1)[1]

        if user.id not in ADMIN_IDS and user.id not in MANAGERS:
            return

        return await applications_list(
            update,
            context,
            status_filter,
        )

    if data.startswith("view:"):
        return await view_application(
            query,
            int(data.split(":", 1)[1]),
        )

    if data.startswith("take:") and user.id in MANAGERS:
        application_id = int(data.split(":", 1)[1])

        with db() as c:
            c.execute(
                """
                UPDATE applications
                SET status='in_work',
                    taken_at=?
                WHERE id=?
                  AND status='new'
                """,
                (now(), application_id),
            )
            c.commit()

        return await view_application(
            query,
            application_id,
        )

    if data.startswith("complete:") and user.id in MANAGERS:
        application_id = int(data.split(":", 1)[1])

        with db() as c:
            c.execute(
                """
                UPDATE applications
                SET status='completed',
                    completed_at=?
                WHERE id=?
                  AND manager_id=?
                """,
                (
                    now(),
                    application_id,
                    user.id,
                ),
            )
            c.commit()

        return await view_application(
            query,
            application_id,
        )


# ============================================================
# COMMANDS
# ============================================================

async def stats_command(update, context):
    user = update.effective_user

    if user.id in ADMIN_IDS:
        await update.message.reply_text(
            await stats_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=admin_kb(),
        )
    elif user.id in MANAGERS:
        await update.message.reply_text(
            await stats_text(user.id),
            parse_mode=ParseMode.HTML,
            reply_markup=manager_panel_kb(),
        )


async def report_command(update, context):
    if update.effective_user.id not in ADMIN_IDS:
        return

    await send_report(context.bot)

    await update.message.reply_text(
        "Отчёт отправлен в рабочую группу."
    )


# ============================================================
# SCHEDULE
# ============================================================

async def scheduled_jobs(context):
    current = datetime.now(timezone.utc)

    if (
        current.hour == DAILY_REPORT_HOUR
        and current.minute == DAILY_REPORT_MINUTE
    ):
        await send_report(context.bot)

    if (
        current.weekday() == WEEKLY_REPORT_DAY
        and current.hour == WEEKLY_REPORT_HOUR
        and current.minute == WEEKLY_REPORT_MINUTE
    ):
        await send_report(context.bot)

    # Резервная копия БД раз в сутки.
    if current.hour == 3 and current.minute == 0:
        backup_database()


# ============================================================
# TELEGRAM SETUP
# ============================================================

async def post_init(application):
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Главное меню"),
            BotCommand("stats", "Статистика"),
            BotCommand("report", "Отчёт"),
        ]
    )

    await application.bot.set_chat_menu_button(
        menu_button=MenuButtonCommands()
    )

    log.info(
        "Premium button icons: %s",
        ENABLE_BUTTON_PREMIUM,
    )

    log.info(
        "Premium review IDs: review=%s debit=%s credit=%s rko=%s bank=%s",
        EMOJI["review"],
        EMOJI["debit"],
        EMOJI["credit"],
        EMOJI["rko"],
        EMOJI["bank"],
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing in environment"
        )

    if not GROUP_ID:
        raise RuntimeError(
            "GROUP_ID is missing in environment"
        )

    init_db()
    backup_database()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )
    application.add_handler(
        CommandHandler("stats", stats_command)
    )
    application.add_handler(
        CommandHandler("report", report_command)
    )
    application.add_handler(
        CallbackQueryHandler(callback)
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_input,
        )
    )

    if application.job_queue:
        application.job_queue.run_repeating(
            scheduled_jobs,
            interval=60,
            first=10,
        )

    print("======================================")
    print("MEDIA BANK — CLEAN REBUILD")
    print("DB:", DB_FILE)
    print("Premium button icons:", ENABLE_BUTTON_PREMIUM)
    print("Managers:", MANAGERS)
    print("Admin IDs:", ADMIN_IDS)
    print("======================================")

    application.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
