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
# Render и другие хостинги дают порт в переменной PORT
PORT = int(os.getenv("PORT", "10000"))

BASE_DIR = Path(__file__).parent
FILES_DIR = BASE_DIR / "files"
DB_PATH = BASE_DIR / "files.json"

FILES_DIR.mkdir(exist_ok=True)

router = Router()


class AddFile(StatesGroup):
    waiting_file = State()
    waiting_name = State()


def load_catalog() -> list[dict]:
    if not DB_PATH.exists():
        return []
    try:
        return json.loads(DB_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def save_catalog(catalog: list[dict]) -> None:
    DB_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def files_keyboard(catalog: list[dict], prefix: str = "get") -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=item["name"][:64], callback_data=f"{prefix}:{item['id']}")]
        for item in catalog
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("start"))
async def cmd_start(message: Message):
    catalog = load_catalog()
    if not catalog:
        await message.answer(
            "Привет! Пока файлов нет. Загляни позже."
            + ("\n\nТы админ: добавь файл через /add" if is_admin(message.from_user.id) else "")
        )
        return
    await message.answer(
        "Привет! Выбери файл для скачивания:",
        reply_markup=files_keyboard(catalog),
    )


@router.message(Command("list"))
async def cmd_list(message: Message):
    catalog = load_catalog()
    if not catalog:
        await message.answer("Каталог пуст.")
        return
    await message.answer(
        "Доступные файлы:",
        reply_markup=files_keyboard(catalog),
    )


@router.callback_query(F.data.startswith("get:"))
async def cb_get_file(callback: CallbackQuery):
    file_id = callback.data.split(":", 1)[1]
    catalog = load_catalog()
    item = next((x for x in catalog if x["id"] == file_id), None)
    if not item:
        await callback.answer("Файл удалён", show_alert=True)
        return
    await callback.answer()

    # Новый формат (бесплатный хостинг): файл хранится в Telegram по file_id,
    # скачивать на диск не нужно — переживает перезапуски.
    tg_file_id = item.get("tg_file_id")
    file_type = item.get("file_type", "document")
    if tg_file_id:
        if file_type == "photo":
            await callback.message.answer_photo(tg_file_id, caption=item["name"])
        elif file_type == "video":
            await callback.message.answer_video(tg_file_id, caption=item["name"])
        elif file_type == "audio":
            await callback.message.answer_audio(tg_file_id, caption=item["name"])
        elif file_type == "voice":
            await callback.message.answer_voice(tg_file_id, caption=item["name"])
        else:
            await callback.message.answer_document(tg_file_id, caption=item["name"])
        return

    # Старый формат: файл лежал в папке files/ (для совместимости локально)
    stored = item.get("stored_name")
    if not stored:
        await callback.message.answer("Файл нужно перезалить через /add (старый формат).")
        return
    path = FILES_DIR / stored
    if not path.exists():
        await callback.answer("Файл не найден на сервере, попроси админа перезалить.", show_alert=True)
        return
    await callback.message.answer_document(
        FSInputFile(path, filename=item.get("original_name", "file")),
        caption=item["name"],
    )


# --- Добавление (только админ) ---

@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может добавлять файлы.")
        return
    await state.set_state(AddFile.waiting_file)
    await message.answer("Пришли мне файл (документ, фото, видео, аудио).")


@router.message(AddFile.waiting_file, F.document | F.photo | F.video | F.audio | F.voice)
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
    await state.set_state(AddFile.waiting_name)
    await message.answer("Файл получил. Теперь пришли название для кнопки (например: «Прайс 2026»).")


@router.message(AddFile.waiting_name, F.text)
async def add_got_name(message: Message, state: FSMContext):
    data = await state.get_data()
    name = message.text.strip()[:100]

    catalog = load_catalog()
    new_id = str(max([int(x["id"]) for x in catalog] + [0]) + 1)

    catalog.append({
        "id": new_id,
        "name": name,
        "original_name": data["original_name"],
        "file_type": data.get("file_type", "document"),
        "tg_file_id": data["tg_file_id"],
        # stored_name больше не нужен, оставляем для старых записей
    })
    save_catalog(catalog)
    await state.clear()
    await message.answer(
        f"Готово! «{name}» добавлен.",
        reply_markup=files_keyboard(catalog),
    )


@router.message(Command("del"))
async def cmd_del(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Только админ может удалять файлы.")
        return
    catalog = load_catalog()
    if not catalog:
        await message.answer("Удалять нечего.")
        return
    await message.answer(
        "Что удалить?",
        reply_markup=files_keyboard(catalog, prefix="del"),
    )


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
    # удаляем локальный файл если был (старый формат)
    if item.get("stored_name"):
        (FILES_DIR / item["stored_name"]).unlink(missing_ok=True)
    catalog = [x for x in catalog if x["id"] != file_id]
    save_catalog(catalog)
    await callback.answer("Удалено")
    await callback.message.edit_text(f"Удалён: {item['name']}")


# --- Мини веб-сервер для бесплатного хостинга ---
# Render требует открытый порт, иначе гасит сервис.
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
