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


def _title_matches(title: str, keyword: str) -> bool:
    """Ключевое слово должно встречаться в названии (как подстрока)."""
    return _normalize(keyword) in _normalize(title)


@dataclass
class Finder:
    client: object = None
    results: list[ChatInfo] = field(default_factory=list)

    async def connect(self):
        from telethon import TelegramClient

        self.client = TelegramClient(
            config.SESSION_NAME, config.API_ID, config.API_HASH
        )
        await self.client.start()
        return self.client

    # ------------------------------------------------------------------
    # Получение деталей канала (число подписчиков и пр.)
    # ------------------------------------------------------------------
    async def _resolve_channel(self, username: str):
        """Возвращает объект Channel по username или None."""
        try:
            r = await self.client(functions.contacts.ResolveUsernameRequest(username))
        except (UsernameNotOccupiedError, UsernameInvalidError, RPCError):
            return None
        for c in r.chats:
            if isinstance(c, (types.Channel, types.Chat)):
                return c
        return None

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
        """contacts.Search — публичные каналы/боты по названию."""
        found: list[ChatInfo] = []
        try:
            r = await self.client(
                functions.contacts.SearchRequest(q=keyword, limit=100, broadcasts=True)
            )
        except FloodWaitError as e:
            print(f"  FloodWait: ждём {e.seconds} сек...")
            await asyncio.sleep(e.seconds + 1)
            return found

        for c in r.chats:
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
        for source in (self._search_contacts, self._search_global):
            for c in await source(keyword):
                key = (c.username or c.title).lower()
                if key not in seen:
                    seen[key] = c
        res = sorted(seen.values(), key=lambda c: c.subscribers, reverse=True)
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
