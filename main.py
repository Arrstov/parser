#!/usr/bin/env python3
"""CLI: поиск популярных чатов/каналов Telegram по ключевым словам.

Примеры:
    python main.py "крипта" "crypto" "работа удаленная"
    python main.py --file keywords.txt --min 1000 --out results.csv
    python main.py --format json "новости"
"""
import argparse
import asyncio
import sys

import config
from finder import Finder


def parse_args():
    p = argparse.ArgumentParser(description="Поиск популярных чатов/каналов Telegram по ключевым словам")
    p.add_argument("keywords", nargs="*", help="Ключевые слова (например: крипта работа crypto)")
    p.add_argument("--file", "-f", help="Файл со списком ключевых слов (по одному в строке)")
    p.add_argument("--min", type=int, default=config.MIN_SUBSCRIBERS,
                   help=f"Мин. подписчиков для 'популярного' (по умолчанию {config.MIN_SUBSCRIBERS})")
    p.add_argument("--limit", type=int, default=config.RESULTS_PER_KEYWORD,
                   help=f"Макс. результатов на слово (по умолчанию {config.RESULTS_PER_KEYWORD})")
    p.add_argument("--format", choices=["table", "json", "csv"], default="table")
    p.add_argument("--out", help="Файл для сохранения результатов (results.json / results.csv)")
    return p.parse_args()


def print_table(chats):
    if not chats:
        print("Ничего не найдено.")
        return
    print(f"\n{'Название':<40} {'Тип':<10} {'Подписчики':>12}  {'Ссылка'}")
    print("-" * 100)
    for c in chats:
        title = c.title[:38] + (".." if len(c.title) > 40 else "")
        print(f"{title:<40} {c.type:<10} {c.subscribers:>12}  {c.link or '(приватный)'}")
    print("-" * 100)
    print(f"Итого: {len(chats)}")


async def run(args):
    keywords = list(args.keywords)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            keywords += [line.strip() for line in f if line.strip()]
    if not keywords:
        print("Укажите ключевые слова или файл (--file). Запустите с -h для справки.")
        sys.exit(1)

    finder = Finder()
    await finder.connect()
    try:
        chats = await finder.find_all(keywords)
    finally:
        await finder.client.disconnect()

    if args.format == "json":
        finder.save_json(args.out or "results.json")
        print(f"Сохранено в {args.out or 'results.json'}")
    elif args.format == "csv":
        finder.save_csv(args.out or "results.csv")
        print(f"Сохранено в {args.out or 'results.csv'}")
    else:
        print_table(chats)
        if args.out:
            if args.out.endswith(".csv"):
                finder.save_csv(args.out)
            else:
                finder.save_json(args.out)
            print(f"Также сохранено в {args.out}")


def main():
    if not config.API_ID or not config.API_HASH:
        print("Ошибка: задайте TELEGRAM_API_ID и TELEGRAM_API_HASH "
              "(файл .env или переменные окружения).")
        print("Получить их можно на https://my.telegram.org -> API development tools")
        sys.exit(1)
    args = parse_args()
    config.MIN_SUBSCRIBERS = args.min
    config.RESULTS_PER_KEYWORD = args.limit
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
