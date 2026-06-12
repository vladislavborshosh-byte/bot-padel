import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
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
    JobQueue,
)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"])

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
    level         TEXT
)
""")

conn.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
)
""")

for col in ("name", "phone"):
    try:
        conn.execute(f"ALTER TABLE bookings ADD COLUMN {col} TEXT")
    except Exception:
        pass

conn.commit()

TIMES = {
    "individual": [
        "09:00-10:00", "10:00-11:00", "11:00-12:00", "12:00-13:00",
        "13:00-14:00", "14:00-15:00", "15:00-16:00", "16:00-17:00",
        "17:00-18:00", "18:00-19:00", "19:00-20:00", "20:00-21:00",
    ],
    "pair": [
        "09:00-10:30", "10:30-12:00", "12:00-13:30", "13:30-15:00",
        "15:00-16:30", "16:30-18:00", "18:00-19:30", "19:30-21:00",
    ],
    "group": [
        "09:00-10:30", "10:30-12:00", "12:00-13:30", "13:30-15:00",
        "15:00-16:30", "16:30-18:00", "18:00-19:30", "19:30-21:00",
    ],
}

TRAININGS = {
    "pair": {"title": "Парне", "capacity": 2},
    "individual": {"title": "Індивідуальне", "capacity": 1},
    "group": {"title": "Групове", "capacity": 4},
}

LEVELS = ["E-D(-)", "D(+)-C(-)", "C(+)-🔝"]

DAY_KEYS = ["saturday", "sunday"]
DAY_NAMES_DEFAULT = {"saturday": "Субота", "sunday": "Неділя"}

ASK_NAME, ASK_PHONE = range(2)
CONFIRM_CANCEL = 10


# ── date helpers ───────────────────────────────────────────────────────────

def get_day_dates():
    """Return dict: {saturday: 'DD.MM', sunday: 'DD.MM'} from settings, or empty strings."""
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
    """Return e.g. 'Субота 14 червня' if date is set, else just 'Субота'."""
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
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(day_display_name("saturday"), callback_data="day_saturday")],
            [InlineKeyboardButton(day_display_name("sunday"), callback_data="day_sunday")],
        ]
    )


def trainings_menu(day):
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Парне", callback_data=f"training_{day}_pair")],
            [InlineKeyboardButton("Індивідуальне", callback_data=f"training_{day}_individual")],
            [InlineKeyboardButton("Групове", callback_data=f"training_{day}_group")],
            [InlineKeyboardButton("⬅️ Назад", callback_data="back_days")],
        ]
    )


def levels_menu(day, training):
    return InlineKeyboardMarkup(
        [
            *[[InlineKeyboardButton(lvl, callback_data=f"lvl_{day}_{training}_{lvl}")] for lvl in LEVELS],
            [InlineKeyboardButton("⬅️ Назад", callback_data=f"day_{day}")],
        ]
    )


def slots_for_level_menu(day, training, level):
    all_bookings = get_day_bookings(day)
    keyboard = []
    for time in TIMES[training]:
        available, booked, cap = compute_slot(time, training, level, all_bookings)
        if available:
            keyboard.append(
                [InlineKeyboardButton(
                    f"{time} ({booked}/{cap})",
                    callback_data=f"slot_{day}_{training}_{level}_{time}",
                )]
            )
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
    _, day, training = q.data.split("_")
    await q.edit_message_text("Оберіть свій рівень:", reply_markup=levels_menu(day, training))


async def handle_level(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    day = parts[1]
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


# ── ConversationHandler: slot → name → phone → confirm ────────────────────

async def conv_entry_slot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    parts = q.data.split("_")
    day = parts[1]
    training = parts[2]
    level = parts[3]
    time = "_".join(parts[4:])

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


async def confirm_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    user = update.effective_user
    d = context.user_data

    conn.execute(
        """INSERT INTO bookings
           (user_id, username, name, phone, day, time, training_type, level)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (user.id, user.username, d["name"], phone, d["day"], d["time"], d["training"], d["level"]),
    )
    conn.commit()

    await update.message.reply_text(
        f"✅ Ви записані!\n\n"
        f"👤 Ім'я: {d['name']}\n"
        f"📞 Телефон: {phone}\n"
        f"📅 День: {day_display_name(d['day'])}\n"
        f"⏰ Час: {d['time']}\n"
        f"🎾 Тип: {TRAININGS[d['training']]['title']}\n"
        f"📊 Рівень: {d['level']}\n\n"
        f"Для нового запису — /book"
    )

    # Notify admin
    username_str = f"@{user.username}" if user.username else "без username"
    await update.get_bot().send_message(
        ADMIN_ID,
        f"🆕 Новий запис!\n\n"
        f"👤 {d['name']} ({username_str})\n"
        f"📞 {phone}\n"
        f"📅 {day_display_name(d['day'])}  ⏰ {d['time']}\n"
        f"🎾 {TRAININGS[d['training']]['title']}  📊 {d['level']}"
    )

    return ConversationHandler.END


async def cancel_conv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Скасовано. Для нового запису — /book")
    return ConversationHandler.END


# ── /mybookings ────────────────────────────────────────────────────────────

async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    rows = conn.execute(
        "SELECT id, day, time, training_type, level, name, phone FROM bookings "
        "WHERE user_id=? ORDER BY day, time",
        (user.id,),
    ).fetchall()

    if not rows:
        await update.message.reply_text("У вас немає активних записів.\n\nДля запису — /book")
        return

    await update.message.reply_text(f"📋 Ваші записи ({len(rows)}):")
    for booking_id, day, time, training, level, name, phone in rows:
        text = (
            f"📅 {day_display_name(day)}  ⏰ {time}\n"
            f"🎾 {TRAININGS[training]['title']}  📊 {level}\n"
            f"👤 {name}  📞 {phone}"
        )
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Скасувати", callback_data=f"cancel_booking_{booking_id}")
            ]]),
        )


async def handle_cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """First tap — ask for confirmation."""
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

    if q.from_user.id != user_id and q.from_user.id != ADMIN_ID:
        await q.edit_message_text("❌ Немає доступу.")
        return

    await q.edit_message_text(
        f"❓ Підтвердіть скасування:\n\n"
        f"📅 {day_display_name(day)}  ⏰ {time}\n"
        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n"
        f"👤 {name}",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Так, скасувати", callback_data=f"confirm_cancel_{booking_id}"),
                InlineKeyboardButton("↩️ Назад", callback_data=f"keep_booking_{booking_id}"),
            ]
        ])
    )


async def handle_confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Second tap — actually delete."""
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

    if q.from_user.id != user_id and q.from_user.id != ADMIN_ID:
        await q.edit_message_text("❌ Немає доступу.")
        return

    conn.execute("DELETE FROM bookings WHERE id=?", (booking_id,))
    conn.commit()
    await q.edit_message_text("🗑 Запис скасовано.")

    # Notify admin
    username_str = f"@{username}" if username else "без username"
    await q.get_bot().send_message(
        ADMIN_ID,
        f"🗑 Скасування запису!\n\n"
        f"👤 {name} ({username_str})\n"
        f"📞 {phone}\n"
        f"📅 {day_display_name(day)}  ⏰ {time}\n"
        f"🎾 {TRAININGS[training]['title']}  📊 {level}"
    )


async def handle_keep_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User changed their mind — restore original cancel button."""
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


# ── reminders ─────────────────────────────────────────────────────────────

async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Runs every hour. Sends reminder 24h and 1h before training."""
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

            # 24h reminder (between 23.5h and 24.5h before)
            if 23.5 <= diff_hours <= 24.5:
                try:
                    await context.bot.send_message(
                        user_id,
                        f"⏰ Нагадування!\n\n"
                        f"Завтра у вас тренування:\n"
                        f"📅 {day_display_name(day_key)}  ⏰ {slot_time}\n"
                        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n\n"
                        f"Гарного тренування! 🎾"
                    )
                except Exception:
                    pass

            # 1h reminder (between 0.5h and 1.5h before)
            elif 0.5 <= diff_hours <= 1.5:
                try:
                    await context.bot.send_message(
                        user_id,
                        f"🔔 Через 1 годину тренування!\n\n"
                        f"📅 {day_display_name(day_key)}  ⏰ {slot_time}\n"
                        f"🎾 {TRAININGS[training]['title']}  📊 {level}\n\n"
                        f"Не забудьте взяти ракетку! 🎾"
                    )
                except Exception:
                    pass


# ── /setdates ──────────────────────────────────────────────────────────────

async def set_dates(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Usage: /setdates 14.06 15.06"""
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Немає доступу.")
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text(
            "❌ Використання: /setdates DD.MM DD.MM\nНаприклад: /setdates 14.06 15.06"
        )
        return

    for i, (day_key, date_str) in enumerate(zip(DAY_KEYS, args)):
        try:
            datetime.strptime(date_str + f".{datetime.now().year}", "%d.%m.%Y")
            set_day_date(day_key, date_str)
        except ValueError:
            await update.message.reply_text(f"❌ Невірний формат дати: {date_str}. Використовуйте DD.MM")
            return

    await update.message.reply_text(
        f"✅ Дати встановлено:\n"
        f"📅 {day_display_name('saturday')}\n"
        f"📅 {day_display_name('sunday')}"
    )


# ── shared text builders ───────────────────────────────────────────────────

def build_schedule_text():
    rows = conn.execute(
        "SELECT day, time, training_type, name, phone, level, username "
        "FROM bookings ORDER BY day, time, training_type"
    ).fetchall()
    if not rows:
        return None
    slots = defaultdict(list)
    for day, time, training, name, phone, level, username in rows:
        slots[(day, time, training)].append((name, phone, level, username))
    text = "📋 РОЗКЛАД\n"
    current_day = None
    for (day, time, training), people in slots.items():
        cap = TRAININGS[training]["capacity"]
        if day != current_day:
            current_day = day
            text += f"\n📅 {day_display_name(day)}\n"
        text += f"\n⏰ {time}  🎾 {TRAININGS[training]['title']} ({len(people)}/{cap})\n"
        for name, phone, level, username in people:
            text += f"  👤 {name}  📞 {phone}  📊 {level}\n"
    return text


def build_freeslots_text():
    text = "🟢 ВІЛЬНІ СЛОТИ\n"
    any_free = False
    for day in ("saturday", "sunday"):
        all_bookings = get_day_bookings(day)
        day_lines = []
        for training, info in TRAININGS.items():
            free_times = []
            for slot_time in TIMES[training]:
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


# ── /schedule ──────────────────────────────────────────────────────────────

async def schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Немає доступу.")
        return
    text = build_schedule_text()
    await update.message.reply_text(text or "Немає записів.")


# ── /freeslots ─────────────────────────────────────────────────────────────

async def free_slots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Немає доступу.")
        return
    await update.message.reply_text(build_freeslots_text())


# ── inline menu ────────────────────────────────────────────────────────────

def menu_inline(user_id):
    kb = [
        [InlineKeyboardButton("📅 Записатися", callback_data="menucmd_book")],
        [InlineKeyboardButton("📋 Мої записи", callback_data="menucmd_mybookings")],
    ]
    if user_id == ADMIN_ID:
        kb += [
            [InlineKeyboardButton("🗓 Розклад", callback_data="menucmd_schedule")],
            [InlineKeyboardButton("🟢 Вільні слоти", callback_data="menucmd_freeslots")],
            [InlineKeyboardButton("📤 Експорт розкладу", callback_data="menucmd_export")],
            [InlineKeyboardButton("📆 Встановити дати", callback_data="menucmd_setdates")],
            [InlineKeyboardButton("🗑 Скинути все", callback_data="menucmd_resetall")],
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
            "SELECT id, day, time, training_type, level, name, phone FROM bookings "
            "WHERE user_id=? ORDER BY day, time",
            (user_id,),
        ).fetchall()
        if not rows:
            await q.edit_message_text("У вас немає активних записів.", reply_markup=InlineKeyboardMarkup(back))
            return
        text = f"📋 Ваші записи ({len(rows)}):\n"
        kb = []
        for bid, day, time, training, level, name, phone in rows:
            text += f"\n📅 {day_display_name(day)}  ⏰ {time}  🎾 {TRAININGS[training]['title']}  📊 {level}"
            kb.append([InlineKeyboardButton(f"❌ Скасувати {time}", callback_data=f"cancel_booking_{bid}")])
        kb += back
        await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

    elif cmd == "schedule":
        if user_id != ADMIN_ID:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        text = build_schedule_text()
        await q.edit_message_text(text or "Немає записів.", reply_markup=InlineKeyboardMarkup(back))

    elif cmd == "freeslots":
        if user_id != ADMIN_ID:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        await q.edit_message_text(build_freeslots_text(), reply_markup=InlineKeyboardMarkup(back))

    elif cmd == "export":
        if user_id != ADMIN_ID:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        text = build_schedule_text()
        if not text:
            await q.edit_message_text("Немає записів для експорту.", reply_markup=InlineKeyboardMarkup(back))
            return
        # Send as a separate message so it's easy to forward/copy
        await q.get_bot().send_message(user_id, text)
        await q.edit_message_text("📤 Розклад надіслано окремим повідомленням.", reply_markup=InlineKeyboardMarkup(back))

    elif cmd == "setdates":
        if user_id != ADMIN_ID:
            await q.answer("❌ Немає доступу.", show_alert=True)
            return
        dates = get_day_dates()
        sat = dates.get("saturday", "не встановлено")
        sun = dates.get("sunday", "не встановлено")
        await q.edit_message_text(
            f"📆 Поточні дати:\n"
            f"Субота: {sat}\n"
            f"Неділя: {sun}\n\n"
            f"Для зміни використовуйте команду:\n/setdates DD.MM DD.MM\n\nНаприклад: /setdates 14.06 15.06",
            reply_markup=InlineKeyboardMarkup(back)
        )

    elif cmd == "resetall":
        if user_id != ADMIN_ID:
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
    if update.effective_user.id != ADMIN_ID:
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
        BotCommand("book", "Записатися на тренування"),
        BotCommand("mybookings", "Мої записи"),
        BotCommand("menu", "Список команд"),
    ]
    admin_commands = user_commands + [
        BotCommand("schedule", "Розклад усіх записів"),
        BotCommand("freeslots", "Всі вільні слоти"),
        BotCommand("setdates", "Встановити дати вихідних"),
        BotCommand("resetall", "Видалити всі записи"),
    ]
    await application.bot.set_my_commands(user_commands)
    await application.bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=ADMIN_ID))

    # Schedule reminder job — runs every hour
    application.job_queue.run_repeating(send_reminders, interval=3600, first=10)


app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

conv_handler = ConversationHandler(
    entry_points=[CallbackQueryHandler(conv_entry_slot, pattern=r"^slot_")],
    states={
        ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_phone)],
        ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_booking)],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_conv),
        MessageHandler(filters.Regex("^📋 Меню$"), handle_menu_button),
    ],
)

app.add_handler(CommandHandler("book", book))
app.add_handler(CommandHandler("start", book))
app.add_handler(CommandHandler("schedule", schedule))
app.add_handler(CommandHandler("freeslots", free_slots))
app.add_handler(CommandHandler("menu", menu))
app.add_handler(CommandHandler("mybookings", my_bookings))
app.add_handler(CommandHandler("resetall", reset_all))
app.add_handler(CommandHandler("setdates", set_dates))
app.add_handler(MessageHandler(filters.Regex("^📋 Меню$"), handle_menu_button))
app.add_handler(conv_handler)
app.add_handler(CallbackQueryHandler(menucmd_dispatch, pattern=r"^menucmd_"))
app.add_handler(CallbackQueryHandler(handle_back_days, pattern=r"^back_days$"))
app.add_handler(CallbackQueryHandler(handle_day, pattern=r"^day_"))
app.add_handler(CallbackQueryHandler(handle_training, pattern=r"^training_"))
app.add_handler(CallbackQueryHandler(handle_level, pattern=r"^lvl_"))
app.add_handler(CallbackQueryHandler(handle_cancel_booking, pattern=r"^cancel_booking_"))
app.add_handler(CallbackQueryHandler(handle_confirm_cancel, pattern=r"^confirm_cancel_"))
app.add_handler(CallbackQueryHandler(handle_keep_booking, pattern=r"^keep_booking_"))
app.add_handler(CallbackQueryHandler(handle_noop, pattern=r"^noop$"))

print("BOT STARTED")
app.run_polling()
