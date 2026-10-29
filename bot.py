import asyncio
import os
import re
import html
import aiosqlite
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from googleapiclient.discovery import build


# ============================================================
# CONFIG
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
YT_KEY = os.getenv("YOUTUBE_API_KEY")
YT_CHANNEL = os.getenv("YOUTUBE_CHANNEL_ID")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", 300))
DB_PATH = "bot.db"

if not BOT_TOKEN:
    raise RuntimeError("Не задан BOT_TOKEN в файле .env")
if not YT_KEY:
    raise RuntimeError("Не задан YOUTUBE_API_KEY в файле .env")
if not YT_CHANNEL:
    raise RuntimeError("Не задан YOUTUBE_CHANNEL_ID в файле .env")

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()
youtube = build("youtube", "v3", developerKey=YT_KEY)


# ============================================================
# ACCESS: ONLY @YaZlyka
# ============================================================

ALLOWED_USERNAME = "yazlyka"


class OwnerOnlyMiddleware(BaseMiddleware):
    """Разрешает пользоваться ботом только аккаунту @YaZlyka."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")

        if user and (user.username or "").casefold() == ALLOWED_USERNAME:
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer(
                "⛔ Доступ закрыт. Этот бот доступен только владельцу.",
                show_alert=True,
            )
        elif isinstance(event, Message):
            await event.answer(
                "⛔ <b>Доступ закрыт</b>\n\n"
                "Управление этим ботом доступно только пользователю "
                "<code>@YaZlyka</code>."
            )
        return None


dp.message.middleware(OwnerOnlyMiddleware())
dp.callback_query.middleware(OwnerOnlyMiddleware())


# ============================================================
# FSM
# ============================================================

class SetText(StatesGroup):
    waiting = State()


# ============================================================
# DATABASE
# ============================================================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS posted (
                video_id TEXT PRIMARY KEY,
                posted_at TEXT
            )
        """)

        defaults = {
            "tmpl_video": (
                "🎬 <b>НОВОЕ ВИДЕО</b>\n\n"
                "📺 <b>{name}</b>\n\n"
                "{user}\n\n"
                "▶️ <a href=\"{url}\">Смотреть на YouTube</a>"
            ),
            "tmpl_short": (
                "⚡ <b>НОВЫЙ SHORTS</b>\n\n"
                "📱 <b>{name}</b>\n\n"
                "{user}\n\n"
                "▶️ <a href=\"{url}\">Смотреть Shorts</a>"
            ),
            "tmpl_stream": (
                "🔴 <b>СТРИМ НАЧАЛСЯ</b>\n\n"
                "🎥 <b>{name}</b>\n\n"
                "{user}\n\n"
                "🔴 <a href=\"{url}\">Подключиться к трансляции</a>"
            ),
        }

        for key, value in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()


async def get_setting(key: str) -> str | None:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT value FROM settings WHERE key = ?",
            (key,),
        )
        row = await cur.fetchone()
        return row[0] if row else None


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def is_posted(video_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT 1 FROM posted WHERE video_id = ?",
            (video_id,),
        )
        return await cur.fetchone() is not None


async def mark_posted(video_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO posted (video_id, posted_at) VALUES (?, ?)",
            (video_id, datetime.utcnow().isoformat()),
        )
        await db.commit()


# ============================================================
# YOUTUBE
# ============================================================

ISO_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def parse_duration(iso: str) -> int:
    match = ISO_RE.match(iso or "")
    if not match:
        return 0
    hours, minutes, seconds = (
        int(value) if value else 0 for value in match.groups()
    )
    return hours * 3600 + minutes * 60 + seconds


async def fetch_latest_videos(max_results=10):
    """Возвращает свежие видео: id, title, url, тип и миниатюру."""
    loop = asyncio.get_running_loop()

    def _call():
        search = youtube.search().list(
            channelId=YT_CHANNEL,
            part="id,snippet",
            order="date",
            maxResults=max_results,
            type="video",
        ).execute()

        ids = [
            item["id"]["videoId"]
            for item in search.get("items", [])
            if "videoId" in item.get("id", {})
        ]
        if not ids:
            return []

        details = youtube.videos().list(
            id=",".join(ids),
            part="snippet,contentDetails,liveStreamingDetails",
        ).execute()

        result = []
        for video in details.get("items", []):
            video_id = video["id"]
            snippet = video["snippet"]
            duration = parse_duration(
                video.get("contentDetails", {}).get("duration", "")
            )
            live_status = snippet.get("liveBroadcastContent")

            if live_status == "live":
                kind = "stream"
            elif duration and duration <= 65:
                kind = "short"
            else:
                kind = "video"

            thumbnails = snippet.get("thumbnails", {})
            thumbnail = (
                thumbnails.get("high")
                or thumbnails.get("medium")
                or thumbnails.get("default")
                or {}
            )

            result.append({
                "id": video_id,
                "title": snippet["title"],
                "kind": kind,
                "url": f"https://youtube.com/watch?v={video_id}",
                "thumbnail": thumbnail.get("url"),
            })
        return result

    return await loop.run_in_executor(None, _call)


# ============================================================
# PUBLICATION
# ============================================================

async def build_caption(kind: str, data: dict) -> str:
    key = f"tmpl_{kind}"
    template = await get_setting(key) or "<b>{name}</b>\n{url}"
    channel_name = await get_setting("tg_channel_name") or "канал"
    channel_username = channel_name.lstrip("@")
    channel_link = (
        f'<a href="https://t.me/{html.escape(channel_username, quote=True)}">'
        f'📢 {html.escape(channel_name)}</a>'
    )

    # Сохраняем поддержку пользовательских HTML-шаблонов.
    return (
        template
        .replace("{user}", channel_link)
        .replace("{name}", html.escape(data.get("title", "")))
        .replace("{url}", html.escape(data.get("url", ""), quote=True))
        .replace("{channel}", html.escape(YT_CHANNEL or ""))
    )


async def publish(kind: str, data: dict):
    chat_id = await get_setting("tg_channel_id")
    if not chat_id:
        return

    caption = await build_caption(kind, data)
    try:
        if data.get("thumbnail"):
            await bot.send_photo(
                chat_id=chat_id,
                photo=data["thumbnail"],
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )

        await mark_posted(data["id"])
        print(f"[POSTED] {kind}: {data['title']}")
    except Exception as error:
        print(f"[ERROR publish] {error}")


# ============================================================
# MONITORING
# ============================================================

async def monitor_loop():
    # При первом запуске отмечаем уже существующие видео,
    # чтобы не публиковать архивные ролики.
    first_run = True

    while True:
        try:
            videos = await fetch_latest_videos()
            for video in videos:
                if await is_posted(video["id"]):
                    continue
                if first_run:
                    await mark_posted(video["id"])
                    continue
                await publish(video["kind"], video)

            first_run = False
        except Exception as error:
            print(f"[monitor error] {error}")

        await asyncio.sleep(CHECK_INTERVAL)


# ============================================================
# VISUAL INTERFACE
# ============================================================

def main_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="🎬  Шаблон видео",
                callback_data="edit_video",
            ),
            InlineKeyboardButton(
                text="⚡  Shorts",
                callback_data="edit_short",
            ),
        ],
        [
            InlineKeyboardButton(
                text="🔴  Шаблон стрима",
                callback_data="edit_stream",
            ),
        ],
        [
            InlineKeyboardButton(
                text="📢  Telegram-канал",
                callback_data="pick_channel",
            ),
            InlineKeyboardButton(
                text="👁  Предпросмотр",
                callback_data="preview",
            ),
        ],
        [
            InlineKeyboardButton(
                text="ℹ️  Помощь",
                callback_data="help",
            ),
        ],
    ])


def back_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="◀️  В главное меню",
                callback_data="menu",
            ),
        ],
    ])


def editor_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="💾  Сохранить можно сообщением",
                callback_data="editor_hint",
            ),
        ],
        [
            InlineKeyboardButton(
                text="❌  Отмена",
                callback_data="menu",
            ),
        ],
    ])


def header(title: str, subtitle: str = "") -> str:
    text = f"✨ <b>{title}</b>"
    if subtitle:
        text += f"\n<i>{subtitle}</i>"
    return text


@dp.message(Command("start"))
async def cmd_start(message: Message):
    channel_name = await get_setting("tg_channel_name")
    channel_status = (
        f"🟢 <b>Канал подключён:</b> {html.escape(channel_name)}"
        if channel_name
        else "🟡 <b>Канал ещё не выбран</b>"
    )

    await message.answer(
        "╭────────────────────╮\n"
        "     🚀 <b>YOUTUBE AUTOPостер</b>\n"
        "╰────────────────────╯\n\n"
        "Привет, <b>@YaZlyka</b>! 👋\n"
        "Здесь ты можешь оформить публикации и настроить автопостинг.\n\n"
        f"{channel_status}\n\n"
        "👇 <b>Выбери нужный раздел:</b>",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "menu")
async def cb_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "⚙️ <b>ГЛАВНОЕ МЕНЮ</b>\n\n"
        "Настрой оформление постов, выбери канал или посмотри результат "
        "до публикации.\n\n"
        "🎬 Видео  ·  ⚡ Shorts  ·  🔴 Стримы",
        reply_markup=main_menu(),
    )
    await callback.answer()


@dp.callback_query(F.data == "help")
async def cb_help(callback: CallbackQuery):
    await callback.message.edit_text(
        "🧭 <b>КАК ПОЛЬЗОВАТЬСЯ</b>\n\n"
        "1. 📢 Выбери Telegram-канал для публикаций.\n"
        "2. 🎬 Открой нужный шаблон.\n"
        "3. ✍️ Отправь новый текст отдельным сообщением.\n"
        "4. 👁 Проверь результат через предпросмотр.\n\n"
        "🧩 <b>Переменные шаблона</b>\n"
        "<code>{name}</code> — название видео\n"
        "<code>{url}</code> — ссылка на YouTube\n"
        "<code>{user}</code> — ссылка на Telegram-канал\n"
        "<code>{channel}</code> — ID YouTube-канала\n\n"
        "💎 Можно использовать HTML-оформление и Telegram Premium-эмодзи.",
        reply_markup=back_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "editor_hint")
async def cb_editor_hint(callback: CallbackQuery):
    await callback.answer(
        "Отправь текст следующим сообщением — он сохранится как шаблон.",
        show_alert=True,
    )


@dp.callback_query(F.data.in_({"edit_video", "edit_short", "edit_stream"}))
async def cb_edit(callback: CallbackQuery, state: FSMContext):
    kind = callback.data.replace("edit_", "")
    key = f"tmpl_{kind}"
    current = await get_setting(key) or "(шаблон пока пуст)"
    names = {
        "video": "обычного видео 🎬",
        "short": "Shorts ⚡",
        "stream": "стрима 🔴",
    }

    await state.set_state(SetText.waiting)
    await state.update_data(key=key)

    await callback.message.edit_text(
        f"✏️ <b>РЕДАКТОР: {names[kind].upper()}</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "📄 <b>Текущий шаблон</b>\n"
        f"<pre>{html.escape(current)}</pre>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Пришли следующим сообщением новый текст шаблона.\n\n"
        "🧩 <b>Доступные переменные:</b>\n"
        "<code>{name}</code> · <code>{url}</code>\n"
        "<code>{user}</code> · <code>{channel}</code>\n\n"
        "💡 Можно отправить форматированный текст или сообщение "
        "с Premium-эмодзи.",
        reply_markup=back_kb(),
    )
    await callback.answer()


@dp.message(SetText.waiting)
async def save_text(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("key")

    if not key:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось определить шаблон. Открой редактор заново.",
            reply_markup=main_menu(),
        )
        return

    text = message.html_text or message.text or message.caption
    if not text:
        await message.answer(
            "⚠️ Не получилось прочитать текст. Отправь текстовое сообщение "
            "или форматированный текст ещё раз.",
            reply_markup=back_kb(),
        )
        return

    await set_setting(key, text)
    await state.clear()

    await message.answer(
        "✅ <b>ШАБЛОН СОХРАНЁН</b>\n\n"
        "Изменения записаны. Можешь посмотреть результат в предпросмотре "
        "или выбрать другой раздел.",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "pick_channel")
async def cb_pick_channel(callback: CallbackQuery):
    channel_name = await get_setting("tg_channel_name")
    channel_id = await get_setting("tg_channel_id")

    if channel_name and channel_id:
        status = (
            "🟢 <b>Текущий канал</b>\n"
            f"📢 {html.escape(channel_name)}\n"
            f"🆔 <code>{html.escape(channel_id)}</code>\n\n"
        )
    else:
        status = "🟡 <b>Канал пока не подключён</b>\n\n"

    await callback.message.edit_text(
        "📢 <b>TELEGRAM-КАНАЛ</b>\n\n"
        f"{status}"
        "Чтобы подключить канал:\n\n"
        "① Добавь бота в канал администратором с правом публикации.\n"
        "② Перешли сюда сообщение из этого канала\n"
        "   или отправь его <code>@username</code>.\n\n"
        "🔐 Бот проверит доступ и сохранит выбранный канал.",
        reply_markup=back_kb(),
    )
    await callback.answer()


@dp.message(F.forward_from_chat)
async def set_channel_by_forward(message: Message):
    chat = message.forward_from_chat
    if chat.type != "channel":
        await message.answer(
            "⚠️ Это сообщение не из канала.",
            reply_markup=back_kb(),
        )
        return

    try:
        me = await bot.get_chat_member(chat.id, (await bot.me()).id)
        if me.status not in ("administrator", "creator"):
            await message.answer(
                "⛔ <b>Недостаточно прав</b>\n\n"
                "Сначала добавь бота администратором канала "
                "с разрешением публиковать сообщения.",
                reply_markup=back_kb(),
            )
            return
    except Exception:
        await message.answer(
            "⚠️ Не удалось проверить права бота.\n"
            "Убедись, что он добавлен администратором канала.",
            reply_markup=back_kb(),
        )
        return

    await set_setting("tg_channel_id", str(chat.id))
    await set_setting("tg_channel_name", chat.username or chat.title)

    await message.answer(
        "✅ <b>КАНАЛ ПОДКЛЮЧЁН</b>\n\n"
        f"📢 <b>{html.escape(chat.title)}</b>\n"
        f"🆔 <code>{chat.id}</code>\n\n"
        "Теперь новые видео смогут публиковаться в этом канале.",
        reply_markup=main_menu(),
    )


@dp.message(F.text.regexp(r"^@\w+"))
async def set_channel_by_username(message: Message):
    username = message.text.strip().split()[0]
    try:
        chat = await bot.get_chat(username)
        await set_setting("tg_channel_id", str(chat.id))
        await set_setting("tg_channel_name", chat.username or username.lstrip("@"))

        await message.answer(
            "✅ <b>КАНАЛ СОХРАНЁН</b>\n\n"
            f"📢 <b>{html.escape(chat.title)}</b>\n"
            f"🆔 <code>{chat.id}</code>\n\n"
            "Проверь, что бот добавлен администратором и может публиковать посты.",
            reply_markup=main_menu(),
        )
    except Exception:
        await message.answer(
            "❌ <b>Не удалось открыть канал</b>\n\n"
            "Проверь username и добавь бота администратором канала, "
            "затем попробуй ещё раз.",
            reply_markup=back_kb(),
        )


@dp.callback_query(F.data == "preview")
async def cb_preview(callback: CallbackQuery):
    demo = {
        "id": "demo",
        "title": "Пример названия нового видео",
        "url": "https://youtu.be/dQw4w9WgXcQ",
    }

    parts = []
    for kind, label in [
        ("video", "🎬 ОБЫЧНОЕ ВИДЕО"),
        ("short", "⚡ YOUTUBE SHORTS"),
        ("stream", "🔴 ПРЯМОЙ ЭФИР"),
    ]:
        caption = await build_caption(kind, demo)
        parts.append(f"<b>{label}</b>\n\n{caption}")

    text = (
        "👁 <b>ПРЕДПРОСМОТР ОФОРМЛЕНИЯ</b>\n"
        "<i>Пример того, как могут выглядеть публикации</i>\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        + "\n\n━━━━━━━━━━━━━━━━━━\n\n".join(parts)
    )

    # Ограничиваем длину предпросмотра лимитом Telegram для одного сообщения.
    if len(text) > 3900:
        text = text[:3850] + "\n\n<i>Предпросмотр сокращён — шаблоны длинные.</i>"

    await callback.message.edit_text(
        text,
        reply_markup=back_kb(),
        disable_web_page_preview=True,
    )
    await callback.answer()


# ============================================================
# STARTUP
# ============================================================

async def main():
    await init_db()
    await bot.set_my_commands([
        BotCommand(command="start", description="Открыть главное меню"),
    ])

    asyncio.create_task(monitor_loop())
    print("🚀 YouTube Autoposter запущен. Доступ: @YaZlyka")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
