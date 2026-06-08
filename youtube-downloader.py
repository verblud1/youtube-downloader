import os
import shutil
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
import customtkinter as ctk
from yt_dlp import YoutubeDL

ctk.set_appearance_mode("Dark")


class YoutubeDownloaderApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.bg_color = "#000000"

        # Настройка окна
        self.title("Multi YouTube Downloader & Extractor")
        self.geometry("700x520")  # Немного расширили окно под новые элементы
        self.resizable(False, False)
        self.configure(fg_color=self.bg_color)

        self.download_path = os.path.join(os.path.expanduser("~"), "Downloads")

        # Пул потоков для параллельного скачивания (макс. 3 одновременных задачи)
        self.executor = ThreadPoolExecutor(max_workers=3)

        # Список для хранения объектов (словарей) каждого поля ввода
        self.download_rows = []

        # Инициализация интерфейса
        self.create_widgets()
        self.create_context_menu()

        # Проверка зависимостей
        self.check_system_dependencies()

        # Добавляем первое поле по умолчанию
        self.add_download_row()

        # Автоподстановка из буфера в первое поле
        self.after(200, self.check_clipboard_on_start)

    def create_widgets(self):
        # Заголовок
        self.title_label = ctk.CTkLabel(
            self,
            text="Мультипоточное скачивание видео и аудио",
            font=("Arial", 16, "bold"),
            text_color="#FFFFFF",
            fg_color=self.bg_color,
        )
        self.title_label.pack(pady=15)

        # Панель управления (Выбор папки и кнопка "+")
        self.control_frame = ctk.CTkFrame(self, fg_color=self.bg_color)
        self.control_frame.pack(pady=5, fill="x", padx=40)

        self.path_label = ctk.CTkLabel(
            self.control_frame,
            text=f"Папка: {self.download_path}",
            font=("Arial", 11),
            text_color="#BBBBBB",
        )
        self.path_label.pack(side="left", padx=5)

        self.path_button = ctk.CTkButton(
            self.control_frame,
            text="Изменить папку",
            width=120,
            height=28,
            command=self.choose_path,
            fg_color="#111111",
            hover_color="#1A1A1A",
            text_color="#FFFFFF",
            border_color="#333333",
            border_width=1,
        )
        self.path_button.pack(side="right", padx=5)

        self.add_button = ctk.CTkButton(
            self.control_frame,
            text="+ Добавить ссылку",
            width=130,
            height=28,
            command=self.add_download_row,
            fg_color="#27AE60",
            hover_color="#219653",
            text_color="#FFFFFF",
        )
        self.add_button.pack(side="right", padx=10)

        # Системные зависимости (FFmpeg / Node.js)
        self.deps_frame = ctk.CTkFrame(self, fg_color=self.bg_color)
        self.deps_frame.pack(pady=5)

        self.ffmpeg_label = ctk.CTkLabel(
            self.deps_frame,
            text="FFmpeg: Проверка...",
            font=("Arial", 10, "bold"),
            text_color="#F39C12",
        )
        self.ffmpeg_label.pack(side="left", padx=15)

        self.nodejs_label = ctk.CTkLabel(
            self.deps_frame,
            text="Node.js: Проверка...",
            font=("Arial", 10, "bold"),
            text_color="#F39C12",
        )
        self.nodejs_label.pack(side="left", padx=15)

        # ПРОКРУЧИВАЕМЫЙ ФРЕЙМ ДЛЯ СПИСКА ССЫЛОК
        self.scroll_frame = ctk.CTkScrollableFrame(
            self,
            width=640,
            height=240,
            fg_color="#050505",
            border_color="#111111",
            border_width=1,
        )
        self.scroll_frame.pack(pady=10, padx=20)

        # Кнопка СКАЧАТЬ ВСЁ
        self.download_button = ctk.CTkButton(
            self,
            text="Скачать всё",
            font=("Arial", 14, "bold"),
            command=self.start_all_downloads,
            fg_color="#111111",
            hover_color="#1A1A1A",
            text_color="#FFFFFF",
            border_color="#333333",
            border_width=1,
        )
        self.download_button.pack(pady=15)

    def add_download_row(self):
        """Динамически добавляет новую строку (Поле + Галочка аудио + Прогресс + Удаление)."""
        row_frame = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
        row_frame.pack(fill="x", pady=5)

        # Верхняя линия: Поле ввода + Чекбокс + Кнопка удаления
        entry_and_controls = ctk.CTkFrame(row_frame, fg_color="transparent")
        entry_and_controls.pack(fill="x")

        entry = ctk.CTkEntry(
            entry_and_controls,
            placeholder_text="Вставьте ссылку на YouTube...",
            fg_color="#080808",
            border_color="#222222",
            text_color="#DDDDDD",
            width=430,
        )
        entry.pack(side="left", padx=(5, 5))

        # Привязываем контекстное меню к новому полю
        entry.bind("<Button-3>", self.show_context_menu)
        entry.bind("<Button-2>", self.show_context_menu)

        # ЧЕКБОКС: Скачать только звук
        audio_only_var = tk.BooleanVar(value=False)
        audio_checkbox = ctk.CTkCheckBox(
            entry_and_controls,
            text="Только звук",
            variable=audio_only_var,
            font=("Arial", 11),
            width=90,
            checkbox_width=16,
            checkbox_height=16,
            border_width=1,
            fg_color="#333333",
            hover_color="#555555",
            text_color="#BBBBBB",
        )
        audio_checkbox.pack(side="left", padx=5)

        # Кнопка удаления строки
        delete_btn = ctk.CTkButton(
            entry_and_controls,
            text="✕",
            width=30,
            height=28,
            fg_color="#C0392B",
            hover_color="#A93226",
            command=lambda: self.remove_download_row(row_dict),
        )
        delete_btn.pack(side="right", padx=5)

        # Нижняя линия: Индикатор прогресса и текстовый статус
        status_bar_frame = ctk.CTkFrame(row_frame, fg_color="transparent")
        status_bar_frame.pack(fill="x", pady=(2, 5))

        progress = ctk.CTkProgressBar(
            status_bar_frame, width=320, progress_color="#FFFFFF", fg_color="#0F0F0F"
        )
        progress.set(0)
        progress.pack(side="left", padx=5)

        status = ctk.CTkLabel(
            status_bar_frame,
            text="Ожидание ссылки",
            font=("Arial", 10),
            text_color="#888888",
        )
        status.pack(side="left", padx=10)

        # Сохраняем ссылки на виджеты строки в общий список
        row_dict = {
            "frame": row_frame,
            "entry": entry,
            "audio_only_var": audio_only_var,
            "checkbox": audio_checkbox,
            "progress": progress,
            "status": status,
            "delete_btn": delete_btn,
        }
        self.download_rows.append(row_dict)

        self.update_delete_buttons_state()

    def remove_download_row(self, row_dict):
        if len(self.download_rows) > 1:
            row_dict["frame"].destroy()
            self.download_rows.remove(row_dict)
            self.update_delete_buttons_state()

    def update_delete_buttons_state(self):
        state = "normal" if len(self.download_rows) > 1 else "disabled"
        for row in self.download_rows:
            row["delete_btn"].configure(state=state)

    def check_system_dependencies(self):
        if shutil.which("ffmpeg"):
            self.ffmpeg_label.configure(text="FFmpeg: ОК", text_color="#27AE60")
            self.ffmpeg_available = True
        else:
            self.ffmpeg_label.configure(
                text="FFmpeg: НЕ НАЙДЕН (Качество/Звук ограничены)",
                text_color="#C0392B",
            )
            self.ffmpeg_available = False

        if shutil.which("node"):
            self.nodejs_label.configure(text="Node.js: ОК", text_color="#27AE60")
        else:
            self.nodejs_label.configure(
                text="Node.js: НЕ НАЙДЕН (Возможны ошибки)", text_color="#C0392B"
            )

    def create_context_menu(self):
        self.context_menu = tk.Menu(
            self,
            tearoff=0,
            background="#000000",
            foreground="#FFFFFF",
            activebackground="#222222",
            activeforeground="#FFFFFF",
        )
        self.context_menu.add_command(
            label="Вставить", command=self.paste_from_clipboard
        )
        self.context_menu.add_command(label="Очистить", command=self.clear_entry)

    def show_context_menu(self, event):
        self.active_entry = event.widget
        self.active_entry.focus()
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def is_valid_youtube_link(self, text):
        if not text or len(text) > 250 or "\n" in text or "\r" in text:
            return False
        text_lower = text.lower()
        return "youtube.com" in text_lower or "youtu.be" in text_lower

    def check_clipboard_on_start(self):
        try:
            clipboard_content = self.clipboard_get().strip()
            if (
                self.is_valid_youtube_link(clipboard_content)
                and self.download_rows
            ):
                self.download_rows[0]["entry"].insert(0, clipboard_content)
                self.download_rows[0]["status"].configure(
                    text="Автоподстановка", text_color="#27AE60"
                )
        except Exception:
            pass

    def paste_from_clipboard(self):
        try:
            text = self.clipboard_get().strip()
            if len(text) > 500:
                return
            if hasattr(self, "active_entry"):
                self.active_entry.delete(0, "end")
                self.active_entry.insert(0, text)
        except Exception:
            pass

    def clear_entry(self):
        if hasattr(self, "active_entry"):
            self.active_entry.delete(0, "end")

    def choose_path(self):
        directory = ctk.filedialog.askdirectory(initialdir=self.download_path)
        if directory:
            self.download_path = directory
            self.path_label.configure(text=f"Папка: {directory}")

    def start_all_downloads(self):
        self.download_button.configure(state="disabled")

        for row in self.download_rows:
            url = row["entry"].get().strip()
            if not url:
                row["status"].configure(
                    text="Пропущено: пустая ссылка", text_color="#C0392B"
                )
                continue

            row["status"].configure(text="В очереди...", text_color="#F39C12")
            row["entry"].configure(state="disabled")
            row["checkbox"].configure(state="disabled")
            row["delete_btn"].configure(state="disabled")

            # Передаем управление в пул потоков
            self.executor.submit(self.download_video, url, row)

        self.after(1000, lambda: self.download_button.configure(state="normal"))

    def download_video(self, url, row):
        audio_only = row["audio_only_var"].get()
        row["status"].configure(text="Анализ...", text_color="#F39C12")

        # Базовые опции yt-dlp
        ydl_opts = {
            "outtmpl": os.path.join(self.download_path, "%(title)s.%(ext)s"),
            "progress_hooks": [lambda d: self.progress_hook(d, row)],
            "nocheckcertificate": True,
        }

        # МЕХАНИКА: Разделение логики Видео / Аудио
        if audio_only:
            # Скачиваем только аудиодорожку
            ydl_opts["format"] = "bestaudio/best"
            if self.ffmpeg_available:
                # Если FFmpeg есть, извлекаем аудио и конвертируем в чистый mp3
                ydl_opts["postprocessors"] = [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ]
        else:
            # Скачиваем полноценное видео
            ydl_opts["format"] = (
                "bestvideo+bestaudio/best" if self.ffmpeg_available else "best"
            )
            ydl_opts["merge_output_format"] = "mp4"

        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            row["status"].configure(text="Готово!", text_color="#27AE60")
            row["progress"].set(1)
        except Exception as e:
            row["status"].configure(text="Ошибка", text_color="#C0392B")
            print(f"Ошибка скачивания: {e}")
        finally:
            row["entry"].configure(state="normal")
            row["checkbox"].configure(state="normal")
            self.update_delete_buttons_state()

    def progress_hook(self, d, row):
        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)

            if total > 0:
                percent = downloaded / total
                row["progress"].set(percent)
                speed = d.get("_speed_str", "N/A")
                row["status"].configure(text=f"{speed}", text_color="#FFFFFF")
        elif d["status"] == "finished":
            audio_only = row["audio_only_var"].get()
            if self.ffmpeg_available:
                status_text = (
                    "Конвертация в MP3..."
                    if audio_only
                    else "Склейка FFmpeg..."
                )
                row["status"].configure(text=status_text, text_color="#F39C12")
            else:
                row["status"].configure(text="Сохранение...", text_color="#F39C12")


if __name__ == "__main__":
    app = YoutubeDownloaderApp()
    app.mainloop()