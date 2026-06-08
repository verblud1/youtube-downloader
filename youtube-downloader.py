import os
import shutil
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
import threading
import customtkinter as ctk
from yt_dlp import YoutubeDL

ctk.set_appearance_mode("Dark")


class PlaylistWindow(ctk.CTkToplevel):
    """Модальное окно для выбора видеороликов из найденного плейлиста."""
    def __init__(self, parent, entries, callback):
        super().__init__(parent)
        
        self.title("Выбор треков из плейлиста")
        self.geometry("550x450")
        self.resizable(False, False)
        self.configure(fg_color="#000000")
        
        # Делаем окно модальным
        self.transient(parent)
        self.grab_set()
        
        self.entries = entries
        self.callback = callback
        self.checkbox_vars = []
        
        self.create_widgets()

    def create_widgets(self):
        # Заголовок окна
        title = ctk.CTkLabel(
            self, 
            text=f"Обнаружен плейлист ({len(self.entries)} видео)\nОтметьте файлы, которые хотите скачать:", 
            font=("Arial", 13, "bold"),
            text_color="#FFFFFF"
        )
        title.pack(pady=15)

        # Панель быстрых действий
        action_frame = ctk.CTkFrame(self, fg_color="transparent")
        action_frame.pack(fill="x", padx=25, pady=(0, 10))

        select_all_btn = ctk.CTkButton(
            action_frame, text="Выбрать все", width=100, height=24,
            fg_color="#111111", hover_color="#222222", text_color="#FFFFFF",
            border_color="#333333", border_width=1, command=self.select_all
        )
        select_all_btn.pack(side="left", padx=5)

        deselect_all_btn = ctk.CTkButton(
            action_frame, text="Снять все", width=100, height=24,
            fg_color="#111111", hover_color="#222222", text_color="#FFFFFF",
            border_color="#333333", border_width=1, command=self.deselect_all
        )
        deselect_all_btn.pack(side="left", padx=5)

        # Скролл-зона для чекбоксов
        self.scroll_frame = ctk.CTkScrollableFrame(
            self, width=480, height=260, fg_color="#050505", 
            border_color="#111111", border_width=1
        )
        self.scroll_frame.pack(padx=25, pady=5)

        # Заполнение списка видео
        for idx, entry in enumerate(self.entries):
            var = tk.BooleanVar(value=True)  # По умолчанию выбраны все
            self.checkbox_vars.append(var)
            
            # Ограничиваем длину названия для GUI
            raw_title = entry.get("title") or f"Видео #{idx + 1}"
            display_title = raw_title if len(raw_title) < 60 else raw_title[:57] + "..."
            
            cb = ctk.CTkCheckBox(
                self.scroll_frame, text=display_title, variable=var,
                font=("Arial", 11), checkbox_width=16, checkbox_height=16,
                fg_color="#27AE60", hover_color="#219653", text_color="#DDDDDD"
            )
            cb.pack(anchor="w", pady=4, padx=5)

        # Кнопка подтверждения
        confirm_btn = ctk.CTkButton(
            self, text="Добавить выбранные в очередь", font=("Arial", 13, "bold"),
            fg_color="#27AE60", hover_color="#219653", text_color="#FFFFFF",
            height=35, command=self.confirm_selection
        )
        confirm_btn.pack(pady=15)

    def select_all(self):
        for var in self.checkbox_vars:
            var.set(True)

    def deselect_all(self):
        for var in self.checkbox_vars:
            var.set(False)

    def confirm_selection(self):
        # Фильтруем ссылки, оставляя только выбранные пользователем
        selected_urls = []
        for idx, entry in enumerate(self.entries):
            if self.checkbox_vars[idx].get():
                url = entry.get("url") or entry.get("webpage_url")
                if url:
                    # Корректируем относительные ссылки, если yt-dlp отдал только ID
                    if not url.startswith("http"):
                        url = f"https://www.youtube.com/watch?v={url}"
                    selected_urls.append(url)
        
        # Передаем массив в главный поток через callback и закрываем окно
        self.callback(selected_urls)
        self.destroy()


class YoutubeDownloaderApp(ctk.CTk):

    def __init__(self):
        super().__init__()

        self.bg_color = "#000000"

        # Настройка главного окна
        self.title("Multi YouTube Downloader Ultra Pro")
        self.geometry("860x540")
        self.resizable(False, False)
        self.configure(fg_color=self.bg_color)

        self.download_path = os.path.join(os.path.expanduser("~"), "Downloads")
        self.manual_cookies_path = None
        
        self.executor = ThreadPoolExecutor(max_workers=3)
        self.download_rows = []

        self.create_widgets()
        self.create_context_menu()
        self.check_system_dependencies()

        self.add_download_row()
        self.after(200, self.check_clipboard_on_start)

    def create_widgets(self):
        self.title_label = ctk.CTkLabel(
            self, text="Мультипоточное скачивание видео, аудио и плейлистов",
            font=("Arial", 16, "bold"), text_color="#FFFFFF", fg_color=self.bg_color,
        )
        self.title_label.pack(pady=15)

        # Панель управления
        self.control_frame = ctk.CTkFrame(self, fg_color=self.bg_color)
        self.control_frame.pack(pady=5, fill="x", padx=30)

        self.path_label = ctk.CTkLabel(
            self.control_frame, text=f"Папка: {self.download_path}",
            font=("Arial", 11), text_color="#BBBBBB",
        )
        self.path_label.pack(side="left", padx=5)

        self.path_button = ctk.CTkButton(
            self.control_frame, text="Изменить папку", width=110, height=28,
            command=self.choose_path, fg_color="#111111", hover_color="#1A1A1A",
            text_color="#FFFFFF", border_color="#333333", border_width=1,
        )
        self.path_button.pack(side="right", padx=5)

        self.cookies_button = ctk.CTkButton(
            self.control_frame, text="Куки (.txt)", width=90, height=28,
            command=self.choose_cookies_file, fg_color="#111111", hover_color="#1A1A1A",
            text_color="#F39C12", border_color="#333333", border_width=1,
        )
        self.cookies_button.pack(side="right", padx=5)

        self.add_button = ctk.CTkButton(
            self.control_frame, text="+ Добавить ссылку", width=130, height=28,
            command=self.add_download_row, fg_color="#27AE60", hover_color="#219653",
            text_color="#FFFFFF",
        )
        self.add_button.pack(side="right", padx=5)

        # Панель системных зависимостей
        self.deps_frame = ctk.CTkFrame(self, fg_color=self.bg_color)
        self.deps_frame.pack(pady=5)

        self.ffmpeg_label = ctk.CTkLabel(
            self.deps_frame, text="FFmpeg: Проверка...", font=("Arial", 10, "bold"), text_color="#F39C12"
        )
        self.ffmpeg_label.pack(side="left", padx=15)

        self.nodejs_label = ctk.CTkLabel(
            self.deps_frame, text="Node.js: Проверка...", font=("Arial", 10, "bold"), text_color="#F39C12"
        )
        self.nodejs_label.pack(side="left", padx=15)

        # Главный прокручиваемый список ссылок
        self.scroll_frame = ctk.CTkScrollableFrame(
            self, width=800, height=240, fg_color="#050505", border_color="#111111", border_width=1
        )
        self.scroll_frame.pack(pady=10, padx=20)

        # Кнопка запуска
        self.download_button = ctk.CTkButton(
            self, text="Скачать все файлы", font=("Arial", 14, "bold"),
            command=self.start_all_downloads, fg_color="#111111", hover_color="#1A1A1A",
            text_color="#FFFFFF", border_color="#333333", border_width=1,
        )
        self.download_button.pack(pady=15)

    def add_download_row(self):
        row_frame = ctk.CTkFrame(self.scroll_frame, fg_color="transparent")
        row_frame.pack(fill="x", pady=5)

        entry_and_controls = ctk.CTkFrame(row_frame, fg_color="transparent")
        entry_and_controls.pack(fill="x")

        entry = ctk.CTkEntry(
            entry_and_controls, placeholder_text="Вставьте ссылку на видео или плейлист...",
            fg_color="#080808", border_color="#222222", text_color="#DDDDDD", width=420,
        )
        entry.pack(side="left", padx=5)

        entry.bind("<KeyRelease>", lambda event: self.on_link_changed(row_dict))
        entry.bind("<Button-3>", self.show_context_menu)
        entry.bind("<Button-2>", self.show_context_menu)

        quality_menu = ctk.CTkOptionMenu(
            entry_and_controls, values=["Максимальное"], width=130, height=28,
            fg_color="#111111", button_color="#222222", button_hover_color="#333333", state="disabled",
        )
        quality_menu.pack(side="left", padx=5)

        audio_only_var = tk.BooleanVar(value=False)
        audio_checkbox = ctk.CTkCheckBox(
            entry_and_controls, text="Только звук", variable=audio_only_var,
            font=("Arial", 11), width=90, checkbox_width=16, checkbox_height=16,
            border_width=1, fg_color="#333333", hover_color="#555555", text_color="#BBBBBB",
            command=lambda: self.toggle_audio_mode(row_dict),
        )
        audio_checkbox.pack(side="left", padx=5)

        delete_btn = ctk.CTkButton(
            entry_and_controls, text="✕", width=30, height=28, fg_color="#C0392B", hover_color="#A93226",
            command=lambda: self.remove_download_row(row_dict),
        )
        delete_btn.pack(side="right", padx=5)

        status_bar_frame = ctk.CTkFrame(row_frame, fg_color="transparent")
        status_bar_frame.pack(fill="x", pady=(2, 5))

        progress = ctk.CTkProgressBar(status_bar_frame, width=320, progress_color="#FFFFFF", fg_color="#0F0F0F")
        progress.set(0)
        progress.pack(side="left", padx=5)

        status = ctk.CTkLabel(status_bar_frame, text="Ожидание ссылки", font=("Arial", 10), text_color="#888888")
        status.pack(side="left", padx=10)

        row_dict = {
            "frame": row_frame,
            "entry": entry,
            "quality_menu": quality_menu,
            "audio_only_var": audio_only_var,
            "checkbox": audio_checkbox,
            "progress": progress,
            "status": status,
            "delete_btn": delete_btn,
            "last_url": "",
        }
        self.download_rows.append(row_dict)
        self.update_delete_buttons_state()

    def toggle_audio_mode(self, row_dict):
        if row_dict["audio_only_var"].get():
            row_dict["quality_menu"].configure(state="disabled")
        else:
            if len(row_dict["quality_menu"].cget("values")) > 1:
                row_dict["quality_menu"].configure(state="normal")

    def is_playlist(self, url):
        return "list=" in url.lower()

    def on_link_changed(self, row_dict):
        url = row_dict["entry"].get().strip()
        if url == row_dict["last_url"]:
            return

        if self.is_valid_youtube_link(url):
            row_dict["last_url"] = url
            
            # РАЗВЕТВЛЕНИЕ: Плейлист или одиночное Видео
            if self.is_playlist(url):
                row_dict["status"].configure(text="Обнаружен плейлист. Чтение структуры...", text_color="#F39C12")
                threading.Thread(target=self.fetch_playlist_entries, args=(url, row_dict)).start()
            else:
                row_dict["status"].configure(text="Чтение доступных разрешений...", text_color="#F39C12")
                threading.Thread(target=self.fetch_video_resolutions, args=(url, row_dict)).start()

    def fetch_playlist_entries(self, url, row_dict):
        """Фоновое извлечение плоского списка треков из плейлиста."""
        ydl_opts = {
            "extract_flat": True,  # Забирает структуру мгновенно без анализа медиа-потоков
            "skip_download": True,
            "nocheckcertificate": True,
            "quiet": True,
            "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
        }
        if self.manual_cookies_path and os.path.exists(self.manual_cookies_path):
            ydl_opts["cookiefile"] = self.manual_cookies_path

        try:
            with YoutubeDL(ydl_opts) as ydl:
                playlist_info = ydl.extract_info(url, download=False)
                entries = playlist_info.get("entries", [])

            if entries:
                # Открываем модальное окно в основном потоке GUI
                self.after(0, lambda: PlaylistWindow(
                    self, entries, lambda urls: self.handle_selected_playlist_videos(row_dict, urls)
                ))
            else:
                self.after(0, lambda: row_dict["status"].configure(text="Плейлист пуст или скрыт", text_color="#C0392B"))
        except Exception as e:
            self.after(0, lambda: row_dict["status"].configure(text="Ошибка разбора плейлиста", text_color="#C0392B"))
            print(f"Ошибка плейлиста: {e}")

    def handle_selected_playlist_videos(self, target_row, urls):
        """Интегрирует выбранные из плейлиста видео в рабочую область GUI."""
        if not urls:
            target_row["status"].configure(text="Отменено: видео не выбраны", text_color="#BBBBBB")
            return

        # Первое видео перезаписываем в текущую активную строку
        target_row["entry"].delete(0, "end")
        target_row["entry"].insert(0, urls[0])
        self.on_link_changed(target_row)

        # Все последующие видео генерируем как новые независимые строки
        for url in urls[1:]:
            self.add_download_row()
            new_row = self.download_rows[-1]
            new_row["entry"].insert(0, url)
            self.on_link_changed(new_row)

    def fetch_video_resolutions(self, url, row_dict):
        ydl_opts = {
            "nocheckcertificate": True, "quiet": True,
            "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
        }
        if self.manual_cookies_path and os.path.exists(self.manual_cookies_path):
            ydl_opts["cookiefile"] = self.manual_cookies_path

        try:
            with YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                formats = info.get("formats", [])

            resolutions = set()
            for f in formats:
                if f.get("vcodec") != "none" and f.get("height"):
                    resolutions.add(f.get("height"))

            sorted_res = sorted(list(resolutions), reverse=True)
            dropdown_values = ["Максимальное"] + [f"{r}p" for r in sorted_res]
            self.after(0, lambda: self.update_quality_menu(row_dict, dropdown_values))
        except Exception as e:
            self.after(0, lambda: row_dict["status"].configure(text="Защита от ботов / Нет формата", text_color="#C0392B"))

    def update_quality_menu(self, row_dict, values):
        row_dict["quality_menu"].configure(values=values)
        row_dict["quality_menu"].set(values[0])
        row_dict["status"].configure(text="Форматы успешно загружены", text_color="#27AE60")
        if not row_dict["audio_only_var"].get():
            row_dict["quality_menu"].configure(state="normal")

    def choose_cookies_file(self):
        file_path = ctk.filedialog.askopenfilename(
            title="Выберите файл cookies.txt", filetypes=[("Text Files", "*.txt"), ("All Files", "*.*")]
        )
        if file_path:
            self.manual_cookies_path = file_path
            self.cookies_button.configure(text="Куки: Активны", text_color="#27AE60")

    def start_all_downloads(self):
        self.download_button.configure(state="disabled")
        for row in self.download_rows:
            url = row["entry"].get().strip()
            if not url:
                row["status"].configure(text="Пропущено: пустая ссылка", text_color="#C0392B")
                continue

            row["status"].configure(text="В очереди...", text_color="#F39C12")
            row["entry"].configure(state="disabled")
            row["checkbox"].configure(state="disabled")
            row["quality_menu"].configure(state="disabled")
            row["delete_btn"].configure(state="disabled")

            self.executor.submit(self.download_video, url, row)
        self.after(1000, lambda: self.download_button.configure(state="normal"))

    def download_video(self, url, row):
        audio_only = row["audio_only_var"].get()
        selected_quality = row["quality_menu"].get()
        row["status"].configure(text="Анализ...", text_color="#F39C12")

        ydl_opts = {
            "outtmpl": os.path.join(self.download_path, "%(title)s.%(ext)s"),
            "progress_hooks": [lambda d: self.progress_hook(d, row)],
            "nocheckcertificate": True,
            "retries": 15, "fragment_retries": 15, "socket_timeout": 45, "ignoreerrors": True,
            "extractor_args": {"youtube": {"player_client": ["web_safari"]}},
        }
        if shutil.which("node"):
            ydl_opts["javascript_runtimes"] = ["node"]

        if self.manual_cookies_path and os.path.exists(self.manual_cookies_path):
            ydl_opts["cookiefile"] = self.manual_cookies_path
        else:
            try: ydl_opts["cookiesfrombrowser"] = ("firefox",)
            except Exception: ydl_opts.pop("cookiesfrombrowser", None)

        if audio_only:
            ydl_opts["format"] = "bestaudio/best"
            if self.ffmpeg_available:
                ydl_opts["postprocessors"] = [{
                    "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"
                }]
        else:
            if selected_quality == "Максимальное" or not self.ffmpeg_available:
                ydl_opts["format"] = "bestvideo+bestaudio/best" if self.ffmpeg_available else "best"
            else:
                height = selected_quality.replace("p", "")
                ydl_opts["format"] = f"bestvideo[height<={height}]+bestaudio/best"
            ydl_opts["merge_output_format"] = "mp4"

        try:
            with YoutubeDL(ydl_opts) as ydl:
                result = ydl.download([url])
                if result == 0:
                    row["status"].configure(text="Готово!", text_color="#27AE60")
                    row["progress"].set(1)
                else: raise Exception("Блокировка")
        except Exception as e:
            error_str = str(e).lower()
            if "sign in" in error_str or "bot" in error_str:
                row["status"].configure(text="Бот-блок! Нужен куки .txt", text_color="#C0392B")
            else:
                row["status"].configure(text="Ошибка соединения", text_color="#C0392B")
        finally:
            row["entry"].configure(state="normal")
            row["checkbox"].configure(state="normal")
            self.toggle_audio_mode(row)
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
            self.ffmpeg_label.configure(text="FFmpeg: НЕ НАЙДЕН (Ограничение 720p/Аудио)", text_color="#C0392B")
            self.ffmpeg_available = False
        if shutil.which("node"):
            self.nodejs_label.configure(text="Node.js: ОК", text_color="#27AE60")
        else:
            self.nodejs_label.configure(text="Node.js: НЕ НАЙДЕН", text_color="#C0392B")

    def create_context_menu(self):
        self.context_menu = tk.Menu(self, tearoff=0, background="#000000", foreground="#FFFFFF", activebackground="#222222", activeforeground="#FFFFFF")
        self.context_menu.add_command(label="Вставить", command=self.paste_from_clipboard)
        self.context_menu.add_command(label="Очистить", command=self.clear_entry)

    def show_context_menu(self, event):
        self.active_entry = event.widget
        self.active_entry.focus()
        self.context_menu.tk_popup(event.x_root, event.y_root)

    def is_valid_youtube_link(self, text):
        if not text or len(text) > 250 or "\n" in text or "\r" in text: return False
        return "youtube.com" in text.lower() or "youtu.be" in text.lower()

    def check_clipboard_on_start(self):
        try:
            clipboard_content = self.clipboard_get().strip()
            if self.is_valid_youtube_link(clipboard_content) and self.download_rows:
                self.download_rows[0]["entry"].insert(0, clipboard_content)
                self.on_link_changed(self.download_rows[0])
        except Exception: pass

    def paste_from_clipboard(self):
        try:
            text = self.clipboard_get().strip()
            if len(text) > 500: return
            if hasattr(self, "active_entry"):
                self.active_entry.delete(0, "end")
                self.active_entry.insert(0, text)
                for row in self.download_rows:
                    if row["entry"] == self.active_entry:
                        self.on_link_changed(row)
                        break
        except Exception: pass

    def clear_entry(self):
        if hasattr(self, "active_entry"): self.active_entry.delete(0, "end")

    def choose_path(self):
        directory = ctk.filedialog.askdirectory(initialdir=self.download_path)
        if directory:
            self.download_path = directory
            self.path_label.configure(text=f"Папка: {directory}")

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
                status_text = "Конвертация в MP3..." if audio_only else "Склейка FFmpeg..."
                row["status"].configure(text=status_text, text_color="#F39C12")
            else: row["status"].configure(text="Сохранение...", text_color="#F39C12")


if __name__ == "__main__":
    app = YoutubeDownloaderApp()
    app.mainloop()