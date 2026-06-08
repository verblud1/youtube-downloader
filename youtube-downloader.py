import os
import threading
import tkinter as tk
import customtkinter as ctk
from yt_dlp import YoutubeDL

# Настройка темы оформления
ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class YoutubeDownloaderApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        # Настройка окна
        self.title("YouTube Downloader")
        self.geometry("600x340")
        self.resizable(False, False)

        # Путь сохранения по умолчанию — папка "Загрузки"
        self.download_path = os.path.join(os.path.expanduser("~"), "Downloads")

        # Инициализация интерфейса
        self.create_widgets()

        # Создаем контекстное меню для ПКМ
        self.create_context_menu()

        # МЕХАНИКА 1: Безопасная автоподстановка при старте окна
        self.after(200, self.check_clipboard_on_start)

    def create_widgets(self):
        # Заголовок
        self.title_label = ctk.CTkLabel(
            self,
            text="Скачивание видео в максимальном качестве",
            font=("Arial", 16, "bold"),
        )
        self.title_label.pack(pady=15)

        # Поле ввода URL
        # МЕХАНИКА 2: Ctrl + V / Cmd + V работает здесь автоматически на уровне ОС
        self.url_entry = ctk.CTkEntry(
            self,
            width=500,
            placeholder_text="Вставьте ссылку на YouTube видео здесь...",
        )
        self.url_entry.pack(pady=10)

        # МЕХАНИКА 3: Привязка клика ПКМ для вызова контекстного меню
        self.url_entry.bind("<Button-3>", self.show_context_menu)
        self.url_entry.bind(
            "<Button-2>", self.show_context_menu
        )  # Для пользователей Mac

        # Выбор папки для сохранения
        self.path_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.path_frame.pack(pady=5, fill="x", padx=50)

        self.path_label = ctk.CTkLabel(
            self.path_frame,
            text=f"Папка: {self.download_path}",
            font=("Arial", 11),
            text_color="gray",
        )
        self.path_label.pack(side="left", padx=5)

        self.path_button = ctk.CTkButton(
            self.path_frame,
            text="Изменить",
            width=80,
            height=24,
            command=self.choose_path,
        )
        self.path_button.pack(side="right", padx=5)

        # Индикатор прогресса
        self.progress_bar = ctk.CTkProgressBar(self, width=500)
        self.progress_bar.set(0)
        self.progress_bar.pack(pady=15)

        # Текстовый статус
        self.status_label = ctk.CTkLabel(
            self, text="Готов к работе", font=("Arial", 12), text_color="gray"
        )
        self.status_label.pack(pady=5)

        # Кнопка СКАЧАТЬ
        self.download_button = ctk.CTkButton(
            self,
            text="Скачать видео",
            font=("Arial", 14, "bold"),
            command=self.start_download_thread,
        )
        self.download_button.pack(pady=15)

    def create_context_menu(self):
        """Создает контекстное меню (tk.Menu)."""
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(
            label="Вставить", command=self.paste_from_clipboard
        )
        self.context_menu.add_command(label="Очистить", command=self.clear_entry)

    def show_context_menu(self, event):
        """Отображает контекстное меню в месте клика."""
        self.url_entry.focus()
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def is_valid_youtube_link(self, text):
        """Проверяет, является ли текст безопасным для вставки и похож ли он на ссылку."""
        if not text:
            return False

        # Защита от гигантского текста: ссылка не бывает длиннее 250 символов
        if len(text) > 250:
            return False

        # Защита от многострочного текста: ссылка всегда в одну строку
        if "\n" in text or "\r" in text:
            return False

        # Проверка на принадлежность к YouTube
        text_lower = text.lower()
        if "youtube.com" in text_lower or "youtu.be" in text_lower:
            return True

        return False

    def check_clipboard_on_start(self):
        """Безопасная автоподстановка при старте."""
        try:
            clipboard_content = self.clipboard_get().strip()

            if self.is_valid_youtube_link(clipboard_content):
                self.url_entry.insert(0, clipboard_content)
                self.status_label.configure(
                    text="Ссылка автоматически вставлена из буфера обмена",
                    text_color="green",
                )
        except Exception:
            pass

    def paste_from_clipboard(self):
        """Безопасная вставка через контекстное меню (ПКМ)."""
        try:
            text = self.clipboard_get().strip()

            # Защита от зависания при ручной вставке огромного текста
            if len(text) > 500:
                self.status_label.configure(
                    text="Ошибка: Слишком большой текст в буфере!",
                    text_color="red",
                )
                return

            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, text)
            self.status_label.configure(
                text="Вставлено из буфера", text_color="gray"
            )
        except Exception:
            pass

    def clear_entry(self):
        """Очистка поля ввода."""
        self.url_entry.delete(0, "end")

    def choose_path(self):
        directory = ctk.filedialog.askdirectory(initialdir=self.download_path)
        if directory:
            self.download_path = directory
            self.path_label.configure(text=f"Папка: {directory}")

    def start_download_thread(self):
        url = self.url_entry.get().strip()
        if not url:
            self.status_label.configure(
                text="Ошибка: Введите ссылку!", text_color="red"
            )
            return

        self.download_button.configure(state="disabled")
        self.status_label.configure(text="Анализ видео...", text_color="orange")

        download_thread = threading.Thread(target=self.download_video, args=(url,))
        download_thread.start()

    def download_video(self, url):
        ydl_opts = {
            "format": "bestvideo+bestaudio/best",
            "outtmpl": os.path.join(self.download_path, "%(title)s.%(ext)s"),
            "merge_output_format": "mp4",
            "progress_hooks": [self.progress_hook],
            "nocheckcertificate": True,
        }

        try:
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            self.status_label.configure(text="Успешно скачано!", text_color="green")
            self.progress_bar.set(1)
        except Exception as e:
            error_message = str(e)
            if "ffmpeg is not installed" in error_message:
                self.status_label.configure(
                    text="Ошибка: Установите FFmpeg в систему!", text_color="red"
                )
            else:
                self.status_label.configure(
                    text="Ошибка скачивания. См. консоль", text_color="red"
                )
            print(f"Ошибка при скачивании: {e}")
        finally:
            self.download_button.configure(state="normal")

    def progress_hook(self, d):
        if d["status"] == "downloading":
            downloaded = d.get("downloaded_bytes", 0)
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)

            if total > 0:
                percent = downloaded / total
                self.progress_bar.set(percent)
                speed = d.get("_speed_str", "N/A")
                self.status_label.configure(
                    text=f"Скачивание... Скорость: {speed}", text_color="white"
                )
        elif d["status"] == "finished":
            self.status_label.configure(
                text="Склеивание аудио и видео через FFmpeg...",
                text_color="orange",
            )


if __name__ == "__main__":
    app = YoutubeDownloaderApp()
    app.mainloop()