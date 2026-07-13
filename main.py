import os
import sqlite3
from collections import defaultdict
from datetime import datetime
import pytz
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])
ADMIN_IDS = {ADMIN_ID}
# Add second admin if set
_admin2 = os.environ.get("ADMIN_TELEGRAM_ID2", "")
if _admin2:
    ADMIN_IDS.add(int(_admin2))

PRAGUE_TZ = pytz.timezone("Europe/Prague")

MENU_KB = ReplyKeyboardMarkup([["📋 Меню"]], resize_keyboard=True, is_persistent=True)

conn = sqlite3.connect("bot/padel.db", check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")

conn.execute("""
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER,
    username      TEXT,
    name          TEXT,
    phone         TEXT,
    day           TEXT,
    time          TEXT,
    training_type TEXT,
    level         TEXT,
    partner_name  TEXT,
    partner_phone TEXT,
    needs_partner INTEGER DEFAULT 0
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
)
""")

for col in ("name", "phone", "partner_name", "partner_phone", "needs_partner"):
    try:
        conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} TEXT")
    except Exception:
        pass

conn.commit()

# ── slot definitions ───────────────────────────────────────────────────────

TIMES = {
    "friday": {
        "individual": [
            "11:30-12:30", "12:30-13:30", "13:30-14:30", "14:30-15:30", "15:30-16:30",
            "18:00-19:00", "19:00-20:00", "20:00-21:00", "21:00-22:00",
        ],
        "pair": [
            "11:30-12:30", "12:30-13:30", "13:30-14:30", "14:30-15:30", "15:30-16:30",
            "18:00-19:00", "19:00-20:00", "20:00-21:00", "21:00-22:00",
        ],
    },
    "saturday": {
        "individual": [
            "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
            "13:00-14:00", "15:00-16:00", "16:00-17:00",
            "17:00-18:00", "18:00-19:00", "19:00-20:00",
        ],
        "pair": [
            "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
            "13:00-14:00", "15:00-16:00", "16:00-17:00",
            "17:00-18:00", "18:00-19:00", "19:00-20:00",
        ],
        "group": [
            "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
            "13:00-14:00", "15:00-16:00", "16:00-17:00",
            "17:00-18:00", "18:00-19:00", "19:00-20:00",
        ],
        "game_group": [
            "10:00-11:30",
            "11:30-13:00",
        ],
    },
    "sunday": {
        "individual": [
            "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
            "13:00-14:00", "15:00-16:00", "16:00-17:00", "18:00-19:00",
        ],
        "pair": [
            "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
            "13:00-14:00", "15:00-16:00", "16:00-17:00", "18:00-19:00",
        ],
        "group": [
            "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
            "13:00-14:00", "15:00-16:00", "16:00-17:00", "18:00-19:00",
        ],
        "game_group": [
            "10:00-11:30",
            "11:30-13:00",
        ],
    },
}

TRAININGS = {
    "pair":       {"title": "Парне",              "capacity": 2},
    "individual": {"title": "Індивідуальне",      "capacity": 1},
    "group":      {"title": "Групове тренування", "capacity": 4},
    "game_group": {"title": "Ігрове тренування",     "capacity": 4},
}

LEVELS = ["E-D(-)", "D(+)-C(-)", "C(+)-🔝"]

DAY_KEYS = ["friday", "saturday", "sunday"]
DAY_NAMES_DEFAULT = {"friday": "П'ятниця", "saturday": "Субота", "sunday": "Неділя"}

ASK_NAME, ASK_PHONE, ASK_HAS_PARTNER, ASK_PARTNER_NAME, ASK_PARTNER_PHONE = range(5)


# ── date helpers ───────────────────────────────────────────────────────────

def get_day_dates():
    result = {}
    for key in DAY_KEYS:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (f"date_{key}",)).fetchone()
        result[key] = row[0] if row else ""
    return result


def set_day_date(day_key, date_str):
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (f"date_{day_key}", date_str)
    )
    conn.commit()


def day_display_name(day_key):
    dates = get_day_dates()
    date_str = dates.get(day_key, "")
    base = DAY_NAMES_DEFAULT.get(day_key, day_key)
    if date_str:
        try:
            dt = datetime.strptime(date_str + f".{datetime.now().year}", "%d.%m.%Y")
            months = ["січня","лютого","березня","квітня","травня","червня",
                      "липня","серпня","вересня","жовтня","листопада","грудня"]
            return f"{base} {dt.day} {months[dt.month - 1]}"
        except Exception:
            return f"{base} {date_str}"
    return base


# ── time overlap helpers ───────────────────────────────────────────────────

def parse_minutes(t):
    h, m = map(int, t.split(":"))
    return h * 60 + m


def times_overlap(a, b):
    sa, ea = [parse_minutes(x) for x in a.split("-")]
    sb, eb = [parse_minutes(x) for x in b.split("-")]
    return sa < eb and sb < ea


def get_day_bookings(day):
    return conn.execute(
        "SELECT time, training_type, level FROM bookings WHERE day=?", (day,)
    ).fetchall()


def get_slot_people(day, slot_time, training):
    return conn.execute(
        "SELECT name, needs_partner FROM bookings WHERE day=? AND time=? AND training_type=?",
        (day, slot_time, training)
    ).fetchall()


def compute_slot(slot_time, training, level, all_bookings):
    cap = TRAININGS[training]["capacity"]
    exact = 0
    slot_level = None
    court_conflict = False

    for booked_time, booked_training, booked_level in all_bookings:
        if booked_time == slot_time and booked_training == training:
            exact += 1
            slot_level = booked_level
        elif times_overlap(slot_time, booked_time):
            court_conflict = True

    if exact >= cap or court_conflict:
        return False, exact, cap
    if slot_level and slot_level != level:
        return False, exact, cap
    return True, exact, cap


def slot_available(day, time, training):
    cap = TRAININGS[training]["capacity"]
    bookings = get_day_bookings(day)
    exact = sum(1 for t, tt, _ in bookings if t == time and tt == training)
    if exact >= cap:
        return False, exact, cap
    for booked_time, booked_training, _ in bookings:
        if booked_time == time and booked_training == training:
            continue
        if times_overlap(time, booked_time):
            return False, exact, cap
    return True, exact, cap


def get_slot_level(day, time, training):
    row = conn.execute(
        "SELECT level FROM bookings WHERE day=? AND time=? AND training_type=? LIMIT 1",
        (day, time, training),
    ).fetchone()
    return row[0] if row else None


def slot_available_for_level(day, time, training, level):
    available, booked, cap = slot_available(day, time, training)
    if not available:
        return False, booked, cap
    existing = get_slot_level(day, time, training)
    if existing and existing != level:
        return False, booked, cap
    return True, booked, cap


# ── menus ──────────────────────────────────────────────────────────────────

def days_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(day_display_name("friday"),   callback_data="day_friday")],
        [InlineKeyboardButton(day_display_name("saturday"), callback_data="day_saturday")],
        [InlineKeyboardButton(day_display_name("sunday"),   callback_data="day_sunday")],
    ])


def trainings_menu(day):
    kb = [
        [InlineKeyboardButton("Парне",         callback_data=f"training_{day}_pair")],
        [InlineKeyboardButton("Індивідуальне", callback_data=f"training_{day}_individual")],
    ]
    if day != "friday":
        kb += [
            [InlineKeyboardButton("Групове тренування", callback_data=f"training_{day}_group")],
            [InlineKeyboardButton("Ігрове тренування",  callback_data=f"training_{day}_game_group")],
        ]
    kb.append([InlineKeyboardButton("⬅️ Назад", callback_data="back_days")])
    return InlineKeyboardMarkup(kb)


def levels_menu(day, training):
    return InlineKeyboardMarkup([
        *[[InlineKeyboardButton(lvl, callback_data=f"lvl_{day}_{training}_{lvl}")] for lvl in LEVELS],
        [InlineKeyboardButton("⬅️ Назад", callback_data=f"day_{day}")],
    ])


def slots_for_level_menu(day, training, level):
    all_bookings = get_day_bookings(day)
    keyboard = []
    for time in TIMES.get(day, {}).get(training, []):
        available, booked, cap = compute_slot(time, training, level, all_bookings)
        if available:
            people = get_slot_people(day, time, training)
            if people:
                if training == "pair":
                    names = ", ".join(f"{p[0]}{'⚠️' if p[1] else ''}" for p in people)
                else:
                    names = ", ".join(p[0] for p in people)
                label = f"{time} ({booked}/{cap}) — {names}"
            else:
                label = f"{time} ({booked}/{cap})"
            keyboard.append([InlineKeyboardButton(
                label,
                callback_data=f"slot|{day}|{training}|{level}|{time}",
            )])
    if not keyboard:
        keyboard = [[InlineKeyboardButton("❌ Немає вільних слотів для вашого рівня", callback_data="noop")]]
    keyboard.append([InlineKeyboardButton("⬅️ Назад", callback_data=f"training_{day}_{training}")])
    return InlineKeyboardMarkup(keyboard)


# ── navigation handlers ────────────────────────────────────────────────────

async def book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("👋 Вітаємо!", reply_markup=MENU_KB)
    await update.message.reply_text("Оберіть день:", reply_markup=days_menu())


async def handle_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    day = q.data.split("_")[1]
    await q.edit_message_text("Оберіть тип тренування:", reply_markup=trainings_menu(day))


async def handle_training(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    day = parts[1]
    training = "_".join(parts[2:])
    # Individual training — skip level selection
    if training == "individual":
        await q.edit_message_text(
            "Оберіть час:",
            reply_markup=slots_for_level_menu(day, training, "none"),
        )
    else:
        await q.edit_message_text("Оберіть свій рівень:", reply_markup=levels_menu(day, training))


async def handle_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # Format: lvl_{day}_{training}_{level}
    # training can be "game_group" (has underscore), so parse carefully
    parts = q.data.split("_")
    day = parts[1]
    # Check if training is game_group
    if parts[2] == "game" and parts[3] == "group":
        training = "game_group"
        level = "_".join(parts[4:])
    else:
        training = parts[2]
        level = "_".join(parts[3:])
    await q.edit_message_text(
        f"📊 Рівень: {level}\n\nОберіть час:",
        reply_markup=slots_for_level_menu(day, training, level),
    )


async def handle_back_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Оберіть день:", reply_markup=days_menu())


async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ── ConversationHandler ────────────────────────────────────────────────────

async def conv_entry_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    parts = q.data.split("|")
    day = parts[1]
    training = parts[2]
    level = parts[3]
    time = parts[4]
    user = q.from_user

    if conn.execute(
        "SELECT id FROM bookings WHERE user_id=? AND day=? AND time=?",
        (user.id, day, time),
    ).fetchone():
        await q.edit_message_text("⚠️ Ви вже записані на цей слот.")
        return ConversationHandler.END

    available, _, _ = slot_available_for_level(day, time, training, level)
    if not available:
        existing = get_slot_level(day, time, training)
        if existing and existing != level:
            await q.edit_message_text(
                f"❌ Цей слот зайнятий гравцями рівня «{existing}».\nОберіть інший час — /book"
            )
        else:
            await q.edit_message_text("❌ Цей слот вже зайнятий. Оберіть інший час — /book")
        return ConversationHandler.END

    context.user_data.update({"day": day, "training": training, "time": time, "level": level})
    await q.edit_message_text(
        f"📅 {day_display_name(day)}  ⏰ {time}\n"
        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n\n"
        f"Введіть ваше ім'я та прізвище:"
    )
    return ASK_NAME


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("📞 Введіть ваш номер телефону:")
    return ASK_PHONE


async def after_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["phone"] = update.message.text.strip()
    training = context.user_data.get("training")

    if training == "pair":
        await update.message.reply_text(
            "👥 Чи маєте ви пару?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ Так, маю пару", callback_data="has_partner_yes")],
                [InlineKeyboardButton("❌ Ні, шукаю пару", callback_data="has_partner_no")],
            ])
        )
        return ASK_HAS_PARTNER

    return await _finalize_booking(update, context)


async def handle_has_partner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "has_partner_yes":
        context.user_data["needs_partner"] = False
        await q.edit_message_text("👤 Введіть ім'я та прізвище вашої пари:")
        return ASK_PARTNER_NAME
    else:
        context.user_data["needs_partner"] = True
        context.user_data["partner_name"] = None
        context.user_data["partner_phone"] = None
        return await _finalize_booking_query(q, context)


async def ask_partner_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["partner_name"] = update.message.text.strip()
    await update.message.reply_text("📞 Введіть номер телефону вашої пари:")
    return ASK_PARTNER_PHONE


async def after_partner_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["partner_phone"] = update.message.text.strip()
    return await _finalize_booking(update, context)


async def _finalize_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    d = context.user_data
    needs_partner = d.get("needs_partner", False)

    conn.execute(
        """INSERT INTO bookings
           (user_id, username, name, phone, day, time, training_type, level,
            partner_name, partner_phone, needs_partner)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user.id, user.username, d["name"], d["phone"],
            d["day"], d["time"], d["training"], d["level"],
            d.get("partner_name"), d.get("partner_phone"),
            1 if needs_partner else 0,
        ),
    )
    conn.commit()

    partner_info = ""
    if d.get("partner_name"):
        partner_info = f"\n👥 Пара: {d['partner_name']}  📞 {d['partner_phone']}"
    elif needs_partner:
        partner_info = "\n⚠️ Шукає пару"

    await update.message.reply_text(
        f"✅ Ви записані!\n\n"
        f"👤 Ім'я: {d['name']}\n"
        f"📞 Телефон: {d['phone']}\n"
        f"📅 День: {day_display_name(d['day'])}\n"
        f"⏰ Час: {d['time']}\n"
        f"🎾 Тип: {TRAININGS[d['training']]['title']}\n"
        f"📊 Рівень: {d['level']}"
        f"{partner_info}\n\n"
        f"Для нового запису — /book"
    )

    username_str = f"@{user.username}" if user.username else "без username"
    for _aid in ADMIN_IDS:
      await update.get_bot().send_message(
        _aid,
        f"🆕 Новий запис!\n\n"
        f"👤 {d['name']} ({username_str})\n"
        f"📞 {d['phone']}\n"
        f"📅 {day_display_name(d['day'])}  ⏰ {d['time']}\n"
        f"🎾 {TRAININGS[d['training']]['title']}  📊 {d['level']}"
        f"{partner_info}"
    )
    return ConversationHandler.END


async def _finalize_booking_query(q, context):
    user = q.from_user
    d = context.user_data
    needs_partner = d.get("needs_partner", False)

    conn.execute(
        """INSERT INTO bookings
           (user_id, username, name, phone, day, time, training_type, level,
            partner_name, partner_phone, needs_partner)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            user.id, user.username, d["name"], d["phone"],
            d["day"], d["time"], d["training"], d["level"],
            d.get("partner_name"), d.get("partner_phone"),
            1 if needs_partner else 0,
        ),
    )
    conn.commit()

    partner_info = "\n⚠️ Шукає пару" if needs_partner else ""

    await q.edit_message_text(
        f"✅ Ви записані!\n\n"
        f"👤 Ім'я: {d['name']}\n"
        f"📞 Телефон: {d['phone']}\n"
        f"📅 День: {day_display_name(d['day'])}\n"
        f"⏰ Час: {d['time']}\n"
        f"🎾 {TRAININGS[d['training']]['title']}  📊 {d['level']}"
        f"{partner_info}\n\n"
        f"Для нового запису — /book"
    )

    username_str = f"@{user.username}" if user.username else "без username"
    for _aid in ADMIN_IDS:
      await q.get_bot().send_message(
        _aid,
        f"🆕 Новий запис!\n\n"
        f"👤 {d['name']} ({username_str})\n"
        f"📞 {d['phone']}\n"
        f"📅 {day_display_name(d['day'])}  ⏰ {d['time']}\n"
        f"🎾 {TRAININGS[d['training']]['title']}  📊 {d['level']}"
        f"{partner_info}"
    )
    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано. Для нового запису — /book")
    return ConversationHandler.END


# ── /mybookings ────────────────────────────────────────────────────────────

async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = conn.execute(
        "SELECT id, day, time, training_type, level, name, phone, partner_name, partner_phone, needs_partner "
        "FROM bookings WHERE user_id=? ORDER BY day, time",
        (user.id,),
    ).fetchall()

    if not rows:
        await update.message.reply_text("У вас немає активних записів.\n\nДля запису — /book")
        return

    await update.message.reply_text(f"📋 Ваші записи ({len(rows)}):")
    for booking_id, day, time, training, level, name, phone, partner_name, partner_phone, needs_partner in rows:
        partner_info = ""
        if partner_name:
            partner_info = f"\n👥 Пара: {partner_name}  📞 {partner_phone}"
        elif needs_partner:
            partner_info = "\n⚠️ Шукає пару"

        text = (
            f"📅 {day_display_name(day)}  ⏰ {time}\n"
            f"🎾 {TRAININGS[training]['title']}  📊 {level}\n"
            f"👤 {name}  📞 {phone}"
            f"{partner_info}"
        )
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel_booking_{booking_id}")
            ]]),
        )


async def handle_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    booking_id = int(q.data.split("_")[-1])

    row = conn.execute(
        "SELECT user_id, day, time, training_type, level, name FROM bookings WHERE id=?",
        (booking_id,)
    ).fetchone()

    if not row:
        await q.edit_message_text("⚠️ Запис не знайдено.")
        return

    user_id, day, time, training, level, name = row

    if q.from_user.id != user_id and q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Немає доступу.")
        return

    await q.edit_message_text(
        f"❓ Підтвердіть скасування:\n\n"
        f"📅 {day_display_name(day)}  ⏰ {time}\n"
        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n"
        f"👤 {name}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Так, скасувати", callback_data=f"confirm_cancel_{booking_id}"),
            InlineKeyboardButton("↩️ Назад", callback_data=f"keep_booking_{booking_id}"),
        ]])
    )


async def handle_confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    booking_id = int(q.data.split("_")[-1])

    row = conn.execute(
        "SELECT user_id, username, name, phone, day, time, training_type, level FROM bookings WHERE id=?",
        (booking_id,)
    ).fetchone()

    if not row:
        await q.edit_message_text("⚠️ Запис не знайдено.")
        return

    user_id, username, name, phone, day, time, training, level = row

    if q.from_user.id != user_id and q.from_user.id not in ADMIN_IDS:
        await q.edit_message_text("❌ Немає доступу.")
        return

    conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    await q.edit_message_text("🗑 Запис скасовано.")

    username_str = f"@{username}" if username else "без username"
    for _aid in ADMIN_IDS:
      await q.get_bot().send_message(
        _aid,
        f"🗑 Скасування запису!\n\n"
        f"👤 {name} ({username_str})\n"
        f"📞 {phone}\n"
        f"📅 {day_display_name(day)}  ⏰ {time}\n"
        f"🎾 {TRAININGS[training]['title']}  📊 {level}"
    )


async def handle_keep_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    booking_id = int(q.data.split("_")[-1])

    row = conn.execute(
        "SELECT day, time, training_type, level, name, phone FROM bookings WHERE id=?",
        (booking_id,)
    ).fetchone()

    if not row:
        await q.edit_message_text("⚠️ Запис не знайдено.")
        return

    day, time, training, level, name, phone = row
    await q.edit_message_text(
        f"📅 {day_display_name(day)}  ⏰ {time}\n"
        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n"
        f"👤 {name}  📞 {phone}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel_booking_{booking_id}")
        ]])
    )


# ── reminders ──────────────────────────────────────────────────────────────

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(PRAGUE_TZ)
    dates = get_day_dates()

    for day_key, date_str in dates.items():
        if not date_str:
            continue
        try:
            training_date = datetime.strptime(date_str + f".{now.year}", "%d.%m.%Y")
            training_date = PRAGUE_TZ.localize(training_date)
        except Exception:
            continue

        rows = conn.execute(
            "SELECT user_id, name, time, training_type, level FROM bookings WHERE day=?",
            (day_key,)
        ).fetchall()

        for user_id, name, slot_time, training, level in rows:
            start_str = slot_time.split("-")[0]
            h, m = map(int, start_str.split(":"))
            slot_dt = training_date.replace(hour=h, minute=m, second=0, microsecond=0)

            diff = slot_dt - now
            diff_hours = diff.total_seconds() / 3600

            if 23.5 <= diff_hours <= 24.5:
                try:
                    await context.bot.send_message(
                        user_id,
                        f"⏰ Нагадування!\n\nЗавтра у вас тренування:\n"
                        f"📅 {day_display_name(day_key)}  ⏰ {slot_time}\n"
                        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n\nГарного тренування! 🎾"
                    )
                except Exception:
                    pass

            elif 0.5 <= diff_hours <= 1.5:
                try:
                    await context.bot.send_message(
                        user_id,
                        f"🔔 Через 1 годину тренування!\n\n"
                        f"📅 {day_display_name(day_key)}  ⏰ {slot_time}\n"
                        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n\nНе забудьте взяти ракетку! 🎾"
                    )
                except Exception:
                    pass


# ── /setdates ──────────────────────────────────────────────────────────────

async def set_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return

    args = context.args
    if len(args) != 3:
        await update.message.reply_text(
            "❌ Використання: /setdates DD.MM DD.MM DD.MM\n"
            "Наприклад: /setdates 13.06 14.06 15.06\n"
            "(п'ятниця субота неділя)"
        )
        return

    for day_key, date_str in zip(DAY_KEYS, args):
        try:
            datetime.strptime(date_str + f".{datetime.now().year}", "%d.%m.%Y")
            set_day_date(day_key, date_str)
        except ValueError:
            await update.message.reply_text(f"❌ Невірний формат дати: {date_str}. Використовуйте DD.MM")
            return

    await update.message.reply_text(
        f"✅ Дати встановлено:\n"
        f"📅 {day_display_name('friday')}\n"
        f"📅 {day_display_name('saturday')}\n"
        f"📅 {day_display_name('sunday')}"
    )


# ── shared text builders ───────────────────────────────────────────────────

def build_schedule_text():
    rows = conn.execute(
        "SELECT day, time, training_type, name, phone, level, username, partner_name, partner_phone, needs_partner "
        "FROM bookings ORDER BY day, time, training_type"
    ).fetchall()
    if not rows:
        return None
    slots = defaultdict(list)
    for day, time, training, name, phone, level, username, partner_name, partner_phone, needs_partner in rows:
        slots[(day, time, training)].append((name, phone, level, username, partner_name, partner_phone, needs_partner))
    text = "📋 РОЗКЛАД\n"
    current_day = None
    for (day, time, training), people in slots.items():
        cap = TRAININGS[training]["capacity"]
        if day != current_day:
            current_day = day
            text += f"\n📅 {day_display_name(day)}\n"
        text += f"\n⏰ {time}  🎾 {TRAININGS[training]['title']} ({len(people)}/{cap})\n"
        for name, phone, level, username, partner_name, partner_phone, needs_partner in people:
            line = f"  👤 {name}  📞 {phone}  📊 {level}"
            if partner_name:
                line += f"\n     👥 Пара: {partner_name}  📞 {partner_phone}"
            elif needs_partner:
                line += "  ⚠️ шукає пару"
            text += line + "\n"
    return text


def build_freeslots_text():
    text = "🟢 ВІЛЬНІ СЛОТИ\n"
    any_free = False
    for day in DAY_KEYS:
        all_bookings = get_day_bookings(day)
        day_lines = []
        day_times = TIMES.get(day, {})
        for training, info in TRAININGS.items():
            if training not in day_times:
                continue
            free_times = []
            for slot_time in day_times[training]:
                cap = info["capacity"]
                exact = sum(1 for t, tt, _ in all_bookings if t == slot_time and tt == training)
                conflict = any(
                    times_overlap(slot_time, bt)
                    for bt, btt, _ in all_bookings
                    if not (bt == slot_time and btt == training)
                )
                if exact < cap and not conflict:
                    free_times.append(f"  ⏰ {slot_time}  ({exact}/{cap})")
            if free_times:
                day_lines.append(f"\n🎾 {info['title']}")
                day_lines.extend(free_times)
        if day_lines:
            text += f"\n📅 {day_display_name(day)}"
            text += "\n".join(day_lines) + "\n"
            any_free = True
    return text if any_free else "❌ Вільних слотів немає — всі зайняті."


# ── /schedule & /freeslots ─────────────────────────────────────────────────

async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    text = build_schedule_text()
    await update.message.reply_text(text or "Немає записів.")


async def free_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    await update.message.reply_text(build_freeslots_text())


# ── inline menu ────────────────────────────────────────────────────────────

def menu_inline(user_id):
    kb = [
        [InlineKeyboardButton("📅 Записатися",  callback_data="menucmd_book")],
        [InlineKeyboardButton("📋 Мої записи",  callback_data="menucmd_mybookings")],
    ]
    if user_id in ADMIN_IDS:
        kb += [
            [InlineKeyboardButton("🗓 Розклад",          callback_data="menucmd_schedule")],
            [InlineKeyboardButton("🟢 Вільні слоти",     callback_data="menucmd_freeslots")],
            [InlineKeyboardButton("📤 Експорт розкладу", callback_data="menucmd_export")],
            [InlineKeyboardButton("📆 Встановити дати",  callback_data="menucmd_setdates")],
            [InlineKeyboardButton("🗑 Скинути все",      callback_data="menucmd_resetall")],
        ]
    return InlineKeyboardMarkup(kb)


async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Меню:", reply_markup=menu_inline(update.effective_user.id))
    return ConversationHandler.END


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Меню:", reply_markup=menu_inline(update.effective_user.id))


async def menucmd_dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cmd = q.data[len("menucmd_"):]
    user_id = q.from_user.id
    back = [[InlineKeyboardButton("⬅️ Назад до меню", callback_data="menucmd_back")]]

    if cmd == "back":
        await q.edit_message_text("📋 Меню:", reply_markup=menu_inline(user_id))

    elif cmd == "book":
        context.user_data.clear()
        await q.edit_message_text("Оберіть день:", reply_markup=days_menu())

    elif cmd == "mybookings":
        rows = conn.execute(
            "SELECT id, day, time, training_type, level, name, phone, needs_partner FROM bookings "
            "WHERE user_id=? ORDER BY day, time",
            (user_id,),
        ).fetchall()
        if not rows:
            await q.edit_message_text("У вас немає активних записів.", reply_markup=InlineKeyboardMarkup(back))
            return
        text = f"📋 Ваші записи ({len(rows)}):\n"
        kb = []
        for bid, day, time, training, level, name, phone, needs_partner in rows:
            partner_str = "  ⚠️ шукає пару" if needs_partner else ""
            text += f"\n📅 {day_display_name(day)}  ⏰ {time}  🎾 {TRAININGS[training]['title']}  📊 {level}{partner_str}"
            kb.append([InlineKeyboardButton(f"❌ Скасувати {time}", callback_data=f"cancel_booking_{bid}")])
        kb += back
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

    elif cmd == "schedule":
        if user_id not in ADMIN_IDS:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        text = build_schedule_text()
        await q.edit_message_text(text or "Немає записів.", reply_markup=InlineKeyboardMarkup(back))

    elif cmd == "freeslots":
        if user_id not in ADMIN_IDS:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        await q.edit_message_text(build_freeslots_text(), reply_markup=InlineKeyboardMarkup(back))

    elif cmd == "export":
        if user_id not in ADMIN_IDS:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        text = build_schedule_text()
        if not text:
            await q.edit_message_text("Немає записів для експорту.", reply_markup=InlineKeyboardMarkup(back))
            return
        await q.get_bot().send_message(user_id, text)
        await q.edit_message_text("📤 Розклад надіслано окремим повідомленням.", reply_markup=InlineKeyboardMarkup(back))

    elif cmd == "setdates":
        if user_id not in ADMIN_IDS:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        dates = get_day_dates()
        fri = dates.get("friday", "не встановлено")
        sat = dates.get("saturday", "не встановлено")
        sun = dates.get("sunday", "не встановлено")
        await q.edit_message_text(
            f"📆 Поточні дати:\nП'ятниця: {fri}\nСубота: {sat}\nНеділя: {sun}\n\n"
            f"Для зміни:\n/setdates DD.MM DD.MM DD.MM\n\nНаприклад: /setdates 13.06 14.06 15.06",
            reply_markup=InlineKeyboardMarkup(back)
        )

    elif cmd == "resetall":
        if user_id not in ADMIN_IDS:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        count = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
        if count == 0:
            await q.edit_message_text("Записів немає — нічого скидати.", reply_markup=InlineKeyboardMarkup(back))
            return
        conn.execute("DELETE FROM bookings")
        conn.commit()
        await q.edit_message_text(f"🗑 Усі записи видалено ({count} шт.).", reply_markup=InlineKeyboardMarkup(back))


# ── /resetall ──────────────────────────────────────────────────────────────

async def reset_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Немає доступу.")
        return
    count = conn.execute("SELECT COUNT(*) FROM bookings").fetchone()[0]
    if count == 0:
        await update.message.reply_text("Записів немає — нічого скидати.")
        return
    conn.execute("DELETE FROM bookings")
    conn.commit()
    await update.message.reply_text(f"🗑 Усі записи видалено ({count} шт.).")


# ── app setup ──────────────────────────────────────────────────────────────

async def post_init(application):
    user_commands = [
        BotCommand("book",       "Записатися на тренування"),
        BotCommand("mybookings", "Мої записи"),
        BotCommand("menu",       "Список команд"),
    ]
    admin_commands = user_commands + [
        BotCommand("schedule",  "Розклад усіх записів"),
        BotCommand("freeslots", "Всі вільні слоти"),
        BotCommand("setdates",  "Встановити дати вихідних"),
        BotCommand("resetall",  "Видалити всі записи"),
    ]
    await application.bot.set_my_commands(user_commands)
    for admin_id in ADMIN_IDS:
        await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
    application.job_queue.run_repeating(send_reminders, interval=3600, first=10)


app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(conv_entry_slot, pattern=r"^slot\|")],
    states={
        ASK_NAME:          [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
        ASK_PHONE:         [MessageHandler(filters.TEXT & ~filters.COMMAND, after_phone)],
        ASK_HAS_PARTNER:   [CallbackQueryHandler(handle_has_partner, pattern=r"^has_partner_")],
        ASK_PARTNER_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_partner_phone)],
        ASK_PARTNER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, after_partner_phone)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conv),
        MessageHandler(filters.Regex("^📋 Меню$"), handle_menu_button),
    ],
)

app.add_handler(CommandHandler("book",       book))
app.add_handler(CommandHandler("start",      book))
app.add_handler(CommandHandler("schedule",   schedule))
app.add_handler(CommandHandler("freeslots",  free_slots))
app.add_handler(CommandHandler("menu",       menu))
app.add_handler(CommandHandler("mybookings", my_bookings))
app.add_handler(CommandHandler("resetall",   reset_all))
app.add_handler(CommandHandler("setdates",   set_dates))
app.add_handler(MessageHandler(filters.Regex("^📋 Меню$"), handle_menu_button))
app.add_handler(conv_handler)
app.add_handler(CallbackQueryHandler(menucmd_dispatch,      pattern=r"^menucmd_"))
app.add_handler(CallbackQueryHandler(handle_back_days,      pattern=r"^back_days$"))
app.add_handler(CallbackQueryHandler(handle_day,            pattern=r"^day_"))
app.add_handler(CallbackQueryHandler(handle_training,       pattern=r"^training_"))
app.add_handler(CallbackQueryHandler(handle_level,          pattern=r"^lvl_"))
app.add_handler(CallbackQueryHandler(handle_cancel_booking, pattern=r"^cancel_booking_"))
app.add_handler(CallbackQueryHandler(handle_confirm_cancel, pattern=r"^confirm_cancel_"))
app.add_handler(CallbackQueryHandler(handle_keep_booking,   pattern=r"^keep_booking_"))
app.add_handler(CallbackQueryHandler(handle_noop,           pattern=r"^noop$"))

print("BOT STARTED")
app.run_polling()
