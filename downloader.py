import argparse
import gc
import html
import os
import re
import sys
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import yt_dlp
from yt_dlp.networking import Request
from yt_dlp.networking.exceptions import HTTPError, RequestError
from yt_dlp.utils import DownloadCancelled, sanitize_filename

# ---------------------------------------------------------------------------
# 미지원 사이트 테스트용: 페이지 HTML 에서 미디어 링크를 직접 탐색
# ---------------------------------------------------------------------------
MEDIA_EXTS = ("m3u8", "mpd", "mp4", "webm", "m4v", "mkv", "flv", "mov")
_EXT_PATTERN = "|".join(MEDIA_EXTS)
# 따옴표로 감싼 미디어 경로 (절대/상대 모두) - JS 플레이어 설정, JSON 등
_QUOTED_MEDIA_RE = re.compile(
    rf'''["']([^"'\s<>\\]+?\.(?:{_EXT_PATTERN})(?:\?[^"'\s<>\\]*)?)["']''', re.I)
# 따옴표 없이 노출된 절대 URL
_BARE_MEDIA_RE = re.compile(
    rf'''(?:https?:)?//[^\s"'<>\\]+?\.(?:{_EXT_PATTERN})(?:\?[^\s"'<>\\]*)?(?=[\s"'<>\\]|$)''', re.I)
_VIDEO_TAG_SRC_RE = re.compile(r'''<(?:video|source)\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']''', re.I)
_IFRAME_SRC_RE = re.compile(r'''<iframe\b[^>]*?\b(?:data-)?src\s*=\s*["']([^"']+)["']''', re.I)
_META_CHARSET_RE = re.compile(rb'''<meta[^>]+charset\s*=\s*["']?([\w-]+)''', re.I)
_OG_TITLE_RE = re.compile(r'''<meta[^>]+property\s*=\s*["']og:title["'][^>]*content\s*=\s*["']([^"']+)''', re.I)
_TITLE_RE = re.compile(r'<title[^>]*>([^<]+)</title>', re.I)
_A_HREF_RE = re.compile(r'''<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["']''', re.I)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')
# 자막 파일: 미디어 URL 과 같은 JSON 객체 안에 있거나 <video> 의 <track> 으로 지정된 것만 연결
_QUOTED_SUB_RE = re.compile(r'''["']([^"'\s<>\\]+?\.(?:vtt|srt|ass|ssa|smi)(?:\?[^"'\s<>\\]*)?)["']''', re.I)
_TRACK_SRC_RE = re.compile(r'''<track\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']''', re.I)

_KIND_PRIORITY = {"HLS": 0, "DASH": 1, "비디오 파일": 2}

# 동시에 받을 HLS/DASH 조각 수 기본값과 선택지.
# 사이트 서버는 연결 하나당 속도가 낮고(시간대에 따라 약 2~9MB/s) 조각마다 응답 대기가 있어
# 전체 속도는 대략 '동시 연결 수 x 연결당 속도'. 서버가 느릴 때는 늘리면 빨라지지만
# 너무 많으면 사이트에서 차단될 수 있고 CPU 사용도 늘어남.
CONCURRENT_FRAGMENTS = 4
CONCURRENT_CHOICES = (4, 8, 16, 32)

# 화질 선택지: 최대 해상도(p), 0 = 최고 화질
QUALITY_CHOICES = (0, 2160, 1440, 1080, 720, 480, 360)


@dataclass
class MediaCandidate:
    url: str
    kind: str      # HLS / DASH / 비디오 파일 / iframe
    referer: str   # 이 링크를 발견한 페이지 (요청 시 Referer 헤더로 사용)
    subtitles: list[str] = field(default_factory=list)  # 이 영상에 딸린 별도 자막 파일


@dataclass
class ProbeResult:
    title: str = ""
    candidates: list[MediaCandidate] = field(default_factory=list)
    child_pages: list[str] = field(default_factory=list)  # 목록 페이지의 하위 페이지 (예: 회차 목록)


class YtdlpLogger:
    """yt-dlp 내부 메시지를 log 콜백(print, GUI 로그창 등)으로 전달합니다."""

    def __init__(self, log, verbose: bool = True):
        # 콘솔에서 실행하면 yt-dlp 가 ANSI 색상 코드를 붙이므로 제거 후 전달
        self.log = lambda msg: log(_ANSI_RE.sub("", msg))
        self.verbose = verbose

    def debug(self, msg: str):
        # yt-dlp 는 일반 메시지도 debug 로 넘기며, 진짜 디버그 메시지는 '[debug] ' 접두사가 붙습니다.
        if self.verbose and not msg.startswith("[debug] "):
            self.log(msg)

    def info(self, msg: str):
        if self.verbose:
            self.log(msg)

    def warning(self, msg: str):
        if self.verbose:
            self.log(f"[경고] {msg}")

    def error(self, msg: str):
        self.log(msg)


def _media_kind(url: str) -> str:
    path = url.split("?", 1)[0].lower()
    if path.endswith(".m3u8"):
        return "HLS"
    if path.endswith(".mpd"):
        return "DASH"
    return "비디오 파일"


def _fetch_html(ydl: yt_dlp.YoutubeDL, url: str, referer: str | None, log) -> str | None:
    headers = {"Referer": referer} if referer else {}
    try:
        with ydl.urlopen(Request(url, headers=headers)) as res:
            raw = res.read()
            charset = res.headers.get_content_charset()
    except HTTPError as e:
        hint = " (Cloudflare 등 봇 차단 또는 로그인이 필요한 페이지일 수 있습니다)" if e.status in (401, 403, 503) else ""
        log(f"[탐색 실패] {url} - HTTP {e.status}{hint}")
        return None
    except RequestError as e:
        log(f"[탐색 실패] {url} - {e}")
        return None

    if not charset:
        m = _META_CHARSET_RE.search(raw[:4096])
        charset = m.group(1).decode("ascii") if m else "utf-8"
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _page_title(page: str) -> str:
    m = _OG_TITLE_RE.search(page) or _TITLE_RE.search(page)
    return html.unescape(m.group(1)).strip() if m else ""


_SITE_SUFFIX_RE = re.compile(r'\s+[|｜]\s*[^|｜]{1,30}$')
# 공백으로 구분된 회차 표기만 제거 ('부제 1화' 의 '제' 가 '제1화' 로 오인되지 않도록 앞에 공백 필수)
_EPISODE_SUFFIX_RE = re.compile(
    r'(?:^|\s+)(?:(?:제\s*)?\d+(?:\.\d+)?\s*(?:화|회|話)|(?:EP|Ep|ep|E|Episode|episode|#)\s*\.?\s*\d+)\s*$')


def clean_title(title: str) -> str:
    """페이지 제목 끝의 사이트 이름을 제거합니다. (예: '작품 1화 | 사이트' -> '작품 1화')"""
    return _SITE_SUFFIX_RE.sub("", title or "").strip()


def series_title(title: str) -> str:
    """회차 표기까지 제거한 작품 제목 (예: '작품 1화 | 사이트' -> '작품'). 폴더 이름에 사용."""
    cleaned = clean_title(title)
    return _EPISODE_SUFFIX_RE.sub("", cleaned).strip() or cleaned


def _literal(name: str) -> str:
    """제목을 파일/폴더 이름으로 쓸 수 있게 정리하고 yt-dlp 출력 템플릿용으로 % 를 이스케이프."""
    return sanitize_filename(name).replace("%", "%%")


def _enclosing_object(text: str, start: int, end: int, limit: int = 2000) -> str:
    """미디어 URL 이 JSON/JS 객체({ ... }) 안에 있으면 그 객체 범위의 텍스트를 반환합니다."""
    left = text.rfind("{", max(0, start - limit), start)
    right = text.find("}", end, end + limit)
    if left == -1 or right == -1 or "}" in text[left:start]:
        return ""
    return text[left:right]


def _scan_html(page: str, base_url: str) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """HTML/JS 에서 ([(미디어 URL, 자막 URL 목록)], iframe URL 목록)을 찾아 반환합니다."""
    # JS 문자열/JSON 에 이스케이프된 문자 복원 (Next.js 등은 페이지 데이터를 \"...\" 형태로 넣음)
    text = page.replace("\\/", "/").replace('\\"', '"').replace("\\u0026", "&")

    def normalize(u: str) -> str | None:
        u = html.unescape(u.strip())
        if not u or u.startswith(("about:", "javascript:", "data:", "blob:")):
            return None
        u = urljoin(base_url, u)
        return u if u.startswith(("http://", "https://")) else None

    def unique(items) -> list[str]:
        out = []
        for u in map(normalize, items):
            if u and u not in out:
                out.append(u)
        return out

    subs_of: dict[str, list[str]] = {}  # 미디어 URL -> 자막 URL 목록 (발견 순서 유지)

    def add_media(raw: str, subs):
        u = normalize(raw)
        if not u:
            return
        found = subs_of.setdefault(u, [])
        for sub in unique(subs):
            if sub not in found:
                found.append(sub)

    track_subs = _TRACK_SRC_RE.findall(text)
    for raw in _VIDEO_TAG_SRC_RE.findall(text):
        add_media(raw, track_subs)
    for regex in (_QUOTED_MEDIA_RE, _BARE_MEDIA_RE):
        for m in regex.finditer(text):
            obj = _enclosing_object(text, m.start(), m.end())
            add_media(m.group(1) if regex.groups else m.group(0), _QUOTED_SUB_RE.findall(obj))

    media = sorted(subs_of.items(), key=lambda item: _KIND_PRIORITY[_media_kind(item[0])])
    iframes = unique(_IFRAME_SRC_RE.findall(text))
    return media, iframes


def _natural_key(url: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", url)]


def _child_pages(page: str, base_url: str) -> list[str]:
    """
    현재 경로 아래의 같은 사이트 링크(예: /anime/11030 -> /anime/11030/1, /anime/11030/2 ...)를
    회차 목록으로 보고 번호 순으로 정렬해 반환합니다.
    """
    base = urlsplit(base_url)
    base_path = base.path.rstrip("/")
    if not base_path:  # 사이트 첫 페이지는 모든 링크가 하위 경로이므로 제외
        return []
    children = []
    for href in _A_HREF_RE.findall(page):
        u = urljoin(base_url, html.unescape(href)).split("#", 1)[0]
        parts = urlsplit(u)
        if parts.netloc == base.netloc and parts.path.startswith(base_path + "/") \
                and parts.path.rstrip("/") != base_path and u not in children:
            children.append(u)
    return sorted(children, key=_natural_key)


def probe_page(url: str, log=print, max_iframes: int = 5) -> ProbeResult:
    """
    yt-dlp 가 지원하지 않는 페이지의 HTML 을 직접 읽어 미디어 링크(m3u8/mp4 등)와
    iframe(임베드 플레이어)을 찾습니다. iframe 내부 페이지도 한 단계까지 탐색합니다.
    """
    result = ProbeResult()
    seen: set[str] = set()

    def add(cand_url: str, kind: str, referer: str, subtitles: list[str] | None = None):
        if cand_url not in seen:
            seen.add(cand_url)
            result.candidates.append(MediaCandidate(cand_url, kind, referer, subtitles or []))

    with yt_dlp.YoutubeDL({"logger": YtdlpLogger(log, verbose=False)}) as ydl:
        page = _fetch_html(ydl, url, None, log)
        if page is None:
            return result
        result.title = _page_title(page)

        media, iframes = _scan_html(page, url)
        for m, subs in media:
            add(m, _media_kind(m), url, subs)
        result.child_pages = _child_pages(page, url)

        for iframe_url in iframes[:max_iframes]:
            add(iframe_url, "iframe", url)
            sub_page = _fetch_html(ydl, iframe_url, url, log)
            if sub_page is None:
                continue
            sub_media, _ = _scan_html(sub_page, iframe_url)
            for m, subs in sub_media:
                add(m, _media_kind(m), iframe_url, subs)

    return result


def _log_candidates(probe: ProbeResult, log):
    log(f"[탐색 결과] 페이지 제목: {probe.title or '(없음)'} / 후보 {len(probe.candidates)}개")
    for i, cand in enumerate(probe.candidates, 1):
        subs = f" (+ 자막 파일 {len(cand.subtitles)}개)" if cand.subtitles else ""
        log(f"  {i}. [{cand.kind}] {cand.url}{subs}")


def _log_child_pages(pages: list[str], log, limit: int = 20):
    log(f"[목록 페이지] 영상 대신 하위 페이지 {len(pages)}개를 찾았습니다:")
    for u in pages[:limit]:
        log(f"  - {u}")
    if len(pages) > limit:
        log(f"  ... 외 {len(pages) - limit}개")


# ---------------------------------------------------------------------------
# yt-dlp 실행
# ---------------------------------------------------------------------------
def progress_hook(d: dict):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        downloaded = d.get('downloaded_bytes', 0)
        speed = d.get('speed', 0)
        speed_str = f"{speed / (1024 * 1024):.2f} MB/s" if speed else "N/A"
        if total > 0:
            percent = downloaded / total * 100
            print(f"\r진행률: {percent:.1f}% ({downloaded / (1024*1024):.1f}/{total / (1024*1024):.1f} MB) - 속도: {speed_str}", end="")
        else:
            print(f"\r다운로드 중: {downloaded / (1024*1024):.1f} MB - 속도: {speed_str}", end="")
    elif d['status'] == 'finished':
        print("\n다운로드 완료! 후처리(변환/병합)를 진행합니다...")


def create_ydl_opts(
    output_dir: str = "downloads",
    extract_audio: bool = False,
    progress_hooks: list | None = None,
    logger: YtdlpLogger | None = None,
    referer: str | None = None,
    title: str | None = None,
    subtitles: bool = False,
    subfolder: str | None = None,
    concurrent_fragments: int = CONCURRENT_FRAGMENTS,
    max_height: int = 0,
) -> dict:
    # 직접 링크(m3u8 등)는 파일명이 index/master 등이 되므로 페이지 제목을 사용
    name = _literal(title) if title else ""
    ydl_opts = {
        # subfolder: 저장 폴더 아래 하위 폴더 (yt-dlp 출력 템플릿 형식)
        'outtmpl': os.path.join(output_dir, subfolder or "", f"{name or '%(title)s'}.%(ext)s"),
        'progress_hooks': list(progress_hooks or []),
        'ignoreerrors': True,  # 재생목록 중 일부 영상 실패 시 계속 진행
        'noplaylist': False,   # 재생목록 URL일 경우 전체 목록 지원
        'noprogress': True,    # 진행률은 progress_hooks 에서 직접 표시
        # HLS/DASH 조각을 동시에 여러 개 받음. 조각마다 서버 응답 대기가 있어
        # 1개씩 받으면 대기 시간이 대부분을 차지함 (실측: 1개 약 4MB/s -> 4개 약 16MB/s)
        'concurrent_fragment_downloads': concurrent_fragments,
    }
    if logger:
        ydl_opts['logger'] = logger
    if referer:
        ydl_opts['http_headers'] = {'Referer': referer}

    if extract_audio:
        ydl_opts.update({
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
        })
    else:
        # 비디오 + 오디오 최고 화질 병합 (FFmpeg 필요), 불가능 시 단일 최고 화질 스트림 선택
        ydl_opts.update({
            'format': 'bestvideo+bestaudio/best',
        })
        if max_height:
            # 정렬 기준을 바꿔 선택: max_height 이하 중 가장 높은 화질, 이하가 없으면 그 위에서 가장 낮은 화질
            # (필터로 거르면 해당 화질이 없는 영상은 실패하므로 사용하지 않음)
            ydl_opts['format_sort'] = [f'res:{max_height}']
        if subtitles:
            # yt-dlp 지원 사이트: 사이트가 제공하는 자막(자동 생성 제외)을 영상 옆에 저장
            ydl_opts.update({
                'writesubtitles': True,
                'subtitleslangs': ['all', '-live_chat'],
            })

    return ydl_opts


@dataclass
class _Job:
    """한 번의 download_media 호출 동안 공유되는 설정."""
    output_dir: str
    extract_audio: bool
    subtitles: bool
    fallback: bool
    log: object
    progress_hooks: list
    cancel: object = None   # threading.Event - set() 되면 다운로드 중지
    on_page: object = None  # 회차 목록 처리 시 on_page(현재 번호, 전체 수, URL) 호출
    title_folder: bool = False  # 작품 제목으로 하위 폴더를 만들어 저장
    concurrent_fragments: object = CONCURRENT_FRAGMENTS  # int 또는 int 를 돌려주는 함수(실행 중 변경 반영)
    max_height: int = 0  # 최대 화질(p), 0 = 최고 화질

    def __post_init__(self):
        self.logger = YtdlpLogger(self.log)

    def check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise DownloadCancelled("사용자가 다운로드를 중지했습니다.")

    def opts(self, referer: str | None = None, title: str | None = None, folder: str | None = None) -> dict:
        subfolder = None
        if self.title_folder:
            # folder(작품 제목)를 모르면 yt-dlp 정보 사용: 재생목록이면 목록 제목, 아니면 영상 제목
            subfolder = _literal(folder) if folder else "%(playlist_title,title)s"
        n = self.concurrent_fragments() if callable(self.concurrent_fragments) else self.concurrent_fragments
        return create_ydl_opts(self.output_dir, self.extract_audio, self.progress_hooks, self.logger,
                               referer=referer, title=title, subtitles=self.subtitles, subfolder=subfolder,
                               concurrent_fragments=max(1, int(n)), max_height=self.max_height)


class _CancellableYDL(yt_dlp.YoutubeDL):
    """
    모든 네트워크 요청 직전에 중지 여부를 확인하는 YoutubeDL.
    조각을 동시에 받을 때 yt-dlp 는 남은 조각 작업이 모두 끝나야 멈추는데,
    중지 후에는 남은 조각이 요청을 보내지 않고 즉시 끝나도록 합니다.
    """

    def __init__(self, params, job: _Job):
        super().__init__(params)
        self._job = job

    def urlopen(self, req):
        self._job.check_cancel()
        return super().urlopen(req)


def _run_ytdlp(url: str, job: _Job, opts: dict) -> tuple[int, int, list[str]]:
    """yt-dlp 로 다운로드하고 (종료 코드, 다운로드 완료된 파일 수, 최종 파일 경로 목록)을 반환합니다."""
    finished = 0
    filepaths: list[str] = []

    def hook(d: dict):
        nonlocal finished
        job.check_cancel()  # 다운로드 도중 중지 요청 시 예외로 yt-dlp 를 빠져나옴
        if d.get('status') == 'finished':
            finished += 1

    opts = {**opts, 'progress_hooks': [*opts['progress_hooks'], hook], 'post_hooks': [filepaths.append]}
    job.check_cancel()
    cancelled = False
    try:
        with _CancellableYDL(opts, job) as ydl:
            retcode = ydl.download([url])
    except DownloadCancelled:
        cancelled = True
    if cancelled:
        # 중지로 중단된 yt-dlp 는 .part 파일을 닫지 않은 채 순환 참조로 남겨 둠.
        # 그대로 두면 같은 프로그램에서 다시 시작할 때 파일 이름 변경이 실패(WinError 32)하므로 정리 후 다시 알림
        gc.collect()
        raise DownloadCancelled("사용자가 다운로드를 중지했습니다.")
    return retcode, finished, filepaths


def _save_subtitles(cand: MediaCandidate, video_path: str, log):
    """영상과 같은 이름으로 자막 파일을 저장합니다 (팟플레이어/VLC 등이 자동으로 불러옴)."""
    base = os.path.splitext(video_path)[0]
    with yt_dlp.YoutubeDL({'logger': YtdlpLogger(log, verbose=False)}) as ydl:
        for i, sub_url in enumerate(cand.subtitles):
            ext = os.path.splitext(urlsplit(sub_url).path)[1].lower() or ".vtt"
            path = f"{base}{'' if i == 0 else f'.{i + 1}'}{ext}"
            try:
                with ydl.urlopen(Request(sub_url, headers={'Referer': cand.referer})) as res:
                    data = res.read()
            except (HTTPError, RequestError) as e:
                log(f"[자막 실패] {sub_url} - {e}")
                continue
            with open(path, "wb") as f:
                f.write(data)
            log(f"[자막 저장] {path}")


def _download_candidates(probe: ProbeResult, job: _Job, folder: str | None) -> bool:
    log = job.log
    folder = folder or series_title(probe.title) or None
    if job.title_folder and folder:
        log(f"[저장 폴더] {os.path.join(job.output_dir, sanitize_filename(folder))}")
    for i, cand in enumerate(probe.candidates, 1):
        log(f"[시도 {i}/{len(probe.candidates)}] [{cand.kind}] {cand.url}")
        opts = job.opts(referer=cand.referer, folder=folder,
                        title=None if cand.kind == "iframe" else clean_title(probe.title))
        retcode, finished, filepaths = _run_ytdlp(cand.url, job, opts)
        if retcode == 0:
            if job.subtitles and not job.extract_audio and cand.subtitles and filepaths:
                _save_subtitles(cand, filepaths[-1], log)
            return True
        if finished:
            log("[주의] 다운로드는 되었으나 일부 처리에 실패했습니다.")
            return False

    log("[실패] 탐색된 모든 후보로 다운로드하지 못했습니다.")
    return False


def _download_page(url: str, job: _Job, expand_children: bool, folder: str | None = None) -> bool:
    """folder: 회차 목록에서 내려온 경우 목록 페이지의 작품 제목 (모든 회차를 같은 폴더에 저장)."""
    log = job.log
    retcode, finished, _ = _run_ytdlp(url, job, job.opts(folder=folder))
    if retcode == 0:
        return True
    if finished:
        log("[주의] 일부 항목의 다운로드 또는 후처리에 실패했습니다.")
        return False
    if not job.fallback:
        return False

    log("[테스트 모드] yt-dlp 가 처리하지 못한 URL 입니다. 페이지에서 미디어 링크를 직접 탐색합니다...")
    probe = probe_page(url, log=log)
    _log_candidates(probe, log)
    if probe.candidates:
        return _download_candidates(probe, job, folder)

    if expand_children and probe.child_pages:
        pages = probe.child_pages
        list_folder = series_title(probe.title) or None
        _log_child_pages(pages, log)
        failed = []
        for i, page_url in enumerate(pages, 1):
            job.check_cancel()
            log(f"===== [{i}/{len(pages)}] {page_url} =====")
            if job.on_page:
                job.on_page(i, len(pages), page_url)
            if not _download_page(page_url, job, expand_children=False, folder=list_folder):
                failed.append(page_url)
        log(f"[목록 완료] {len(pages)}개 중 {len(pages) - len(failed)}개 성공")
        for u in failed:
            log(f"  실패: {u}")
        return not failed

    log("[실패] 미디어 후보를 찾지 못했습니다. (JS 로 동적 로딩되거나 난독화된 사이트일 수 있습니다)")
    return False


def download_media(
    url: str,
    output_dir: str = "downloads",
    extract_audio: bool = False,
    log=print,
    progress_hooks: list | None = None,
    fallback: bool = True,
    subtitles: bool = True,
    cancel=None,
    on_page=None,
    title_folder: bool = True,
    concurrent_fragments=CONCURRENT_FRAGMENTS,
    max_height: int = 0,
) -> bool:
    """
    지정된 URL의 미디어(단일 영상 또는 재생목록)를 다운로드합니다.
    yt-dlp 가 처리하지 못하는 URL 이면(fallback=True) 페이지에서 찾은 미디어 링크를 순서대로 시도하고,
    영상 없이 회차 목록만 있는 페이지면 각 회차 페이지를 순서대로 다운로드합니다.
    subtitles=True 면 별도 자막 파일이 있을 때 영상과 같은 이름으로 함께 저장합니다.
    cancel(threading.Event)이 set() 되면 DownloadCancelled 예외로 중단됩니다.
    title_folder=True 면 저장 폴더 아래에 작품 제목 폴더를 만들어 저장합니다.
    concurrent_fragments: 동시에 받을 조각 수 (함수를 주면 회차/후보마다 다시 읽어 실행 중 변경 반영).
    max_height: 최대 화질(예: 720 -> 720p 이하 중 최고). 0 이면 최고 화질. 해당 화질 이하가 없으면 가장 가까운 화질.
    """
    job = _Job(output_dir, extract_audio, subtitles, fallback, log, list(progress_hooks or []), cancel, on_page,
               title_folder, concurrent_fragments, max_height)
    return _download_page(url, job, expand_children=True)


# ---------------------------------------------------------------------------
# 분석 (다운로드 없이 확인)
# ---------------------------------------------------------------------------
def _extract_info(url: str, logger: YtdlpLogger, referer: str | None = None) -> dict | None:
    opts = {'logger': logger, 'extract_flat': 'in_playlist', 'noplaylist': False}
    if referer:
        opts['http_headers'] = {'Referer': referer}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        return None  # 원인은 logger 로 이미 출력됨


def _describe(info: dict) -> str:
    if info.get('_type') == 'playlist':
        return f"재생목록 '{info.get('title')}' - {len(list(info.get('entries') or []))}개 항목"
    formats = info.get('formats') or []
    heights = sorted({f['height'] for f in formats if f.get('height')}, reverse=True)
    return f"'{info.get('title')}' - 포맷 {len(formats)}개" + (
        f", 화질 {' / '.join(f'{h}p' for h in heights)}" if heights else "")


def _log_info(info: dict, log):
    log(f"[분석 결과] yt-dlp 지원 (추출기: {info.get('extractor_key')})")
    log(f"  {_describe(info)}")
    if info.get('_type') == 'playlist':
        entries = list(info.get('entries') or [])
        for e in entries[:10]:
            log(f"   - {e.get('title') or e.get('url')}")
        if len(entries) > 10:
            log(f"   ... 외 {len(entries) - 10}개")
        return
    formats = info.get('formats') or []
    for f in formats[::-1][:10]:
        log(f"   - {f.get('format_id')} | {f.get('ext')} | {f.get('resolution')} | {f.get('protocol')}")


def _analyze(url: str, log, expand_children: bool):
    log(f"[분석 시작] {url}")
    info = _extract_info(url, YtdlpLogger(log))
    if info:
        _log_info(info, log)
        return

    log("[분석] yt-dlp 가 직접 처리하지 못했습니다. 페이지에서 미디어 링크를 탐색합니다...")
    probe = probe_page(url, log=log)
    _log_candidates(probe, log)
    if probe.candidates:
        log("[분석] 각 후보를 yt-dlp 로 처리할 수 있는지 확인합니다...")
        quiet_logger = YtdlpLogger(log, verbose=False)
        for i, cand in enumerate(probe.candidates, 1):
            info = _extract_info(cand.url, quiet_logger, referer=cand.referer)
            status = f"처리 가능 ({_describe(info)})" if info else "처리 불가"
            log(f"  {i}. [{cand.kind}] {status}")
        planned = os.path.join(sanitize_filename(series_title(probe.title)),
                               sanitize_filename(clean_title(probe.title)) + ".mp4")
        log(f"[분석] 저장 이름: {planned} ('제목 폴더' 옵션을 끄면 폴더 없이 저장)")
        log("[분석 완료] 다운로드 시 '처리 가능' 후보 중 가장 앞의 것이 사용됩니다.")
        return

    if expand_children and probe.child_pages:
        _log_child_pages(probe.child_pages, log)
        log("[분석] 첫 번째 하위 페이지로 다운로드 가능 여부를 확인합니다...")
        _analyze(probe.child_pages[0], log, expand_children=False)
        log(f"[분석 완료] 다운로드 시 하위 페이지 {len(probe.child_pages)}개를 순서대로 받습니다.")
        return

    log("[분석] 미디어 후보를 찾지 못했습니다. (JS 로 동적 로딩되거나 난독화된 사이트일 수 있습니다)")


def analyze_url(url: str, log=print):
    """다운로드 없이 yt-dlp 지원 여부와 페이지 내 미디어 후보를 확인합니다 (테스트용)."""
    _analyze(url, log, expand_children=True)


def main():
    parser = argparse.ArgumentParser(
        description="EpiGrab - yt-dlp 기반 범용 동영상/재생목록 다운로더"
    )
    parser.add_argument("url", nargs="?", help="다운로드할 영상 또는 재생목록 URL")
    parser.add_argument(
        "-o", "--output", default="downloads", help="저장 디렉터리 경로 (기본값: downloads)"
    )
    parser.add_argument(
        "-a", "--audio-only", action="store_true", help="음원(MP3)만 추출하여 다운로드"
    )
    parser.add_argument(
        "--analyze", action="store_true", help="다운로드 없이 지원 여부와 미디어 후보만 확인 (테스트용)"
    )
    parser.add_argument(
        "--no-fallback", action="store_true", help="yt-dlp 미지원 시 페이지 직접 탐색을 하지 않음"
    )
    parser.add_argument(
        "--no-subs", action="store_true", help="별도 자막 파일을 받지 않음"
    )
    parser.add_argument(
        "--no-folder", action="store_true", help="작품 제목 폴더를 만들지 않고 저장 폴더에 바로 저장"
    )
    parser.add_argument(
        "-N", "--concurrent-fragments", type=int, default=CONCURRENT_FRAGMENTS, metavar="N",
        help=f"동시에 받을 조각 수 (기본값: {CONCURRENT_FRAGMENTS}, 서버가 느릴 때 8~32 로 올리면 빨라질 수 있음)"
    )
    parser.add_argument(
        "-q", "--quality", type=int, default=0, choices=[h for h in QUALITY_CHOICES if h], metavar="P",
        help="최대 화질 (예: 720 -> 720p 이하 중 최고 화질, 기본값: 최고 화질)"
    )

    args = parser.parse_args()

    url = args.url
    if not url:
        url = input("다운로드할 미디어 URL을 입력하세요: ").strip()

    if not url:
        print("URL이 입력되지 않았습니다. 프로그램을 종료합니다.")
        return

    if args.analyze:
        analyze_url(url)
        return

    print(f"\n[작업 시작] URL: {url}")
    print(f"[저장 경로] {args.output}")
    try:
        ok = download_media(url, output_dir=args.output, extract_audio=args.audio_only,
                            progress_hooks=[progress_hook], fallback=not args.no_fallback,
                            subtitles=not args.no_subs, title_folder=not args.no_folder,
                            concurrent_fragments=args.concurrent_fragments, max_height=args.quality)
    except Exception as e:
        print(f"\n[오류 발생] 다운로드 중 문제가 발생했습니다: {e}", file=sys.stderr)
        sys.exit(1)

    if ok:
        print("\n[완료] 모든 작업이 끝났습니다.")
    else:
        print("\n[실패] 다운로드하지 못했습니다. 위 로그를 확인하세요.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
