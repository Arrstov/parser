"""Поиск популярных чатов и каналов в Telegram по ключевым словам.

Использует Telethon (MTProto-клиент) и встроенный поиск Telegram:
  * contacts.SearchRequest — глобальный поиск публичных каналов по названию;
  * messages.SearchGlobalRequest — дополнительный охват (группы/чаты).

"Популярным" считается чат с числом подписчиков не меньше MIN_SUBSCRIBERS.

Требуется аккаунт Telegram: API_ID + API_HASH (https://my.telegram.org).
"""
from __future__ import annotations

import asyncio
import csv
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Iterable

from telethon import functions, types
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    RPCError,
    UsernameNotOccupiedError,
    UsernameInvalidError,
)

import config


@dataclass
class ChatInfo:
    """Найденный чат/канал."""

    title: str
    username: str | None
    type: str  # "channel" | "group" | "megagroup"
    subscribers: int  # число подписчиков/участников
    keyword: str = ""
    verified: bool = False
    description: str = ""
    link: str = ""

    def __post_init__(self):
        if not self.link and self.username:
            self.link = f"https://t.me/{self.username}"


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _stem(word: str) -> str:
    """Грубая «морфология»: отбрасываем типичные русские падежные окончания,
    чтобы «вахтой» совпадало с «вахта», «зарплату» — с «зарплата» и т.п.
    Работает по принципам подобия, без словарей."""
    for suf in ("ией", "ами", "ями", "ого", "его", "ому", "ым", "их", "ых",
                "ую", "юю", "ах", "ях", "ою", "ею", "а", "о", "е", "ы", "и",
                "у", "ю", "ь", "й"):
        if len(word) - len(suf) >= 4:  # не укорачиваем короче 4 символов
            return word[: -len(suf)]
    return word


def _title_matches(title: str, keyword: str) -> bool:
    """Название считается подходящим, если содержит ВСЕ слова из запроса
    (в любом порядке, регистре и близко к исходной форме).

    Строгая проверка «вся фраза как подстрока» отбрасывала почти всё:
    Telegram сам ищет по отдельным словам, а каналы редко называются
    точной копией фразы («работа вахтой с проживанием» ≠ «Вахта | Работа»).
    """
    words = [w for w in _normalize(keyword).split(" ") if w]
    norm_title = _normalize(title)
    title_words = norm_title.split(" ")
    for w in words:
        stem = _stem(w)
        ok = (
            w in norm_title                                    # точное вхождение
            or any(_stem(tw) == stem for tw in title_words)    # та же основа слова
            or (len(stem) >= 5 and stem in norm_title)         # основа внутри слова
        )
        if not ok:
            return False
    return True


@dataclass
class Finder:
    client: object = None
    results: list[ChatInfo] = field(default_factory=list)

    @staticmethod
    def _parse_proxy(url: str):
        """socks5://host:port[/user:pass] | http://host:port -> кортеж для Telethon."""
        from urllib.parse import urlparse

        from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

        p = urlparse(url)
        if p.scheme in ("mtproxy", "socks5mtproxy"):
            # Формат: mtproxy://host:port:secret — разбираем вручную,
            # т.к. urlparse не понимает третью часть «порт:секрет».
            body = url.split("://", 1)[1]
            parts = body.split(":")
            if len(parts) == 3 and parts[0] and parts[2]:
                try:
                    port = int(parts[1])
                except ValueError:
                    return None
                return (
                    ConnectionTcpMTProxyRandomizedIntermediate,
                    (parts[0], port, parts[2]),
                )
            return None
        if p.scheme.startswith("socks5"):
            kind = "socks5"
        elif p.scheme.startswith("http"):
            kind = "http"
        else:
            return None
        host, port = p.hostname, p.port
        if p.username and p.password:
            return (kind, (host, port, True, p.username, p.password))
        return (kind, (host, port))

    async def connect(self):
        from telethon import TelegramClient

        kwargs = {}
        if config.PROXY_URL:
            proxy = self._parse_proxy(config.PROXY_URL)
            if proxy is None:
                raise ValueError(
                    f"Не удалось разобрать PROXY_URL: {config.PROXY_URL!r}. "
                    "Ожидается socks5://host:port или http://host:port "
                    "или mtproxy://host:port:secret"
                )
            kwargs["proxy"] = proxy
            kwargs["connection"] = None  # telethon сам выберет по типу прокси

        self.client = TelegramClient(
            config.SESSION_NAME, config.API_ID, config.API_HASH, **kwargs
        )
        # client.start() умеет запрашивать Бот-токен, но боты не имеют доступа
        # к поиску каналов/чатов (contacts.search / messages.searchGlobal).
        # Поэтому просим номер обычного пользовательского аккаунта.
        async def prompt_phone():
            while True:
                phone = input("Введите номер телефона аккаунта Telegram "
                              "(формат +79991234567): ").strip()
                if phone and not phone.endswith(":"):
                    return phone
                print("Бот-токен здесь не подойдёт — нужен номер "
                      "пользовательского аккаунта.")
        await self.client.start(phone=prompt_phone)
        return self.client

    # ------------------------------------------------------------------
    # Получение деталей канала (число подписчиков и пр.)
    # ------------------------------------------------------------------
    async def _resolve_channel(self, username: str):
        """Возвращает объект Channel по username или None."""
        try:
            ent = await self.client.get_entity(username)
        except (UsernameNotOccupiedError, UsernameInvalidError, ChannelPrivateError,
                ValueError, TypeError, RPCError):
            return None
        return ent if isinstance(ent, types.Channel) else None

    async def _channel_info(
        self, username: str | None, title_hint: str, keyword: str, peer=None
    ) -> ChatInfo | None:
        """Собирает полную информацию о канале; None если он не 'популярный'."""
        ent = None
        if username:
            ent = await self._resolve_channel(username)
        elif peer is not None:
            try:
                ent = await self.client.get_entity(peer)
            except (ChannelPrivateError, ValueError, TypeError):
                return None
        if ent is None or not isinstance(ent, types.Channel):
            return None

        title = ent.title or title_hint
        if not _title_matches(title, keyword):
            return None

        subscribers = getattr(ent, "participants_count", 0) or 0
        if subscribers < config.MIN_SUBSCRIBERS:
            return None

        ptype = "megagroup" if getattr(ent, "megagroup", False) else "channel"
        about = getattr(ent, "about", "") or ""
        return ChatInfo(
            title=title,
            username=getattr(ent, "username", None) or username,
            type=ptype,
            subscribers=subscribers,
            keyword=keyword,
            verified=bool(getattr(ent, "verified", False)),
            description=about.strip(),
        )

    # ------------------------------------------------------------------
    # Два источника результатов
    # ------------------------------------------------------------------
    async def _search_contacts(self, keyword: str) -> list[ChatInfo]:
        """contacts.Search — публичные каналы/боты по названию.

        ВАЖНО: Telegram возвращает ВСЕ найденные сущности (каналы, ботов,
        пользователей) в поле `my_results`, а не только «мои контакты» —
        поэтому фильтр `if not me` выкидывал 100% результатов.
        Также сервер сам решает, как искать фразу: «работа вахтой» может
        вернуть мало/ничего, поэтому дополнительно ищем по первому слову.
        """
        found: list[ChatInfo] = []
        queries = [keyword]
        first_word = keyword.split()[0] if keyword.split() else ""
        if first_word and first_word.lower() != keyword.lower():
            queries.append(first_word)

        for q in queries:
            q = re.sub(r"\s+", " ", q.replace('"', " ")).strip()
            try:
                r = await self.client(
                    functions.contacts.SearchRequest(q=q, limit=200)
                )
            except FloodWaitError as e:
                print(f"  FloodWait: ждём {e.seconds} сек...")
                await asyncio.sleep(e.seconds + 1)
                return found

            candidates = list(r.my_results) + list(r.chats)
            for c in candidates:
                # Telethon кладёт всё в my_results; берём только каналы/супергруппы
                if not isinstance(c, types.Channel):
                    continue
                title = c.title or ""
                if not _title_matches(title, keyword):
                    continue
                uname = getattr(c, "username", None)
                info = await self._channel_info(uname, title, keyword)
                if info:
                    found.append(info)
        return found

    async def _search_global(self, keyword: str) -> list[ChatInfo]:
        """messages.SearchGlobal — дополнительные результаты (группы и каналы)."""
        found: list[ChatInfo] = []
        offset_id = 0
        limit = max(config.RESULTS_PER_KEYWORD, 50)
        try:
            while True:
                r = await self.client(
                    functions.messages.SearchGlobalRequest(
                        q=keyword,
                        filter=types.InputMessagesFilterEmpty(),
                        min_date=None,
                        max_date=None,
                        offset_rate=offset_id,
                        offset_peer=types.InputPeerEmpty(),
                        offset_id=offset_id,
                        limit=min(100, limit),
                    )
                )
                chats = getattr(r, "chats", []) or []
                by_id = {c.id: c for c in chats if isinstance(c, types.Channel)}
                new = 0
                for sr in getattr(r, "results", []) or []:
                    peer = getattr(sr, "peer", None)
                    if not isinstance(peer, types.PeerChannel):
                        continue
                    new += 1
                    ch = by_id.get(peer.channel_id)
                    if ch is None:
                        continue
                    title = ch.title or ""
                    if not _title_matches(title, keyword):
                        continue
                    uname = getattr(ch, "username", None)
                    info = await self._channel_info(uname, title, keyword, peer=peer)
                    if info:
                        found.append(info)
                if not getattr(r, "more", False) or new == 0:
                    break
                offset_id = getattr(r, "next_offset_id", 0) or 0
                if len(found) >= limit:
                    break
        except FloodWaitError as e:
            print(f"  FloodWait: ждём {e.seconds} сек...")
            await asyncio.sleep(e.seconds + 1)
        except RPCError:
            pass  # некоторые аккаунты не имеют доступа к global search
        return found

    # ------------------------------------------------------------------
    # Основной API
    # ------------------------------------------------------------------
    async def search_keyword(self, keyword: str) -> list[ChatInfo]:
        """Ищет популярные чаты/каналы по одному ключевому слову."""
        seen: dict[str, ChatInfo] = {}
        raw_count = 0
        for source in (self._search_contacts, self._search_global):
            chats = await source(keyword)
            raw_count += len(chats)
            for c in chats:
                key = (c.username or c.title).lower()
                if key not in seen:
                    seen[key] = c
        res = sorted(seen.values(), key=lambda c: c.subscribers, reverse=True)
        if not res and raw_count == 0:
            # Ни один канал не прошёл фильтры — подсказываем, что можно ослабить.
            print(
                f"  (нет результатов; пороги: MIN_SUBSCRIBERS="
                f"{config.MIN_SUBSCRIBERS}, название должно содержать все слова запроса)"
            )
        return res[: config.RESULTS_PER_KEYWORD]

    async def find_all(self, keywords: Iterable[str]) -> list[ChatInfo]:
        """Ищет по всем ключевым словам, объединяет и дедуплицирует результаты."""
        merged: dict[str, ChatInfo] = {}
        for kw in keywords:
            kw = kw.strip()
            if not kw:
                continue
            print(f"Ищу: «{kw}» ...")
            chats = await self.search_keyword(kw)
            print(f"  найдено популярных: {len(chats)}")
            for c in chats:
                key = (c.username or c.title).lower()
                if key in merged:
                    if kw not in merged[key].keyword:
                        merged[key].keyword += f", {kw}"
                else:
                    merged[key] = c
        self.results = sorted(merged.values(), key=lambda c: c.subscribers, reverse=True)
        return self.results

    # ---- экспорты ----

    def save_json(self, path: str = "results.json"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump([asdict(c) for c in self.results], f, ensure_ascii=False, indent=2)

    def save_csv(self, path: str = "results.csv"):
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["title", "type", "subscribers", "username", "link", "keywords", "description"]
            )
            for c in self.results:
                writer.writerow(
                    [c.title, c.type, c.subscribers, c.username, c.link, c.keyword, c.description]
                )
