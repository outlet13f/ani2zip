import itertools
import json
import os
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk, messagebox, filedialog
from tkinter.scrolledtext import ScrolledText

from downloader import (CONCURRENT_CHOICES, CONCURRENT_FRAGMENTS, QUALITY_CHOICES, DownloadCancelled, analyze_url,
                        download_media)

# 대기열 항목 상태
PENDING, RUNNING, DONE, FAILED, STOPPED = "대기", "진행 중", "완료", "실패", "중지됨"
_STATUS_TAG = {PENDING: "pending", RUNNING: "running", DONE: "done", FAILED: "failed", STOPPED: "stopped"}
_item_ids = itertools.count(1)

# 프로그램 폴더에 보관하는 파일: 사용자 설정(저장 폴더 등) / 대기열 (프로그램을 껐다 켜도 유지)
APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")
QUEUE_PATH = os.path.join(APP_DIR, "queue.json")


def _load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):  # 파일 없음 / 손상된 JSON
        return default


def _save_json(path: str, data):
    # 임시 파일에 쓴 뒤 교체: 저장 도중 프로그램이 꺼져도 기존 파일이 깨지지 않음
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        pass


def load_settings() -> dict:
    return _load_json(SETTINGS_PATH, {})


def save_settings(settings: dict):
    _save_json(SETTINGS_PATH, settings)


def quality_label(height: int) -> str:
    return f"{height}p" if height else "최고 화질"


@dataclass
class QueueItem:
    url: str
    output_dir: str
    audio: bool
    fallback: bool
    subtitles: bool
    title_folder: bool = True
    quality: int = 0  # 최대 화질(p), 0 = 최고 화질
    status: str = PENDING
    id: int = field(default_factory=lambda: next(_item_ids))

    @property
    def option_text(self) -> str:
        video = f"{self.quality}p" if self.quality else "비디오"
        parts = ["MP3"] if self.audio else [video] + (["자막"] if self.subtitles else [])
        if self.title_folder:
            parts.append("폴더")
        return " · ".join(parts)


class DownloaderApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("EpiGrab")
        self.root.geometry("760x760")
        self.root.minsize(640, 640)

        # 상태 변수
        self.is_downloading = False  # 대기열 다운로드 또는 분석 작업 진행 여부
        self.default_download_dir = os.path.abspath("downloads")
        os.makedirs(self.default_download_dir, exist_ok=True)

        # 마지막으로 사용한 저장 폴더 복원 (폴더가 사라졌으면 기본 폴더 사용)
        self.settings = load_settings()
        saved_dir = self.settings.get("output_dir")
        self.initial_download_dir = (
            saved_dir if isinstance(saved_dir, str) and os.path.isdir(saved_dir) else self.default_download_dir
        )
        # 동시 조각 수 (작업 스레드는 Tk 변수 대신 이 값을 읽음 -> 바꾸면 다음 회차부터 바로 적용)
        saved_n = self.settings.get("concurrent_fragments")
        self.concurrent_fragments = saved_n if saved_n in CONCURRENT_CHOICES else CONCURRENT_FRAGMENTS
        # 마지막으로 고른 화질 (대기열 추가 시 항목에 저장됨)
        saved_q = self.settings.get("quality")
        self.quality = saved_q if saved_q in QUALITY_CHOICES else 0

        # 대기열 (작업 스레드와 공유하므로 queue_lock 으로 보호)
        self.queue: list[QueueItem] = []
        self.queue_lock = threading.Lock()
        self.cancel_event = threading.Event()
        self._current: QueueItem | None = None
        self._page_info = ""      # 회차 목록 처리 중일 때 "3/14"
        self._last_percent = -1

        self._apply_style()
        self._build_ui()
        self._restore_queue()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _apply_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")

        # 전반적인 폰트 및 패딩 설정
        self.root.option_add("*Font", ("Malgun Gothic", 10))
        style.configure("TLabel", font=("Malgun Gothic", 10))
        style.configure("TButton", font=("Malgun Gothic", 10), padding=5)
        style.configure("Action.TButton", font=("Malgun Gothic", 10, "bold"), padding=6)
        style.configure("Header.TLabel", font=("Malgun Gothic", 12, "bold"))
        style.configure("Status.TLabel", font=("Malgun Gothic", 9), foreground="#555555")
        style.configure("Treeview", font=("Malgun Gothic", 9), rowheight=22)
        style.configure("Treeview.Heading", font=("Malgun Gothic", 9, "bold"))
        # clam 기본 진행률 색은 배경과 거의 같아 구분이 어려우므로 파란색으로 지정
        style.configure("Horizontal.TProgressbar", background="#3b82c4")

    def _build_ui(self):
        main_frame = ttk.Frame(self.root, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 1. URL 입력 / 저장 위치 섹션
        input_frame = ttk.Frame(main_frame)
        input_frame.pack(fill=tk.X, pady=(0, 10))
        input_frame.columnconfigure(1, weight=1)

        ttk.Label(input_frame, text="URL", style="Header.TLabel").grid(row=0, column=0, sticky=tk.W, padx=(0, 8))
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(input_frame, textvariable=self.url_var, font=("Consolas", 10))
        self.url_entry.grid(row=0, column=1, sticky=tk.EW, padx=(0, 6))
        self.url_entry.bind("<Return>", self.add_to_queue)
        ttk.Button(input_frame, text="붙여넣기", command=self.paste_url).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(input_frame, text="대기열 추가", command=self.add_to_queue).grid(row=0, column=3, sticky=tk.EW)

        ttk.Label(input_frame, text="저장 폴더", style="Header.TLabel").grid(
            row=1, column=0, sticky=tk.W, padx=(0, 8), pady=(8, 0))
        self.folder_var = tk.StringVar(value=self.initial_download_dir)
        self.folder_entry = ttk.Entry(input_frame, textvariable=self.folder_var, font=("Malgun Gothic", 9))
        self.folder_entry.grid(row=1, column=1, sticky=tk.EW, padx=(0, 6), pady=(8, 0))
        ttk.Button(input_frame, text="폴더 열기", command=self.open_output_folder).grid(
            row=1, column=2, padx=(0, 4), pady=(8, 0))
        ttk.Button(input_frame, text="찾아보기...", command=self.browse_folder).grid(
            row=1, column=3, sticky=tk.EW, pady=(8, 0))

        # 2. 다운로드 옵션 섹션 (대기열에 추가하는 시점의 옵션이 항목별로 저장됨)
        options_frame = ttk.LabelFrame(main_frame, text=" 다운로드 옵션 (대기열 추가 시 적용) ", padding="10")
        options_frame.pack(fill=tk.X, pady=(0, 10))

        format_frame = ttk.Frame(options_frame)
        format_frame.pack(fill=tk.X)

        # 동시 조각 수: 대기열 항목별이 아니라 전체 설정 (진행 중에도 다음 회차부터 적용)
        self.concurrency_var = tk.StringVar(value=str(self.concurrent_fragments))
        concurrency_box = ttk.Combobox(format_frame, textvariable=self.concurrency_var, width=4, state="readonly",
                                       values=[str(n) for n in CONCURRENT_CHOICES])
        concurrency_box.pack(side=tk.RIGHT)
        concurrency_box.bind("<<ComboboxSelected>>", self._on_concurrency_changed)
        ttk.Label(format_frame, text="동시 조각 수 (느릴 때 ↑)").pack(side=tk.RIGHT, padx=(0, 6))

        self.format_mode = tk.StringVar(value="video")
        ttk.Radiobutton(
            format_frame,
            text="비디오",
            variable=self.format_mode,
            value="video",
            command=self._on_format_mode_changed,
        ).pack(side=tk.LEFT, padx=(0, 4))
        # 화질: 고른 화질 이하 중 가장 높은 것 (없으면 가장 가까운 화질)
        self.quality_var = tk.StringVar(value=quality_label(self.quality))
        self.quality_box = ttk.Combobox(format_frame, textvariable=self.quality_var, width=8, state="readonly",
                                        values=[quality_label(h) for h in QUALITY_CHOICES])
        self.quality_box.pack(side=tk.LEFT, padx=(0, 20))
        self.quality_box.bind("<<ComboboxSelected>>", self._on_quality_changed)
        ttk.Radiobutton(
            format_frame,
            text="오디오 전용 (MP3 음원 추출)",
            variable=self.format_mode,
            value="audio",
            command=self._on_format_mode_changed,
        ).pack(side=tk.LEFT)

        ttk.Separator(options_frame).pack(fill=tk.X, pady=6)

        self.fallback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="미지원 사이트 테스트 모드 (yt-dlp 실패 시 페이지에서 영상 링크 직접 탐색)",
            variable=self.fallback_var,
        ).pack(anchor=tk.W, pady=2)

        self.subtitle_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="자막 함께 받기 (별도 자막 파일이 있으면 영상과 같은 이름으로 저장)",
            variable=self.subtitle_var,
        ).pack(anchor=tk.W, pady=2)

        self.title_folder_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options_frame,
            text="제목 폴더 만들어 저장 (예: 저장 폴더\\작품 제목\\작품 제목 1화.mp4)",
            variable=self.title_folder_var,
        ).pack(anchor=tk.W, pady=2)

        # 3. 대기열 섹션
        self.queue_frame = ttk.LabelFrame(main_frame, text=" 대기열 (0개) ", padding="10")
        self.queue_frame.pack(fill=tk.X, pady=(0, 10))

        tree_frame = ttk.Frame(self.queue_frame)
        tree_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("status", "url", "option"), show="headings", height=6)
        self.tree.heading("status", text="상태")
        self.tree.heading("url", text="URL")
        self.tree.heading("option", text="옵션")
        self.tree.column("status", width=120, minwidth=100, stretch=False)
        self.tree.column("url", width=360, minwidth=200)
        self.tree.column("option", width=125, minwidth=90, stretch=False, anchor=tk.CENTER)
        self.tree.tag_configure("running", foreground="#0b5cad")
        self.tree.tag_configure("done", foreground="#1e7b34")
        self.tree.tag_configure("failed", foreground="#b42318")
        self.tree.tag_configure("stopped", foreground="#8a6d00")
        tree_scroll = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        tree_scroll.pack(side=tk.LEFT, fill=tk.Y)

        queue_btns = ttk.Frame(self.queue_frame)
        queue_btns.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 0))
        for text, cmd in (
            ("▲ 위로", lambda: self.move_selected(-1)),
            ("▼ 아래로", lambda: self.move_selected(1)),
            ("다시 시도", self.retry_selected),
            ("선택 삭제", self.remove_selected),
            ("완료 정리", self.clear_finished),
        ):
            ttk.Button(queue_btns, text=text, command=cmd, width=9, padding=2).pack(fill=tk.X, pady=(0, 3))

        # 4. 액션 버튼 & 진행 상태
        action_frame = ttk.Frame(main_frame)
        action_frame.pack(fill=tk.X, pady=(0, 5))

        self.download_btn = ttk.Button(
            action_frame,
            text="대기열 다운로드 시작",
            style="Action.TButton",
            command=self.start_queue,
        )
        self.download_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        self.analyze_btn = ttk.Button(
            action_frame,
            text="분석만 하기 (테스트)",
            command=self.start_analyze_thread,
        )
        self.analyze_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self.stop_btn = ttk.Button(action_frame, text="중지", command=self.stop_queue, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # 진행률 바 및 상태 텍스트
        self.progress_var = tk.DoubleVar(value=0.0)
        self.progress_bar = ttk.Progressbar(
            main_frame, variable=self.progress_var, maximum=100.0, mode="determinate"
        )
        self.progress_bar.pack(fill=tk.X, pady=(8, 2))

        self.status_label = ttk.Label(main_frame, text="대기 중...", style="Status.TLabel")
        self.status_label.pack(anchor=tk.W, pady=(0, 10))

        # 5. 콘솔 로그 창
        log_label = ttk.Label(main_frame, text="실행 로그:", style="Header.TLabel")
        log_label.pack(anchor=tk.W, pady=(0, 4))

        self.log_text = ScrolledText(
            main_frame,
            height=7,
            wrap=tk.WORD,
            font=("Consolas", 9),
            background="#f8f9fa",
            relief=tk.SOLID,
            borderwidth=1,
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------------
    # 입력 / 폴더
    # ------------------------------------------------------------------
    def paste_url(self):
        try:
            clipboard = self.root.clipboard_get().strip()
        except Exception:
            return
        urls = [t for t in clipboard.split() if t.startswith(("http://", "https://"))]
        if len(urls) > 1:
            # 여러 URL 을 한 번에 복사한 경우 모두 대기열에 추가
            added = sum(self._enqueue(u) for u in urls)
            self.append_log(f"[대기열] 클립보드의 URL {added}개를 추가했습니다.")
        else:
            self.url_var.set(clipboard)

    def _on_concurrency_changed(self, event=None):
        self.concurrent_fragments = int(self.concurrency_var.get())
        self.settings["concurrent_fragments"] = self.concurrent_fragments
        save_settings(self.settings)
        note = " (진행 중인 다운로드는 다음 회차부터 적용)" if self.is_downloading else ""
        self.append_log(f"[설정] 동시 조각 수: {self.concurrent_fragments}{note}")

    def _on_quality_changed(self, event=None):
        label = self.quality_var.get()
        self.quality = next((h for h in QUALITY_CHOICES if quality_label(h) == label), 0)
        self.settings["quality"] = self.quality
        save_settings(self.settings)

    def _on_format_mode_changed(self):
        # 오디오 전용(MP3)은 화질 선택이 의미 없으므로 비활성화
        self.quality_box.config(state="disabled" if self.format_mode.get() == "audio" else "readonly")

    def _remember_folder(self):
        """현재 저장 폴더를 settings.json 에 기록 (다음 실행 시 복원)."""
        folder = self.folder_var.get().strip()
        if folder and os.path.abspath(folder) != self.settings.get("output_dir"):
            self.settings["output_dir"] = os.path.abspath(folder)
            save_settings(self.settings)

    def browse_folder(self):
        selected = filedialog.askdirectory(initialdir=self.folder_var.get())
        if selected:
            self.folder_var.set(os.path.abspath(selected))
            self._remember_folder()

    def open_output_folder(self):
        folder = self.folder_var.get()
        if not os.path.exists(folder):
            os.makedirs(folder, exist_ok=True)
        self._remember_folder()
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.run(["open", folder])
        else:
            subprocess.run(["xdg-open", folder])

    # ------------------------------------------------------------------
    # UI 갱신 (작업 스레드에서도 호출 가능)
    # ------------------------------------------------------------------
    def _after(self, fn):
        """메인 스레드에서 fn 실행 예약. 창이 이미 닫혔으면 무시 (종료 직후 작업 스레드의 호출 대비)."""
        try:
            self.root.after(0, fn)
        except (RuntimeError, tk.TclError):
            pass

    def append_log(self, text: str):
        def _update():
            self.log_text.insert(tk.END, text + "\n")
            self.log_text.see(tk.END)
        self._after(_update)

    def set_status(self, text: str, progress: float = None):
        def _update():
            self.status_label.config(text=text)
            if progress is not None:
                self.progress_var.set(progress)
        self._after(_update)

    def _refresh_item(self, item: QueueItem, detail: str = ""):
        def _update():
            if self.tree.exists(str(item.id)):
                status = f"{item.status} {detail}".strip()
                self.tree.item(str(item.id), values=(status, item.url, item.option_text),
                               tags=(_STATUS_TAG[item.status],))
        self._after(_update)

    def _update_queue_title(self):
        with self.queue_lock:
            total = len(self.queue)
            pending = sum(it.status in (PENDING, STOPPED) for it in self.queue)
        self.queue_frame.config(text=f" 대기열 ({total}개 · 남은 항목 {pending}개) ")

    def _queue_changed(self):
        """대기열이 바뀔 때마다 제목을 갱신하고 queue.json 에 저장 (메인 스레드에서 호출)."""
        self._update_queue_title()
        self._save_queue()

    def _save_queue(self):
        with self.queue_lock:
            data = [{
                "url": it.url,
                "output_dir": it.output_dir,
                "audio": it.audio,
                "fallback": it.fallback,
                "subtitles": it.subtitles,
                "title_folder": it.title_folder,
                "quality": it.quality,
                # 받던 중에 저장(종료)되면 다음 실행 때 '중지됨'으로 보여 주고 시작 시 이어받기
                "status": STOPPED if it.status == RUNNING else it.status,
            } for it in self.queue]
        _save_json(QUEUE_PATH, data)

    def _restore_queue(self):
        """이전 실행에서 저장한 대기열 복원."""
        for d in _load_json(QUEUE_PATH, []):
            if not isinstance(d, dict) or not isinstance(d.get("url"), str) or not d["url"].strip():
                continue
            status = d.get("status") if d.get("status") in _STATUS_TAG else PENDING
            item = QueueItem(
                url=d["url"],
                output_dir=d["output_dir"] if isinstance(d.get("output_dir"), str) and d["output_dir"]
                else self.default_download_dir,
                audio=bool(d.get("audio", False)),
                fallback=bool(d.get("fallback", True)),
                subtitles=bool(d.get("subtitles", True)),
                title_folder=bool(d.get("title_folder", True)),
                quality=d.get("quality") if d.get("quality") in QUALITY_CHOICES else 0,
                status=STOPPED if status == RUNNING else status,
            )
            self.queue.append(item)
            self.tree.insert("", tk.END, iid=str(item.id), values=(item.status, item.url, item.option_text),
                             tags=(_STATUS_TAG[item.status],))
        if self.queue:
            self._update_queue_title()
            remaining = sum(it.status in (PENDING, STOPPED) for it in self.queue)
            self.append_log(f"[대기열] 이전 대기열 {len(self.queue)}개를 불러왔습니다. (남은 항목 {remaining}개 - "
                            "'대기열 다운로드 시작'을 누르면 이어서 받습니다)")

    def _progress_hook(self, d: dict):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed", 0)
            speed_str = f"{speed / (1024 * 1024):.2f} MB/s" if speed else "계산 중"

            percent = (downloaded / total * 100) if total > 0 else 0
            status_msg = f"다운로드 중: {percent:.1f}% ({downloaded/(1024*1024):.1f}MB / {total/(1024*1024):.1f}MB) - 속도: {speed_str}"
            self.set_status(status_msg, progress=percent)

            item = self._current
            if item and int(percent) != self._last_percent:
                self._last_percent = int(percent)
                self._refresh_item(item, " · ".join(filter(None, [self._page_info, f"{percent:.0f}%"])))
        elif d.get("status") == "finished":
            self.set_status("다운로드 완료. 파일 병합/후처리 중...", progress=100.0)
            self.append_log("[완료] 영상 다운로드 완료. 후처리를 진행합니다.")

    def _on_page(self, index: int, total: int, url: str):
        """회차 목록 페이지를 처리할 때 몇 번째 회차인지 대기열에 표시."""
        self._page_info = f"{index}/{total}"
        self._last_percent = -1
        if self._current:
            self._refresh_item(self._current, self._page_info)

    def _set_busy(self, busy: bool, queue_running: bool = False):
        self.is_downloading = busy
        state = tk.DISABLED if busy else tk.NORMAL
        if busy:
            text = "대기열 진행 중..." if queue_running else "분석 중..."
        else:
            text = "대기열 다운로드 시작"
        self.download_btn.config(state=state, text=text)
        self.analyze_btn.config(state=state)
        self.stop_btn.config(state=tk.NORMAL if busy and queue_running else tk.DISABLED)

    # ------------------------------------------------------------------
    # 대기열 관리
    # ------------------------------------------------------------------
    def _enqueue(self, url: str) -> bool:
        with self.queue_lock:
            if any(it.url == url and it.status in (PENDING, RUNNING, STOPPED) for it in self.queue):
                self.append_log(f"[대기열] 이미 대기 중인 URL 입니다: {url}")
                return False
            item = QueueItem(
                url=url,
                output_dir=self.folder_var.get().strip() or self.default_download_dir,
                audio=self.format_mode.get() == "audio",
                fallback=self.fallback_var.get(),
                subtitles=self.subtitle_var.get(),
                title_folder=self.title_folder_var.get(),
                quality=self.quality,
            )
            self.queue.append(item)
        self.tree.insert("", tk.END, iid=str(item.id), values=(item.status, item.url, item.option_text),
                         tags=(_STATUS_TAG[item.status],))
        self._queue_changed()
        self._remember_folder()
        return True

    def add_to_queue(self, event=None):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("경고", "대기열에 추가할 URL을 입력해 주세요.")
            return
        if self._enqueue(url):
            self.append_log(f"[대기열] 추가: {url}")
        # 중복으로 거부된 경우도 비워서 '시작' 시 다시 추가되지 않도록 함
        self.url_var.set("")

    def move_selected(self, delta: int):
        selected = set(self.tree.selection())
        if not selected:
            return
        with self.queue_lock:
            order = range(len(self.queue)) if delta < 0 else reversed(range(len(self.queue)))
            for i in list(order):
                j = i + delta
                if str(self.queue[i].id) in selected and 0 <= j < len(self.queue) \
                        and str(self.queue[j].id) not in selected:
                    self.queue[i], self.queue[j] = self.queue[j], self.queue[i]
                    self.tree.move(str(self.queue[j].id), "", j)
        self._save_queue()

    def retry_selected(self):
        """선택한 실패/완료 항목을 다시 대기 상태로 (다음 '시작' 때 다시 받음)."""
        selected = set(self.tree.selection())
        with self.queue_lock:
            for it in self.queue:
                if str(it.id) in selected and it.status in (FAILED, DONE):
                    it.status = PENDING
                    self._refresh_item(it)
        self._queue_changed()

    def remove_selected(self):
        selected = set(self.tree.selection())
        with self.queue_lock:
            removable = [it for it in self.queue if str(it.id) in selected and it.status != RUNNING]
            for it in removable:
                self.queue.remove(it)
                self.tree.delete(str(it.id))
        self._queue_changed()

    def clear_finished(self):
        with self.queue_lock:
            for it in [it for it in self.queue if it.status == DONE]:
                self.queue.remove(it)
                self.tree.delete(str(it.id))
        self._queue_changed()

    # ------------------------------------------------------------------
    # 대기열 실행
    # ------------------------------------------------------------------
    def start_queue(self):
        if self.is_downloading:
            return

        # 입력창에 URL 이 남아 있으면 먼저 대기열에 추가
        if self.url_var.get().strip():
            self.add_to_queue()

        with self.queue_lock:
            for it in self.queue:
                if it.status == STOPPED:  # 중지했던 항목은 다시 대기로 (이어받기)
                    it.status = PENDING
                    self._refresh_item(it)
            has_pending = any(it.status == PENDING for it in self.queue)
        if not has_pending:
            messagebox.showwarning("경고", "대기열에 받을 항목이 없습니다.\nURL을 입력하고 '대기열 추가'를 눌러 주세요.")
            return

        self.cancel_event.clear()
        self._set_busy(True, queue_running=True)
        self.progress_var.set(0.0)
        self._queue_changed()
        threading.Thread(target=self.run_queue, daemon=True).start()

    def stop_queue(self):
        if not self.is_downloading:
            return
        self.cancel_event.set()
        self.stop_btn.config(state=tk.DISABLED)
        self.set_status("중지하는 중... (진행 중인 요청이 끝나면 멈춥니다)")

    def run_queue(self):
        while True:
            with self.queue_lock:
                item = next((it for it in self.queue if it.status == PENDING), None)
                if item is None or self.cancel_event.is_set():
                    break
                item.status = RUNNING
            self._current, self._page_info, self._last_percent = item, "", -1
            self._refresh_item(item)
            self._after(self._queue_changed)

            os.makedirs(item.output_dir, exist_ok=True)
            self.append_log(f"--- [대기열] 다운로드 시작: {item.url} (저장: {item.output_dir}) ---")
            self.set_status("다운로드 준비 중...", progress=0.0)
            try:
                ok = download_media(
                    item.url,
                    output_dir=item.output_dir,
                    extract_audio=item.audio,
                    log=self.append_log,
                    progress_hooks=[self._progress_hook],
                    fallback=item.fallback,
                    subtitles=item.subtitles,
                    title_folder=item.title_folder,
                    concurrent_fragments=lambda: self.concurrent_fragments,
                    max_height=item.quality,
                    cancel=self.cancel_event,
                    on_page=self._on_page,
                )
                item.status = DONE if ok else FAILED
            except DownloadCancelled:
                item.status = STOPPED
                self.append_log("[중지] 다운로드를 중지했습니다. 다시 시작하면 이어서 받습니다.")
            except Exception as e:
                item.status = FAILED
                self.append_log(f"[오류] {e}")

            if item.status == DONE:
                self.append_log("[성공] 다운로드가 정상적으로 완료되었습니다.")
            self._current = None
            self._refresh_item(item, self._page_info if item.status == STOPPED else "")
            self._after(self._queue_changed)  # 항목이 끝날 때마다 상태 저장

        self._after(lambda: self._queue_finished(self.cancel_event.is_set()))

    def _queue_finished(self, stopped: bool):
        self._set_busy(False)
        self._queue_changed()
        with self.queue_lock:
            done = sum(it.status == DONE for it in self.queue)
            failed = sum(it.status == FAILED for it in self.queue)
        summary = f"완료 {done}개, 실패 {failed}개"
        if stopped:
            self.set_status(f"중지됨 - {summary}")
            return
        self.set_status(f"대기열 처리 끝 - {summary}", progress=100.0)
        if failed:
            messagebox.showwarning("대기열 완료", f"대기열 처리가 끝났습니다.\n{summary}\n실패 항목은 로그를 확인하세요.")
        else:
            messagebox.showinfo("대기열 완료", f"대기열의 모든 항목을 받았습니다.\n{summary}")

    # ------------------------------------------------------------------
    # 분석
    # ------------------------------------------------------------------
    def start_analyze_thread(self):
        if self.is_downloading:
            return

        # 입력창이 비어 있으면 대기열에서 선택한 항목을 분석
        url = self.url_var.get().strip()
        if not url and self.tree.selection():
            url = self.tree.set(self.tree.selection()[0], "url")
        if not url:
            messagebox.showwarning("경고", "분석할 URL을 입력하거나 대기열에서 항목을 선택해 주세요.")
            return

        self._set_busy(True)
        self.progress_var.set(0.0)
        self.set_status("분석 중... (다운로드하지 않습니다)")
        self.append_log(f"--- 분석 시작: {url} ---")

        threading.Thread(target=self.run_analyze, args=(url,), daemon=True).start()

    def run_analyze(self, url: str):
        try:
            analyze_url(url, log=self.append_log)
            self.set_status("분석 완료. 실행 로그를 확인하세요.")
        except Exception as e:
            self.set_status(f"오류 발생: {e}")
            self.append_log(f"[오류] {e}")

        self._after(lambda: self._set_busy(False))

    def on_close(self):
        if self.is_downloading and not messagebox.askyesno(
                "종료 확인", "작업이 진행 중입니다. 종료할까요?\n(받던 파일은 다음에 다시 받으면 이어받습니다)"):
            return
        self._remember_folder()
        self._save_queue()
        self.cancel_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    app = DownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
