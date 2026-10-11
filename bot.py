import asyncio
import json
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}
PORT = int(os.getenv("PORT", "10000"))

CHANNEL_ID = os.getenv("CHANNEL_ID", "@luxe_client")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/luxe_client")

BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "files"
DB_PATH = BASE_DIR / "files.json"

FILES_DIR.mkdir(exist_ok=True)

router = Router()

SECTIONS = {
    "shop": "Магазин",
    "services": "Услуги",
    "files": "Файлы",
}

SECTION_EMOJI = {"shop": "🛍", "services": "🛠", "files": "📁"}


class AddItem(StatesGroup):
    waiting_file = State()
    waiting_name = State()
    waiting_price = State()


class Support(StatesGroup):
    waiting_question = State()


def load_catalog() -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
        # миграция старых записей без section -> files
        changed = False
        for x in data:
            if "section" not in x:
                x["section"] = "files"
                changed = True
        if changed:
            save_catalog(data)
        return data
    except (json.JSONDecodeError, OSError):
        return []


def save_catalog(catalog: list[dict]) -> None:
    DB_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def section_of(item: dict) -> str:
    return item.get("section", "files")


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if is_admin(user_id):
        return True
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        status = getattr(member, "status", "")
        if status in ("member", "administrator", "creator"):
            return True
        if status == "restricted" and bool(getattr(member, "is_member", False)):
            return True
        return False
    except Exception:
        return False


def sub_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="Проверить подписку", callback_data="check_sub")],
    ])


async def require_sub(message: Message, bot: Bot) -> bool:
    if await is_subscribed(bot, message.from_user.id):
        return True
    await message.answer(
        "Бот доступен только подписчикам канала.\nПодпишись и нажми «Проверить подписку».",
        reply_markup=sub_keyboard(),
    )
    return False


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Магазин", callback_data="menu:shop")],
        [InlineKeyboardButton(text="🛠 Услуги", callback_data="menu:services")],
        [InlineKeyboardButton(text="📁 Файлы", callback_data="menu:files")],
        [InlineKeyboardButton(text="🆘 Поддержка", callback_data="menu:support")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="← В меню", callback_data="menu:home")],
    ])


def section_items_keyboard(items: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=item["name"][:64], callback_data=f"item:{item['id']}")]
        for item in items
    ]
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def item_card_keyboard(item: dict) -> InlineKeyboardMarkup:
    sec = section_of(item)
    rows = []
    if sec in ("shop", "services"):
        rows.append([InlineKeyboardButton(text="Заказать", callback_data=f"order:{item['id']}")])
        rows.append([InlineKeyboardButton(text="← Назад", callback_data=f"menu:{sec}")])
    else:
        rows.append([InlineKeyboardButton(text="← Назад", callback_data="menu:files")])
    rows.append([InlineKeyboardButton(text="← В меню", callback_data="menu:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍ Написать в поддержку", callback_data="support_write")],
        [InlineKeyboardButton(text="← В меню", callback_data="menu:home")],
    ])


def format_price(item: dict) -> str:
    price = (item.get("price") or "").strip()
    return f"\n💰 Цена: {price}" if price else ""


async def send_stored_file(message: Message, item: dict) -> None:
    tg_file_id = item.get("tg_file_id")
    file_type = item.get("file_type", "document")
    caption = item["name"] + format_price(item)
    if tg_file_id:
        if file_type == "photo":
            await message.answer_photo(tg_file_id, caption=caption)
        elif file_type == "video":
            await message.answer_video(tg_file_id, caption=caption)
        elif file_type == "audio":
            await message.answer_audio(tg_file_id, caption=caption)
        elif file_type == "voice":
            await message.answer_voice(tg_file_id, caption=caption)
        else:
            await message.answer_document(tg_file_id, caption=caption)
        return
    stored = item.get("stored_name")
    if not stored:
        await message.answer("Файл нужно перезалить через /add_file (старый формат).")
        return
    path = FILES_DIR / stored
    if not path.exists():
        await message.answer("Файл не найден, попроси админа перезалить.")
        return
    await message.answer_document(
        FSInputFile(path, filename=item.get("original_name", "file")),
        caption=caption,
    )


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot):
    if not await require_sub(message, bot):
        return
    await message.answer("Выбери раздел:", reply_markup=main_menu())


@router.message(Command("list"))
async def cmd_list(message: Message, bot: Bot):
    if not await require_sub(message, bot):
        return
    await message.answer("Выбери раздел:", reply_markup=main_menu())


@router.callback_query(F.data == "check_sub")
async def cb_check_sub(callback: CallbackQuery, bot: Bot):
    if await is_subscribed(bot, callback.from_user.id):
        await callback.answer("Подписка подтверждена!")
        await callback.message.answer("Выбери раздел:", reply_markup=main_menu())
    else:
        await callback.answer("Ты еще не подписан на канал", show_alert=True)


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery, bot: Bot):
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишись на канал", show_alert=True)
        await callback.message.answer(
            "Бот доступен только подписчикам канала.",
            reply_markup=sub_keyboard(),
        )
        return
    sec = callback.data.split(":", 1)[1]
    await callback.answer()
    if sec == "home":
        await callback.message.answer("Выбери раздел:", reply_markup=main_menu())
        return
    if sec == "support":
        await callback.message.answer(
            "🆘 Поддержка.\nНажми кнопку ниже и опиши проблему — сообщение уйдет админу.",
            reply_markup=support_keyboard(),
        )
        return
    catalog = [x for x in load_catalog() if section_of(x) == sec]
    title = SECTIONS.get(sec, sec)
    if not catalog:
        await callback.message.answer(
            f"{SECTION_EMOJI.get(sec, '')} {title}: пока пусто.",
            reply_markup=back_menu(),
        )
        return
    await callback.message.answer(
        f"{SECTION_EMOJI.get(sec, '')} {title}: выбери позицию:",
        reply_markup=section_items_keyboard(catalog),
    )


@router.callback_query(F.data.startswith("item:"))
async def cb_item(callback: CallbackQuery, bot: Bot):
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишись на канал", show_alert=True)
        return
    item_id = callback.data.split(":", 1)[1]
    item = next((x for x in load_catalog() if x["id"] == item_id), None)
    if not item:
        await callback.answer("Позиция удалена", show_alert=True)
        return
    await callback.answer()
    sec = section_of(item)
    if sec == "files":
        await send_stored_file(callback.message, item)
        return
    await send_stored_file(callback.message, item)
    await callback.message.answer(
        f"{item['name']}{format_price(item)}\n\nЧтобы заказать — нажми кнопку.",
        reply_markup=item_card_keyboard(item),
    )


@router.callback_query(F.data.startswith("order:"))
async def cb_order(callback: CallbackQuery, bot: Bot):
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишись на канал", show_alert=True)
        return
    item_id = callback.data.split(":", 1)[1]
    item = next((x for x in load_catalog() if x["id"] == item_id), None)
    if not item:
        await callback.answer("Позиция удалена", show_alert=True)
        return
    user = callback.from_user
    uname = f"@{user.username}" if user.username else "(без username)"
    text = (
        f"🛒 Новый заказ!\n"
        f"Раздел: {SECTIONS.get(section_of(item), '')}\n"
        f"Позиция: {item['name']}{format_price(item)}\n"
        f"Клиент: {user.full_name} {uname} (id={user.id})"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text)
            if item.get("tg_file_id"):
                await bot.send_message(admin_id, f"ID позиции: {item['id']}")
        except Exception:
            pass
    await callback.answer("Заказ отправлен!")
    await callback.message.answer("Заказ принят! Админ свяжется с тобой.")


@router.callback_query(F.data == "support_write")
async def cb_support_write(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not await is_subscribed(bot, callback.from_user.id):
        await callback.answer("Сначала подпишись на канал", show_alert=True)
        return
    await state.set_state(Support.waiting_question)
    await callback.answer()
    await callback.message.answer("Опиши проблему одним сообщением.")


@router.message(Support.waiting_question, F.text)
async def support_got_question(message: Message, state: FSMContext, bot: Bot):
    user = message.from_user
    uname = f"@{user.username}" if user.username else "(без username)"
    await state.clear()
    if not ADMIN_IDS:
        await message.answer("Поддержка пока не настроена.")
        return
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"📩 Вопрос в поддержку\nОт: {user.full_name} {uname} (id={user.id})\n\n{message.text}\n\nОтветить: /reply {user.id} текст",
            )
        except Exception:
            pass
    await message.answer("Сообщение отправлено! Админ ответит сюда.", reply_markup=main_menu())


@router.message(Command("reply"))
async def cmd_reply(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer("Формат: /reply user_id текст")
        return
    try:
        await bot.send_message(int(parts[1]), f"💬 Ответ поддержки:\n{parts[2]}")
        await message.answer("Отправлено.")
    except Exception as e:
        await message.answer(f"Не получилось: {e}")


# --- Добавление (только админ) ---

@router.message(Command("add"))
async def cmd_add(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может добавлять.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 В Магазин", callback_data="addsec:shop")],
        [InlineKeyboardButton(text="🛠 В Услуги", callback_data="addsec:services")],
        [InlineKeyboardButton(text="📁 В Файлы", callback_data="addsec:files")],
    ])
    await message.answer("В какой раздел добавить?", reply_markup=kb)


@router.message(Command("add_shop"))
async def cmd_add_shop(message: Message, state: FSMContext):
    await start_add(message, state, "shop", message.from_user.id)


@router.message(Command("add_service"))
async def cmd_add_service(message: Message, state: FSMContext):
    await start_add(message, state, "services", message.from_user.id)


@router.message(Command("add_file"))
async def cmd_add_file(message: Message, state: FSMContext):
    await start_add(message, state, "files", message.from_user.id)


async def start_add(target: Message, state: FSMContext, section: str, user_id: int):
    if not is_admin(user_id):
        await target.answer("Только админ может добавлять.")
        return
    await state.update_data(section=section)
    await state.set_state(AddItem.waiting_file)
    await target.answer(
        f"Раздел: {SECTIONS[section]}.\nПришли файл (фото/видео/документ)."
    )


@router.callback_query(F.data.startswith("addsec:"))
async def cb_addsec(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    await start_add(callback.message, state, callback.data.split(":", 1)[1], callback.from_user.id)
    await callback.answer()


@router.message(AddItem.waiting_file, F.document | F.photo | F.video | F.audio | F.voice)
async def add_got_file(message: Message, state: FSMContext):
    if message.document:
        tg_file_id = message.document.file_id
        original_name = message.document.file_name or "file.bin"
        file_type = "document"
    elif message.photo:
        tg_file_id = message.photo[-1].file_id
        original_name = "photo.jpg"
        file_type = "photo"
    elif message.video:
        tg_file_id = message.video.file_id
        original_name = message.video.file_name or "video.mp4"
        file_type = "video"
    elif message.audio:
        tg_file_id = message.audio.file_id
        original_name = message.audio.file_name or "audio.mp3"
        file_type = "audio"
    else:
        tg_file_id = message.voice.file_id
        original_name = "voice.ogg"
        file_type = "voice"
    await state.update_data(
        tg_file_id=tg_file_id, original_name=original_name, file_type=file_type
    )
    await state.set_state(AddItem.waiting_name)
    await message.answer("Файл получил. Пришли название позиции.")


@router.message(AddItem.waiting_name, F.text)
async def add_got_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text.strip()[:100])
    data = await state.get_data()
    if data.get("section") in ("shop", "services"):
        await state.set_state(AddItem.waiting_price)
        await message.answer("Теперь пришли цену (например: 500 ₽). Или отправь «-» если без цены.")
    else:
        await finish_add(message, state)


@router.message(AddItem.waiting_price, F.text)
async def add_got_price(message: Message, state: FSMContext):
    price = message.text.strip()[:50]
    await state.update_data(price="" if price == "-" else price)
    await finish_add(message, state)


async def finish_add(message: Message, state: FSMContext):
    data = await state.get_data()
    catalog = load_catalog()
    new_id = str(max([int(x["id"]) for x in catalog] + [0]) + 1)
    catalog.append({
        "id": new_id,
        "section": data.get("section", "files"),
        "name": data.get("name", "Без названия"),
        "price": data.get("price", ""),
        "original_name": data["original_name"],
        "file_type": data.get("file_type", "document"),
        "tg_file_id": data["tg_file_id"],
    })
    save_catalog(catalog)
    await state.clear()
    await message.answer(
        f"Готово! «{data.get('name')}» в разделе {SECTIONS[data.get('section', 'files')]}.",
        reply_markup=main_menu(),
    )


@router.message(Command("del"))
async def cmd_del(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может удалять.")
        return
    catalog = load_catalog()
    if not catalog:
        await message.answer("Удалять нечего.")
        return
    rows = [
        [InlineKeyboardButton(
            text=f"{SECTION_EMOJI.get(section_of(x), '')} {x['name'][:60]}",
            callback_data=f"del:{x['id']}",
        )]
        for x in catalog
    ]
    await message.answer("Что удалить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data.startswith("del:"))
async def cb_del_file(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет прав", show_alert=True)
        return
    file_id = callback.data.split(":", 1)[1]
    catalog = load_catalog()
    item = next((x for x in catalog if x["id"] == file_id), None)
    if not item:
        await callback.answer("Уже удалён", show_alert=True)
        return
    if item.get("stored_name"):
        (FILES_DIR / item["stored_name"]).unlink(missing_ok=True)
    catalog = [x for x in catalog if x["id"] != file_id]
    save_catalog(catalog)
    await callback.answer("Удалено")
    await callback.message.edit_text(f"Удалён: {item['name']}")


# --- Мини веб-сервер для бесплатного хостинга ---
async def health(request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def start_webserver() -> None:
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web healthcheck на порту {PORT}")


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "вставь_сюда_токен_от_BotFather":
        raise RuntimeError("Нет токена. Локально создай .env, на хостинге задай Env Vars.")
    await start_webserver()
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    print("Бот запущен.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
