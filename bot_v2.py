import os
import time
import sqlite3
import telebot
import threading
from telebot import types
from datetime import datetime, timedelta

# ================= CONFIG =================
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = telebot.TeleBot(TOKEN, threaded=True)
DB = "vip_bot.db"

ESCROW_DAYS = 14
TASK_HOLD_MINUTES = 0


# ================= DB =================
def get_db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init():
    with get_db() as conn:
        cur = conn.cursor()

        cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0,
            hold REAL DEFAULT 0,
            state TEXT DEFAULT ''
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            url TEXT,
            reward REAL
        )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            job_id INTEGER,
            time TEXT,
            UNIQUE(user_id, job_id)
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS withdraws(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            card TEXT,
            amount REAL,
            status TEXT DEFAULT 'pending',
            time TEXT
        )
        """)

        cur.execute("""
        CREATE TABLE IF NOT EXISTS escrow(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount REAL,
            created_at TEXT,
            release_at TEXT,
            task_id INTEGER,
            job_id INTEGER
        )
        """)

        conn.commit()


# ================= STATE =================
def set_state(uid, state):
    with get_db() as conn:
        conn.execute("UPDATE users SET state=? WHERE id=?", (state, uid))
        conn.commit()


def get_state(uid):
    with get_db() as conn:
        r = conn.execute("SELECT state FROM users WHERE id=?", (uid,)).fetchone()
        return r[0] if r else ""


# ================= ESCROW ENGINE =================
def process_tasks(uid):
    now = datetime.now()

    with get_db() as conn:
        cur = conn.cursor()

        rows = cur.execute(
            "SELECT id, job_id, time FROM tasks WHERE user_id=?",
            (uid,)
        ).fetchall()

        for tid, jid, t in rows:
            try:
                t = datetime.fromisoformat(t)
            except:
                continue

            if now >= t + timedelta(minutes=TASK_HOLD_MINUTES):

                reward = cur.execute(
                    "SELECT reward FROM jobs WHERE id=?",
                    (jid,)
                ).fetchone()

                if not reward:
                    continue

                reward = reward[0]

                release_time = now + timedelta(days=ESCROW_DAYS)

                cur.execute("""
                    INSERT INTO escrow(user_id, amount, created_at, release_at, task_id, job_id)
                    VALUES(?,?,?,?,?,?)
                """, (
                    uid,
                    reward,
                    now.isoformat(),
                    release_time.isoformat(),
                    tid,
                    jid
                ))

                cur.execute("UPDATE users SET hold = hold - ? WHERE id=?", (reward, uid))
                cur.execute("DELETE FROM tasks WHERE id=?", (tid,))

        conn.commit()


def process_escrow():
    now = datetime.now()

    with get_db() as conn:
        cur = conn.cursor()

        rows = cur.execute("SELECT id, user_id, amount, release_at FROM escrow").fetchall()

        for eid, uid, amount, release_at in rows:
            try:
                release_dt = datetime.fromisoformat(release_at)
            except:
                continue

            if now >= release_dt:
                cur.execute(
                    "UPDATE users SET balance = balance + ? WHERE id=?",
                    (amount, uid)
                )
                cur.execute("DELETE FROM escrow WHERE id=?", (eid,))

        conn.commit()


def worker():
    while True:
        try:
            with get_db() as conn:
                users = conn.execute("SELECT id FROM users").fetchall()

            for u in users:
                process_tasks(u[0])

            process_escrow()

        except Exception as e:
            print("worker error:", e)

        time.sleep(20)


# ================= UNSUBSCRIBE CHECK =================
def check_unsubscribes():
    while True:
        try:
            with get_db() as conn:
                cur = conn.cursor()

                escrows = cur.execute(
                    "SELECT id, user_id, job_id FROM escrow"
                ).fetchall()

                for eid, uid, jid in escrows:
                    job = cur.execute(
                        "SELECT url FROM jobs WHERE id=?",
                        (jid,)
                    ).fetchone()

                    if not job:
                        continue

                    try:
                        channel = job[0].split("/")[-1]
                        status = bot.get_chat_member(f"@{channel}", uid).status

                        if status not in ["member", "administrator", "creator"]:
                            cur.execute("DELETE FROM escrow WHERE id=?", (eid,))
                    except:
                        continue

                conn.commit()

        except Exception as e:
            print("unsubscribe worker error:", e)

        time.sleep(60)


# ================= INIT =================
init()
threading.Thread(target=worker, daemon=True).start()
threading.Thread(target=check_unsubscribes, daemon=True).start()

print("🚀 BOT RUNNING")


# ================= UI =================
def kb():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("💎 ЗАРАБОТАТЬ", "👤 ПРОФИЛЬ")
    m.row("🏦 ВЫВОД", "⚙️ АДМИН")
    return m


def admin_kb():
    m = types.ReplyKeyboardMarkup(resize_keyboard=True)
    m.row("📊 СТАТИСТИКА", "💸 ВЫПЛАТЫ")
    m.row("👥 ПОЛЬЗОВАТЕЛИ", "➕ ЗАДАНИЕ")
    m.row("🗑 ЗАДАНИЯ")
    m.row("🔙 ВЫХОД")
    return m


def is_admin(uid):
    return uid == ADMIN_ID


# ================= START =================
@bot.message_handler(commands=['start'])
def start(m):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(id) VALUES(?)", (m.from_user.id,))
        conn.commit()

    bot.send_message(
        m.chat.id,
        "🚀 SaaS система активна\n💰 Заработок включён",
        reply_markup=kb()
    )


# ================= PROFILE =================
@bot.message_handler(func=lambda m: m.text == "👤 ПРОФИЛЬ")
def profile(m):
    process_tasks(m.from_user.id)

    now = datetime.now()

    with get_db() as conn:
        u = conn.execute(
            "SELECT balance FROM users WHERE id=?",
            (m.from_user.id,)
        ).fetchone()

        esc = conn.execute(
            "SELECT amount, release_at FROM escrow WHERE user_id=?",
            (m.from_user.id,)
        ).fetchall()

    if not u:
        return

    total = 0
    min_left = None

    for amount, release_at in esc:
        try:
            dt = datetime.fromisoformat(release_at)
        except:
            continue

        total += amount

        left = (dt - now).total_seconds()

        if left > 0:
            if min_left is None or left < min_left:
                min_left = left

    if min_left:
        days = int(min_left // 86400) + 1
        time_text = f"{days} дн."
    else:
        time_text = "—"

    bot.send_message(
        m.chat.id,
        f"👤 ПРОФИЛЬ\n"
        f"💰 Баланс: {u[0]:.2f} ₴\n"
        f"⏳ В обработке ({time_text}): {total:.2f} ₴"
    )


# ================= EARN =================
@bot.message_handler(func=lambda m: m.text == "💎 ЗАРАБОТАТЬ")
def earn(m):
    with get_db() as conn:
        jobs = conn.execute("SELECT * FROM jobs").fetchall()

    kb = types.InlineKeyboardMarkup()
    for j in jobs:
        kb.add(types.InlineKeyboardButton(f"{j[1]} +{j[3]}₴", callback_data=f"job_{j[0]}"))

    bot.send_message(m.chat.id, "📋 ДОСТУПНЫЕ ЗАДАНИЯ", reply_markup=kb)


# ================= JOB =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("job_"))
def job(c):
    jid = int(c.data.split("_")[1])

    with get_db() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    if not job:
        bot.answer_callback_query(c.id, "Задание не найдено")
        return

    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("🔗 Открыть", url=job[2]))
    kb.add(types.InlineKeyboardButton("✅ Проверить", callback_data=f"check_{jid}"))

    bot.edit_message_text(
        "📌 Подпишись и нажми проверку",
        c.message.chat.id,
        c.message.message_id,
        reply_markup=kb
    )


# ================= CHECK =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("check_"))
def check(c):
    uid = c.from_user.id
    jid = int(c.data.split("_")[1])

    with get_db() as conn:
        cur = conn.cursor()

        # защита от повторного выполнения
        if cur.execute("SELECT id FROM escrow WHERE user_id=? AND job_id=?", (uid, jid)).fetchone():
            bot.answer_callback_query(c.id, "Уже выполнено")
            return

        job = cur.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not job:
            bot.answer_callback_query(c.id, "Ошибка задания")
            return

        try:
            channel = job[2].split("/")[-1]
            status = bot.get_chat_member(f"@{channel}", uid).status

            if status in ["member", "administrator", "creator"]:

                now = datetime.now()
                release_time = now + timedelta(days=ESCROW_DAYS)

                cur.execute("""
                    INSERT INTO escrow(user_id, amount, created_at, release_at, task_id, job_id)
                    VALUES(?,?,?,?,?,?)
                """, (
                    uid,
                    job[3],
                    now.isoformat(),
                    release_time.isoformat(),
                    0,
                    jid
                ))

                conn.commit()

                bot.edit_message_text(
                    "⏳ Проверка пройдена. Средства в обработке (14 дней)",
                    c.message.chat.id,
                    c.message.message_id
                )

            else:
                bot.answer_callback_query(c.id, "Ты не подписан")

        except:
            bot.answer_callback_query(c.id, "Ошибка проверки")


# ================= WITHDRAW (UPDATED) =================
@bot.message_handler(func=lambda m: m.text == "🏦 ВЫВОД")
def withdraw(m):
    uid = m.from_user.id

    with get_db() as conn:
        bal = conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]

    if bal < 100:
        bot.send_message(m.chat.id, "❌ Минимальный вывод: 100₴")
        return

    set_state(uid, "withdraw_amount")
    bot.send_message(m.chat.id, f"💰 Введите сумму вывода:\nДоступно: {bal:.2f}₴")


@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "withdraw_amount")
def withdraw_amount(m):
    uid = m.from_user.id

    try:
        amount = float(m.text)
    except:
        bot.send_message(m.chat.id, "❌ Введите число")
        return

    with get_db() as conn:
        bal = conn.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]

    if amount < 100:
        bot.send_message(m.chat.id, "❌ Минимум 100₴")
        return

    if amount > bal:
        bot.send_message(m.chat.id, "❌ Недостаточно средств")
        return

    set_state(uid, f"withdraw_card_{amount}")
    bot.send_message(m.chat.id, "💳 Введите номер карты:")


@bot.message_handler(func=lambda m: get_state(m.from_user.id).startswith("withdraw_card_"))
def withdraw_card(m):
    uid = m.from_user.id
    state = get_state(uid)

    amount = float(state.split("_")[2])

    with get_db() as conn:
        conn.execute("""
            INSERT INTO withdraws(user_id,card,amount,status,time)
            VALUES(?,?,?,?,?)
        """, (uid, m.text, amount, "pending", datetime.now().isoformat()))

        conn.commit()

    set_state(uid, "")
    bot.send_message(m.chat.id, "✅ Заявка отправлена на обработку")


# ================= ADMIN =================
@bot.message_handler(func=lambda m: m.text == "⚙️ АДМИН")
def admin(m):
    if not is_admin(m.from_user.id):
        return

    bot.send_message(
        m.chat.id,
        "🔐 SaaS Admin Panel\n📊 Real-time control system",
        reply_markup=admin_kb()
    )


@bot.message_handler(func=lambda m: m.text == "🔙 ВЫХОД")
def exit_admin(m):
    bot.send_message(m.chat.id, "Выход из админки", reply_markup=kb())


# ================= STAT =================
@bot.message_handler(func=lambda m: m.text == "📊 СТАТИСТИКА")
def stats(m):
    if not is_admin(m.from_user.id):
        return

    with get_db() as conn:
        u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        t = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        e = conn.execute("SELECT COUNT(*) FROM escrow").fetchone()[0]

    bot.send_message(m.chat.id, f"👥 Пользователи: {u}\n📌 Задания: {t}\n⏳ Escrow: {e}")


# ================= USERS =================
@bot.message_handler(func=lambda m: m.text == "👥 ПОЛЬЗОВАТЕЛИ")
def users(m):
    if not is_admin(m.from_user.id):
        return

    with get_db() as conn:
        rows = conn.execute("SELECT id,balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()

    text = "👥 ТОП ПОЛЬЗОВАТЕЛЕЙ\n\n"
    for i, r in enumerate(rows, 1):
        text += f"{i}. ID: {r[0]} | {r[1]:.2f}₴\n"

    bot.send_message(m.chat.id, text)


# ================= WITHDRAW ADMIN =================
@bot.message_handler(func=lambda m: m.text == "💸 ВЫПЛАТЫ")
def payouts(m):
    if not is_admin(m.from_user.id):
        return

    with get_db() as conn:
        rows = conn.execute("SELECT id,user_id,card,amount FROM withdraws WHERE status='pending'").fetchall()

    if not rows:
        bot.send_message(m.chat.id, "Нет заявок")
        return

    for wid, uid, card, amount in rows:
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("✔ Одобрить", callback_data=f"pay_{wid}"))
        kb.add(types.InlineKeyboardButton("❌ Отклонить", callback_data=f"rej_{wid}"))

        bot.send_message(m.chat.id, f"ID:{uid}\n{card}\n{amount}", reply_markup=kb)


# ================= PAY / REJECT =================
@bot.callback_query_handler(func=lambda c: c.data.startswith("pay_"))
def pay(c):
    if not is_admin(c.from_user.id):
        return

    wid = int(c.data.split("_")[1])

    with get_db() as conn:
        r = conn.execute("SELECT user_id,amount FROM withdraws WHERE id=?", (wid,)).fetchone()

        if r:
            uid, amount = r
            conn.execute("UPDATE users SET balance=balance-? WHERE id=?", (amount, uid))
            conn.execute("UPDATE withdraws SET status='paid' WHERE id=?", (wid,))
            conn.commit()

    bot.answer_callback_query(c.id, "Выплачено")


@bot.callback_query_handler(func=lambda c: c.data.startswith("rej_"))
def rej(c):
    if not is_admin(c.from_user.id):
        return

    wid = int(c.data.split("_")[1])

    with get_db() as conn:
        conn.execute("UPDATE withdraws SET status='rejected' WHERE id=?", (wid,))
        conn.commit()

    bot.answer_callback_query(c.id, "Отклонено")


# ================= JOBS ADMIN =================
@bot.message_handler(func=lambda m: m.text == "🗑 ЗАДАНИЯ")
def jobs_admin(m):
    if not is_admin(m.from_user.id):
        return

    with get_db() as conn:
        jobs = conn.execute("SELECT id,title FROM jobs").fetchall()

    kb = types.InlineKeyboardMarkup()
    for j in jobs:
        kb.add(types.InlineKeyboardButton(f"❌ {j[1]}", callback_data=f"deljob_{j[0]}"))

    bot.send_message(m.chat.id, "🗂 Управление заданиями", reply_markup=kb)


@bot.callback_query_handler(func=lambda c: c.data.startswith("deljob_"))
def del_job(c):
    if not is_admin(c.from_user.id):
        return

    jid = int(c.data.split("_")[1])

    with get_db() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (jid,))
        conn.commit()

    bot.answer_callback_query(c.id, "Удалено")


# ================= ADD JOB =================
@bot.message_handler(func=lambda m: m.text == "➕ ЗАДАНИЕ")
def add_job(m):
    if not is_admin(m.from_user.id):
        return

    set_state(m.from_user.id, "add_job")
    bot.send_message(m.chat.id, "Формат: название | ссылка | награда")


@bot.message_handler(func=lambda m: get_state(m.from_user.id) == "add_job")
def save_job(m):
    try:
        t,u,r = m.text.split("|")

        with get_db() as conn:
            conn.execute("INSERT INTO jobs(title,url,reward) VALUES(?,?,?)",
                         (t.strip(),u.strip(),float(r)))
            conn.commit()

        set_state(m.from_user.id, "")
        bot.send_message(m.chat.id, "✔ Добавлено")

    except:
        bot.send_message(m.chat.id, "❌ Ошибка формата")


# ================= RUN =================
while True:
    try:
        bot.polling(none_stop=True, interval=0, timeout=20)
    except Exception as e:
        print(e)
        time.sleep(3)