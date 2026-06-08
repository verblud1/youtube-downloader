"""
Multi YouTube Downloader — рефакторированная версия.

Архитектура:
  config.py        → константы и настройки (здесь: класс Config)
  DownloadRow      → dataclass вместо god-object dict
  YdlService       → вся логика yt-dlp (build_opts, fetch_info, download)
  DownloadManager  → очередь задач, ThreadPoolExecutor
  PlaylistWindow   → модальное окно выбора треков
  App              → только GUI-слой, делегирует всё выше
"""

from __future__ import annotations

import logging
import os
import shutil
import threading
import tkinter as tk
from concurrent.futures import Future, ThreadPoolExecutor
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
# Config — все магические значения в одном месте
# ---------------------------------------------------------------------------
class Config:
    WINDOW_TITLE = "Multi YouTube Downloader Ultra Pro"
    WINDOW_SIZE = "860x560"

    THREAD_WORKERS = 3
    YDL_RETRIES = 15
    YDL_SOCKET_TIMEOUT = 45
    YDL_PLAYER_CLIENT = ["web_safari"]
    AUDIO_CODEC = "mp3"
    AUDIO_QUALITY = "192"
    MERGE_FORMAT = "mp4"

    ENTRY_MAX_LEN = 250
    PLAYLIST_TITLE_MAX_LEN = 60

    COLOR_OK = "#27AE60"
    COLOR_WARN = "#F39C12"
    COLOR_ERR = "#C0392B"
    COLOR_MUTED = "#888888"
    COLOR_WHITE = "#FFFFFF"
    COLOR_BG = "#000000"
    COLOR_SURFACE = "#050505"
    COLOR_BORDER = "#222222"
    COLOR_BTN = "#111111"
    COLOR_BTN_HOVER = "#1A1A1A"
    COLOR_DELETE = "#C0392B"
    COLOR_DELETE_HOVER = "#A93226"
    COLOR_ADD = "#27AE60"
    COLOR_ADD_HOVER = "#219653"


# ---------------------------------------------------------------------------
# DownloadRow — типизированный объект строки загрузки
# ---------------------------------------------------------------------------
@dataclass
class DownloadRow:
    """Хранит все виджеты и состояние одной строки загрузки."""

    frame: ctk.CTkFrame
    entry: ctk.CTkEntry
    quality_menu: ctk.CTkOptionMenu
    audio_only_var: tk.BooleanVar
    checkbox: ctk.CTkCheckBox
    progress: ctk.CTkProgressBar
    status: ctk.CTkLabel
    delete_btn: ctk.CTkButton
    last_url: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)

    # --- Удобные методы обновления GUI (вызывать из любого потока через after) ---

    def set_status(self, text: str, color: str = Config.COLOR_MUTED) -> None:
        self.status.configure(text=text, text_color=color)

    def set_progress(self, value: float) -> None:
        self.progress.set(value)

    def lock(self) -> None:
        """Заблокировать виджеты строки на время загрузки."""
        self.entry.configure(state="disabled")
        self.checkbox.configure(state="disabled")
        self.quality_menu.configure(state="disabled")
        self.delete_btn.configure(state="disabled")

    def unlock(self, ffmpeg_ok: bool) -> None:
        """Разблокировать виджеты после завершения."""
        self.entry.configure(state="normal")
        self.checkbox.configure(state="normal")
        self.delete_btn.configure(state="normal")
        # quality_menu разблокируем только если не аудио-режим и есть форматы
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
# YdlService — вся логика yt-dlp, без GUI
# ---------------------------------------------------------------------------
class YdlService:
    """Сервис-обёртка над yt-dlp. Не знает ничего о GUI."""

    def __init__(self, cookies_path: str | None = None, ffmpeg_ok: bool = True) -> None:
        self.cookies_path = cookies_path
        self.ffmpeg_ok = ffmpeg_ok

    # --- Общая база опций ---

    def _base_opts(self) -> dict:
        opts: dict = {
            "nocheckcertificate": True,
            "quiet": True,
            "extractor_args": {"youtube": {"player_client": Config.YDL_PLAYER_CLIENT}},
        }
        if self.cookies_path and os.path.exists(self.cookies_path):
            opts["cookiefile"] = self.cookies_path
        if shutil.which("node"):
            opts["javascript_runtimes"] = ["node"]
        return opts

    def _apply_browser_cookies(self, opts: dict) -> dict:
        """Добавляет куки из браузера, если не задан файл вручную."""
        if not self.cookies_path:
            try:
                opts["cookiesfrombrowser"] = ("firefox",)
            except Exception:
                opts.pop("cookiesfrombrowser", None)
        return opts

    # --- Публичные методы ---

    def fetch_playlist_entries(self, url: str) -> list[dict]:
        """Возвращает плоский список треков плейлиста без загрузки медиа."""
        opts = {**self._base_opts(), "extract_flat": True, "skip_download": True, "ignoreerrors": True}
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        return info.get("entries", []) if info else []

    def fetch_resolutions(self, url: str) -> list[str]:
        """Возвращает список доступных высот видео (например ['1080', '720', ...])."""
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

    def build_download_opts(
        self,
        output_dir: str,
        audio_only: bool,
        selected_quality: str,
        progress_hook: Callable,
        cancel_event: threading.Event,
    ) -> dict:
        """Собирает полный набор опций для загрузки."""
        opts = {
            **self._base_opts(),
            "outtmpl": os.path.join(output_dir, "%(title)s.%(ext)s"),
            "progress_hooks": [progress_hook],
            "retries": Config.YDL_RETRIES,
            "fragment_retries": Config.YDL_RETRIES,
            "socket_timeout": Config.YDL_SOCKET_TIMEOUT,
            "ignoreerrors": True,
        }
        opts = self._apply_browser_cookies(opts)
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

    def download(self, url: str, ydl_opts: dict) -> bool:
        """Запускает загрузку. Возвращает True при успехе."""
        with YoutubeDL(ydl_opts) as ydl:
            result = ydl.download([url])
        return result == 0


# ---------------------------------------------------------------------------
# DownloadManager — управляет очередью и пулом потоков
# ---------------------------------------------------------------------------
class DownloadManager:
    """Запускает задачи загрузки в ThreadPoolExecutor."""

    def __init__(self, workers: int = Config.THREAD_WORKERS) -> None:
        self._executor = ThreadPoolExecutor(max_workers=workers)
        self._futures: list[Future] = []

    def submit(self, fn: Callable, *args) -> Future:
        future = self._executor.submit(fn, *args)
        self._futures.append(future)
        return future

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)


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

        self.entries = entries
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

        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.pack(fill="x", padx=25, pady=(0, 10))

        for text, cmd in [("Выбрать все", self._select_all), ("Снять все", self._deselect_all)]:
            ctk.CTkButton(
                action_frame, text=text, width=100, height=24,
                fg_color=Config.COLOR_BTN, hover_color="#222222",
                text_color=Config.COLOR_WHITE,
                border_color="#333333", border_width=1,
                command=cmd,
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
            raw_title = entry.get("title") or f"Видео #{idx + 1}"
            display = (
                raw_title
                if len(raw_title) < Config.PLAYLIST_TITLE_MAX_LEN
                else raw_title[: Config.PLAYLIST_TITLE_MAX_LEN - 3] + "..."
            )
            ctk.CTkCheckBox(
                scroll, text=display, variable=var,
                font=("Arial", 11), checkbox_width=16, checkbox_height=16,
                fg_color=Config.COLOR_OK, hover_color=Config.COLOR_ADD_HOVER,
                text_color="#DDDDDD",
            ).pack(anchor="w", pady=4, padx=5)

        ctk.CTkButton(
            self,
            text="Добавить выбранные в очередь",
            font=("Arial", 13, "bold"),
            fg_color=Config.COLOR_OK, hover_color=Config.COLOR_ADD_HOVER,
            text_color=Config.COLOR_WHITE, height=35,
            command=self._confirm,
        ).pack(pady=15)

    def _select_all(self) -> None:
        for var in self.checkbox_vars:
            var.set(True)

    def _deselect_all(self) -> None:
        for var in self.checkbox_vars:
            var.set(False)

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
# App — GUI-слой, делегирует бизнес-логику сервисам
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

        # Зависимости
        self._check_deps()
        self._ydl = YdlService(ffmpeg_ok=self.ffmpeg_ok)
        self._manager = DownloadManager()

        self._build_ui()
        self._create_context_menu()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._add_row()
        self.after(200, self._paste_clipboard_on_start)

    # ------------------------------------------------------------------
    # Системные зависимости
    # ------------------------------------------------------------------

    def _check_deps(self) -> None:
        if shutil.which("ffmpeg"):
            self.ffmpeg_ok = True
            self._ffmpeg_status = ("FFmpeg: ОК", Config.COLOR_OK)
        else:
            self.ffmpeg_ok = False
            self._ffmpeg_status = ("FFmpeg: НЕ НАЙДЕН (ограничение 720p/Аудио)", Config.COLOR_ERR)

        if shutil.which("node"):
            self._node_status = ("Node.js: ОК", Config.COLOR_OK)
        else:
            self._node_status = ("Node.js: НЕ НАЙДЕН", Config.COLOR_ERR)

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

        for text, cmd, color, tc in [
            ("Изменить папку", self._choose_path, Config.COLOR_BTN, Config.COLOR_WHITE),
            ("Куки (.txt)",    self._choose_cookies, Config.COLOR_BTN, Config.COLOR_WARN),
            ("+ Добавить ссылку", self._add_row, Config.COLOR_ADD, Config.COLOR_WHITE),
        ]:
            ctk.CTkButton(
                bar, text=text, width=130, height=28,
                command=cmd, fg_color=color,
                hover_color=Config.COLOR_BTN_HOVER if color == Config.COLOR_BTN else Config.COLOR_ADD_HOVER,
                text_color=tc,
                border_color="#333333", border_width=1,
            ).pack(side="right", padx=5)

    def _build_deps_bar(self) -> None:
        bar = ctk.CTkFrame(self, fg_color=Config.COLOR_BG)
        bar.pack(pady=5)

        self._ffmpeg_label = ctk.CTkLabel(
            bar, text=self._ffmpeg_status[0],
            font=("Arial", 10, "bold"), text_color=self._ffmpeg_status[1],
        )
        self._ffmpeg_label.pack(side="left", padx=15)

        self._node_label = ctk.CTkLabel(
            bar, text=self._node_status[0],
            font=("Arial", 10, "bold"), text_color=self._node_status[1],
        )
        self._node_label.pack(side="left", padx=15)

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
        row_frame = ctk.CTkFrame(self._scroll, fg_color="transparent")
        row_frame.pack(fill="x", pady=5)

        controls = ctk.CTkFrame(row_frame, fg_color="transparent")
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

        status_bar = ctk.CTkFrame(row_frame, fg_color="transparent")
        status_bar.pack(fill="x", pady=(2, 5))

        progress = ctk.CTkProgressBar(
            status_bar, width=320, progress_color=Config.COLOR_WHITE, fg_color="#0F0F0F",
        )
        progress.set(0)
        progress.pack(side="left", padx=5)

        status = ctk.CTkLabel(
            status_bar, text="Ожидание ссылки",
            font=("Arial", 10), text_color=Config.COLOR_MUTED,
        )
        status.pack(side="left", padx=10)

        row = DownloadRow(
            frame=row_frame, entry=entry, quality_menu=quality_menu,
            audio_only_var=audio_var, checkbox=checkbox,
            progress=progress, status=status, delete_btn=delete_btn,
        )

        # Привязки после создания row
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
        else:
            if len(row.quality_menu.cget("values")) > 1:
                row.quality_menu.configure(state="normal")

    # ------------------------------------------------------------------
    # Обработка ссылок
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

        if self._is_playlist(url):
            row.set_status("Обнаружен плейлист. Чтение структуры...", Config.COLOR_WARN)
            threading.Thread(target=self._bg_fetch_playlist, args=(url, row), daemon=True).start()
        else:
            row.set_status("Чтение доступных разрешений...", Config.COLOR_WARN)
            threading.Thread(target=self._bg_fetch_resolutions, args=(url, row), daemon=True).start()

    # ------------------------------------------------------------------
    # Фоновые задачи
    # ------------------------------------------------------------------

    def _bg_fetch_playlist(self, url: str, row: DownloadRow) -> None:
        try:
            entries = self._ydl.fetch_playlist_entries(url)
            if entries:
                self.after(
                    0,
                    lambda: PlaylistWindow(
                        self, entries,
                        callback=lambda urls: self._integrate_playlist_urls(row, urls),
                    ),
                )
            else:
                self.after(0, lambda: row.set_status("Плейлист пуст или скрыт", Config.COLOR_ERR))
        except Exception as exc:
            log.exception("Ошибка разбора плейлиста: %s", exc)
            self.after(0, lambda: row.set_status("Ошибка разбора плейлиста", Config.COLOR_ERR))

    def _integrate_playlist_urls(self, target_row: DownloadRow, urls: list[str]) -> None:
        if not urls:
            target_row.set_status("Отменено: видео не выбраны", "#BBBBBB")
            return
        # Первый URL — в текущую строку
        target_row.entry.delete(0, "end")
        target_row.entry.insert(0, urls[0])
        self._on_link_changed(target_row)
        # Остальные — новые строки
        for url in urls[1:]:
            self._add_row()
            new_row = self.rows[-1]
            new_row.entry.insert(0, url)
            self._on_link_changed(new_row)

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
    # Загрузка
    # ------------------------------------------------------------------

    def _start_all(self) -> None:
        self._dl_button.configure(state="disabled")
        for row in self.rows:
            if not row.url:
                row.set_status("Пропущено: пустая ссылка", Config.COLOR_ERR)
                continue
            row.set_status("В очереди...", Config.COLOR_WARN)
            row.lock()
            self._manager.submit(self._download_task, row)
        self.after(1000, lambda: self._dl_button.configure(state="normal"))

    def _download_task(self, row: DownloadRow) -> None:
        self.after(0, lambda: row.set_status("Анализ...", Config.COLOR_WARN))
        row.reset_cancel()

        def hook(d: dict) -> None:
            if row.cancel_event.is_set():
                raise Exception("Отменено пользователем")
            self._progress_hook(d, row)

        opts = self._ydl.build_download_opts(
            output_dir=self.download_path,
            audio_only=row.audio_only,
            selected_quality=row.selected_quality,
            progress_hook=hook,
            cancel_event=row.cancel_event,
        )

        try:
            success = self._ydl.download(row.url, opts)
            if success:
                self.after(0, lambda: (row.set_status("Готово!", Config.COLOR_OK), row.set_progress(1.0)))
            else:
                raise RuntimeError("yt-dlp вернул ненулевой код")
        except Exception as exc:
            err = str(exc).lower()
            if "отменено" in err:
                self.after(0, lambda: row.set_status("Отменено", Config.COLOR_MUTED))
            elif "sign in" in err or "bot" in err:
                self.after(0, lambda: row.set_status("Бот-блок! Нужен куки .txt", Config.COLOR_ERR))
            else:
                log.exception("Ошибка загрузки: %s", exc)
                self.after(0, lambda: row.set_status("Ошибка соединения", Config.COLOR_ERR))
        finally:
            self.after(0, lambda: (row.unlock(self.ffmpeg_ok), self._refresh_delete_states()))

    def _progress_hook(self, d: dict, row: DownloadRow) -> None:
        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            if total > 0:
                self.after(0, lambda: row.set_progress(downloaded / total))
            speed = d.get("_speed_str", "")
            if speed:
                self.after(0, lambda: row.set_status(speed, Config.COLOR_WHITE))
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
        directory = ctk.filedialog.askdirectory(initialdir=self.download_path)
        if directory:
            self.download_path = directory
            self._path_label.configure(text=f"Папка: {directory}")

    def _choose_cookies(self) -> None:
        path = ctk.filedialog.askopenfilename(
            title="Выберите файл cookies.txt",
            filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")],
        )
        if path:
            self._ydl.cookies_path = path
            # Найти кнопку куки и обновить текст
            for widget in self.winfo_children():
                if isinstance(widget, ctk.CTkFrame):
                    for child in widget.winfo_children():
                        if isinstance(child, ctk.CTkButton) and "Куки" in str(child.cget("text")):
                            child.configure(text="Куки: Активны", text_color=Config.COLOR_OK)

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