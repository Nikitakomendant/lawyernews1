# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
#                         ОСНОВНОЙ УПРАВЛЯЮЩИЙ ФАЙЛ
# ---------------------------------------------------------------------------
# Это точка входа в приложение. Он инициализирует все компоненты,
# настраивает планировщик и запускает основной цикл работы бота.
# ---------------------------------------------------------------------------

import asyncio
import logging
import random
import re
import io
import requests
from telegram import Bot, InputFile
from telegram.error import TelegramError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

# --- Импорт наших модулей и конфигурации ---
from config import (
    TELEGRAM_TOKEN, CHANNEL_ID, POSTS_PER_DAY,
    TIMEZONE, START_HOUR, END_HOUR, CHANNEL_LINK
)
import data_fetcher
import ai_content_processor

# --- Настройка системы логирования ---
# Логи будут выводиться и в консоль, и в файл (если нужно будет добавить FileHandler)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
# Приглушаем слишком "болтливые" логгеры от сторонних библиотек
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('apscheduler').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# --- Инициализация основных компонентов ---
bot = Bot(token=TELEGRAM_TOKEN)
scheduler = AsyncIOScheduler(timezone=timezone(TIMEZONE))


def _escape_md_v2_preserving_formatting(text: str) -> str:
    """Escape MarkdownV2 while preserving bold/italic markers."""
    # Convert underscore formatting to asterisk variants
    text = re.sub(r"__([^_\n]+?)__", r"**\1**", text)
    text = re.sub(r"_([^_\n]+?)_", r"*\1*", text)

    # Mask formatting tokens
    BOLD_MASK = "\ue001"
    ITALIC_MASK = "\ue002"
    text = text.replace("**", BOLD_MASK)
    text = text.replace("*", ITALIC_MASK)

    # Escape backslashes first
    text = text.replace("\\", "\\\\")

    # Escape remaining special characters for MarkdownV2
    text = re.sub(r"([_\[\]\(\)~`>#+\-=|{}\.!])", r"\\\1", text)

    # Unmask
    text = text.replace(BOLD_MASK, "**")
    text = text.replace(ITALIC_MASK, "*")
    return text


def _truncate_markdown_v2_safely(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    open_bold = 0
    open_italic = 0
    i = 0
    last_safe_space = -1
    while i < min(len(text), limit):
        ch = text[i]
        prev_backslash = (i > 0 and text[i - 1] == "\\")
        if i + 1 < len(text) and text[i:i + 2] == "**" and not prev_backslash:
            open_bold ^= 1
            i += 2
            continue
        if ch == "*" and not prev_backslash:
            is_double = (i + 1 < len(text) and text[i + 1] == "*")
            if not is_double:
                open_italic ^= 1
        if ch == " " and open_bold == 0 and open_italic == 0:
            last_safe_space = i
        i += 1
    cut_index = last_safe_space if last_safe_space != -1 else max(0, limit - 1)
    truncated = text[:cut_index].rstrip()
    return truncated + "…"


def prepare_markdown_v2(text: str, limit: int | None = None) -> str:
    escaped = _escape_md_v2_preserving_formatting(text)
    if limit is not None and len(escaped) > limit:
        escaped = _truncate_markdown_v2_safely(escaped, limit)
    return escaped


def _build_input_file_from_url(url: str) -> InputFile | None:
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        content_type = resp.headers.get('Content-Type', '').lower()
        if 'image' not in content_type or 'svg' in content_type:
            return None
        ext = '.jpg'
        if 'png' in content_type:
            ext = '.png'
        elif 'webp' in content_type:
            ext = '.webp'
        elif 'jpeg' in content_type or 'jpg' in content_type:
            ext = '.jpg'
        bio = io.BytesIO(resp.content)
        bio.seek(0)
        return InputFile(bio, filename=f"image{ext}")
    except Exception:
        return None


async def send_to_telegram(post_text: str, image_url: str | None):
    """
    Отправляет финальный пост в Telegram-канал.
    Сначала пытается отправить с фото, при неудаче - отправляет как текст.
    """
    # --- ИСПРАВЛЕНИЕ: Создаем готовую MarkdownV2 ссылку, которую не нужно экранировать ---
    link_md2 = f"\n\n[Перейти до каналу]({CHANNEL_LINK})"

    # --- ИСПРАВЛЕНИЕ: Формируем простой текст для fallback-сценариев ---
    plain_text_link = f"\n\nПерейти до каналу: {CHANNEL_LINK}"
    full_plain_text = post_text + plain_text_link

    photo_sent = False
    if image_url:
        # --- ИСПРАВЛЕНИЕ: Экранируем только основной текст, а ссылку добавляем после ---
        base_caption_md2 = prepare_markdown_v2(post_text)

        # Проверяем, помещается ли текст с ссылкой в лимит подписи (1024)
        if len(base_caption_md2) + len(link_md2) > 1024:
            new_limit = 1024 - len(link_md2)
            base_caption_md2 = _truncate_markdown_v2_safely(base_caption_md2, new_limit)
            logger.warning("Текст поста был обрезан для подписи к фото.")

        final_caption_md2 = base_caption_md2 + link_md2

        try:
            await bot.send_photo(
                chat_id=CHANNEL_ID,
                photo=image_url,
                caption=final_caption_md2,
                parse_mode="MarkdownV2"
            )
            logger.info("Пост успешно отправлен в канал с изображением.")
            photo_sent = True
        except TelegramError as e:
            logger.error(f"Ошибка Telegram API при отправке фото по URL: {e}. Пробую загрузить и отправить файл.")
            try:
                input_file = _build_input_file_from_url(image_url)
                if input_file:
                    await bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=input_file,
                        caption=final_caption_md2,  # Используем ту же готовую подпись
                        parse_mode="MarkdownV2"
                    )
                    logger.info("Пост с изображением отправлен через загрузку файла.")
                    photo_sent = True
                else:
                    raise TelegramError("Не удалось подготовить файл изображения для загрузки.")
            except TelegramError as e2:
                logger.error(f"Ошибка при отправке загруженного изображения: {e2}. Отправляю как текст.")
                # Если ничего не помогло, переменная photo_sent останется False, и код перейдет к отправке текста
        except Exception as e:
            logger.error(f"Неизвестная ошибка при отправке фото {image_url}: {e}. Попробую отправить как текст.")

    # Если отправка с фото не удалась или изображения не было
    if not photo_sent:
        try:
            # --- ИСПРАВЛЕНИЕ: Экранируем только основной текст, а ссылку добавляем в конце ---
            base_text_md2 = prepare_markdown_v2(post_text)
            final_text_md2 = base_text_md2 + link_md2

            # Убедимся, что не превышаем общий лимит в 4096 символов
            if len(final_text_md2) > 4096:
                new_limit = 4096 - len(link_md2)
                base_text_md2 = _truncate_markdown_v2_safely(base_text_md2, new_limit)
                final_text_md2 = base_text_md2 + link_md2

            await bot.send_message(
                chat_id=CHANNEL_ID,
                text=final_text_md2,
                parse_mode="MarkdownV2",
                disable_web_page_preview=True  # Отключаем превью для ссылки на свой же канал
            )
            logger.info("Пост успешно отправлен в канал как текстовое сообщение.")
        except TelegramError as e:
            logger.error(f"Ошибка Telegram API при отправке текста с MarkdownV2: {e}. Пробую без разметки.")
            try:
                # Используем заранее подготовленный простой текст
                plain_text = full_plain_text if len(full_plain_text) <= 4096 else full_plain_text[:4092] + "..."
                await bot.send_message(
                    chat_id=CHANNEL_ID,
                    text=plain_text,
                    parse_mode=None,
                    disable_web_page_preview=True
                )
                logger.info("Пост отправлен как простой текст без разметки.")
            except TelegramError as e2:
                logger.critical(f"Критическая ошибка Telegram API при отправке текста без разметки: {e2}")


async def process_and_post_news():
    """
    Основной рабочий цикл: от поиска новости до ее публикации.
    """
    logger.info("--- Запуск нового цикла обработки новости ---")
    try:
        # 1. Получаем ссылку на последнюю неопубликованную новость
        _, article_url = data_fetcher.get_latest_news_from_rss()
        if not article_url:
            logger.info("Новых статей для публикации не найдено. Цикл завершен.")
            return

        # 2. Скрапим контент со страницы статьи
        scraped_data = data_fetcher.scrape_article_content(article_url)
        if not scraped_data or not scraped_data.get("raw_text"):
            logger.error(f"Не удалось получить контент со страницы: {article_url}")
            return

        # 3. Генерируем текст поста с помощью ИИ
        generated_post = ai_content_processor.generate_news_post(scraped_data["raw_text"])
        if not generated_post:
            logger.error("Не удалось сгенерировать текст поста. Публикация отменена.")
            return

        # 4. Выбираем лучшее изображение с помощью ИИ
        best_image_url = None
        if scraped_data.get("image_urls"):
            best_image_url = ai_content_processor.select_best_image(
                image_urls=scraped_data["image_urls"],
                post_text=generated_post
            )
        else:
            logger.info("В статье не найдено изображений для анализа.")

        # 5. Отправляем готовый пост в Telegram
        await send_to_telegram(post_text=generated_post, image_url=best_image_url)

    except Exception as e:
        logger.critical(f"Произошла непредвиденная ошибка в главном цикле: {e}", exc_info=True)


async def main():
    """
    Главная асинхронная функция, которая настраивает и запускает планировщик.
    """
    logger.info("🤖 Запуск Telegram-бота...")

    # --- Настройка расписания ---
    total_minutes_in_range = (END_HOUR - START_HOUR) * 60
    # Предотвращение деления на ноль, если постов 0
    if POSTS_PER_DAY > 0:
        interval_minutes = total_minutes_in_range // POSTS_PER_DAY
    else:
        interval_minutes = total_minutes_in_range + 1

    for i in range(POSTS_PER_DAY):
        # Выбираем случайное время внутри каждого интервала, чтобы посты не выходили в одно и то же время
        random_offset = random.randint(0, max(0, interval_minutes - 1))
        scheduled_minute_abs = (i * interval_minutes) + random_offset

        scheduled_hour = START_HOUR + scheduled_minute_abs // 60
        scheduled_minute = scheduled_minute_abs % 60

        scheduler.add_job(process_and_post_news, "cron", hour=scheduled_hour, minute=scheduled_minute)
        logger.info(f"Запланирована публикация на {scheduled_hour:02d}:{scheduled_minute:02d}")

    scheduler.start()

    # --- Запуск первого поста сразу после старта ---
    logger.info("Запускаю немедленную публикацию первого поста...")
    await process_and_post_news()

    # --- Бесконечный цикл для поддержания работы бота ---
    logger.info("Бот запущен и работает в штатном режиме.")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен вручную.")
    except Exception as e:
        logger.critical(f"Глобальная ошибка при запуске бота: {e}", exc_info=True)
