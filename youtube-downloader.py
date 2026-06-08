"""
YouTube Downloader v2 — антибот-архитектура.

Новые компоненты:
  Session          → одна авторизованная сессия (куки + visitor_data + po_token)
  SessionPool      → пул сессий; задачи берут сессию и возвращают её
  RateLimiter      → Semaphore(2) + случайный jitter 1–4 с между стартами
  TokenRefresher   → TTL-кеш po_token / visitor_data, обновляет по истечению
  RetryOrchestrator→ экспоненциальный backoff; при bot-блоке меняет сессию
  DownloadManager  → asyncio event loop в отдельном потоке; принимает задачи
  PlaylistTask     → плейлист скачивается последовательно в одной сессии

Сохранённые компоненты из v1:
  Config, DownloadRow, YdlService (расширен), PlaylistWindow, App
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import asyncio
import logging
import math
import os
import random
import shutil
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from typing import Callable

import customtkinter as ctk
from yt_dlp import YoutubeDL

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ytdl")

ctk.set_appearance_mode("Dark")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    WINDOW_TITLE  = "Multi YouTube Downloader Ultra Pro"
    WINDOW_SIZE   = "860x580"

    # Антибот-параметры
    SESSION_POOL_SIZE  = 3       # сколько независимых сессий держать
    MAX_CONCURRENT     = 2       # одновременных загрузок (не больше 2!)
    JITTER_MIN         = 1.0     # мин. задержка между стартами (сек)
    JITTER_MAX         = 4.0     # макс. задержка
    MAX_RETRY          = 3       # попыток на одну задачу
    BACKOFF_BASE       = 2.0     # основание exp. backoff
    BACKOFF_JITTER     = 0.3     # ±30% к backoff
    TOKEN_TTL          = 3300    # обновлять po_token раньше 55 мин (сек)
    PO_TOKEN_TIMEOUT   = 15      # таймаут запроса po_token (сек)

    # yt-dlp
    YDL_RETRIES        = 10
    YDL_SOCKET_TIMEOUT = 45
    YDL_PLAYER_CLIENTS = ["web_safari", "web", "ios"]  # fallback-цепочка
    AUDIO_CODEC        = "mp3"
    AUDIO_QUALITY      = "192"
    MERGE_FORMAT       = "mp4"

    ENTRY_MAX_LEN          = 250
    PLAYLIST_TITLE_MAX_LEN = 60

    # Цвета
    COLOR_OK          = "#27AE60"
    COLOR_WARN        = "#F39C12"
    COLOR_ERR         = "#C0392B"
    COLOR_MUTED       = "#888888"
    COLOR_WHITE       = "#FFFFFF"
    COLOR_BG          = "#000000"
    COLOR_SURFACE     = "#050505"
    COLOR_BORDER      = "#222222"
    COLOR_BTN         = "#111111"
    COLOR_BTN_HOVER   = "#1A1A1A"
    COLOR_DELETE      = "#C0392B"
    COLOR_DELETE_HOVER= "#A93226"
    COLOR_ADD         = "#27AE60"
    COLOR_ADD_HOVER   = "#219653"
    COLOR_SESSION_OK  = "#2980B9"


# ---------------------------------------------------------------------------
# DownloadRow — типизированный объект строки GUI
# ---------------------------------------------------------------------------
@dataclass
class DownloadRow:
    frame:          ctk.CTkFrame
    entry:          ctk.CTkEntry
    quality_menu:   ctk.CTkOptionMenu
    audio_only_var: tk.BooleanVar
    checkbox:       ctk.CTkCheckBox
    progress:       ctk.CTkProgressBar
    status:         ctk.CTkLabel
    delete_btn:     ctk.CTkButton
    last_url:       str              = ""
    cancel_event:   threading.Event  = field(default_factory=threading.Event)
    # плейлист-режим: список URL для последовательной загрузки
    playlist_urls:  list[str]        = field(default_factory=list)

    def set_status(self, text: str, color: str = Config.COLOR_MUTED) -> None:
        self.status.configure(text=text, text_color=color)

    def set_progress(self, value: float) -> None:
        self.progress.set(value)

    def lock(self) -> None:
        self.entry.configure(state="disabled")
        self.checkbox.configure(state="disabled")
        self.quality_menu.configure(state="disabled")
        self.delete_btn.configure(state="disabled")

    def unlock(self, ffmpeg_ok: bool) -> None:
        self.entry.configure(state="normal")
        self.checkbox.configure(state="normal")
        self.delete_btn.configure(state="normal")
        if not self.audio_only_var.get() and len(self.quality_menu.cget("values")) > 1:
            self.quality_menu.configure(state="normal")

    def reset_cancel(self) -> None:
        self.cancel_event.clear()

    @property
    def url(self) -> str:
        return self.entry.get().strip()

    @property
    def audio_only(self) -> bool:
        return self.audio_only_var.get()

    @property
    def selected_quality(self) -> str:
        return self.quality_menu.get()


# ---------------------------------------------------------------------------
# Session — одна авторизованная сессия YouTube
# ---------------------------------------------------------------------------
@dataclass
class Session:
    id:             int
    visitor_data:   str   = ""
    po_token:       str   = ""
    token_fetched:  float = 0.0   # unix timestamp последнего обновления
    compromised:    bool  = False  # помечена как заблокированная
    in_use:         bool  = False

    def is_token_fresh(self) -> bool:
        return (time.monotonic() - self.token_fetched) < Config.TOKEN_TTL

    def mark_compromised(self) -> None:
        self.compromised = True
        log.warning("Session %d помечена как скомпрометированная", self.id)

    def reset(self) -> None:
        """Сброс флагов после восстановления."""
        self.compromised = False
        self.token_fetched = 0.0


# ---------------------------------------------------------------------------
# TokenRefresher — получение и кеширование po_token / visitor_data
# ---------------------------------------------------------------------------
class TokenRefresher:
    """
    Получает po_token и visitor_data через yt-dlp extractor.
    Кешируется по сессии с TTL = Config.TOKEN_TTL секунд.
    """

    def __init__(self, cookies_path: str | None = None) -> None:
        self._cookies_path = cookies_path
        self._lock = threading.Lock()

    def ensure_fresh(self, session: Session) -> None:
        """Обновляет токены сессии, если истёк TTL или сессия новая."""
        if session.is_token_fresh() and session.visitor_data:
            return
        with self._lock:
            # Двойная проверка внутри лока
            if session.is_token_fresh() and session.visitor_data:
                return
            self._refresh(session)

    def _refresh(self, session: Session) -> None:
        log.info("Session %d: обновление visitor_data / po_token...", session.id)
        opts: dict = {
            "quiet": True,
            "skip_download": True,
            "nocheckcertificate": True,
            "extractor_args": {"youtube": {"player_client": ["web"]}},
        }
        if self._cookies_path and os.path.exists(self._cookies_path):
            opts["cookiefile"] = self._cookies_path
        # Firefox auto-cookies removed — use explicit cookies.txt file

        try:
            with YoutubeDL(opts) as ydl:
                # Извлекаем пустую страницу чтобы получить visitor_data
                info = ydl.extract_info(
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    download=False,
                )
                if info:
                    session.visitor_data = info.get("visitor_data", "")
                    # po_token появляется в extractor_data у некоторых форматов
                    for fmt in (info.get("formats") or []):
                        pt = (fmt.get("downloader_options") or {}).get("http_chunk_size")
                        # po_token живёт в другом месте — берём из cookies или
                        # оставляем пустым (yt-dlp сам его подставит при загрузке)
                        break
            session.token_fetched = time.monotonic()
            log.info("Session %d: visitor_data обновлён", session.id)
        except Exception as exc:
            log.warning("Session %d: не удалось обновить токен: %s", session.id, exc)
            # Не фатально — продолжим без visitor_data


# ---------------------------------------------------------------------------
# SessionPool — пул сессий с ожиданием свободной
# ---------------------------------------------------------------------------
class SessionPool:
    """
    Поддерживает N сессий. Задача берёт одну через acquire(),
    выполняет работу и возвращает через release().
    При bot-блоке сессия помечается как compromised и заменяется.
    """

    def __init__(self, size: int, refresher: TokenRefresher) -> None:
        self._sessions = [Session(id=i) for i in range(size)]
        self._refresher = refresher
        self._lock = threading.Condition(threading.Lock())

    def acquire(self, prefer_not: Session | None = None) -> Session:
        """Блокирует поток, пока не появится свободная незаблокированная сессия."""
        with self._lock:
            while True:
                available = [
                    s for s in self._sessions
                    if not s.in_use and not s.compromised and s is not prefer_not
                ]
                if not available:
                    # Попробуем скомпрометированные (всё лучше чем ждать вечно)
                    available = [s for s in self._sessions if not s.in_use]
                if available:
                    session = available[0]
                    session.in_use = True
                    return session
                self._lock.wait(timeout=5.0)

    def release(self, session: Session) -> None:
        with self._lock:
            session.in_use = False
            self._lock.notify_all()

    def rotate(self, bad_session: Session) -> Session:
        """Помечает сессию как скомпрометированную и возвращает другую."""
        with self._lock:
            bad_session.in_use = False
            bad_session.mark_compromised()
            # Через некоторое время сбросим флаг (реализуем через reset позже)
        return self.acquire(prefer_not=bad_session)

    def recover_all(self) -> None:
        """Сбрасывает флаг compromised у всех сессий (вызывается периодически)."""
        with self._lock:
            for s in self._sessions:
                if not s.in_use:
                    s.reset()


# ---------------------------------------------------------------------------
# RateLimiter — контроль параллельности и случайные задержки
# ---------------------------------------------------------------------------
class RateLimiter:
    """
    Ограничивает одновременных загрузок до MAX_CONCURRENT.
    Добавляет случайный jitter перед каждым стартом чтобы разбить
    временной паттерн "все запросы одновременно".
    """

    def __init__(
        self,
        max_concurrent: int = Config.MAX_CONCURRENT,
        jitter: tuple[float, float] = (Config.JITTER_MIN, Config.JITTER_MAX),
    ) -> None:
        self._sem = threading.Semaphore(max_concurrent)
        self._jitter = jitter

    def acquire(self) -> None:
        """Блокирует до получения слота, затем ждёт случайное время."""
        self._sem.acquire()
        delay = random.uniform(*self._jitter)
        log.debug("RateLimiter: jitter %.1f с", delay)
        time.sleep(delay)

    def release(self) -> None:
        self._sem.release()


# ---------------------------------------------------------------------------
# RetryOrchestrator — экспоненциальный backoff со сменой сессии
# ---------------------------------------------------------------------------
class BotBlockError(Exception):
    """YouTube заблокировал запрос."""

class NetworkError(Exception):
    """Временная сетевая ошибка."""

class FatalError(Exception):
    """Ошибка, которую retry не исправит."""


def classify_error(exc: Exception) -> type[Exception]:
    """Классифицирует исключение yt-dlp по типу."""
    msg = str(exc).lower()
    if any(k in msg for k in ("sign in", "bot", "confirm you", "429", "too many")):
        return BotBlockError
    if any(k in msg for k in ("connection", "timeout", "network", "reset", "eof")):
        return NetworkError
    if any(k in msg for k in ("отменено", "cancel")):
        return FatalError
    return NetworkError  # неизвестное — считаем временным


class RetryOrchestrator:
    """
    Обёртка над функцией загрузки.
    При BotBlockError: меняет сессию, ждёт backoff.
    При NetworkError:  ждёт backoff, та же сессия.
    При FatalError:    не повторяет.
    """

    def __init__(self, pool: SessionPool, rate: RateLimiter) -> None:
        self._pool = pool
        self._rate = rate

    def run(
        self,
        task_fn: Callable[[Session], bool],
        on_retry: Callable[[int, str], None] | None = None,
    ) -> bool:
        """
        Запускает task_fn(session) -> bool.
        Возвращает True при успехе, False при исчерпании попыток.
        """
        session = None
        attempt = 0
        last_session = None

        try:
            self._rate.acquire()
            session = self._pool.acquire()

            while attempt < Config.MAX_RETRY:
                try:
                    return task_fn(session)

                except Exception as exc:
                    kind = classify_error(exc)

                    if kind is FatalError:
                        log.info("Задача отменена: %s", exc)
                        return False

                    attempt += 1
                    if attempt >= Config.MAX_RETRY:
                        log.warning("Исчерпаны попытки (%d): %s", attempt, exc)
                        return False

                    # Вычисляем backoff
                    base_wait = Config.BACKOFF_BASE ** attempt
                    jitter = base_wait * Config.BACKOFF_JITTER
                    wait = base_wait + random.uniform(-jitter, jitter)
                    wait = max(wait, 1.0)

                    if kind is BotBlockError:
                        log.warning(
                            "Bot-блок (попытка %d/%d). Меняем сессию, ждём %.1f с",
                            attempt, Config.MAX_RETRY, wait,
                        )
                        if on_retry:
                            on_retry(attempt, f"Бот-блок, смена сессии [{attempt}/{Config.MAX_RETRY}]...")
                        old_session = session
                        session = self._pool.rotate(old_session)
                    else:
                        log.warning(
                            "Сеть (попытка %d/%d). Ждём %.1f с: %s",
                            attempt, Config.MAX_RETRY, wait, exc,
                        )
                        if on_retry:
                            on_retry(attempt, f"Ошибка сети, повтор [{attempt}/{Config.MAX_RETRY}]...")

                    time.sleep(wait)

        finally:
            if session is not None:
                self._pool.release(session)
            self._rate.release()

        return False


# ---------------------------------------------------------------------------
# YdlService — вся логика yt-dlp; сессионно-независимые методы остались,
#              download теперь принимает Session для инъекции контекста
# ---------------------------------------------------------------------------
class YdlService:

    def __init__(
        self,
        cookies_path: str | None = None,
        ffmpeg_ok: bool = True,
    ) -> None:
        self.cookies_path = cookies_path
        self.ffmpeg_ok    = ffmpeg_ok

    # ------------------------------------------------------------------
    # Базовые опции
    # ------------------------------------------------------------------

    def _base_opts(self, session: Session | None = None) -> dict:
        # Пробуем клиентов по цепочке; первый в списке — приоритетный
        player_clients = list(Config.YDL_PLAYER_CLIENTS)
        opts: dict = {
            "nocheckcertificate": True,
            "quiet": True,
            "extractor_args": {
                "youtube": {"player_client": player_clients},
            },
        }
        # Инъекция visitor_data сессии
        if session and session.visitor_data:
            opts["extractor_args"]["youtube"]["visitor_data"] = [session.visitor_data]

        if self.cookies_path and os.path.exists(self.cookies_path):
            opts["cookiefile"] = self.cookies_path
        # Автоматическое чтение cookies из браузера отключено —
        # используйте кнопку "Куки" в интерфейсе для загрузки cookies.txt

        # Ищем node: сначала явный путь, потом PATH
        _NODE_PATHS = [
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe",
        ]
        node_bin = shutil.which("node")
        if not node_bin:
            for p in _NODE_PATHS:
                if os.path.exists(p):
                    node_bin = p
                    break
        if node_bin:
            opts["js_runtimes"] = {"node": {"path": node_bin}}
            opts["remote_components"] = {"ejs:github"}  # set, как ожидает yt-dlp

        return opts

    # ------------------------------------------------------------------
    # Получение метаданных (без загрузки)
    # ------------------------------------------------------------------

    def fetch_playlist_entries(self, url: str) -> list[dict]:
        opts = {
            **self._base_opts(),
            "extract_flat": True,
            "skip_download": True,
            "ignoreerrors": True,
        }
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("entries", []) if info else []

    def fetch_resolutions(self, url: str) -> list[str]:
        opts = self._base_opts()
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return []
        heights: set[int] = set()
        for fmt in info.get("formats", []):
            if fmt.get("vcodec") != "none" and fmt.get("height"):
                heights.add(fmt["height"])
        return [str(h) for h in sorted(heights, reverse=True)]

    # ------------------------------------------------------------------
    # Сборка опций загрузки
    # ------------------------------------------------------------------

    def build_download_opts(
        self,
        output_dir: str,
        audio_only: bool,
        selected_quality: str,
        progress_hook: Callable,
        session: Session | None = None,
    ) -> dict:
        opts = {
            **self._base_opts(session),
            "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
            "progress_hooks": [progress_hook],
            "retries": Config.YDL_RETRIES,
            "fragment_retries": Config.YDL_RETRIES,
            "socket_timeout": Config.YDL_SOCKET_TIMEOUT,
            "ignoreerrors": False,  # нам нужны исключения для retry
        }
        opts = self._apply_format(opts, audio_only, selected_quality)
        return opts

    def _apply_format(self, opts: dict, audio_only: bool, quality: str) -> dict:
        if audio_only:
            opts["format"] = "bestaudio/best"
            if self.ffmpeg_ok:
                opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": Config.AUDIO_CODEC,
                    "preferredquality": Config.AUDIO_QUALITY,
                }]
        else:
            if quality == "Максимальное" or not self.ffmpeg_ok:
                opts["format"] = "bestvideo+bestaudio/best" if self.ffmpeg_ok else "best"
            else:
                opts["format"] = f"bestvideo[height<={quality}]+bestaudio/best"
            opts["merge_output_format"] = Config.MERGE_FORMAT
        return opts

    # ------------------------------------------------------------------
    # Загрузка
    # ------------------------------------------------------------------

    def download(self, url: str, ydl_opts: dict) -> bool:
        with YoutubeDL(ydl_opts) as ydl:
            result = ydl.download([url])
        return result == 0


# ---------------------------------------------------------------------------
# DownloadManager — принимает задачи от GUI, выполняет в пуле потоков
# ---------------------------------------------------------------------------
class DownloadManager:
    """
    Запускает задачи через ThreadPoolExecutor.
    Каждая задача проходит через RetryOrchestrator → SessionPool → YdlService.
    """

    def __init__(
        self,
        ydl: YdlService,
        pool: SessionPool,
        rate: RateLimiter,
        refresher: TokenRefresher,
    ) -> None:
        self._ydl       = ydl
        self._pool      = pool
        self._rate      = rate
        self._refresher = refresher
        self._orchestrator = RetryOrchestrator(pool, rate)
        self._executor  = ThreadPoolExecutor(
            max_workers=Config.SESSION_POOL_SIZE,
            thread_name_prefix="ytdl",
        )
        # Периодически сбрасываем флаги compromised
        self._recovery_timer: threading.Timer | None = None
        self._schedule_recovery()

    # ------------------------------------------------------------------

    def submit_single(
        self,
        url: str,
        output_dir: str,
        audio_only: bool,
        quality: str,
        progress_hook: Callable,
        on_retry: Callable[[int, str], None],
        on_done: Callable[[bool], None],
        cancel_event: threading.Event,
    ) -> None:
        """Одиночная загрузка видео."""
        self._executor.submit(
            self._run_single,
            url, output_dir, audio_only, quality,
            progress_hook, on_retry, on_done, cancel_event,
        )

    def submit_playlist(
        self,
        urls: list[str],
        output_dir: str,
        audio_only: bool,
        quality: str,
        on_item_start: Callable[[int, int], None],
        progress_hook: Callable,
        on_retry: Callable[[int, str], None],
        on_done: Callable[[bool], None],
        cancel_event: threading.Event,
    ) -> None:
        """
        Плейлист: все URL в одной сессии, последовательно.
        Это ключевое отличие от одиночных задач.
        """
        self._executor.submit(
            self._run_playlist,
            urls, output_dir, audio_only, quality,
            on_item_start, progress_hook, on_retry, on_done, cancel_event,
        )

    def shutdown(self) -> None:
        if self._recovery_timer:
            self._recovery_timer.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------
    # Внутренние worker-методы
    # ------------------------------------------------------------------

    def _run_single(
        self,
        url: str,
        output_dir: str,
        audio_only: bool,
        quality: str,
        progress_hook: Callable,
        on_retry: Callable[[int, str], None],
        on_done: Callable[[bool], None],
        cancel_event: threading.Event,
    ) -> None:
        def task(session: Session) -> bool:
            if cancel_event.is_set():
                raise FatalError("Отменено пользователем")
            self._refresher.ensure_fresh(session)
            opts = self._ydl.build_download_opts(
                output_dir, audio_only, quality,
                self._wrap_hook(progress_hook, cancel_event),
                session,
            )
            return self._ydl.download(url, opts)

        success = self._orchestrator.run(task, on_retry)
        on_done(success)

    def _run_playlist(
        self,
        urls: list[str],
        output_dir: str,
        audio_only: bool,
        quality: str,
        on_item_start: Callable[[int, int], None],
        progress_hook: Callable,
        on_retry: Callable[[int, str], None],
        on_done: Callable[[bool], None],
        cancel_event: threading.Event,
    ) -> None:
        """
        Плейлист скачивается в ОДНОЙ сессии последовательно.
        Смена сессии происходит только при bot-блоке на весь плейлист.
        """
        total = len(urls)
        all_ok = True

        # Одна сессия на весь плейлист
        self._rate.acquire()
        session = self._pool.acquire()
        try:
            self._refresher.ensure_fresh(session)

            for idx, url in enumerate(urls):
                if cancel_event.is_set():
                    break

                on_item_start(idx + 1, total)

                attempt = 0
                item_ok = False
                while attempt < Config.MAX_RETRY and not cancel_event.is_set():
                    try:
                        opts = self._ydl.build_download_opts(
                            output_dir, audio_only, quality,
                            self._wrap_hook(progress_hook, cancel_event),
                            session,
                        )
                        item_ok = self._ydl.download(url, opts)
                        break
                    except Exception as exc:
                        kind = classify_error(exc)
                        if kind is FatalError:
                            break
                        attempt += 1
                        if kind is BotBlockError:
                            # При bot-блоке меняем сессию для оставшихся треков
                            on_retry(attempt, f"Бот-блок #{idx+1}, смена сессии [{attempt}/{Config.MAX_RETRY}]...")
                            self._pool.release(session)
                            session = self._pool.rotate(session)
                            self._refresher.ensure_fresh(session)
                        else:
                            on_retry(attempt, f"Трек {idx+1}: повтор [{attempt}/{Config.MAX_RETRY}]...")

                        wait = (Config.BACKOFF_BASE ** attempt) * random.uniform(
                            1 - Config.BACKOFF_JITTER, 1 + Config.BACKOFF_JITTER
                        )
                        time.sleep(max(wait, 1.0))

                if not item_ok:
                    all_ok = False

                # Небольшая пауза между треками плейлиста — имитация просмотра
                if idx < total - 1 and not cancel_event.is_set():
                    time.sleep(random.uniform(1.5, 3.5))

        finally:
            self._pool.release(session)
            self._rate.release()

        on_done(all_ok)

    @staticmethod
    def _wrap_hook(
        hook: Callable,
        cancel_event: threading.Event,
    ) -> Callable:
        """Оборачивает progress_hook проверкой отмены."""
        def wrapped(d: dict) -> None:
            if cancel_event.is_set():
                raise FatalError("Отменено пользователем")
            hook(d)
        return wrapped

    def _schedule_recovery(self) -> None:
        """Каждые 10 минут сбрасывает флаги compromised у неиспользуемых сессий."""
        self._pool.recover_all()
        self._recovery_timer = threading.Timer(600, self._schedule_recovery)
        self._recovery_timer.daemon = True
        self._recovery_timer.start()


# ---------------------------------------------------------------------------
# PlaylistWindow — модальное окно выбора треков
# ---------------------------------------------------------------------------
class PlaylistWindow(ctk.CTkToplevel):

    def __init__(
        self,
        parent: ctk.CTk,
        entries: list[dict],
        callback: Callable[[list[str]], None],
    ) -> None:
        super().__init__(parent)
        self.title("Выбор треков из плейлиста")
        self.geometry("550x450")
        self.resizable(False, False)
        self.configure(fg_color=Config.COLOR_BG)
        self.transient(parent)
        self.grab_set()

        self.entries  = entries
        self.callback = callback
        self.checkbox_vars: list[tk.BooleanVar] = []
        self._build_ui()

    def _build_ui(self) -> None:
        ctk.CTkLabel(
            self,
            text=(
                f"Обнаружен плейлист ({len(self.entries)} видео)\n"
                "Отметьте файлы, которые хотите скачать:"
            ),
            font=("Arial", 13, "bold"),
            text_color=Config.COLOR_WHITE,
        ).pack(pady=15)

        af = ctk.CTkFrame(self, fg_color="transparent")
        af.pack(fill="x", padx=25, pady=(0, 10))
        for text, cmd in [("Выбрать все", self._select_all), ("Снять все", self._deselect_all)]:
            ctk.CTkButton(
                af, text=text, width=100, height=24,
                fg_color=Config.COLOR_BTN, hover_color="#222222",
                text_color=Config.COLOR_WHITE,
                border_color="#333333", border_width=1, command=cmd,
            ).pack(side="left", padx=5)

        scroll = ctk.CTkScrollableFrame(
            self, width=480, height=260,
            fg_color=Config.COLOR_SURFACE,
            border_color="#111111", border_width=1,
        )
        scroll.pack(padx=25, pady=5)

        for idx, entry in enumerate(self.entries):
            var = tk.BooleanVar(value=True)
            self.checkbox_vars.append(var)
            raw = entry.get("title") or f"Видео #{idx + 1}"
            display = raw if len(raw) < Config.PLAYLIST_TITLE_MAX_LEN else raw[:57] + "..."
            ctk.CTkCheckBox(
                scroll, text=display, variable=var,
                font=("Arial", 11), checkbox_width=16, checkbox_height=16,
                fg_color=Config.COLOR_OK, hover_color=Config.COLOR_ADD_HOVER,
                text_color="#DDDDDD",
            ).pack(anchor="w", pady=4, padx=5)

        ctk.CTkButton(
            self, text="Добавить выбранные в очередь",
            font=("Arial", 13, "bold"),
            fg_color=Config.COLOR_OK, hover_color=Config.COLOR_ADD_HOVER,
            text_color=Config.COLOR_WHITE, height=35,
            command=self._confirm,
        ).pack(pady=15)

    def _select_all(self) -> None:
        for v in self.checkbox_vars: v.set(True)

    def _deselect_all(self) -> None:
        for v in self.checkbox_vars: v.set(False)

    def _confirm(self) -> None:
        selected: list[str] = []
        for idx, entry in enumerate(self.entries):
            if not self.checkbox_vars[idx].get():
                continue
            url = entry.get("url") or entry.get("webpage_url") or ""
            if url and not url.startswith("http"):
                url = f"https://www.youtube.com/watch?v={url}"
            if url:
                selected.append(url)
        self.callback(selected)
        self.destroy()


# ---------------------------------------------------------------------------
# App — GUI-слой
# ---------------------------------------------------------------------------
class App(ctk.CTk):

    def __init__(self) -> None:
        super().__init__()
        self.configure(fg_color=Config.COLOR_BG)
        self.title(Config.WINDOW_TITLE)
        self.geometry(Config.WINDOW_SIZE)
        self.resizable(False, False)

        self.download_path = os.path.join(os.path.expanduser("~"), "Downloads")
        self.ffmpeg_ok: bool = False
        self.rows: list[DownloadRow] = []

        self._check_deps()
        self._init_services()
        self._build_ui()
        self._create_context_menu()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._add_row()
        self.after(200, self._paste_clipboard_on_start)
        self.after(300, self._start_youtube_check)

    # ------------------------------------------------------------------
    # Инициализация сервисов
    # ------------------------------------------------------------------

    def _check_deps(self) -> None:
        self.ffmpeg_ok = bool(shutil.which("ffmpeg"))
        self._ffmpeg_status = (
            ("FFmpeg: ОК", Config.COLOR_OK)
            if self.ffmpeg_ok
            else ("FFmpeg: НЕ НАЙДЕН (ограничение 720p/Аудио)", Config.COLOR_ERR)
        )
        self._node_status = (
            ("Node.js: ОК", Config.COLOR_OK)
            if shutil.which("node")
            else ("Node.js: НЕ НАЙДЕН", Config.COLOR_ERR)
        )
        # Заглушка — реальная проверка выполняется в фоне после запуска UI
        self._youtube_status = ("YouTube: Проверка...", Config.COLOR_WARN)

    def _start_youtube_check(self) -> None:
        """Запускает проверку YouTube в фоновом потоке и обновляет метку."""
        def _run() -> None:
            status = self._check_youtube_access()
            self.after(0, lambda: self._youtube_label.configure(
                text=status[0], text_color=status[1],
            ))
        threading.Thread(target=_run, daemon=True).start()

    @staticmethod
    def _check_youtube_access() -> tuple[str, str]:
        """Быстрая проверка доступа к YouTube (HEAD-запрос, таймаут 5 сек)."""
        import urllib.request
        import urllib.error
        try:
            req = urllib.request.Request(
                "https://www.youtube.com",
                method="HEAD",
                headers={"User-Agent": "Mozilla/5.0"},
            )
            with urllib.request.urlopen(req, timeout=5):
                pass
            return ("YouTube: Доступен", Config.COLOR_OK)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return ("YouTube: Доступен", Config.COLOR_OK)
            return (f"YouTube: Ошибка сервера {e.code}", Config.COLOR_WARN)
        except OSError:
            return ("YouTube: Недоступен", Config.COLOR_ERR)

    def _init_services(self) -> None:
        self._ydl       = YdlService(ffmpeg_ok=self.ffmpeg_ok)
        self._refresher = TokenRefresher()
        self._pool      = SessionPool(
            size=Config.SESSION_POOL_SIZE,
            refresher=self._refresher,
        )
        self._rate      = RateLimiter()
        self._manager   = DownloadManager(
            self._ydl, self._pool, self._rate, self._refresher,
        )

    # ------------------------------------------------------------------
    # Построение интерфейса
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        ctk.CTkLabel(
            self,
            text="Мультипоточное скачивание видео, аудио и плейлистов",
            font=("Arial", 16, "bold"),
            text_color=Config.COLOR_WHITE,
            fg_color=Config.COLOR_BG,
        ).pack(pady=15)
        self._build_control_bar()
        self._build_deps_bar()
        self._build_scroll_area()
        self._build_action_bar()

    def _build_control_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=Config.COLOR_BG)
        bar.pack(pady=5, fill="x", padx=30)

        self._path_label = ctk.CTkLabel(
            bar, text=f"Папка: {self.download_path}",
            font=("Arial", 11), text_color="#BBBBBB",
        )
        self._path_label.pack(side="left", padx=5)

        buttons = [
            ("+ Добавить ссылку", self._add_row,     Config.COLOR_ADD,  Config.COLOR_WHITE),
            ("Изменить папку",    self._choose_path, Config.COLOR_BTN,  Config.COLOR_WHITE),
        ]
        self._cookies_btn = ctk.CTkButton(
            bar, text="Куки (.txt)", width=90, height=28,
            command=self._choose_cookies,
            fg_color=Config.COLOR_BTN, hover_color=Config.COLOR_BTN_HOVER,
            text_color=Config.COLOR_WARN,
            border_color="#333333", border_width=1,
        )
        self._cookies_btn.pack(side="right", padx=5)

        for text, cmd, fc, tc in buttons:
            ctk.CTkButton(
                bar, text=text, width=130, height=28, command=cmd,
                fg_color=fc,
                hover_color=Config.COLOR_ADD_HOVER if fc == Config.COLOR_ADD else Config.COLOR_BTN_HOVER,
                text_color=tc, border_color="#333333", border_width=1,
            ).pack(side="right", padx=5)

    def _build_deps_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=Config.COLOR_BG)
        bar.pack(pady=4)
        ctk.CTkLabel(
            bar, text=self._ffmpeg_status[0],
            font=("Arial", 10, "bold"), text_color=self._ffmpeg_status[1],
        ).pack(side="left", padx=15)
        ctk.CTkLabel(
            bar, text=self._node_status[0],
            font=("Arial", 10, "bold"), text_color=self._node_status[1],
        ).pack(side="left", padx=15)
        self._youtube_label = ctk.CTkLabel(
            bar, text=self._youtube_status[0],
            font=("Arial", 10, "bold"), text_color=self._youtube_status[1],
        )
        self._youtube_label.pack(side="left", padx=15)

        # Индикатор состояния сессий
        self._session_label = ctk.CTkLabel(
            bar,
            text=f"Сессий: {Config.SESSION_POOL_SIZE}  |  Макс. параллельно: {Config.MAX_CONCURRENT}",
            font=("Arial", 10), text_color=Config.COLOR_SESSION_OK,
        )
        self._session_label.pack(side="left", padx=15)

    def _build_scroll_area(self) -> None:
        self._scroll = ctk.CTkScrollableFrame(
            self, width=800, height=240,
            fg_color=Config.COLOR_SURFACE,
            border_color="#111111", border_width=1,
        )
        self._scroll.pack(pady=10, padx=20)

    def _build_action_bar(self) -> None:
        self._dl_button = ctk.CTkButton(
            self, text="Скачать все файлы",
            font=("Arial", 14, "bold"),
            command=self._start_all,
            fg_color=Config.COLOR_BTN,
            hover_color=Config.COLOR_BTN_HOVER,
            text_color=Config.COLOR_WHITE,
            border_color="#333333", border_width=1,
        )
        self._dl_button.pack(pady=15)

    # ------------------------------------------------------------------
    # Управление строками
    # ------------------------------------------------------------------

    def _add_row(self) -> None:
        frame = ctk.CTkFrame(self._scroll, fg_color="transparent")
        frame.pack(fill="x", pady=5)

        controls = ctk.CTkFrame(frame, fg_color="transparent")
        controls.pack(fill="x")

        entry = ctk.CTkEntry(
            controls,
            placeholder_text="Вставьте ссылку на видео или плейлист...",
            fg_color="#080808", border_color=Config.COLOR_BORDER,
            text_color="#DDDDDD", width=420,
        )
        entry.pack(side="left", padx=5)

        quality_menu = ctk.CTkOptionMenu(
            controls, values=["Максимальное"], width=130, height=28,
            fg_color=Config.COLOR_BTN, button_color="#222222",
            button_hover_color="#333333", state="disabled",
        )
        quality_menu.pack(side="left", padx=5)

        audio_var = tk.BooleanVar(value=False)
        checkbox = ctk.CTkCheckBox(
            controls, text="Только звук", variable=audio_var,
            font=("Arial", 11), width=90,
            checkbox_width=16, checkbox_height=16, border_width=1,
            fg_color="#333333", hover_color="#555555", text_color="#BBBBBB",
        )
        checkbox.pack(side="left", padx=5)

        delete_btn = ctk.CTkButton(
            controls, text="✕", width=30, height=28,
            fg_color=Config.COLOR_DELETE, hover_color=Config.COLOR_DELETE_HOVER,
        )
        delete_btn.pack(side="right", padx=5)

        status_bar = ctk.CTkFrame(frame, fg_color="transparent")
        status_bar.pack(fill="x", pady=(2, 5))

        progress = ctk.CTkProgressBar(
            status_bar, width=320,
            progress_color=Config.COLOR_WHITE, fg_color="#0F0F0F",
        )
        progress.set(0)
        progress.pack(side="left", padx=5)

        status = ctk.CTkLabel(
            status_bar, text="Ожидание ссылки",
            font=("Arial", 10), text_color=Config.COLOR_MUTED,
        )
        status.pack(side="left", padx=10)

        row = DownloadRow(
            frame=frame, entry=entry, quality_menu=quality_menu,
            audio_only_var=audio_var, checkbox=checkbox,
            progress=progress, status=status, delete_btn=delete_btn,
        )

        entry.bind("<KeyRelease>", lambda _e: self._on_link_changed(row))
        entry.bind("<Button-3>", self._show_context_menu)
        entry.bind("<Button-2>", self._show_context_menu)
        checkbox.configure(command=lambda: self._toggle_audio_mode(row))
        delete_btn.configure(command=lambda: self._remove_row(row))

        self.rows.append(row)
        self._refresh_delete_states()

    def _remove_row(self, row: DownloadRow) -> None:
        if len(self.rows) > 1:
            row.cancel_event.set()
            row.frame.destroy()
            self.rows.remove(row)
            self._refresh_delete_states()

    def _refresh_delete_states(self) -> None:
        state = "normal" if len(self.rows) > 1 else "disabled"
        for row in self.rows:
            row.delete_btn.configure(state=state)

    def _toggle_audio_mode(self, row: DownloadRow) -> None:
        if row.audio_only:
            row.quality_menu.configure(state="disabled")
        elif len(row.quality_menu.cget("values")) > 1:
            row.quality_menu.configure(state="normal")

    # ------------------------------------------------------------------
    # Обработка ввода ссылок
    # ------------------------------------------------------------------

    @staticmethod
    def _is_youtube(text: str) -> bool:
        if not text or len(text) > Config.ENTRY_MAX_LEN or "\n" in text or "\r" in text:
            return False
        low = text.lower()
        return "youtube.com" in low or "youtu.be" in low

    @staticmethod
    def _is_playlist(url: str) -> bool:
        return "list=" in url.lower()

    def _on_link_changed(self, row: DownloadRow) -> None:
        url = row.url
        if url == row.last_url:
            return
        if not self._is_youtube(url):
            return
        row.last_url = url
        row.reset_cancel()
        row.playlist_urls.clear()

        if self._is_playlist(url):
            row.set_status("Обнаружен плейлист. Чтение структуры...", Config.COLOR_WARN)
            threading.Thread(
                target=self._bg_fetch_playlist, args=(url, row), daemon=True,
            ).start()
        else:
            row.set_status("Чтение доступных разрешений...", Config.COLOR_WARN)
            threading.Thread(
                target=self._bg_fetch_resolutions, args=(url, row), daemon=True,
            ).start()

    # ------------------------------------------------------------------
    # Фоновые задачи (метаданные)
    # ------------------------------------------------------------------

    def _bg_fetch_playlist(self, url: str, row: DownloadRow) -> None:
        try:
            entries = self._ydl.fetch_playlist_entries(url)
            if entries:
                self.after(0, lambda: PlaylistWindow(
                    self, entries,
                    callback=lambda urls: self._handle_playlist_selection(row, urls),
                ))
            else:
                self.after(0, lambda: row.set_status("Плейлист пуст или скрыт", Config.COLOR_ERR))
        except Exception as exc:
            log.exception("Ошибка разбора плейлиста: %s", exc)
            self.after(0, lambda: row.set_status("Ошибка разбора плейлиста", Config.COLOR_ERR))

    def _handle_playlist_selection(self, row: DownloadRow, urls: list[str]) -> None:
        """
        Плейлист хранится в row.playlist_urls — не распихивается по строкам.
        Одна строка = один плейлист, скачивается последовательно в одной сессии.
        """
        if not urls:
            row.set_status("Отменено: видео не выбраны", "#BBBBBB")
            return

        row.playlist_urls = urls
        row.set_status(
            f"Плейлист: {len(urls)} видео  (последовательно)",
            Config.COLOR_SESSION_OK,
        )
        # Показываем количество в поле ввода (read-only)
        row.entry.configure(state="normal")
        row.entry.delete(0, "end")
        row.entry.insert(0, urls[0])
        row.entry.configure(state="disabled")

    def _bg_fetch_resolutions(self, url: str, row: DownloadRow) -> None:
        try:
            heights = self._ydl.fetch_resolutions(url)
            values = ["Максимальное"] + [f"{h}p" for h in heights]
            self.after(0, lambda: self._apply_resolution_menu(row, values))
        except Exception as exc:
            log.exception("Ошибка получения форматов: %s", exc)
            self.after(0, lambda: row.set_status("Защита от ботов / Нет формата", Config.COLOR_ERR))

    def _apply_resolution_menu(self, row: DownloadRow, values: list[str]) -> None:
        row.quality_menu.configure(values=values)
        row.quality_menu.set(values[0])
        row.set_status("Форматы успешно загружены", Config.COLOR_OK)
        if not row.audio_only:
            row.quality_menu.configure(state="normal")

    # ------------------------------------------------------------------
    # Запуск загрузок
    # ------------------------------------------------------------------

    def _start_all(self) -> None:
        self._dl_button.configure(state="disabled")
        has_tasks = False

        for row in self.rows:
            if row.playlist_urls:
                # Плейлист → последовательная задача
                row.set_status("В очереди (плейлист)...", Config.COLOR_WARN)
                row.lock()
                row.reset_cancel()
                has_tasks = True
                urls = list(row.playlist_urls)
                self._manager.submit_playlist(
                    urls=urls,
                    output_dir=self.download_path,
                    audio_only=row.audio_only,
                    quality=row.selected_quality,
                    on_item_start=lambda i, t, r=row: self.after(
                        0, lambda: r.set_status(f"Трек {i}/{t}...", Config.COLOR_WARN)
                    ),
                    progress_hook=lambda d, r=row: self._progress_hook(d, r),
                    on_retry=lambda attempt, msg, r=row: self.after(
                        0, lambda: r.set_status(msg, Config.COLOR_WARN)
                    ),
                    on_done=lambda ok, r=row: self.after(
                        0, lambda: self._on_task_done(r, ok)
                    ),
                    cancel_event=row.cancel_event,
                )
            elif row.url:
                # Одиночное видео
                row.set_status("В очереди...", Config.COLOR_WARN)
                row.lock()
                row.reset_cancel()
                has_tasks = True
                url = row.url
                self._manager.submit_single(
                    url=url,
                    output_dir=self.download_path,
                    audio_only=row.audio_only,
                    quality=row.selected_quality,
                    progress_hook=lambda d, r=row: self._progress_hook(d, r),
                    on_retry=lambda attempt, msg, r=row: self.after(
                        0, lambda: r.set_status(msg, Config.COLOR_WARN)
                    ),
                    on_done=lambda ok, r=row: self.after(
                        0, lambda: self._on_task_done(r, ok)
                    ),
                    cancel_event=row.cancel_event,
                )
            else:
                row.set_status("Пропущено: пустая ссылка", Config.COLOR_ERR)

        if not has_tasks:
            self._dl_button.configure(state="normal")
        else:
            # Разблокируем кнопку через паузу (задачи уже в очереди)
            self.after(1500, lambda: self._dl_button.configure(state="normal"))

    def _on_task_done(self, row: DownloadRow, success: bool) -> None:
        if success:
            row.set_status("Готово!", Config.COLOR_OK)
            row.set_progress(1.0)
        else:
            # Статус уже мог быть установлен on_retry — не перетираем его
            current = row.status.cget("text")
            if "Готово" not in current and "Отмен" not in current:
                row.set_status("Ошибка: проверьте куки или ссылку", Config.COLOR_ERR)
        row.unlock(self.ffmpeg_ok)
        self._refresh_delete_states()

    def _progress_hook(self, d: dict, row: DownloadRow) -> None:
        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total > 0:
                self.after(0, lambda v=downloaded/total: row.set_progress(v))
            speed = d.get("_speed_str", "")
            if speed:
                self.after(0, lambda s=speed: row.set_status(s, Config.COLOR_WHITE))
        elif status == "finished":
            if self.ffmpeg_ok:
                label = "Конвертация в MP3..." if row.audio_only else "Склейка FFmpeg..."
            else:
                label = "Сохранение..."
            self.after(0, lambda: row.set_status(label, Config.COLOR_WARN))

    # ------------------------------------------------------------------
    # Настройки
    # ------------------------------------------------------------------

    def _choose_path(self) -> None:
        d = ctk.filedialog.askdirectory(initialdir=self.download_path)
        if d:
            self.download_path = d
            self._path_label.configure(text=f"Папка: {d}")

    def _choose_cookies(self) -> None:
        path = ctk.filedialog.askopenfilename(
            title="Выберите файл cookies.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if path:
            self._ydl.cookies_path = path
            self._refresher._cookies_path = path
            self._cookies_btn.configure(text="Куки: Активны", text_color=Config.COLOR_OK)
            # Инвалидируем токены — пересоздадим с новыми куками
            for s in self._pool._sessions:
                s.token_fetched = 0.0

    # ------------------------------------------------------------------
    # Буфер обмена
    # ------------------------------------------------------------------

    def _paste_clipboard_on_start(self) -> None:
        try:
            text = self.clipboard_get().strip()
            if self._is_youtube(text) and self.rows:
                self.rows[0].entry.insert(0, text)
                self._on_link_changed(self.rows[0])
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Контекстное меню
    # ------------------------------------------------------------------

    def _create_context_menu(self) -> None:
        self._ctx_menu = tk.Menu(
            self, tearoff=0,
            background=Config.COLOR_BG, foreground=Config.COLOR_WHITE,
            activebackground="#222222", activeforeground=Config.COLOR_WHITE,
        )
        self._ctx_menu.add_command(label="Вставить", command=self._ctx_paste)
        self._ctx_menu.add_command(label="Очистить", command=self._ctx_clear)
        self._active_entry: ctk.CTkEntry | None = None

    def _show_context_menu(self, event: tk.Event) -> None:
        self._active_entry = event.widget
        self._active_entry.focus()
        self._ctx_menu.tk_popup(event.x_root, event.y_root)

    def _ctx_paste(self) -> None:
        if not self._active_entry:
            return
        try:
            text = self.clipboard_get().strip()
            if len(text) > 500:
                return
            self._active_entry.delete(0, "end")
            self._active_entry.insert(0, text)
            for row in self.rows:
                if row.entry == self._active_entry:
                    self._on_link_changed(row)
                    break
        except Exception:
            pass

    def _ctx_clear(self) -> None:
        if self._active_entry:
            self._active_entry.delete(0, "end")

    # ------------------------------------------------------------------
    # Жизненный цикл
    # ------------------------------------------------------------------

    def _on_close(self) -> None:
        for row in self.rows:
            row.cancel_event.set()
        self._manager.shutdown()
        self.destroy()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = App()
    app.mainloop()