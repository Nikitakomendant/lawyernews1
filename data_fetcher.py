# -*- coding: utf-8 -*-

# ---------------------------------------------------------------------------
#                               СБОРЩИК ДАННЫХ
# ---------------------------------------------------------------------------
# Этот модуль отвечает за получение данных из RSS-лент и парсинг
# веб-страниц для извлечения текста статьи и URL-адресов изображений.
# ---------------------------------------------------------------------------

import feedparser
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import os
import re
import logging

# Импортируем настройки из нашего конфигурационного файла
from config import RSS_FEEDS, PUBLISHED_URLS_FILE

# Настройка логирования для этого модуля
logger = logging.getLogger(__name__)


def load_published_urls():
    """Загружает URL уже опубликованных статей из файла."""
    if not os.path.exists(PUBLISHED_URLS_FILE):
        return set()
    try:
        with open(PUBLISHED_URLS_FILE, 'r', encoding='utf-8') as f:
            # Используем set для быстрого поиска
            return set(line.strip() for line in f)
    except Exception as e:
        logger.error(f"Не удалось прочитать файл с опубликованными URL: {e}")
        return set()


def add_url_to_published(url):
    """Добавляет URL в файл опубликованных статей."""
    try:
        with open(PUBLISHED_URLS_FILE, 'a', encoding='utf-8') as f:
            f.write(url + '\n')
    except Exception as e:
        logger.error(f"Не удалось записать URL в файл: {e}")


def get_latest_news_from_rss():
    """
    Сканирует RSS-ленты и возвращает заголовок и URL первой неопубликованной новости.
    """
    logger.info("Начинаю поиск новых статей в RSS-лентах...")
    published_urls = load_published_urls()

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            if feed.bozo:
                logger.warning(f"Некорректный формат RSS-ленты: {feed_url}. Ошибка: {feed.bozo_exception}")
                continue

            for entry in feed.entries:
                article_url = entry.link
                if article_url not in published_urls:
                    logger.info(f"Найдена новая статья: '{entry.title}' из {feed_url}")
                    # Сразу добавляем в список, чтобы избежать дублирования при параллельной работе
                    add_url_to_published(article_url)
                    return entry.title, article_url

        except Exception as e:
            logger.error(f"Ошибка при обработке RSS-ленты {feed_url}: {e}")
            continue

    logger.info("Новых статей не найдено.")
    return None, None


def scrape_article_content(url):
    """
    Извлекает сырой текст и все URL изображений со страницы статьи.
    Возвращает словарь {'raw_text': '...', 'image_urls': [...]}.
    """
    logger.info(f"Начинаю скрапинг статьи по URL: {url}")
    try:
        # Устанавливаем заголовок User-Agent, чтобы имитировать браузер
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()  # Вызовет исключение для кодов 4xx/5xx

        soup = BeautifulSoup(response.text, "html.parser")

        # Ищем основной контейнер статьи по популярным тегам и классам
        article_body = (soup.find("article") or
                        soup.find("main") or
                        soup.find("div", class_=re.compile(r'post|content|article|text', re.IGNORECASE)) or
                        soup.body)  # В крайнем случае берем все тело страницы

        if not article_body:
            logger.warning(f"Не удалось найти основной контент на странице: {url}")
            return None

        # --- Извлечение текста ---
        # Удаляем ненужные теги (скрипты, стили, навигацию и т.д.)
        for tag in article_body(['script', 'style', 'nav', 'header', 'footer', 'aside']):
            tag.decompose()

        # Получаем текст, заменяя разрывы строк на пробелы для последующей очистки
        raw_text = article_body.get_text(separator=' ', strip=True)
        # Сжимаем множественные пробелы и переносы строк в один пробел
        cleaned_text = re.sub(r'\s+', ' ', raw_text).strip()

        # --- Извлечение изображений ---
        image_urls = []
        img_tags = article_body.find_all('img')
        for img in img_tags:
            if img.has_attr('src'):
                src = img['src']
                # Пропускаем data URI и слишком короткие URL (вероятно, пиксели отслеживания)
                if src.startswith('data:') or len(src) < 20:
                    continue

                # Фильтруем заведомо неподдерживаемые форматы для Gemini/Telegram (svg, gif)
                lowered = src.lower()
                if lowered.endswith('.svg') or lowered.endswith('.gif'):
                    continue

                # Преобразуем относительные URL в абсолютные
                absolute_url = urljoin(url, src)
                image_urls.append(absolute_url)

        # Удаляем дубликаты, сохраняя порядок
        unique_image_urls = list(dict.fromkeys(image_urls))

        logger.info(
            f"Скрапинг успешно завершен. Найдено {len(cleaned_text)} символов текста и {len(unique_image_urls)} изображений.")

        return {
            "raw_text": cleaned_text,
            "image_urls": unique_image_urls
        }

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при загрузке страницы {url}: {e}")
        return None
    except Exception as e:
        logger.error(f"Непредвиденная ошибка при скрапинге {url}: {e}")
        return None