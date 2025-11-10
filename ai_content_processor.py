# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
#                      ОБРАБОТЧИК КОНТЕНТА С ПОМОЩЬЮ ИИ
# ---------------------------------------------------------------------------
# Этот модуль взаимодействует с API Google Gemini для:
# 1. Генерации текста новостного поста на основе сырого текста статьи.
# 2. Выбора наиболее релевантного изображения для сгенерированного поста.
# ---------------------------------------------------------------------------

import google.generativeai as genai
import logging
import re
import requests
from PIL import Image
import io

# Импортируем настройки из нашего конфигурационного файла
from config import (
    GEMINI_API_KEY,
    TEXT_GENERATION_PROMPT,
    IMAGE_SELECTION_PROMPT
)

# Настройка логирования для этого модуля
logger = logging.getLogger(__name__)

# --- Инициализация API ---
# Проверяем, что API ключ указан, иначе работа невозможна.
if not GEMINI_API_KEY or GEMINI_API_KEY == "ВАШ_GEMINI_API_КЛЮЧ":
    logger.critical("Ключ GEMINI_API_KEY не настроен в файле config.py!")
    raise ValueError("API-ключ для Gemini не предоставлен. Пожалуйста, укажите его в config.py.")

genai.configure(api_key=GEMINI_API_KEY)


def generate_news_post(article_text: str) -> str | None:
    """
    Генерирует текст поста для Telegram на основе текста статьи с помощью Gemini.

    Args:
        article_text: Сырой текст статьи.

    Returns:
        Отформатированный текст поста или None в случае ошибки.
    """
    logger.info("Начинаю генерацию текста поста с помощью Gemini 2.5 Pro...")

    if not article_text or len(article_text.strip()) < 100:
        logger.warning("Текст статьи слишком короткий или отсутствует. Пропускаю генерацию.")
        return None

    try:
        # --- ИЗМЕНЕНИЕ ---
        # Используем новейшую модель 'gemini-2.5-pro', как указано в документации.
        model = genai.GenerativeModel('gemini-2.5-pro')

        # Формируем промпт, подставляя текст статьи
        prompt = TEXT_GENERATION_PROMPT.format(article_text=article_text)

        response = model.generate_content(prompt)

        generated_post = response.text

        # Очищаем ответ от возможных "оберток" markdown, которые иногда добавляет модель
        cleaned_post = re.sub(r'^```(markdown)?\n|```$', '', generated_post, flags=re.MULTILINE).strip()

        logger.info("Текст поста успешно сгенерирован.")
        return cleaned_post

    except Exception as e:
        logger.error(f"Ошибка при генерации текста поста через Gemini API: {e}")
        return None


def select_best_image(image_urls: list, post_text: str) -> str | None:
    """
    Выбирает лучшее изображение из списка URL, анализируя их релевантность тексту поста.

    Args:
        image_urls: Список URL-адресов изображений.
        post_text: Уже сгенерированный текст поста.

    Returns:
        URL наиболее подходящего изображения или None.
    """
    if not image_urls:
        logger.info("Список URL изображений пуст. Пропускаю выбор изображения.")
        return None

    logger.info(f"Выбор лучшего изображения из {len(image_urls)} вариантов с помощью Gemini 2.5 Flash...")

    # Формируем текстовую часть промпта
    prompt_text = IMAGE_SELECTION_PROMPT.format(post_text=post_text)

    # Готовим данные для мультимодального запроса: [текст, картинка1, картинка2, ...]
    prompt_parts = [prompt_text]
    valid_urls_for_model = []
    index_to_url: list[str] = []

    # Ограничим количество изображений для анализа (экономия токенов и времени)
    for url in image_urls[:5]:
        try:
            # Загружаем изображение по URL
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            # Отбрасываем явно неподдерживаемые форматы (например, SVG)
            content_type = response.headers.get('Content-Type', '').lower()
            if 'svg' in content_type or url.lower().endswith('.svg'):
                raise ValueError('Unsupported image format (SVG)')

            # Преобразуем в формат, понятный для модели (PIL Image)
            img = Image.open(io.BytesIO(response.content))
            if img.format not in {"PNG", "JPEG", "WEBP", "HEIC", "HEIF"}:
                # PIL может вернуть None для некоторых форматов — в этом случае допустим попытку всё равно
                # но если формат явно не из списка, безопаснее пропустить
                logger.debug(f"Пропуск изображения с неподдерживаемым форматом: {img.format}")
                # Попробуем всё же сохранить как RGB и использовать
            
            # Нормализуем изображение к RGB (многие модели ожидают стандартные каналы)
            if img.mode not in ("RGB", "RGBA"):
                img = img.convert("RGB")

            prompt_parts.append(img)
            valid_urls_for_model.append(url)
            index_to_url.append(url)

        except Exception as e:
            logger.warning(f"Не удалось загрузить или обработать изображение {url}: {e}")

    # Если не удалось загрузить ни одного изображения
    if len(valid_urls_for_model) == 0:
        logger.warning("Не удалось загрузить ни одного изображения для анализа.")
        return None

    try:
        # Для анализа изображений используем поддерживаемую мультимодальную модель.
        # Согласно документации, используем 'gemini-2.5-flash' для быстрого и экономичного анализа изображений.
        # См. docs: https://ai.google.dev/gemini-api/docs/image-understanding
        model = genai.GenerativeModel('gemini-2.5-flash')

        response = model.generate_content(prompt_parts)
        raw = (response.text or "").strip()
        # Пытаемся извлечь индекс вида "1", "2." или "1)"
        m = re.search(r"(\d+)", raw)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(index_to_url):
                chosen = index_to_url[idx]
                logger.info(f"ИИ выбрал изображение под номером {idx+1}: {chosen}")
                return chosen
        # Fallback: если индекс не распознан — первое валидное
        logger.warning(
            f"ИИ вернул непредвиденный ответ: '{raw}'. В качестве запасного варианта выбираю первое удачно загруженное изображение.")
        return valid_urls_for_model[0]

    except Exception as e:
        logger.error(f"Ошибка при выборе изображения через Gemini API: {e}")
        # Если ИИ не справился, просто вернем первое изображение из списка
        return valid_urls_for_model[0]