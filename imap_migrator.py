import base64
import configparser
import csv
import email
import imaplib
import json
import os
import queue
import re
import ssl
import socket
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import http.cookiejar
from datetime import datetime, timedelta
from tkinter import filedialog, messagebox, scrolledtext, ttk


VERSION = "v1.1.47"
CONFIG_FILE = "settings.ini"

# IMAP servers can return large lines when a mailbox has many flags or folders.
imaplib._MAXLINE = 10000000


# ---------------------------------------------------------------------------
# Visual theme
# ---------------------------------------------------------------------------
COLORS = {
    "background": "#eeeeee",
    "panel": "#ffffff",
    "panel_alt": "#f7f7f7",
    "border": "#d1d1d1",
    "text": "#292929",
    "muted": "#777777",
    "dark": "#202020",
    "dark_2": "#2d2d2d",
    "dark_3": "#3b3b3b",
    "orange": "#f36a21",
    "orange_dark": "#d95316",
    "orange_light": "#fff0e8",
    "green": "#28a745",
    "red": "#c93d32",
    "blue": "#245b9e",
}

FONT = "Segoe UI"
SPEED_LIMIT_OPTIONS = [f"{value} Мбит/с" for value in range(10, 101, 10)] + ["1 Гбит/с", "Без ограничений"]


def speed_limit_to_bits_per_second(value):
    """Convert a combobox label to bits per second; None means unlimited."""
    value = str(value or "").strip().lower()
    if not value or "без" in value:
        return None
    if "гбит" in value:
        return 1_000_000_000
    match = re.search(r"(\d+)\s*мбит", value)
    if match:
        return int(match.group(1)) * 1_000_000
    return None


def format_migration_speed(processed_messages, elapsed_seconds):
    """Format message throughput for the live migration dashboard."""
    seconds = max(float(elapsed_seconds or 0.0), 0.001)
    per_second = max(0, int(processed_messages or 0)) / seconds
    per_minute = per_second * 60.0
    return f"{per_second:.1f} пис/сек | {per_minute:.0f} пис/мин"


def format_duration(total_seconds):
    """Format seconds as a compact HH:MM:SS duration."""
    total = max(0, int(round(float(total_seconds or 0.0))))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_api_error(response_data):
    """Return a useful, password-free description of a Kerio API error."""
    if not isinstance(response_data, dict):
        return str(response_data)
    error = response_data.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("data") or error
        code = error.get("code")
        return f"code={code}, {message}" if code is not None else str(message)
    if error:
        return str(error)
    result = response_data.get("result")
    if isinstance(result, dict) and result.get("errors"):
        return str(result["errors"])
    return "неизвестный ответ API: " + json.dumps(response_data, ensure_ascii=False)[:1000]


def format_exception(error):
    """Include HTTP response details when an Admin API request is rejected."""
    if isinstance(error, urllib.error.HTTPError):
        try:
            body = error.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        if body:
            return f"HTTP {error.code} {error.reason}: {body[:1000]}"
        return f"HTTP {error.code} {error.reason}"
    if isinstance(error, urllib.error.URLError):
        return f"сетевой сбой: {error.reason}"
    return str(error)


IMAP_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)
IMAP_INTERNALDATE_RE = re.compile(
    r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s+"
    r"(\d{1,2}):(\d{2}):(\d{2})\s+([+-])(\d{2})(\d{2})\s*$"
)


def normalize_imap_internal_date(value):
    """Return a strict RFC 3501 INTERNALDATE or ``None``.

    Some source servers return a value that looks like INTERNALDATE but is
    rejected by Kerio's APPEND parser.  Normalize only the standard English
    month/date form and omit the optional date when the source value is not
    valid; APPEND then lets the destination assign its current internal date
    instead of rejecting the whole message.
    """
    raw_value = str(value or "").strip().strip('"')
    match = IMAP_INTERNALDATE_RE.fullmatch(raw_value)
    if not match:
        return None

    day, month_name, year, hour, minute, second, sign, offset_hour, offset_minute = match.groups()
    month_name = month_name.title()
    if month_name not in IMAP_MONTHS:
        return None

    try:
        day = int(day)
        month = IMAP_MONTHS.index(month_name) + 1
        year = int(year)
        hour = int(hour)
        minute = int(minute)
        second = int(second)
        offset_hour = int(offset_hour)
        offset_minute = int(offset_minute)
        # Validate the calendar/time fields without relying on the process
        # locale (which can make strptime produce non-English month names).
        datetime(year, month, day, hour, minute, second)
    except (TypeError, ValueError):
        return None

    if offset_hour > 23 or offset_minute > 59:
        return None

    return (
        f'"{day:02d}-{month_name}-{year:04d} '
        f"{hour:02d}:{minute:02d}:{second:02d} "
        f"{sign}{offset_hour:02d}{offset_minute:02d}"
        '"'
    )


def encode_imap_folder_name(utf8_str):
    """Encode a UTF-8 folder name to RFC 3501 modified UTF-7.

    Printable ASCII characters stay literal, including a hyphen inside a
    Cyrillic name such as ``КБ-Траст``. Only contiguous non-ASCII runs are
    base64 encoded; encoding the whole component would make Yandex reject
    names that mix Cyrillic and ASCII characters.
    """
    if not utf8_str:
        return ""

    def encode_component(component):
        result = []
        unicode_run = []

        def flush_unicode_run():
            if not unicode_run:
                return
            encoded_utf16 = "".join(unicode_run).encode("utf-16be")
            b64 = base64.b64encode(encoded_utf16).decode("ascii")
            b64 = b64.rstrip("=").replace("/", ",")
            result.append(f"&{b64}-")
            unicode_run.clear()

        for character in component:
            codepoint = ord(character)
            if 0x20 <= codepoint <= 0x7E and character != "&":
                flush_unicode_run()
                result.append(character)
            elif character == "&":
                flush_unicode_run()
                result.append("&-")
            else:
                unicode_run.append(character)
        flush_unicode_run()
        return "".join(result)

    return "/".join(encode_component(part) for part in utf8_str.split("/"))


def decode_imap_folder_name(encoded_str):
    """Decode an IMAP modified UTF-7 folder name."""
    if not encoded_str:
        return ""

    encoded_str = encoded_str.strip('"')

    def mod_utf7_decode(match):
        encoded = match.group(1).replace(",", "/").encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        try:
            return base64.b64decode(encoded).decode("utf-16be")
        except Exception:
            return match.group(0)

    try:
        decoded_parts = []
        for part in encoded_str.split("/"):
            decoded = re.sub(r"&([A-Za-z0-9+,]+)-", mod_utf7_decode, part)
            decoded_parts.append(decoded.replace("&-", "&"))
        return "/".join(decoded_parts)
    except Exception:
        return encoded_str


def extract_imap_folder_name(folder_item):
    """Extract and decode a mailbox name from an IMAP LIST response."""
    value = folder_item
    if isinstance(folder_item, (tuple, list)):
        for candidate in reversed(folder_item):
            if candidate not in (None, b"", ""):
                value = candidate
                break
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="ignore").strip()
    else:
        text = str(value or "").strip()
    if not text:
        return ""

    # LIST responses normally quote the mailbox name. Taking the last quoted
    # token preserves names containing spaces and avoids confusing the quoted
    # hierarchy delimiter with the mailbox itself.
    quoted_matches = list(re.finditer(r'"([^"]*)"', text))
    if quoted_matches and not text[quoted_matches[-1].end():].strip():
        raw_name = quoted_matches[-1].group(1).replace('\\"', '"')
    else:
        match = re.search(r"(\S+)$", text)
        raw_name = match.group(1) if match else text
    if raw_name.upper() == "NIL":
        return ""
    return decode_imap_folder_name(raw_name.strip('"')).strip()


def list_imap_folders(connection):
    """Return decoded IMAP mailboxes, including servers with partial LIST."""
    folder_names = []
    successful_list = False
    # Yandex normally answers LIST "" "*". The percent query is a useful
    # fallback for servers that omit some root-level custom mailboxes from *.
    for pattern in ("*", "%"):
        try:
            status, folder_list = connection.list("", pattern)
        except Exception:
            continue
        if status != "OK":
            continue
        successful_list = True
        for folder_item in folder_list or []:
            folder_name = extract_imap_folder_name(folder_item)
            if folder_name:
                folder_names.append(folder_name)

    if not successful_list:
        try:
            status, folder_list = connection.list()
            if status == "OK":
                for folder_item in folder_list or []:
                    folder_name = extract_imap_folder_name(folder_item)
                    if folder_name:
                        folder_names.append(folder_name)
        except Exception:
            pass

    return deduplicate_folders(folder_names)


def deduplicate_folders(folders_list):
    """Collapse common aliases returned by different IMAP servers."""
    seen = {}
    result = []

    for folder in folders_list:
        clean = folder.strip()
        if not clean:
            continue

        lower_folder = clean.lower()
        for prefix in ("inbox/", "inbox.", "inbox\\"):
            if lower_folder.startswith(prefix):
                lower_folder = lower_folder[len(prefix):]
                break

        canonical = lower_folder
        if canonical in ("sent messages", "sent items"):
            canonical = "sent"
        elif canonical in ("deleted messages", "trash", "корзина"):
            canonical = "trash"
        elif canonical in ("junk", "spam", "спам"):
            canonical = "spam"
        elif canonical in ("drafts", "черновики"):
            canonical = "drafts"

        if canonical not in seen:
            seen[canonical] = clean
            result.append(clean)
        else:
            existing = seen[canonical]
            # Prefer the hierarchical name when both "Sent" and "INBOX/Sent"
            # are returned by the server.
            if "/" not in existing and "/" in clean:
                seen[canonical] = clean
                result = [clean if item == existing else item for item in result]

    return result


class ScrollableFrame(tk.Frame):
    """A fixed-height canvas viewport with a vertical scrollbar.

    Keeping the viewport height fixed is important here: the number of
    mailboxes must never push the log panel below the visible area.
    """

    def __init__(self, parent, height=150, background=None, **kwargs):
        self.background = background or COLORS["panel"]
        super().__init__(parent, bg=self.background, height=height, **kwargs)
        self.pack_propagate(False)

        self.canvas = tk.Canvas(
            self,
            background=self.background,
            highlightthickness=1,
            highlightbackground=COLORS["border"],
            bd=0,
        )
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, background=self.background)
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.inner.bind("<Configure>", self._update_scroll_region)
        self.canvas.bind("<Configure>", self._resize_inner)

        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # Wheel scrolling is enabled only while the cursor is over this
        # particular viewport, so two independent lists do not fight each other.
        self.bind("<Enter>", self._bind_mousewheel)
        self.bind("<Leave>", self._unbind_mousewheel)
        self.canvas.bind("<Enter>", self._bind_mousewheel)
        self.canvas.bind("<Leave>", self._unbind_mousewheel)
        self.inner.bind("<Enter>", self._bind_mousewheel)
        self.inner.bind("<Leave>", self._unbind_mousewheel)
        self._bind_mousewheel_recursive(self.inner)

    def _update_scroll_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        # Account rows and checkboxes are created after initialization. Bind
        # the wheel handlers to those child widgets too, otherwise scrolling
        # may stop when the cursor is directly over a progress bar/checkbox.
        self._bind_mousewheel_recursive(self.inner)

    def _bind_mousewheel_recursive(self, widget):
        try:
            widget.bind("<Enter>", self._bind_mousewheel)
            widget.bind("<Leave>", self._unbind_mousewheel)
        except tk.TclError:
            return
        for child in widget.winfo_children():
            self._bind_mousewheel_recursive(child)

    def _resize_inner(self, event):
        self.canvas.itemconfigure(self.window_id, width=max(event.width, 1))

    def _bind_mousewheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind_all("<Button-4>", self._on_mousewheel_up)
        self.canvas.bind_all("<Button-5>", self._on_mousewheel_down)

    def _unbind_mousewheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_mousewheel(self, event):
        if event.delta:
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_mousewheel_up(self, _event):
        self.canvas.yview_scroll(-3, "units")

    def _on_mousewheel_down(self, _event):
        self.canvas.yview_scroll(3, "units")


class NetworkRateLimiter:
    """Global application-level limiter shared by all migration workers.

    The limiter schedules the bytes belonging to source downloads and Kerio
    uploads on one timeline. It limits migration payload traffic, including
    both directions, rather than allowing every worker to use the selected
    rate independently.
    """

    def __init__(self, bits_per_second=None):
        self.bytes_per_second = (bits_per_second / 8.0) if bits_per_second else None
        self.lock = threading.Lock()
        self.next_available = time.monotonic()

    @property
    def enabled(self):
        return bool(self.bytes_per_second and self.bytes_per_second > 0)

    def throttle(self, byte_count):
        if not self.enabled or not byte_count:
            return

        amount = max(1, int(byte_count))
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_available)
            self.next_available = start + (amount / self.bytes_per_second)
            wait_seconds = start - now

        if wait_seconds > 0:
            time.sleep(wait_seconds)


class ImapMigratorApp:
    def __init__(self, root):
        self.root = root
        self.root.title(f"IMAP Mail Migration Tool ({VERSION})")
        # Keep the central controls compact so the log remains visible on a
        # 1366x768 desktop. Both account lists have their own scrollbar.
        self.root.geometry("1180x720")
        self.root.minsize(1000, 600)
        self.root.configure(background=COLORS["background"])

        self.stop_requested = False
        self.test_stop_event = threading.Event()
        self.test_running = False
        self.server_test_stop_event = threading.Event()
        self.server_test_running = False
        self.analysis_stop_event = threading.Event()
        self.active_connections = []
        self.connection_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        # Progress callbacks can arrive once per message. Do not enqueue one
        # Tk event per message: coalesce the values and paint them at a fixed
        # rate from the Tk main loop instead.
        self.progress_ui_lock = threading.Lock()
        self.account_progress_states = {}
        self.account_progress_display_order = ()
        self.last_stats_text = ""
        self.progress_refresh_ms = 250
        # Keep the visible log bounded as well. Per-account log files remain
        # complete, while the GUI cannot grow without limit during a long run.
        self.log_queue = queue.Queue(maxsize=20000)
        self.log_flush_pending = False
        self.max_visible_log_lines = 20000
        self.account_checkboxes = {}  # email -> (BooleanVar, password)
        self.csv_accounts_data = []
        self.account_full_names = {}  # email -> full name from CSV
        self.runtime_settings = {}
        self.rate_limiter = NetworkRateLimiter(None)

        # Wizard state and the cache populated on the summary step.
        self.current_step = -1
        self.analysis_running = False
        self.analysis_cache = {}
        self.analysis_cache_signature = None
        self.use_analysis_cache = False
        self.process_finished = False
        self.migration_outcome = "idle"
        self.server_test_results = {"source": None, "destination": None}

        self.migration_start_time = None
        self.timer_running = False
        self.total_msgs_cache = 0
        self.copied_msgs_cache = 0
        self.skipped_msgs_cache = 0
        self.error_msgs_cache = 0

        self._configure_styles()
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.load_settings()
        # One lightweight renderer updates progress and statistics four times
        # per second, regardless of how many messages are being migrated.
        self.root.after(self.progress_refresh_ms, self._refresh_progress_ui)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------
    def _configure_styles(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TEntry", padding=(6, 4), font=(FONT, 9))
        style.configure(
            "Card.TCheckbutton",
            background=COLORS["panel"],
            foreground=COLORS["text"],
            font=(FONT, 9),
        )
        style.map("Card.TCheckbutton", background=[("active", COLORS["panel"])])
        style.configure(
            "Accent.Horizontal.TProgressbar",
            troughcolor="#dedede",
            background=COLORS["orange"],
            bordercolor="#dedede",
            lightcolor=COLORS["orange"],
            darkcolor=COLORS["orange"],
            thickness=11,
        )
        style.configure(
            "Green.Horizontal.TProgressbar",
            troughcolor="#dedede",
            background=COLORS["green"],
            bordercolor="#dedede",
            lightcolor=COLORS["green"],
            darkcolor=COLORS["green"],
            thickness=11,
        )
        style.configure(
            "Running.Horizontal.TProgressbar",
            troughcolor="#fff1b8",
            background="#e7a400",
            bordercolor="#fff1b8",
            lightcolor="#e7a400",
            darkcolor="#e7a400",
            thickness=11,
        )
        style.configure(
            "Error.Horizontal.TProgressbar",
            troughcolor="#f8d7da",
            background=COLORS["red"],
            bordercolor="#f8d7da",
            lightcolor=COLORS["red"],
            darkcolor=COLORS["red"],
            thickness=11,
        )
        style.configure(
            "Pending.Horizontal.TProgressbar",
            troughcolor="#e5e7eb",
            background="#9aa0a6",
            bordercolor="#e5e7eb",
            lightcolor="#9aa0a6",
            darkcolor="#9aa0a6",
            thickness=11,
        )
        style.configure("App.TSpinbox", padding=(4, 3), font=(FONT, 9))

    def _card(self, parent, title):
        return tk.LabelFrame(
            parent,
            text=f"  {title}  ",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 9, "bold"),
            bd=1,
            relief="solid",
            highlightthickness=0,
            padx=8,
            pady=5,
        )

    def _label(self, parent, text, muted=False, bold=False, **kwargs):
        return tk.Label(
            parent,
            text=text,
            bg=COLORS["panel"],
            fg=COLORS["muted"] if muted else COLORS["text"],
            font=(FONT, 9, "bold" if bold else "normal"),
            **kwargs,
        )

    def _button(self, parent, text, command, kind="accent", width=None):
        if kind == "accent":
            bg, active, fg = COLORS["orange"], COLORS["orange_dark"], "white"
        elif kind == "danger":
            bg, active, fg = COLORS["dark_2"], COLORS["dark"], "white"
        elif kind == "light":
            bg, active, fg = COLORS["panel"], "#e4e4e4", COLORS["text"]
        else:
            bg, active, fg = COLORS["dark_2"], COLORS["dark"], "white"

        options = {
            "text": text,
            "command": command,
            "bg": bg,
            "activebackground": active,
            "fg": fg,
            "activeforeground": fg,
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 0,
            "font": (FONT, 9, "bold"),
            "padx": 12,
            "pady": 6,
            "cursor": "hand2",
        }
        if kind == "light":
            options["highlightthickness"] = 1
            options["highlightbackground"] = COLORS["border"]
            options["highlightcolor"] = COLORS["border"]
        if width:
            options["width"] = width
        return tk.Button(parent, **options)

    def _build_ui(self):
        self.main_frame = tk.Frame(self.root, bg=COLORS["background"])
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # ----------------------------- header -------------------------
        header = tk.Frame(self.main_frame, bg=COLORS["dark"], height=64)
        header.pack(fill=tk.X)
        header.pack_propagate(False)

        brand = tk.Frame(header, bg=COLORS["dark"])
        brand.pack(side=tk.LEFT, fill=tk.Y, padx=(14, 0))
        tk.Label(
            brand,
            text="IMAP",
            bg=COLORS["orange"],
            fg="white",
            font=(FONT, 10, "bold"),
            padx=8,
            pady=7,
        ).pack(side=tk.LEFT, pady=12)

        title_box = tk.Frame(brand, bg=COLORS["dark"])
        title_box.pack(side=tk.LEFT, padx=(11, 0), pady=9)
        tk.Label(
            title_box,
            text="Миграция почты",
            bg=COLORS["dark"],
            fg="white",
            font=(FONT, 15, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            title_box,
            text="IMAP  →  Kerio Connect",
            bg=COLORS["dark"],
            fg=COLORS["orange"],
            font=(FONT, 9, "bold"),
            anchor="w",
        ).pack(anchor="w")

        header_right = tk.Frame(header, bg=COLORS["dark"])
        header_right.pack(side=tk.RIGHT, fill=tk.Y, padx=14)
        self.header_status = tk.Label(
            header_right,
            text="ШАГ 1 ИЗ 4",
            bg=COLORS["dark_3"],
            fg="white",
            font=(FONT, 9, "bold"),
            padx=10,
            pady=4,
        )
        self.header_status.pack(side=tk.TOP, anchor="e", pady=(10, 2))
        tk.Label(
            header_right,
            text=f"{VERSION}  •  Wizard",
            bg=COLORS["dark"],
            fg="#bdbdbd",
            font=(FONT, 8),
        ).pack(anchor="e")

        # ----------------------------- body ---------------------------
        body = tk.Frame(self.main_frame, bg=COLORS["background"])
        body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(7, 0))

        self.page_title = tk.Label(
            body,
            text="Подключение к серверам",
            bg=COLORS["background"],
            fg=COLORS["text"],
            font=(FONT, 13, "bold"),
            anchor="w",
        )
        self.page_title.pack(anchor="w")
        self.page_hint = tk.Label(
            body,
            text="Укажите IMAP-серверы и проверьте их сетевую доступность.",
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=(FONT, 9),
            anchor="w",
        )
        self.page_hint.pack(anchor="w", pady=(1, 6))

        # Compact step indicator.
        step_bar = tk.Frame(body, bg=COLORS["background"])
        step_bar.pack(fill=tk.X, pady=(0, 7))
        self.step_badges = []
        step_names = [("1", "Серверы"), ("2", "Ящики"), ("3", "Итог"), ("4", "Процесс")]
        for index, (number, name) in enumerate(step_names):
            step_bar.columnconfigure(index * 2, weight=1)
            badge = tk.Frame(step_bar, bg=COLORS["background"])
            badge.grid(row=0, column=index * 2, sticky="ew")
            circle = tk.Label(
                badge,
                text=number,
                bg=COLORS["dark_3"],
                fg="white",
                font=(FONT, 9, "bold"),
                width=3,
                pady=3,
            )
            circle.pack(side=tk.LEFT, padx=(0, 5))
            label = tk.Label(
                badge,
                text=name,
                bg=COLORS["background"],
                fg=COLORS["muted"],
                font=(FONT, 9, "bold"),
            )
            label.pack(side=tk.LEFT)
            self.step_badges.append((circle, label))
            if index < len(step_names) - 1:
                separator = tk.Frame(step_bar, bg=COLORS["border"], height=1)
                separator.grid(row=0, column=index * 2 + 1, sticky="ew", padx=10)

        self.step_content = tk.Frame(body, bg=COLORS["background"])
        self.step_content.pack(fill=tk.BOTH, expand=True)
        self.step_frames = []
        self._build_step_one()
        self._build_step_two()
        self._build_step_three()
        self._build_step_four()

        # Wizard navigation is outside the page content, so it never covers
        # the process log.
        footer = tk.Frame(self.main_frame, bg=COLORS["dark"], height=48)
        footer.pack(fill=tk.X, side=tk.BOTTOM)
        footer.pack_propagate(False)
        self.wizard_back_btn = self._button(
            footer,
            "‹  Назад",
            self.previous_step,
            kind="light",
            width=13,
        )
        self.wizard_back_btn.pack(side=tk.LEFT, padx=(10, 5), pady=7)
        self.wizard_next_btn = self._button(
            footer,
            "Далее  ›",
            self.next_step,
            kind="accent",
            width=18,
        )
        self.wizard_next_btn.pack(side=tk.RIGHT, padx=(5, 10), pady=7)
        self.footer_status = tk.Label(
            footer,
            text="Шаг 1: серверы",
            bg=COLORS["dark"],
            fg="#bdbdbd",
            font=(FONT, 8),
        )
        self.footer_status.pack(side=tk.LEFT, padx=12)

        self._show_step(0)

    def _step_heading(self, parent, title, description):
        box = tk.Frame(parent, bg=COLORS["background"])
        box.pack(fill=tk.X, pady=(0, 7))
        tk.Label(
            box,
            text=title,
            bg=COLORS["background"],
            fg=COLORS["text"],
            font=(FONT, 12, "bold"),
            anchor="w",
        ).pack(anchor="w")
        tk.Label(
            box,
            text=description,
            bg=COLORS["background"],
            fg=COLORS["muted"],
            font=(FONT, 9),
            anchor="w",
        ).pack(anchor="w", pady=(1, 0))

    def _build_step_one(self):
        frame = tk.Frame(self.step_content, bg=COLORS["background"])
        self.step_frames.append(frame)
        self._step_heading(
            frame,
            "Шаг 1. Источник и назначение",
            "Проверьте, что с этого компьютера доступны оба IMAP-сервера.",
        )

        servers_row = tk.Frame(frame, bg=COLORS["background"])
        servers_row.pack(fill=tk.X, pady=(0, 8))
        servers_row.columnconfigure(0, weight=1)
        servers_row.columnconfigure(1, weight=1)

        source_box = self._card(servers_row, "Источник IMAP")
        source_box.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        source_grid = tk.Frame(source_box, bg=COLORS["panel"])
        source_grid.pack(fill=tk.X)
        source_grid.columnconfigure(1, weight=1)
        self._label(source_grid, "Сервер:").grid(row=0, column=0, sticky="w", pady=3)
        self.src_host = ttk.Entry(source_grid, style="App.TEntry")
        self.src_host.grid(row=0, column=1, sticky="ew", padx=(8, 6), pady=3)
        self.src_host.insert(0, "imap.yandex.ru")
        self._label(source_grid, "Порт:", muted=True).grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.src_port = ttk.Entry(source_grid, width=7, style="App.TEntry")
        self.src_port.grid(row=0, column=3, sticky="w", padx=(0, 8), pady=3)
        self.src_port.insert(0, "993")
        self.src_ssl = tk.BooleanVar(value=True)
        self.src_ssl.trace_add("write", lambda *_args: self._invalidate_server_test())
        ttk.Checkbutton(
            source_grid,
            text="SSL",
            variable=self.src_ssl,
            style="Card.TCheckbutton",
        ).grid(row=0, column=4, sticky="w")
        self.server_source_status = tk.Label(
            source_box,
            text="Не проверен",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 8),
            anchor="w",
        )
        self.server_source_status.pack(anchor="w", pady=(5, 0))

        destination_box = self._card(servers_row, "Назначение Kerio IMAP")
        destination_box.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        destination_grid = tk.Frame(destination_box, bg=COLORS["panel"])
        destination_grid.pack(fill=tk.X)
        destination_grid.columnconfigure(1, weight=1)
        self._label(destination_grid, "Сервер:").grid(row=0, column=0, sticky="w", pady=3)
        self.dst_host = ttk.Entry(destination_grid, style="App.TEntry")
        self.dst_host.grid(row=0, column=1, sticky="ew", padx=(8, 6), pady=3)
        self.dst_host.insert(0, "m.technograd.by")
        self._label(destination_grid, "Порт:", muted=True).grid(row=0, column=2, sticky="w", padx=(0, 4))
        self.dst_port = ttk.Entry(destination_grid, width=7, style="App.TEntry")
        self.dst_port.grid(row=0, column=3, sticky="w", padx=(0, 8), pady=3)
        self.dst_port.insert(0, "993")
        self.dst_ssl = tk.BooleanVar(value=True)
        self.dst_ssl.trace_add("write", lambda *_args: self._invalidate_server_test())
        ttk.Checkbutton(
            destination_grid,
            text="SSL",
            variable=self.dst_ssl,
            style="Card.TCheckbutton",
        ).grid(row=0, column=4, sticky="w")
        for server_widget in (self.src_host, self.src_port, self.dst_host, self.dst_port):
            server_widget.bind("<KeyRelease>", self._invalidate_server_test)
        self.server_destination_status = tk.Label(
            destination_box,
            text="Не проверен",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 8),
            anchor="w",
        )
        self.server_destination_status.pack(anchor="w", pady=(5, 0))

        check_box = self._card(frame, "Проверка доступности серверов")
        check_box.pack(fill=tk.X, pady=(0, 8))
        check_row = tk.Frame(check_box, bg=COLORS["panel"])
        check_row.pack(fill=tk.X)
        tk.Label(
            check_row,
            text="Проверяется TCP-подключение и SSL-рукопожатие. Авторизация ящиков — на шаге 2.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 8),
            anchor="w",
        ).pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.server_test_start_btn = self._button(
            check_row,
            "Проверить доступность",
            self.run_server_availability_test,
            kind="accent",
            width=23,
        )
        self.server_test_start_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.server_test_stop_btn = self._button(
            check_row,
            "Остановить",
            self.stop_server_availability_test,
            kind="danger",
            width=13,
        )
        self.server_test_stop_btn.pack(side=tk.RIGHT, padx=(8, 0))
        self.server_test_stop_btn.config(state=tk.DISABLED)

        note = self._card(frame, "Далее")
        note.pack(fill=tk.X)
        tk.Label(
            note,
            text="После успешной проверки перейдите к выбору CSV и тестированию учетных записей.",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 9),
            anchor="w",
        ).pack(anchor="w")

    def _build_step_two(self):
        frame = tk.Frame(self.step_content, bg=COLORS["background"])
        self.step_frames.append(frame)
        self._step_heading(
            frame,
            "Шаг 2. Ящики и проверка пользователей",
            "Загрузите CSV, выберите аккаунты и проверьте вход на источник и Kerio.",
        )

        columns = tk.Frame(frame, bg=COLORS["background"])
        columns.pack(fill=tk.BOTH, expand=True)
        columns.columnconfigure(0, weight=3)
        columns.columnconfigure(1, weight=2)
        columns.rowconfigure(0, weight=1)

        csv_box = self._card(columns, "CSV и параметры миграции")
        csv_box.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        path_grid = tk.Frame(csv_box, bg=COLORS["panel"])
        path_grid.pack(fill=tk.X, pady=(0, 5))
        path_grid.columnconfigure(1, weight=1)
        self._label(path_grid, "CSV файл:").grid(row=0, column=0, sticky="w")
        self.csv_path_entry = ttk.Entry(path_grid, style="App.TEntry")
        self.csv_path_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        self._button(path_grid, "Обзор...", self.browse_csv, kind="light", width=10).grid(row=0, column=2)

        controls = tk.Frame(csv_box, bg=COLORS["panel"])
        controls.pack(fill=tk.X, pady=(2, 6))
        self._label(controls, "Потоков (4–6):").pack(side=tk.LEFT)
        self.threads_spin = ttk.Spinbox(controls, from_=1, to=10, width=5, style="App.TSpinbox")
        self.threads_spin.set(4)
        self.threads_spin.pack(side=tk.LEFT, padx=(7, 13))
        self._button(controls, "Выбрать все", self.select_all_accounts, kind="light", width=12).pack(side=tk.LEFT, padx=(0, 4))
        self._button(controls, "Снять все", self.deselect_all_accounts, kind="light", width=11).pack(side=tk.LEFT)
        self._label(controls, "Лимит:").pack(side=tk.LEFT, padx=(16, 5))
        self.speed_limit_var = tk.StringVar(value="Без ограничений")
        self.speed_limit_combo = ttk.Combobox(
            controls,
            textvariable=self.speed_limit_var,
            values=SPEED_LIMIT_OPTIONS,
            state="readonly",
            width=16,
        )
        self.speed_limit_combo.pack(side=tk.LEFT)

        tk.Label(
            csv_box,
            text="Выбранные учетные записи",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 9, "bold"),
            anchor="w",
        ).pack(fill=tk.X, pady=(0, 3))
        csv_list = ScrollableFrame(csv_box, height=260, background=COLORS["panel"])
        csv_list.pack(fill=tk.BOTH, expand=True)
        self.csv_scroll = csv_list
        self.canvas = csv_list.canvas
        self.accounts_checklist_frame = csv_list.inner
        self.placeholder_label = tk.Label(
            self.accounts_checklist_frame,
            text="Выберите CSV-файл, чтобы увидеть список ящиков...",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 9, "italic"),
            anchor="w",
        )
        self.placeholder_label.pack(anchor="w", pady=10, padx=6)

        right_column = tk.Frame(columns, bg=COLORS["background"])
        right_column.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right_column.columnconfigure(0, weight=1)
        right_column.rowconfigure(1, weight=1)

        credentials_box = self._card(right_column, "Учетные данные")
        credentials_box.grid(row=0, column=0, sticky="ew", pady=(0, 7))
        credentials_grid = tk.Frame(credentials_box, bg=COLORS["panel"])
        credentials_grid.pack(fill=tk.X)
        credentials_grid.columnconfigure(1, weight=1)
        credentials_grid.columnconfigure(3, weight=1)
        self._label(credentials_grid, "Одиночный:").grid(row=0, column=0, sticky="w", pady=3)
        self.src_user = ttk.Entry(credentials_grid, style="App.TEntry")
        self.src_user.grid(row=0, column=1, sticky="ew", padx=(7, 10), pady=3)
        self._label(credentials_grid, "Пароль:").grid(row=0, column=2, sticky="w", pady=3)
        self.src_pass = ttk.Entry(credentials_grid, show="*", style="App.TEntry")
        self.src_pass.grid(row=0, column=3, sticky="ew", padx=(7, 0), pady=3)
        self._label(credentials_grid, "Админ:").grid(row=1, column=0, sticky="w", pady=3)
        self.adm_user = ttk.Entry(credentials_grid, style="App.TEntry")
        self.adm_user.grid(row=1, column=1, sticky="ew", padx=(7, 10), pady=3)
        self.adm_user.insert(0, "admin")
        self._label(credentials_grid, "Пароль:").grid(row=1, column=2, sticky="w", pady=3)
        self.adm_pass = ttk.Entry(credentials_grid, show="*", style="App.TEntry")
        self.adm_pass.grid(row=1, column=3, sticky="ew", padx=(7, 0), pady=3)
        self.auto_create_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            credentials_box,
            text="Автоматически создавать или обновлять ящик в Kerio",
            variable=self.auto_create_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(4, 0))

        user_test_box = self._card(right_column, "Тестирование пользователей")
        user_test_box.grid(row=1, column=0, sticky="nsew")
        test_contents = tk.Frame(user_test_box, bg=COLORS["panel"])
        test_contents.pack(fill=tk.BOTH, expand=True)
        self.test_src_var = tk.BooleanVar(value=True)
        self.test_dst_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            test_contents,
            text="Проверять источник",
            variable=self.test_src_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(2, 3))
        ttk.Checkbutton(
            test_contents,
            text="Проверять Kerio",
            variable=self.test_dst_var,
            style="Card.TCheckbutton",
        ).pack(anchor="w", pady=(0, 5))
        self.test_start_btn = self._button(
            test_contents,
            "Тест выбранных ящиков",
            self.run_connection_test,
            kind="accent",
            width=23,
        )
        self.test_start_btn.pack(anchor="w", pady=(2, 4))
        self.test_stop_btn = self._button(
            test_contents,
            "■  Остановить тестирование",
            self.stop_connection_test,
            kind="danger",
            width=23,
        )
        self.test_stop_btn.pack(anchor="w", pady=(0, 7))
        self.test_stop_btn.config(state=tk.DISABLED)
        self.test_result_label = tk.Label(
            test_contents,
            text="Тест еще не запускался.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            justify=tk.LEFT,
            anchor="w",
            font=(FONT, 8),
        )
        self.test_result_label.pack(fill=tk.X, pady=(4, 0))

    def _build_step_three(self):
        frame = tk.Frame(self.step_content, bg=COLORS["background"])
        self.step_frames.append(frame)
        self._step_heading(
            frame,
            "Шаг 3. Итоговая проверка",
            "Проверьте маршрут, количество писем и ориентировочный объем перед запуском.",
        )

        route_box = self._card(frame, "Маршрут миграции")
        route_box.pack(fill=tk.X, pady=(0, 7))
        self.summary_route_label = tk.Label(
            route_box,
            text="Источник  →  Назначение",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 10, "bold"),
            anchor="w",
        )
        self.summary_route_label.pack(fill=tk.X)
        self.summary_status_label = tk.Label(
            route_box,
            text="Анализ еще не запускался.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 8),
            anchor="w",
        )
        self.summary_status_label.pack(fill=tk.X, pady=(3, 0))

        totals_box = tk.Frame(frame, bg=COLORS["background"])
        totals_box.pack(fill=tk.X, pady=(0, 7))
        for column in range(3):
            totals_box.columnconfigure(column, weight=1)
        self.summary_accounts_card = self._summary_metric(totals_box, 0, "ЯЩИКОВ", "0")
        self.summary_messages_card = self._summary_metric(totals_box, 1, "ПИСЕМ", "0")
        self.summary_volume_card = self._summary_metric(totals_box, 2, "ОБЪЕМ", "0 МБ")

        table_box = self._card(frame, "Что будет перенесено")
        table_box.pack(fill=tk.BOTH, expand=True)
        table_container = tk.Frame(table_box, bg=COLORS["panel"])
        table_container.pack(fill=tk.BOTH, expand=True)
        style = ttk.Style()
        style.configure("Summary.Treeview", rowheight=25, font=(FONT, 8))
        style.configure("Summary.Treeview.Heading", font=(FONT, 8, "bold"))
        columns = ("account", "messages", "volume", "destination", "state")
        self.summary_tree = ttk.Treeview(
            table_container,
            columns=columns,
            show="headings",
            style="Summary.Treeview",
        )
        headings = {
            "account": "Учетная запись",
            "messages": "Писем",
            "volume": "Объем",
            "destination": "Назначение",
            "state": "Статус",
        }
        widths = {"account": 230, "messages": 90, "volume": 100, "destination": 190, "state": 150}
        for column in columns:
            self.summary_tree.heading(column, text=headings[column])
            self.summary_tree.column(column, width=widths[column], anchor="w")
        summary_scrollbar = ttk.Scrollbar(table_container, orient="vertical", command=self.summary_tree.yview)
        self.summary_tree.configure(yscrollcommand=summary_scrollbar.set)
        self.summary_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        summary_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.summary_progress = ttk.Progressbar(
            table_box,
            orient="horizontal",
            mode="determinate",
            style="Accent.Horizontal.TProgressbar",
        )
        self.summary_progress.pack(fill=tk.X, pady=(7, 0))

    def _summary_metric(self, parent, column, title, value):
        card = tk.Frame(parent, bg=COLORS["panel"], bd=1, relief="solid")
        card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 4, 4 if column < 2 else 0))
        value_label = tk.Label(
            card,
            text=value,
            bg=COLORS["panel"],
            fg=COLORS["orange"],
            font=(FONT, 15, "bold"),
        )
        value_label.pack(anchor="w", padx=10, pady=(7, 0))
        tk.Label(
            card,
            text=title,
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 8, "bold"),
        ).pack(anchor="w", padx=10, pady=(0, 7))
        return value_label

    def _build_step_four(self):
        frame = tk.Frame(self.step_content, bg=COLORS["background"])
        self.step_frames.append(frame)
        self._step_heading(
            frame,
            "Шаг 4. Выполнение миграции",
            "Следите за общим прогрессом, отдельными ящиками и подробным журналом.",
        )

        process_route_box = self._card(frame, "Текущий маршрут")
        process_route_box.pack(fill=tk.X, pady=(0, 6))
        self.process_route_label = tk.Label(
            process_route_box,
            text="Источник  →  Назначение",
            bg=COLORS["panel"],
            fg=COLORS["text"],
            font=(FONT, 9, "bold"),
            anchor="w",
        )
        self.process_route_label.pack(side=tk.LEFT)
        self.process_status_label = tk.Label(
            process_route_box,
            text="Ожидание запуска",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 8, "bold"),
            anchor="e",
        )
        self.process_status_label.pack(side=tk.RIGHT)

        stats_box = self._card(frame, "Общая статистика и прогресс")
        stats_box.pack(fill=tk.X, pady=(0, 6))
        self.stats_label = tk.Label(
            stats_box,
            text="Готово к запуску. Всего: 0  |  Скопировано: 0  |  Пропущено: 0  |  С ошибками: 0  |  0,0%  |  Скорость: 0,0 пис/сек | 0 пис/мин  |  Старт: —  |  Прошло: 00:00:00  |  Осталось (расч.): —  |  Завершение (расч.): —",
            bg=COLORS["panel"],
            fg=COLORS["blue"],
            font=(FONT, 9, "bold"),
            anchor="w",
        )
        self.stats_label.pack(fill=tk.X, pady=(0, 4))
        self.global_progress = ttk.Progressbar(
            stats_box,
            orient="horizontal",
            mode="determinate",
            style="Accent.Horizontal.TProgressbar",
        )
        self.global_progress.pack(fill=tk.X)

        account_box = self._card(frame, "Прогресс по ящикам")
        account_box.pack(fill=tk.X, pady=(0, 6))
        account_view = ScrollableFrame(account_box, height=112, background=COLORS["panel"])
        account_view.pack(fill=tk.X)
        self.account_progress_scroll = account_view
        self.accounts_progress_canvas = account_view.canvas
        self.accounts_inner_frame = account_view.inner
        self.account_progress_placeholder = tk.Label(
            self.accounts_inner_frame,
            text="После запуска здесь появится состояние каждого ящика.",
            bg=COLORS["panel"],
            fg=COLORS["muted"],
            font=(FONT, 9, "italic"),
            anchor="w",
        )
        self.account_progress_placeholder.pack(anchor="w", padx=6, pady=12)

        process_controls = tk.Frame(frame, bg=COLORS["background"])
        process_controls.pack(fill=tk.X, pady=(0, 6))
        self.start_btn = self._button(
            process_controls,
            "▶  Запустить повторно",
            self.start_migration_thread,
            kind="accent",
            width=20,
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 7))
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn = self._button(
            process_controls,
            "■  Остановить миграцию",
            self.stop_migration,
            kind="danger",
            width=20,
        )
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 7))
        self.stop_btn.config(state=tk.DISABLED)
        self.clear_btn = self._button(
            process_controls,
            "Очистить лог",
            self.clear_log,
            kind="light",
            width=15,
        )
        self.clear_btn.pack(side=tk.LEFT)

        log_frame = self._card(frame, f"Лог выполнения  •  {VERSION}  •  ПКМ — копирование")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_area = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.WORD,
            height=7,
            font=("Consolas", 9),
            bg=COLORS["dark"],
            fg="#eeeeee",
            insertbackground="white",
            selectbackground=COLORS["orange"],
            selectforeground="white",
            relief="flat",
            bd=0,
            padx=8,
            pady=6,
        )
        self.log_area.pack(fill=tk.BOTH, expand=True)
        self.log_area.config(state=tk.DISABLED)

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(label="Копировать", command=self.copy_log_selection)
        self.context_menu.add_command(label="Выделить всё", command=self.select_all_log)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Очистить лог", command=self.clear_log)
        self.log_area.bind("<Button-3>", self.show_context_menu)

    # ------------------------------------------------------------------
    # Small UI helpers and state
    # ------------------------------------------------------------------
    def show_context_menu(self, event):
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def copy_log_selection(self):
        try:
            selected_text = self.log_area.get(tk.SEL_FIRST, tk.SEL_LAST)
            self.root.clipboard_clear()
            self.root.clipboard_append(selected_text)
        except tk.TclError:
            pass

    def select_all_log(self):
        self.log_area.tag_add(tk.SEL, "1.0", tk.END)
        self.log_area.mark_set(tk.INSERT, "1.0")
        self.log_area.see(tk.INSERT)

    def log(self, message, log_file_path=None):
        """Queue a log line and flush a batch to Tk at most 20 times/sec."""
        if log_file_path:
            try:
                with self.log_lock:
                    with open(log_file_path, "a", encoding="utf-8") as log_file:
                        log_file.write(message + "\n")
            except Exception:
                pass

        try:
            self.log_queue.put_nowait(message)
        except queue.Full:
            # Keep the newest diagnostics if a server starts producing errors
            # faster than Tk can display them.
            try:
                self.log_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.log_queue.put_nowait(message)
            except queue.Full:
                pass

        with self.log_lock:
            if self.log_flush_pending:
                return
            self.log_flush_pending = True

        try:
            self.root.after(50, self._flush_log_queue)
        except tk.TclError:
            with self.log_lock:
                self.log_flush_pending = False

    def _flush_log_queue(self):
        messages = []
        # A cap prevents a burst of malformed-server errors from freezing the
        # UI in one large insert. Remaining lines are flushed on the next tick.
        for _ in range(500):
            try:
                messages.append(self.log_queue.get_nowait())
            except queue.Empty:
                break

        if messages:
            try:
                self.log_area.config(state=tk.NORMAL)
                self.log_area.insert(tk.END, "\n".join(messages) + "\n")
                # Text widgets retain every inserted line. Keep only the last
                # N lines in the on-screen log; individual files are complete.
                line_count = int(self.log_area.index("end-1c").split(".")[0])
                if line_count > self.max_visible_log_lines:
                    first_line_to_keep = line_count - self.max_visible_log_lines + 1
                    self.log_area.delete("1.0", f"{first_line_to_keep}.0")
                self.log_area.see(tk.END)
                self.log_area.config(state=tk.DISABLED)
            except tk.TclError:
                pass

        with self.log_lock:
            self.log_flush_pending = False
            has_more = not self.log_queue.empty()
            if has_more:
                self.log_flush_pending = True

        if has_more:
            try:
                self.root.after(50, self._flush_log_queue)
            except tk.TclError:
                with self.log_lock:
                    self.log_flush_pending = False

    def clear_log(self):
        # Remove lines that have not reached the widget yet, otherwise they
        # could reappear immediately after the user presses Clear.
        while True:
            try:
                self.log_queue.get_nowait()
            except queue.Empty:
                break
        # If a flush callback is already scheduled, leave the flag set: that
        # callback will observe the empty queue. New lines posted meanwhile
        # will be picked up by the same callback.
        self.log_area.config(state=tk.NORMAL)
        self.log_area.delete("1.0", tk.END)
        self.log_area.config(state=tk.DISABLED)

    def set_status(self, text, background=None):
        background = background or COLORS["dark_3"]

        def update():
            try:
                self.header_status.config(text=text, bg=background)
                self.footer_status.config(text=text)
                if hasattr(self, "process_status_label"):
                    self.process_status_label.config(text=text.title(), fg=background)
            except tk.TclError:
                pass

        try:
            self.root.after(0, update)
        except tk.TclError:
            pass

    def _capture_settings(self):
        return {
            "src_host": self.src_host.get().strip(),
            "src_port": self.src_port.get().strip(),
            "src_ssl": self.src_ssl.get(),
            "dst_host": self.dst_host.get().strip(),
            "dst_port": self.dst_port.get().strip(),
            "dst_ssl": self.dst_ssl.get(),
            "adm_user": self.adm_user.get().strip(),
            "adm_pass": self.adm_pass.get(),
            "auto_create": self.auto_create_var.get(),
            "speed_limit": self.speed_limit_var.get(),
            "test_src": self.test_src_var.get(),
            "test_dst": self.test_dst_var.get(),
        }

    def _setting(self, name, widget=None, default=""):
        if name in self.runtime_settings:
            return self.runtime_settings[name]
        if widget is not None:
            return widget.get()
        return default

    def _setting_bool(self, name, variable, default=False):
        if name in self.runtime_settings:
            return bool(self.runtime_settings[name])
        return bool(variable.get()) if variable is not None else default

    def save_settings(self):
        config = configparser.ConfigParser()
        config["Settings"] = {
            "src_host": self.src_host.get(),
            "src_port": self.src_port.get(),
            "src_ssl": str(self.src_ssl.get()),
            "dst_host": self.dst_host.get(),
            "dst_port": self.dst_port.get(),
            "dst_ssl": str(self.dst_ssl.get()),
            "adm_user": self.adm_user.get(),
            "adm_pass": self.adm_pass.get(),
            "auto_create": str(self.auto_create_var.get()),
            "speed_limit": self.speed_limit_var.get(),
            "csv_path": self.csv_path_entry.get(),
            "threads": str(self.threads_spin.get()),
            "test_src": str(self.test_src_var.get()),
            "test_dst": str(self.test_dst_var.get()),
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as config_file:
                config.write(config_file)
        except Exception:
            pass

    def load_settings(self):
        if not os.path.exists(CONFIG_FILE):
            return

        try:
            config = configparser.ConfigParser()
            config.read(CONFIG_FILE, encoding="utf-8")
            if "Settings" not in config:
                return

            settings = config["Settings"]
            self.src_host.delete(0, tk.END)
            self.src_host.insert(0, settings.get("src_host", "imap.yandex.ru"))
            self.src_port.delete(0, tk.END)
            self.src_port.insert(0, settings.get("src_port", "993"))
            self.src_ssl.set(settings.getboolean("src_ssl", True))

            self.dst_host.delete(0, tk.END)
            self.dst_host.insert(0, settings.get("dst_host", "m.technograd.by"))
            self.dst_port.delete(0, tk.END)
            self.dst_port.insert(0, settings.get("dst_port", "993"))
            self.dst_ssl.set(settings.getboolean("dst_ssl", True))

            self.adm_user.delete(0, tk.END)
            self.adm_user.insert(0, settings.get("adm_user", "admin"))
            self.adm_pass.delete(0, tk.END)
            self.adm_pass.insert(0, settings.get("adm_pass", ""))
            self.auto_create_var.set(settings.getboolean("auto_create", True))

            saved_speed = settings.get("speed_limit", "Без ограничений")
            if saved_speed not in SPEED_LIMIT_OPTIONS:
                saved_speed = "Без ограничений"
            self.speed_limit_var.set(saved_speed)

            csv_path = settings.get("csv_path", "")
            self.csv_path_entry.delete(0, tk.END)
            self.csv_path_entry.insert(0, csv_path)
            if csv_path and os.path.exists(csv_path):
                self.load_csv_accounts(csv_path)

            self.threads_spin.set(settings.get("threads", "4"))
            self.test_src_var.set(settings.getboolean("test_src", True))
            self.test_dst_var.set(settings.getboolean("test_dst", True))
        except Exception:
            # A malformed settings file must not prevent the application from
            # opening with its defaults.
            pass

    # ------------------------------------------------------------------
    # CSV and account selectors
    # ------------------------------------------------------------------
    def browse_csv(self):
        filename = filedialog.askopenfilename(
            title="Выберите CSV файл",
            filetypes=[("CSV файлы", "*.csv"), ("Все файлы", "*.*")],
        )
        if filename:
            self.csv_path_entry.delete(0, tk.END)
            self.csv_path_entry.insert(0, filename)
            self.load_csv_accounts(filename)

    def load_csv_accounts(self, csv_path):
        self.csv_accounts_data = []
        self.account_full_names.clear()
        for widget in self.accounts_checklist_frame.winfo_children():
            widget.destroy()
        self.account_checkboxes.clear()

        try:
            with open(csv_path, mode="r", encoding="utf-8-sig", newline="") as csv_file:
                sample = csv_file.read(2048)
                csv_file.seek(0)
                delimiter = ";" if ";" in sample else ","
                reader = csv.reader(csv_file, delimiter=delimiter)
                seen = set()
                for row in reader:
                    if not row or len(row) < 2:
                        continue
                    email_user = row[0].strip()
                    password = row[1].strip()
                    full_name = row[2].strip() if len(row) >= 3 else ""
                    key = email_user.lower()
                    if email_user and password and key not in seen:
                        seen.add(key)
                        self.csv_accounts_data.append((email_user, password))
                        self.account_full_names[email_user] = full_name
        except Exception as error:
            self.log(f"[ОШИБКА ЧТЕНИЯ CSV]: {error}")
            return

        if not self.csv_accounts_data:
            self.placeholder_label = tk.Label(
                self.accounts_checklist_frame,
                text="В выбранном CSV-файле не найдено записей.",
                bg=COLORS["panel"],
                fg=COLORS["red"],
                font=(FONT, 9),
                anchor="w",
            )
            self.placeholder_label.pack(anchor="w", pady=10, padx=6)
            self.log("[ПРЕДУПРЕЖДЕНИЕ] CSV-файл пуст или имеет неверный формат.")
            return

        for email_user, password in self.csv_accounts_data:
            variable = tk.BooleanVar(value=True)
            full_name = self._account_full_name(email_user)
            checkbox = ttk.Checkbutton(
                self.accounts_checklist_frame,
                text=f"{email_user}  •  {full_name}" if full_name else email_user,
                variable=variable,
                style="Card.TCheckbutton",
            )
            checkbox.pack(anchor="w", fill=tk.X, pady=1, padx=4)
            self.account_checkboxes[email_user] = (variable, password)

        self.csv_scroll.canvas.yview_moveto(0)
        self.log(f"[ИНФО] Из CSV загружено аккаунтов: {len(self.csv_accounts_data)}")

    def select_all_accounts(self):
        for variable, _password in self.account_checkboxes.values():
            variable.set(True)

    def deselect_all_accounts(self):
        for variable, _password in self.account_checkboxes.values():
            variable.set(False)

    def _account_full_name(self, email_user):
        """Return the optional display name from the third CSV column."""
        if email_user in self.account_full_names:
            return self.account_full_names[email_user].strip()
        email_key = str(email_user).strip().lower()
        for csv_email, full_name in self.account_full_names.items():
            if csv_email.strip().lower() == email_key:
                return str(full_name or "").strip()
        return ""

    def _selected_accounts(self):
        accounts = []
        if self.account_checkboxes:
            for email_user, (variable, password) in self.account_checkboxes.items():
                if variable.get():
                    accounts.append((email_user, password))
        else:
            email_user = self.src_user.get().strip()
            password = self.src_pass.get()
            if email_user and password:
                accounts.append((email_user, password))
        return accounts

    def _show_step(self, index):
        if index < 0 or index >= len(self.step_frames):
            return

        for step_frame in self.step_frames:
            step_frame.pack_forget()
        self.step_frames[index].pack(fill=tk.BOTH, expand=True)
        self.current_step = index

        titles = [
            ("Подключение к серверам", "Укажите IMAP-серверы и проверьте их сетевую доступность."),
            ("Ящики и пользователи", "Загрузите CSV, выберите аккаунты и проверьте учетные данные."),
            ("Итоговая проверка", "Проверьте маршрут, количество писем и ориентировочный объем."),
            ("Выполнение миграции", "Следите за общим прогрессом, отдельными ящиками и журналом."),
        ]
        title, hint = titles[index]
        self.page_title.config(text=title)
        self.page_hint.config(text=hint)

        step_names = ["серверы", "ящики", "итог", "процесс"]
        for step_index, (circle, label) in enumerate(self.step_badges):
            if step_index < index:
                circle.config(text="✓", bg=COLORS["green"])
                label.config(fg=COLORS["green"])
            elif step_index == index:
                circle.config(text=str(step_index + 1), bg=COLORS["orange"])
                label.config(fg=COLORS["text"])
            else:
                circle.config(text=str(step_index + 1), bg=COLORS["dark_3"])
                label.config(fg=COLORS["muted"])

        self.header_status.config(text=f"ШАГ {index + 1} ИЗ 4", bg=COLORS["dark_3"])
        self.footer_status.config(text=f"Шаг {index + 1}: {step_names[index]}")

        if index == 0:
            self.wizard_back_btn.config(state=tk.DISABLED)
            self.wizard_next_btn.config(text="Далее  ›", state=tk.NORMAL)
        elif index == 1:
            self.wizard_back_btn.config(state=tk.DISABLED if self.test_running else tk.NORMAL)
            next_state = tk.DISABLED if self.test_running else tk.NORMAL
            self.wizard_next_btn.config(text="К анализу  ›", state=next_state)
        elif index == 2:
            self.wizard_back_btn.config(state=tk.DISABLED if self.analysis_running else tk.NORMAL)
            next_state = tk.DISABLED if self.analysis_running else tk.NORMAL
            next_text = "Анализ..." if self.analysis_running else "Запустить миграцию  ›"
            self.wizard_next_btn.config(text=next_text, state=next_state)
        else:
            can_leave_process = self.process_finished and not self.timer_running
            self.wizard_back_btn.config(state=tk.NORMAL if can_leave_process else tk.DISABLED)
            if self.process_finished:
                self.wizard_next_btn.config(text="Новая миграция", state=tk.NORMAL)
            else:
                self.wizard_next_btn.config(text="Выполняется...", state=tk.DISABLED)

    def _valid_server_fields(self):
        checks = (
            (self.src_host, self.src_port, "Источник"),
            (self.dst_host, self.dst_port, "Назначение"),
        )
        for host_widget, port_widget, title in checks:
            host = host_widget.get().strip()
            if not host:
                messagebox.showerror("Ошибка", f"Не указан сервер: {title}.")
                return False
            try:
                port = int(port_widget.get().strip())
                if not 1 <= port <= 65535:
                    raise ValueError
            except (TypeError, ValueError):
                messagebox.showerror("Ошибка", f"Некорректный порт для блока «{title}».")
                return False
        return True

    def next_step(self):
        if self.current_step == 0:
            if self.server_test_running:
                messagebox.showinfo("Проверка выполняется", "Дождитесь окончания проверки серверов.")
                return
            if not self._valid_server_fields():
                return
            if self.server_test_results != {"source": True, "destination": True}:
                messagebox.showwarning(
                    "Серверы не проверены",
                    "Сначала успешно проверьте доступность источника и назначения.",
                )
                return
            self._show_step(1)
            return

        if self.current_step == 1:
            if self.test_running:
                messagebox.showinfo("Тест выполняется", "Сначала остановите или завершите тестирование пользователей.")
                return
            accounts = self._selected_accounts()
            if not accounts:
                messagebox.showerror("Ошибка", "Выберите ящики в CSV или заполните одиночный аккаунт.")
                return
            self._start_summary_analysis(accounts)
            return

        if self.current_step == 2:
            if self.analysis_running:
                return
            if not self.analysis_cache:
                messagebox.showwarning("Нет итоговых данных", "Сначала дождитесь завершения анализа ящиков.")
                return
            self._show_step(3)
            self.start_migration_thread()
            return

        if self.current_step == 3 and self.process_finished:
            self._reset_wizard()

    def previous_step(self):
        if self.current_step == 1:
            self._show_step(0)
        elif self.current_step == 2 and not self.analysis_running:
            self._show_step(1)
        elif self.current_step == 3:
            if self.timer_running:
                messagebox.showinfo("Миграция выполняется", "Нельзя вернуться назад во время миграции.")
            elif self.process_finished:
                self._show_step(2)

    def _reset_wizard(self):
        if self.test_running or self.timer_running or self.analysis_running:
            return
        self.process_finished = False
        self.migration_outcome = "idle"
        self.analysis_cache = {}
        self.analysis_cache_signature = None
        self.use_analysis_cache = False
        self.server_test_results = {"source": None, "destination": None}
        self.server_source_status.config(text="Не проверен", fg=COLORS["muted"])
        self.server_destination_status.config(text="Не проверен", fg=COLORS["muted"])
        self.summary_status_label.config(text="Анализ еще не запускался.", fg=COLORS["muted"])
        self.summary_accounts_card.config(text="0")
        self.summary_messages_card.config(text="0")
        self.summary_volume_card.config(text="0 МБ")
        for item in self.summary_tree.get_children():
            self.summary_tree.delete(item)
        self.clear_log()
        self._show_step(0)

    def _make_analysis_signature(self, accounts, settings=None):
        settings = settings or self._capture_settings()
        return (
            tuple(accounts),
            settings.get("src_host", ""),
            settings.get("src_port", ""),
            bool(settings.get("src_ssl", True)),
            settings.get("dst_host", ""),
            settings.get("dst_port", ""),
            bool(settings.get("dst_ssl", True)),
            bool(settings.get("auto_create", True)),
        )

    def _update_route_labels(self, settings=None):
        settings = settings or self._capture_settings()
        source = f"{settings.get('src_host', '')}:{settings.get('src_port', '')}"
        destination = f"{settings.get('dst_host', '')}:{settings.get('dst_port', '')}"
        route = f"{source}  〰  {destination}"
        self.summary_route_label.config(text=route)
        self.process_route_label.config(text=route)

    def _set_server_status(self, label, text, color):
        try:
            label.config(text=text, fg=color)
        except tk.TclError:
            pass

    def _invalidate_server_test(self, _event=None):
        if self.server_test_running:
            return
        self.server_test_results = {"source": None, "destination": None}
        self._set_server_status(self.server_source_status, "Не проверен", COLORS["muted"])
        self._set_server_status(self.server_destination_status, "Не проверен", COLORS["muted"])

    def run_server_availability_test(self):
        if self.server_test_running:
            return
        if self.test_running or self.timer_running:
            messagebox.showwarning("Операция выполняется", "Дождитесь завершения текущей операции.")
            return
        if not self._valid_server_fields():
            return

        settings = self._capture_settings()
        self.runtime_settings = settings
        self.server_test_stop_event.clear()
        self.server_test_running = True
        self.server_test_results = {"source": None, "destination": None}
        self.server_test_start_btn.config(state=tk.DISABLED)
        self.server_test_stop_btn.config(state=tk.NORMAL)
        self.wizard_next_btn.config(state=tk.DISABLED)
        self._set_server_status(self.server_source_status, "Проверка...", COLORS["orange"])
        self._set_server_status(self.server_destination_status, "Проверка...", COLORS["orange"])
        self.set_status("ПРОВЕРКА СЕРВЕРОВ", COLORS["orange"])

        checks = [
            (
                "source",
                self.server_source_status,
                settings["src_host"],
                int(settings["src_port"]),
                bool(settings["src_ssl"]),
            ),
            (
                "destination",
                self.server_destination_status,
                settings["dst_host"],
                int(settings["dst_port"]),
                bool(settings["dst_ssl"]),
            ),
        ]

        def worker():
            stopped = False
            for key, label, host, port, use_ssl in checks:
                if self.server_test_stop_event.is_set():
                    stopped = True
                    break
                raw_socket = None
                secure_socket = None
                try:
                    raw_socket = socket.create_connection((host, port), timeout=10)
                    if use_ssl:
                        context = ssl.create_default_context()
                        context.check_hostname = False
                        context.verify_mode = ssl.CERT_NONE
                        server_hostname = None if re.match(r"^\d+(?:\.\d+){3}$", host) else host
                        secure_socket = context.wrap_socket(raw_socket, server_hostname=server_hostname)
                    self.server_test_results[key] = True
                    self.root.after(
                        0,
                        lambda target=label: self._set_server_status(
                            target, "Доступен", COLORS["green"]
                        ),
                    )
                except Exception as error:
                    self.server_test_results[key] = False
                    self.root.after(
                        0,
                        lambda target=label, err=error: self._set_server_status(
                            target, f"Ошибка: {err}", COLORS["red"]
                        ),
                    )
                finally:
                    try:
                        if secure_socket:
                            secure_socket.close()
                        elif raw_socket:
                            raw_socket.close()
                    except Exception:
                        pass

            stopped = stopped or self.server_test_stop_event.is_set()
            self.server_test_running = False

            def finish():
                try:
                    self.server_test_start_btn.config(state=tk.NORMAL)
                    self.server_test_stop_btn.config(state=tk.DISABLED)
                    self.wizard_next_btn.config(state=tk.NORMAL)
                    self.set_status(
                        "ПРОВЕРКА ОСТАНОВЛЕНА" if stopped else "СЕРВЕРЫ ПРОВЕРЕНЫ",
                        COLORS["red"] if stopped else COLORS["green"],
                    )
                except tk.TclError:
                    pass

            try:
                self.root.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def stop_server_availability_test(self):
        if not self.server_test_running:
            return
        self.server_test_stop_event.set()
        self.server_test_stop_btn.config(state=tk.DISABLED)
        self.set_status("ОСТАНОВКА ПРОВЕРКИ", COLORS["red"])

    def _analyze_account_for_summary(self, email_user, password):
        source_connection = None
        message_count = 0
        source_size_mb = 0.0
        try:
            source_connection = self.connect_source(email_user, password)
            folder_names = list_imap_folders(source_connection)

            for folder_name in folder_names:
                result, _, _selected_name = self._select_imap_folder(source_connection, folder_name)
                if result == "OK":
                    search_type, search_data = source_connection.search(None, "ALL")
                    if search_type == "OK" and search_data and search_data[0]:
                        message_count += len(search_data[0].split())

            source_size_mb = self.get_mailbox_size_mb(source_connection, email_user, password)
        finally:
            self._close_connection(source_connection)

        destination_status = "Будет создан"
        local_part = email_user.split("@")[0].strip()
        for login_value in (email_user.strip(), local_part):
            destination_connection = None
            try:
                destination_connection = self.connect_dest(login_value, password)
                destination_status = "Доступен"
                break
            except Exception:
                pass
            finally:
                self._close_connection(destination_connection)
        if destination_status != "Доступен" and not bool(self.runtime_settings.get("auto_create", True)):
            destination_status = "Нет доступа"

        return message_count, source_size_mb, destination_status

    def _start_summary_analysis(self, accounts):
        self.save_settings()
        self.runtime_settings = self._capture_settings()
        signature = self._make_analysis_signature(accounts, self.runtime_settings)
        self.analysis_running = True
        self.analysis_stop_event.clear()
        self.analysis_cache = {}
        self.analysis_cache_signature = signature
        self.use_analysis_cache = False
        for item in self.summary_tree.get_children():
            self.summary_tree.delete(item)
        self.summary_accounts_card.config(text=str(len(accounts)))
        self.summary_messages_card.config(text="...")
        self.summary_volume_card.config(text="...")
        self.summary_progress.config(maximum=max(1, len(accounts)), value=0)
        self.summary_status_label.config(text="Выполняется предварительный анализ ящиков...", fg=COLORS["orange"])
        self._update_route_labels(self.runtime_settings)
        self._show_step(2)

        def worker():
            total_messages = 0
            total_volume = 0.0
            for index, (email_user, password) in enumerate(accounts, start=1):
                if self.analysis_stop_event.is_set():
                    break
                try:
                    messages, volume, destination_status = self._analyze_account_for_summary(email_user, password)
                    self.analysis_cache[email_user] = {
                        "messages": messages,
                        "source_size_mb": volume,
                        "destination_status": destination_status,
                        "ok": True,
                    }
                    total_messages += messages
                    total_volume += volume
                    values = (
                        email_user,
                        messages,
                        f"{volume:.2f} МБ",
                        self.runtime_settings.get("dst_host", ""),
                        destination_status,
                    )
                except Exception as error:
                    self.analysis_cache[email_user] = {"messages": 0, "source_size_mb": 0.0, "ok": False}
                    values = (email_user, "—", "—", self.runtime_settings.get("dst_host", ""), f"Ошибка: {error}")

                try:
                    self.root.after(
                        0,
                        lambda row=values, value=index: (
                            self.summary_tree.insert("", tk.END, values=row),
                            self.summary_progress.config(value=value),
                        ),
                    )
                except tk.TclError:
                    return

            cancelled = self.analysis_stop_event.is_set()
            self.analysis_running = False
            self.use_analysis_cache = not cancelled

            def finish():
                try:
                    if cancelled:
                        self.summary_status_label.config(text="Анализ остановлен.", fg=COLORS["red"])
                    else:
                        self.summary_status_label.config(
                            text="Анализ завершен. Проверьте данные и нажмите «Запустить миграцию».",
                            fg=COLORS["green"],
                        )
                    self.summary_messages_card.config(text=f"{total_messages:,}".replace(",", " "))
                    self.summary_volume_card.config(text=f"{total_volume:.2f} МБ")
                    self._show_step(2)
                except tk.TclError:
                    pass

            try:
                self.root.after(0, finish)
            except tk.TclError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Connection test
    # ------------------------------------------------------------------
    def run_connection_test(self):
        if self.test_running:
            return
        if self.timer_running and not self.stop_requested:
            messagebox.showwarning(
                "Миграция выполняется",
                "Сначала дождитесь завершения миграции или остановите ее.",
            )
            return

        self.save_settings()
        self.runtime_settings = self._capture_settings()
        accounts = self._selected_accounts()
        test_source = bool(self.test_src_var.get())
        test_destination = bool(self.test_dst_var.get())
        auto_create = bool(self.auto_create_var.get())

        if not accounts:
            messagebox.showerror("Ошибка", "Не выбрано ни одного ящика для тестирования.")
            return

        self.test_stop_event.clear()
        self.test_running = True
        self.test_start_btn.config(state=tk.DISABLED)
        self.test_stop_btn.config(state=tk.NORMAL)
        self.start_btn.config(state=tk.DISABLED)
        self.wizard_back_btn.config(state=tk.DISABLED)
        self.wizard_next_btn.config(state=tk.DISABLED)
        self.test_result_label.config(text="Идет проверка выбранных ящиков...", fg=COLORS["orange"])
        self.set_status("ТЕСТИРОВАНИЕ", COLORS["orange"])

        def test_worker():
            stopped = False
            unexpected_error = None
            try:
                self.log(f"\n=== ЗАПУСК ТЕСТИРОВАНИЯ ({len(accounts)} аккаунтов) ===")
                for email_user, password in accounts:
                    if self.test_stop_event.is_set():
                        stopped = True
                        break

                    self.log(f"\nТестирование: {email_user}")
                    if test_source:
                        try:
                            source_conn = self.connect_source(email_user, password)
                            self._close_connection(source_conn)
                            self.log("   [OK] ИСТОЧНИК: успешно.")
                        except Exception as error:
                            self.log(f"   [ОШИБКА] ИСТОЧНИК: {error}")

                    if self.test_stop_event.is_set():
                        stopped = True
                        break

                    if test_destination:
                        local_part = email_user.split("@")[0].strip()
                        login_variants = [email_user.strip(), local_part]
                        destination_ok = False
                        last_destination_error = None

                        for login_value in login_variants:
                            if self.test_stop_event.is_set():
                                stopped = True
                                break
                            try:
                                destination_conn = self.connect_dest(login_value, password)
                                self._close_connection(destination_conn)
                                self.log(
                                    f"   [OK] ПОЛУЧАТЕЛЬ: успешно (логин '{login_value}')."
                                )
                                destination_ok = True
                                break
                            except Exception as error:
                                last_destination_error = error

                        if stopped:
                            break

                        if not destination_ok and auto_create:
                            self.log(
                                f"-> Учетная запись {email_user} не найдена. "
                                "Пробуем создать через Admin API..."
                            )
                            if self.create_or_update_kerio_user(email_user, password):
                                # Kerio may need a moment to publish a newly
                                # created mailbox to its IMAP service.
                                if self.test_stop_event.wait(3.0):
                                    stopped = True
                                    break
                                for login_value in login_variants:
                                    if self.test_stop_event.is_set():
                                        stopped = True
                                        break
                                    try:
                                        destination_conn = self.connect_dest(login_value, password)
                                        self._close_connection(destination_conn)
                                        self.log(
                                            "   [OK] Повторная авторизация после создания успешна "
                                            f"(логин '{login_value}')."
                                        )
                                        destination_ok = True
                                        break
                                    except Exception as error:
                                        last_destination_error = error
                                if stopped:
                                    break
                                if not destination_ok:
                                    self.log(
                                        "   [ОШИБКА] Ящик создан через API, но IMAP-авторизация "
                                        f"не прошла. Последняя ошибка: {last_destination_error}"
                                    )
                            else:
                                self.log(
                                    "   [ОШИБКА] Admin API не создал или не обновил учетную запись."
                                )

                        if not destination_ok and not stopped:
                            if last_destination_error is not None:
                                self.log(
                                    "   [ОШИБКА] Авторизация на Kerio не прошла. "
                                    f"Последняя ошибка: {last_destination_error}"
                                )
                            else:
                                self.log("   [ОШИБКА] Авторизация на Kerio не прошла.")

                stopped = stopped or self.test_stop_event.is_set()
                if stopped:
                    self.log("\n=== ТЕСТИРОВАНИЕ ОСТАНОВЛЕНО ===")
                else:
                    self.log("\n=== ТЕСТИРОВАНИЕ ЗАВЕРШЕНО ===")
            except Exception as error:
                unexpected_error = error
                self.log(f"\n[КРИТИЧЕСКАЯ ОШИБКА ТЕСТИРОВАНИЯ]: {error}")
            finally:
                self.test_running = False
                self.test_stop_event.clear()
                status_text = "ОСТАНОВЛЕНО" if stopped else ("ОШИБКА" if unexpected_error else "ГОТОВ К ЗАПУСКУ")
                status_color = COLORS["red"] if stopped or unexpected_error else COLORS["dark_3"]
                self.set_status(status_text, status_color)

                def finish_test_ui():
                    try:
                        self.test_start_btn.config(state=tk.NORMAL)
                        self.test_stop_btn.config(state=tk.DISABLED)
                        if unexpected_error:
                            self.test_result_label.config(text="Тест завершился с ошибкой.", fg=COLORS["red"])
                        elif stopped:
                            self.test_result_label.config(text="Тестирование остановлено.", fg=COLORS["red"])
                        else:
                            self.test_result_label.config(text="Тестирование завершено. Результаты записаны в лог.", fg=COLORS["green"])
                        if self.current_step == 1:
                            self.wizard_back_btn.config(state=tk.NORMAL)
                            self.wizard_next_btn.config(state=tk.NORMAL)
                        if not self.timer_running:
                            self.start_btn.config(state=tk.NORMAL)
                        if unexpected_error:
                            messagebox.showerror("Ошибка тестирования", str(unexpected_error))
                        elif stopped:
                            messagebox.showinfo("Тестирование остановлено", "Проверка была остановлена.")
                        else:
                            messagebox.showinfo("Тест завершен", "Проверка подключений завершена.")
                    except tk.TclError:
                        pass

                try:
                    self.root.after(0, finish_test_ui)
                except tk.TclError:
                    pass

        threading.Thread(target=test_worker, daemon=True).start()

    def stop_connection_test(self):
        if not self.test_running:
            return
        self.test_stop_event.set()
        self.set_status("ОСТАНОВКА ТЕСТА...", COLORS["red"])
        self.log("\n[ВНИМАНИЕ] Запрос на остановку тестирования...")
        self.test_stop_btn.config(state=tk.DISABLED)

        # Closing active IMAP sockets helps interrupt a long login/command.
        with self.connection_lock:
            connections = list(self.active_connections)
        for connection in connections:
            try:
                connection.logout()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Migration lifecycle and progress UI
    # ------------------------------------------------------------------
    def start_migration_thread(self):
        self.save_settings()
        self.runtime_settings = self._capture_settings()
        selected_speed = self.runtime_settings.get("speed_limit", "Без ограничений")
        self.rate_limiter = NetworkRateLimiter(speed_limit_to_bits_per_second(selected_speed))
        accounts = self._selected_accounts()
        self.use_analysis_cache = (
            bool(self.analysis_cache)
            and self.analysis_cache_signature == self._make_analysis_signature(accounts, self.runtime_settings)
            and all(self.analysis_cache.get(email_user, {}).get("ok") for email_user, _ in accounts)
        )

        if not accounts:
            messagebox.showerror("Ошибка", "Не выбрано ни одного ящика для миграции.")
            return

        try:
            max_threads = max(1, min(10, int(self.threads_spin.get())))
        except (TypeError, ValueError):
            max_threads = 4
            self.threads_spin.set(4)

        self.stop_requested = False
        self.process_finished = False
        self.migration_outcome = "running"
        self._update_route_labels(self.runtime_settings)
        self.process_status_label.config(text=f"Подготовка • лимит: {selected_speed}", fg=COLORS["orange"])
        self.wizard_back_btn.config(state=tk.DISABLED)
        self.wizard_next_btn.config(text="Выполняется...", state=tk.DISABLED)
        self.log(f"[НАСТРОЙКА] Лимит сетевой скорости: {selected_speed}; потоков: {max_threads}")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.test_start_btn.config(state=tk.DISABLED)
        self.speed_limit_combo.config(state=tk.DISABLED)
        self.global_progress["value"] = 0
        self.stats_label.config(
            text="Подготовка... Всего: 0  |  Скопировано: 0  |  Пропущено: 0  |  С ошибками: 0  |  0,0%  |  Скорость: 0,0 пис/сек | 0 пис/мин  |  Старт: —  |  Прошло: 00:00:00  |  Осталось (расч.): —  |  Завершение (расч.): —"
        )
        self.set_status("ПОДГОТОВКА", COLORS["orange"])

        account_data_for_ui = self._prepare_account_progress(accounts)
        self.migration_start_time = datetime.now()
        self.timer_running = True
        with self.stats_lock:
            self.total_msgs_cache = 0
            self.copied_msgs_cache = 0
            self.skipped_msgs_cache = 0
            self.error_msgs_cache = 0
        self.last_stats_text = ""

        # Progress/statistics are rendered by one throttled Tk callback. This
        # avoids creating a Tk event for every message in a large mailbox.
        threading.Thread(
            target=self.run_migration,
            args=(accounts, account_data_for_ui, max_threads),
            daemon=True,
        ).start()

    def _prepare_account_progress(self, accounts):
        for widget in self.accounts_inner_frame.winfo_children():
            widget.destroy()

        account_data = {}
        with self.progress_ui_lock:
            self.account_progress_states.clear()
            self.account_progress_display_order = ()

        for order_index, (email_user, _password) in enumerate(accounts):
            row = tk.Frame(self.accounts_inner_frame, bg=COLORS["panel"])
            row.pack(fill=tk.X, pady=2, padx=4)
            row.columnconfigure(1, weight=1)

            label = tk.Label(
                row,
                text=f"{email_user}  •  ожидание",
                anchor="w",
                bg=COLORS["panel"],
                fg=COLORS["text"],
                font=(FONT, 9),
            )
            label.grid(row=0, column=0, sticky="w", padx=(0, 8))

            progress = ttk.Progressbar(
                row,
                orient="horizontal",
                mode="determinate",
                style="Pending.Horizontal.TProgressbar",
            )
            progress.grid(row=0, column=1, sticky="ew")
            progress.configure(maximum=1, value=0)
            account_data[email_user] = (label, progress)

            with self.progress_ui_lock:
                self.account_progress_states[id(label)] = {
                    "row": row,
                    "label": label,
                    "progress": progress,
                    "text": f"{email_user}  •  ожидание",
                    "status": "pending",
                    "order_index": order_index,
                    "maximum": 1,
                    "value": 0,
                    "shown_text": None,
                    "shown_status": None,
                    "shown_maximum": None,
                    "shown_value": None,
                }

        self.accounts_progress_canvas.yview_moveto(0)
        self.accounts_inner_frame.update_idletasks()
        return account_data

    def _set_account_row(self, label, progress, text=None, maximum=None, value=None, status=None):
        """Store row state; the Tk thread paints it in batches."""
        with self.progress_ui_lock:
            state = self.account_progress_states.get(id(label))
            if state is None:
                return
            if text is not None:
                state["text"] = text
            if status is not None:
                state["status"] = status
            if maximum is not None:
                state["maximum"] = max(1, maximum)
            if value is not None:
                state["value"] = max(0, value)

    def _refresh_progress_ui(self):
        """Paint coalesced progress values from the Tk main thread.

        The migration workers can call their callbacks tens of thousands of
        times. This method runs only four times per second, so the number of
        pending Tk callbacks stays constant instead of growing with message
        count and consuming gigabytes of memory.
        """
        try:
            with self.stats_lock:
                total = self.total_msgs_cache
                copied = self.copied_msgs_cache
                skipped = self.skipped_msgs_cache
                errors = self.error_msgs_cache
            processed = copied + skipped + errors

            if self.migration_start_time:
                elapsed = datetime.now() - self.migration_start_time
                hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                elapsed_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                start_text = self.migration_start_time.strftime("%H:%M:%S")
                elapsed_seconds = max(elapsed.total_seconds(), 0.001)
                percent = (processed / total) * 100.0 if total > 0 else 0.0
                percent = min(percent, 100.0)
                speed_text = format_migration_speed(processed, elapsed_seconds)
                if total > 0 and processed > 0 and processed < total:
                    remaining_seconds = (total - processed) / (processed / elapsed_seconds)
                    remaining_text = format_duration(remaining_seconds)
                    finish_text = (datetime.now() + timedelta(seconds=remaining_seconds)).strftime("%H:%M")
                elif total > 0 and processed >= total:
                    remaining_text = "00:00:00"
                    finish_text = datetime.now().strftime("%H:%M")
                else:
                    remaining_text = "—"
                    finish_text = "—"
                stat_text = (
                    f"Всего: {total}  |  Скопировано: {copied}  |  Пропущено: {skipped}  |  С ошибками: {errors}  |  "
                    f"{percent:.1f}%  |  Скорость: {speed_text}  |  Старт: {start_text}  |  "
                    f"Прошло: {elapsed_text}  |  Осталось (расч.): {remaining_text}  |  "
                    f"Завершение (расч.): {finish_text}"
                )
                if stat_text != self.last_stats_text:
                    self.stats_label.config(text=stat_text)
                    self.last_stats_text = stat_text
                self.global_progress.config(
                    maximum=max(total, 1),
                    value=min(processed, max(total, 1)),
                )

            status_order = {"running": 0, "error": 1, "pending": 2, "done": 3}
            status_styles = {
                "running": "Running.Horizontal.TProgressbar",
                "error": "Error.Horizontal.TProgressbar",
                "pending": "Pending.Horizontal.TProgressbar",
                "done": "Green.Horizontal.TProgressbar",
            }
            with self.progress_ui_lock:
                states = list(self.account_progress_states.values())
                ordered_states = sorted(
                    states,
                    key=lambda state: (
                        status_order.get(state.get("status"), 2),
                        state.get("order_index", 0),
                    ),
                )
                display_order = tuple(id(state["row"]) for state in ordered_states)
                reorder_rows = display_order != self.account_progress_display_order
                self.account_progress_display_order = display_order
                snapshots = []
                for state in ordered_states:
                    snapshots.append(
                        (
                            state["row"],
                            state["label"],
                            state["progress"],
                            state["text"],
                            state["status"],
                            status_styles.get(state["status"], status_styles["pending"]),
                            state["maximum"],
                            state["value"],
                        )
                    )

            if reorder_rows:
                for row, _label, _progress, _text, _status, _style, _maximum, _value in snapshots:
                    row.pack_forget()
                for row, _label, _progress, _text, _status, _style, _maximum, _value in snapshots:
                    row.pack(fill=tk.X, pady=2, padx=4)

            for row, label, progress, text, status, style_name, maximum, value in snapshots:
                if text != self.account_progress_states.get(id(label), {}).get("shown_text"):
                    label.config(text=text)
                    with self.progress_ui_lock:
                        state = self.account_progress_states.get(id(label))
                        if state is not None:
                            state["shown_text"] = text
                if status != self.account_progress_states.get(id(label), {}).get("shown_status"):
                    progress.config(style=style_name)
                    with self.progress_ui_lock:
                        state = self.account_progress_states.get(id(label))
                        if state is not None:
                            state["shown_status"] = status
                if maximum != self.account_progress_states.get(id(label), {}).get("shown_maximum"):
                    progress.config(maximum=max(1, maximum))
                    with self.progress_ui_lock:
                        state = self.account_progress_states.get(id(label))
                        if state is not None:
                            state["shown_maximum"] = maximum
                if value != self.account_progress_states.get(id(label), {}).get("shown_value"):
                    progress.config(value=max(0, min(value, max(1, maximum))))
                    with self.progress_ui_lock:
                        state = self.account_progress_states.get(id(label))
                        if state is not None:
                            state["shown_value"] = value
        except tk.TclError:
            return

        try:
            self.root.after(self.progress_refresh_ms, self._refresh_progress_ui)
        except tk.TclError:
            pass

    def update_timer_thread(self):
        while self.timer_running and not self.stop_requested:
            if self.migration_start_time:
                elapsed = datetime.now() - self.migration_start_time
                hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
                minutes, seconds = divmod(remainder, 60)
                elapsed_text = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
                start_text = self.migration_start_time.strftime("%H:%M:%S")

                with self.stats_lock:
                    total = self.total_msgs_cache
                    copied = self.copied_msgs_cache
                    skipped = self.skipped_msgs_cache
                    errors = self.error_msgs_cache
                processed = copied + skipped + errors
                elapsed_seconds = max(elapsed.total_seconds(), 0.001)
                percent = (processed / total) * 100.0 if total > 0 else 0.0
                percent = min(percent, 100.0)
                speed_text = format_migration_speed(processed, elapsed_seconds)
                if total > 0 and processed > 0 and processed < total:
                    remaining_seconds = (total - processed) / (processed / elapsed_seconds)
                    remaining_text = format_duration(remaining_seconds)
                    finish_text = (datetime.now() + timedelta(seconds=remaining_seconds)).strftime("%H:%M")
                elif total > 0 and processed >= total:
                    remaining_text = "00:00:00"
                    finish_text = datetime.now().strftime("%H:%M")
                else:
                    remaining_text = "—"
                    finish_text = "—"
                stat_text = (
                    f"Всего: {total}  |  Скопировано: {copied}  |  Пропущено: {skipped}  |  С ошибками: {errors}  |  "
                    f"{percent:.1f}%  |  Скорость: {speed_text}  |  Старт: {start_text}  |  "
                    f"Прошло: {elapsed_text}  |  Осталось (расч.): {remaining_text}  |  "
                    f"Завершение (расч.): {finish_text}"
                )

                try:
                    self.root.after(0, lambda text=stat_text: self.stats_label.config(text=text))
                except tk.TclError:
                    break
            time.sleep(1.0)

    def stop_migration(self):
        if self.stop_requested:
            return
        self.stop_requested = True
        self.timer_running = False
        self.set_status("ОСТАНОВКА...", COLORS["red"])
        self.log("\n[ВНИМАНИЕ] Запрос на остановку. Ожидается завершение текущей операции...")

        with self.connection_lock:
            connections = list(self.active_connections)
        for connection in connections:
            try:
                connection.logout()
            except Exception:
                pass
        self.stop_btn.config(state=tk.DISABLED)

    # ------------------------------------------------------------------
    # IMAP connections
    # ------------------------------------------------------------------
    def _port(self, widget, default=993):
        try:
            return int(str(self._setting("port", widget, str(default))).strip())
        except (TypeError, ValueError):
            return default

    def _register_connection(self, connection):
        with self.connection_lock:
            self.active_connections.append(connection)

    def _close_connection(self, connection):
        if connection is None:
            return
        try:
            connection.logout()
        except Exception:
            pass
        with self.connection_lock:
            try:
                self.active_connections.remove(connection)
            except ValueError:
                pass

    def connect_source(self, user, password):
        src_host = str(self._setting("src_host", self.src_host, "imap.yandex.ru")).strip()
        try:
            src_port = int(str(self._setting("src_port", self.src_port, "993")).strip())
        except (TypeError, ValueError):
            src_port = 993
        src_ssl = self._setting_bool("src_ssl", self.src_ssl, True)

        connection = imaplib.IMAP4_SSL(src_host, src_port) if src_ssl else imaplib.IMAP4(src_host, src_port)
        connection._encoding = "utf-8"
        # Set the timeout before login too, so the Stop test button can
        # interrupt a server that is not answering.
        if connection.sock:
            connection.sock.settimeout(180)
        connection.login(user.strip(), password)
        self._register_connection(connection)
        return connection

    def connect_dest(self, user, password):
        dst_host = str(self._setting("dst_host", self.dst_host, "m.technograd.by")).strip()
        try:
            dst_port = int(str(self._setting("dst_port", self.dst_port, "993")).strip())
        except (TypeError, ValueError):
            dst_port = 993
        dst_ssl = self._setting_bool("dst_ssl", self.dst_ssl, True)

        connection = imaplib.IMAP4_SSL(dst_host, dst_port) if dst_ssl else imaplib.IMAP4(dst_host, dst_port)
        connection._encoding = "utf-8"
        if connection.sock:
            connection.sock.settimeout(180)
        connection.login(user.strip(), password)
        self._register_connection(connection)
        return connection

    # ------------------------------------------------------------------
    # Kerio admin API
    # ------------------------------------------------------------------
    def create_or_update_kerio_user(self, email_user, password, full_name=None):
        dst_host = str(self._setting("dst_host", self.dst_host, "")).strip()
        admin_user = str(self._setting("adm_user", self.adm_user, "admin")).strip()
        admin_password = self._setting("adm_pass", self.adm_pass, "")

        if not admin_password:
            self.log("   [ПРОПУСК АВТОСОЗДАНИЯ] Не указан пароль администратора Kerio.")
            return False

        parts = email_user.split("@")
        if len(parts) != 2:
            self.log(f"   [ОШИБКА] Некорректный email: {email_user}")
            return False

        full_email = email_user.strip().lower()
        local_part = parts[0].strip().lower()
        domain_name = parts[1].strip().lower()
        full_name_value = str(full_name or self._account_full_name(email_user) or local_part).strip()

        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        base_url = f"https://{dst_host}:4040/admin/api/jsonrpc/"
        self.log(f"   [ИНФО API KERIO] Подключение к {base_url}; администратор: {admin_user}")

        try:
            cookie_jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cookie_jar),
                urllib.request.HTTPSHandler(context=context),
            )

            # Some Kerio installations require the administrator login with
            # its domain suffix (admin@domain.tld), while others accept just
            # admin. Try both forms without exposing the password in the log.
            admin_logins = [admin_user]
            if "@" not in admin_user and domain_name:
                admin_logins.append(f"{admin_user}@{domain_name}")

            token = None
            login_errors = []
            for login_name in dict.fromkeys(admin_logins):
                login_payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "Session.login",
                    "params": {
                        "userName": login_name,
                        "password": admin_password,
                        "application": {
                            "name": "IMAPMigrator",
                            "vendor": "Admin",
                            "version": VERSION,
                        },
                    },
                }
                request = urllib.request.Request(
                    base_url,
                    data=json.dumps(login_payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json-rpc; charset=UTF-8",
                        "Accept": "application/json-rpc",
                    },
                )
                try:
                    with opener.open(request, timeout=10) as response:
                        response_data = json.loads(response.read().decode("utf-8"))
                except Exception as error:
                    login_errors.append(f"{login_name}: {format_exception(error)}")
                    continue

                login_result = response_data.get("result") if isinstance(response_data, dict) else None
                candidate_token = login_result.get("token") if isinstance(login_result, dict) else None
                if candidate_token:
                    token = candidate_token
                    if login_name != admin_user:
                        self.log(f"   [ИНФО API KERIO] Использован логин администратора '{login_name}'.")
                    break
                login_errors.append(f"{login_name}: {format_api_error(response_data)}")

            if not token:
                self.log(
                    "   [ОШИБКА API KERIO] Не удалось войти под администратором. "
                    + "; ".join(login_errors)
                )
                return False

            domain_payload = {
                "jsonrpc": "2.0",
                "id": 2,
                "token": token,
                "method": "Domains.get",
                "params": {"query": {"fields": ["id", "name"]}},
            }
            request = urllib.request.Request(
                base_url,
                data=json.dumps(domain_payload).encode("utf-8"),
                headers={"Content-Type": "application/json-rpc; charset=UTF-8", "Accept": "application/json-rpc", "X-Token": token},
            )
            domain_id = None
            with opener.open(request, timeout=10) as response:
                domain_data = json.loads(response.read().decode("utf-8"))
            if "error" in domain_data:
                self.log(f"   [ОШИБКА API KERIO] Domains.get: {format_api_error(domain_data)}")
                return False
            domain_result = domain_data.get("result") if isinstance(domain_data, dict) else None
            for domain in (domain_result.get("list", []) if isinstance(domain_result, dict) else []):
                if domain.get("name", "").lower() == domain_name:
                    domain_id = domain.get("id")
                    break

            if not domain_id:
                self.log(
                    f"   [ОШИБКА API KERIO] Домен '{domain_name}' не найден. "
                    "Проверьте, что домен из email уже добавлен в Kerio."
                )
                return False

            users_payload = {
                "jsonrpc": "2.0",
                "id": 3,
                "token": token,
                "method": "Users.get",
                "params": {
                    "domainId": domain_id,
                    "query": {
                        "fields": ["id", "loginName", "emailAddresses"],
                        "start": 0,
                        "limit": -1,
                    },
                },
            }
            request = urllib.request.Request(
                base_url,
                data=json.dumps(users_payload).encode("utf-8"),
                headers={"Content-Type": "application/json-rpc; charset=UTF-8", "Accept": "application/json-rpc", "X-Token": token},
            )
            existing_user_id = None
            with opener.open(request, timeout=10) as response:
                users_data = json.loads(response.read().decode("utf-8"))
            if "error" in users_data:
                self.log(f"   [ОШИБКА API KERIO] Users.get: {format_api_error(users_data)}")
                return False
            users_result = users_data.get("result") if isinstance(users_data, dict) else None
            for user in (users_result.get("list", []) if isinstance(users_result, dict) else []):
                login_name = str(user.get("loginName", "")).lower()
                raw_addresses = user.get("emailAddresses", []) or []
                if isinstance(raw_addresses, str):
                    raw_addresses = [raw_addresses]
                email_addresses = [str(item).lower() for item in raw_addresses]
                if login_name in (local_part, full_email) or full_email in email_addresses:
                    existing_user_id = user.get("id")
                    break

            if existing_user_id:
                self.log(
                    f"   [ИНФО] Учетная запись {email_user} существует. "
                    f"Обновляем пароль и полное имя '{full_name_value}'..."
                )
                set_payload = {
                    "jsonrpc": "2.0",
                    "id": 4,
                    "token": token,
                    "method": "Users.set",
                    "params": {
                        "userIds": [existing_user_id],
                        "pattern": {
                            "password": password,
                            "fullName": full_name_value,
                            "isEnabled": True,
                        },
                    },
                }
                request = urllib.request.Request(
                    base_url,
                    data=json.dumps(set_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json-rpc; charset=UTF-8", "Accept": "application/json-rpc", "X-Token": token},
                )
                with opener.open(request, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if "error" in result:
                    self.log(f"   [ОШИБКА ОБНОВЛЕНИЯ ПАРОЛЯ]: {format_api_error(result)}")
                    return False
                result_data = result.get("result") if isinstance(result, dict) else None
                if isinstance(result_data, dict) and result_data.get("errors"):
                    self.log(f"   [ОШИБКА ОБНОВЛЕНИЯ ПАРОЛЯ]: {result_data['errors']}")
                    return False
                self.log(f"   [OK] Пароль для {email_user} обновлен в Kerio Connect.")
            else:
                self.log(
                    f"   [ИНФО] Создаем учетную запись '{local_part}' "
                    f"с fullName='{full_name_value}' и domainId={domain_id}..."
                )
                create_payload = {
                    "jsonrpc": "2.0",
                    "id": 5,
                    "token": token,
                    "method": "Users.create",
                    "params": {
                        "users": [
                            {
                                # Kerio Connect expects domainId in the user
                                # entity for Users.create. It is not a separate
                                # method argument in the Connect API.
                                "domainId": domain_id,
                                "loginName": local_part,
                                "fullName": full_name_value,
                                "password": password,
                                "isEnabled": True,
                            }
                        ],
                    },
                }
                request = urllib.request.Request(
                    base_url,
                    data=json.dumps(create_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json-rpc; charset=UTF-8", "Accept": "application/json-rpc", "X-Token": token},
                )
                with opener.open(request, timeout=10) as response:
                    result = json.loads(response.read().decode("utf-8"))
                if "error" in result:
                    self.log(f"   [ОШИБКА СОЗДАНИЯ KERIO]: {format_api_error(result)}")
                    return False
                result_data = result.get("result") if isinstance(result, dict) else None
                if isinstance(result_data, dict) and result_data.get("errors"):
                    self.log(f"   [ОШИБКА СОЗДАНИЯ KERIO (API Errors)]: {result_data['errors']}")
                    return False
                if not isinstance(result_data, (dict, list)):
                    self.log(f"   [ОШИБКА СОЗДАНИЯ KERIO]: {format_api_error(result)}")
                    return False
                self.log(f"   [OK] Учетная запись {email_user} успешно создана.")

            logout_payload = {
                "jsonrpc": "2.0",
                "id": 99,
                "token": token,
                "method": "Session.logout",
                "params": {},
            }
            try:
                logout_request = urllib.request.Request(
                    base_url,
                    data=json.dumps(logout_payload).encode("utf-8"),
                    headers={"Content-Type": "application/json-rpc; charset=UTF-8", "Accept": "application/json-rpc", "X-Token": token},
                )
                opener.open(logout_request, timeout=5).close()
            except Exception:
                pass
            return True

        except Exception as error:
            self.log(f"   [ОШИБКА ADMIN API KERIO]: {format_exception(error)}")
            return False

    def test_credentials_and_prepare(self, email_user, password):
        self.log(f"-> Тест подключения к источнику для {email_user}...")
        try:
            source_test = self.connect_source(email_user, password)
            self._close_connection(source_test)
            self.log("   [OK] Источник доступен.")
        except Exception as error:
            self.log(f"   [ОШИБКА] Не удалось войти на сервер-источник для {email_user}: {error}")
            return False

        self.log(f"-> Тест подключения к получателю (Kerio) для {email_user}...")
        destination_connected = False
        local_part = email_user.split("@")[0].strip()
        login_variants = [email_user.strip(), local_part]

        for login_value in login_variants:
            try:
                destination_test = self.connect_dest(login_value, password)
                self._close_connection(destination_test)
                self.log(f"   [OK] Получатель доступен (логин: '{login_value}').")
                destination_connected = True
                break
            except Exception:
                pass

        if not destination_connected:
            if self._setting_bool("auto_create", self.auto_create_var, True):
                self.log(f"-> Учетная запись {email_user} отсутствует. Создаем через Admin API...")
                if self.create_or_update_kerio_user(email_user, password):
                    try:
                        time.sleep(3.0)
                        for login_value in login_variants:
                            try:
                                destination_test = self.connect_dest(login_value, password)
                                self._close_connection(destination_test)
                                self.log("   [OK] Повторный тест подключения успешен.")
                                destination_connected = True
                                break
                            except Exception:
                                pass
                        if not destination_connected:
                            self.log("   [ОШИБКА] Ящик создан через API, но IMAP-логин не прошел.")
                    except Exception as error:
                        self.log(f"   [ОШИБКА] Ошибка при повторном подключении: {error}")
                else:
                    return False
            else:
                self.log("   [ОШИБКА] Учетная запись не найдена на Kerio, автосоздание отключено.")
                return False

        return destination_connected

    # ------------------------------------------------------------------
    # Mailbox inspection and migration
    # ------------------------------------------------------------------
    def get_mailbox_size_mb(self, connection, email_user, password):
        total_bytes = 0
        try:
            response_type, data = connection.getquotaroot("INBOX")
            if response_type == "OK" and data:
                for response in data:
                    if isinstance(response, bytes):
                        response_text = response.decode("utf-8", errors="ignore")
                        match = re.search(r"STORAGE\s+(\d+)\s+(\d+)", response_text, re.IGNORECASE)
                        if match:
                            used_kb = int(match.group(1))
                            return round(used_kb / 1024.0, 2)
        except Exception:
            pass

        try:
            folder_names = list_imap_folders(connection)
            if not folder_names:
                return 0.0

            for decoded in folder_names:
                try:
                    connection._encoding = "utf-8"
                    result, _data, _selected_name = self._select_imap_folder(connection, decoded)
                    if result != "OK":
                        continue

                    fetch_type, fetch_data = connection.fetch("1:*", "(RFC822.SIZE)")
                    if fetch_type == "OK" and fetch_data:
                        for part in fetch_data:
                            if isinstance(part, bytes):
                                size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", part)
                                if size_match:
                                    total_bytes += int(size_match.group(1))
                except Exception:
                    pass
        except Exception:
            pass

        return round(total_bytes / (1024 * 1024), 2)

    def _collect_existing_signatures(self, connection, message_numbers):
        """Read destination headers in bounded batches for faster deduplication."""
        existing_messages = set()
        existing_signatures = set()
        numbers = message_numbers[0] if isinstance(message_numbers, (list, tuple)) else message_numbers
        if not numbers:
            return existing_messages, existing_signatures
        if isinstance(numbers, bytes):
            numbers = numbers.split()
        else:
            numbers = str(numbers).split()
        numbers = [
            number.decode("ascii", errors="ignore") if isinstance(number, bytes) else str(number)
            for number in numbers
        ]

        fetch_spec = "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID SUBJECT DATE FROM)])"

        def add_fetch_data(fetch_data):
            for response_part in fetch_data or []:
                if not isinstance(response_part, tuple) or len(response_part) < 2:
                    continue
                metadata = response_part[0]
                header_bytes = response_part[1] or b""
                if not isinstance(metadata, bytes):
                    metadata = str(metadata).encode("utf-8", errors="ignore")
                if not isinstance(header_bytes, bytes):
                    header_bytes = str(header_bytes).encode("utf-8", errors="ignore")

                message_id = None
                subject = ""
                date_value = ""
                from_value = ""
                id_match = re.search(rb"Message-ID:\s*(<[^>]+>)", header_bytes, re.IGNORECASE)
                if id_match:
                    message_id = id_match.group(1).decode("utf-8", errors="ignore").strip().lower()
                subject_match = re.search(rb"Subject:\s*([^\r\n]+)", header_bytes, re.IGNORECASE)
                if subject_match:
                    subject = subject_match.group(1).decode("utf-8", errors="ignore").strip().lower()
                date_match = re.search(rb"Date:\s*([^\r\n]+)", header_bytes, re.IGNORECASE)
                if date_match:
                    date_value = date_match.group(1).decode("utf-8", errors="ignore").strip().lower()
                from_match = re.search(rb"From:\s*([^\r\n]+)", header_bytes, re.IGNORECASE)
                if from_match:
                    from_value = from_match.group(1).decode("utf-8", errors="ignore").strip().lower()

                size = 0
                size_match = re.search(rb"RFC822\.SIZE\s+(\d+)", metadata)
                if size_match:
                    size = int(size_match.group(1))
                if message_id:
                    existing_messages.add(message_id)
                if subject or date_value or from_value:
                    existing_signatures.add(f"{subject}|{date_value}|{from_value}|{size}")

        # Batches of 50 cut network round trips while keeping memory use
        # predictable for very large destination mailboxes.
        batch_size = 50
        for start in range(0, len(numbers), batch_size):
            if self.stop_requested:
                break
            batch = numbers[start:start + batch_size]
            sequence = ",".join(batch)
            try:
                fetch_result, fetch_data = connection.fetch(sequence, fetch_spec)
                if fetch_result == "OK":
                    add_fetch_data(fetch_data)
                    continue
            except Exception:
                pass

            # Compatibility fallback for servers that reject a multi-number
            # sequence set in FETCH.
            for number in batch:
                if self.stop_requested:
                    break
                try:
                    fetch_result, fetch_data = connection.fetch(number, fetch_spec)
                    if fetch_result == "OK":
                        add_fetch_data(fetch_data)
                except Exception:
                    pass

        return existing_messages, existing_signatures

    def _select_imap_folder(self, connection, folder_name):
        """Select a folder using common Yandex/Kerio hierarchy variants."""
        name_variants = [folder_name]
        if "|" in folder_name:
            name_variants.append(folder_name.replace("|", "/"))
        if "/" in folder_name:
            name_variants.append(folder_name.replace("/", "|"))

        select_names = []
        for name in name_variants:
            encoded = encode_imap_folder_name(name)
            for select_name in (encoded, f'"{encoded}"', name, f'"{name}"'):
                if select_name not in select_names:
                    select_names.append(select_name)

        last_result = None
        last_data = None
        for select_name in select_names:
            try:
                result, data = connection.select(select_name, readonly=True)
                last_result, last_data = result, data
                if result == "OK":
                    return result, data, select_name
            except Exception:
                continue
        return last_result, last_data, None

    def migrate_account(
        self,
        email_user,
        password,
        progress_callback=None,
        copied_callback=None,
        skipped_callback=None,
        error_callback=None,
        account_progress_callback=None,
    ):
        logs_dir = "logs"
        os.makedirs(logs_dir, exist_ok=True)

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_email = re.sub(r"[^a-zA-Z0-9_.-]", "_", email_user)
        log_file_path = os.path.join(logs_dir, f"log_{safe_email}_{timestamp}.txt")

        self.log("\n========================================", log_file_path)
        self.log(f"Миграция аккаунта ({VERSION}): {email_user}", log_file_path)

        if not self.test_credentials_and_prepare(email_user, password):
            self.log(f"[ПРОПУСК] Аккаунт {email_user} пропущен.", log_file_path)
            return 0, 0, 0.0, 0.0

        source_connection = None
        destination_connection = None
        total_account_messages = 0
        migrated_account_messages = 0

        try:
            source_connection = self.connect_source(email_user, password)
            parts = email_user.split("@")
            login_variants = [email_user.strip(), parts[0].strip()]
            for login_value in login_variants:
                try:
                    destination_connection = self.connect_dest(login_value, password)
                    break
                except Exception:
                    pass

            if destination_connection is None:
                raise RuntimeError("Не удалось подключиться к Kerio после проверки учетных данных")

            source_size_mb = self.get_mailbox_size_mb(source_connection, email_user, password)

            try:
                source_connection.noop()
            except Exception:
                self._close_connection(source_connection)
                source_connection = self.connect_source(email_user, password)

            parsed_folders = sorted(
                set(list_imap_folders(source_connection)),
                key=lambda item: (item.count("/"), item),
            )
            self.log(
                "[ПАПКИ IMAP] Найдены: " + (", ".join(parsed_folders) if parsed_folders else "нет"),
                log_file_path,
            )

            for decoded_folder_name in parsed_folders:
                if self.stop_requested:
                    self.log("\n[ОСТАНОВЛЕНО ПОЛЬЗОВАТЕЛЕМ]", log_file_path)
                    break

                try:
                    try:
                        source_connection.noop()
                    except Exception:
                        self._close_connection(source_connection)
                        source_connection = self.connect_source(email_user, password)

                    try:
                        destination_connection.noop()
                    except Exception:
                        self._close_connection(destination_connection)
                        destination_connection = None
                        for login_value in login_variants:
                            try:
                                destination_connection = self.connect_dest(login_value, password)
                                break
                            except Exception:
                                pass
                        if destination_connection is None:
                            raise RuntimeError("Не удалось переподключиться к Kerio")

                    source_connection._encoding = "utf-8"
                    result, _data, imap_encoded_folder = self._select_imap_folder(
                        source_connection,
                        decoded_folder_name,
                    )
                    if result != "OK":
                        self.log(
                            f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось открыть IMAP-папку "
                            f"'{decoded_folder_name}'. Ответ: {result}",
                            log_file_path,
                        )
                        continue

                    response_type, message_numbers = source_connection.search(None, "ALL")
                    if response_type != "OK":
                        self.log(
                            f"[ПРЕДУПРЕЖДЕНИЕ] Не удалось получить письма из "
                            f"папки '{decoded_folder_name}'. Ответ: {response_type}",
                            log_file_path,
                        )
                        continue
                    ids = message_numbers[0].split()
                    folder_total = len(ids)
                    total_account_messages += folder_total

                    kerio_folder_name = decoded_folder_name.replace("|", "/")
                    current_path = ""
                    destination_connection._encoding = "utf-8"
                    for part in kerio_folder_name.split("/"):
                        if not part:
                            continue
                        current_path = f"{current_path}/{part}" if current_path else part
                        current_encoded = encode_imap_folder_name(current_path)
                        try:
                            destination_connection.create(current_encoded)
                        except Exception:
                            try:
                                destination_connection.create(f'"{current_encoded}"')
                            except Exception:
                                pass

                    kerio_encoded = encode_imap_folder_name(kerio_folder_name)
                    # A set is substantially smaller than {message_id: True}
                    # for large mailboxes. Header FETCH is batched in bounded
                    # chunks to avoid one network round trip per destination
                    # message while keeping memory usage predictable.
                    existing_messages = set()
                    existing_signatures = set()
                    try:
                        destination_connection._encoding = "utf-8"
                        destination_result, _ = destination_connection.select(kerio_encoded, readonly=True)
                        if destination_result != "OK":
                            destination_connection.select(f'"{kerio_encoded}"', readonly=True)
                        destination_type, destination_numbers = destination_connection.search(None, "ALL")
                        if destination_type == "OK" and destination_numbers and destination_numbers[0]:
                            (
                                existing_messages,
                                existing_signatures,
                            ) = self._collect_existing_signatures(
                                destination_connection,
                                destination_numbers,
                            )
                    except Exception:
                        pass

                    success_count = 0
                    skipped_duplicates = 0
                    use_kerio_internal_date = True
                    date_fallback_logged = False

                    for number in ids:
                        if self.stop_requested:
                            break

                        success_appended = False
                        for attempt in range(3):
                            try:
                                fetch_type, message_data = source_connection.fetch(
                                    number, "(RFC822 FLAGS INTERNALDATE)"
                                )
                                if fetch_type != "OK":
                                    break

                                raw_message = None
                                internal_date = None
                                source_flags = ()
                                for response_part in message_data:
                                    if isinstance(response_part, tuple):
                                        response_header = response_part[0].decode("ascii", errors="ignore")
                                        flags_match = re.search(r"FLAGS\s+\(([^)]*)\)", response_header, re.IGNORECASE)
                                        if flags_match:
                                            source_flags = tuple(flags_match.group(1).split())
                                        date_match = re.search(r'INTERNALDATE\s+"([^"]+)"', response_header, re.IGNORECASE)
                                        if date_match:
                                            internal_date = date_match.group(1)
                                        if b"RFC822" in response_part[0]:
                                            raw_message = response_part[1]
                                    elif isinstance(response_part, bytes):
                                        if not internal_date:
                                            date_match = re.search(rb'INTERNALDATE\s+"([^"]+)"', response_part)
                                            if date_match:
                                                internal_date = date_match.group(1).decode("ascii", errors="ignore")
                                        if not source_flags:
                                            flags_match = re.search(rb"FLAGS\s+\(([^)]*)\)", response_part, re.IGNORECASE)
                                            if flags_match:
                                                source_flags = tuple(
                                                    flags_match.group(1).decode("ascii", errors="ignore").split()
                                                )

                                if not raw_message:
                                    break

                                # Count the source download toward the global
                                # migration limit. This is deliberately after
                                # fetch because only now is the actual payload
                                # size known.
                                self.rate_limiter.throttle(len(raw_message))

                                message_id_value = None
                                subject_value = ""
                                date_value = ""
                                from_value = ""
                                message_size = len(raw_message)
                                try:
                                    message_object = email.message_from_bytes(raw_message)
                                    raw_message_id = message_object.get("Message-ID")
                                    if raw_message_id:
                                        message_id_value = raw_message_id.strip().lower()
                                    subject_value = message_object.get("Subject", "").strip().lower()
                                    date_value = message_object.get("Date", "").strip().lower()
                                    from_value = message_object.get("From", "").strip().lower()
                                except Exception:
                                    pass

                                fallback_signature = f"{subject_value}|{date_value}|{from_value}|{message_size}"
                                is_duplicate = (
                                    (message_id_value and message_id_value in existing_messages)
                                    or fallback_signature in existing_signatures
                                )
                                if is_duplicate:
                                    skipped_duplicates += 1
                                    if progress_callback:
                                        progress_callback(1)
                                    if skipped_callback:
                                        skipped_callback(1)
                                    if account_progress_callback:
                                        account_progress_callback(1)
                                    success_appended = True
                                    break

                                imap_date_arg = (
                                    normalize_imap_internal_date(internal_date)
                                    if use_kerio_internal_date
                                    else None
                                )
                                valid_flags = [flag for flag in source_flags if flag.startswith("\\") or flag.startswith("$")]
                                flags_arg = f"({' '.join(valid_flags)})" if valid_flags else None
                                # The same payload is sent to Kerio. The
                                # limiter is shared by all worker threads.
                                self.rate_limiter.throttle(len(raw_message))
                                try:
                                    destination_connection.append(
                                        kerio_encoded,
                                        flags_arg,
                                        imap_date_arg,
                                        raw_message,
                                    )
                                except Exception as append_error:
                                    # Kerio versions in the field can reject a
                                    # valid RFC 3501 INTERNALDATE with
                                    # "Malformed date parameter". The date is
                                    # optional in APPEND, so retry this message
                                    # without it instead of losing the message.
                                    error_text = str(append_error).lower()
                                    if imap_date_arg and "malformed date parameter" in error_text:
                                        use_kerio_internal_date = False
                                        if not date_fallback_logged:
                                            self.log(
                                                "   [ПРЕДУПРЕЖДЕНИЕ] Kerio отклонил INTERNALDATE "
                                                "в APPEND; повторяем такие сообщения без даты.",
                                                log_file_path,
                                            )
                                            date_fallback_logged = True
                                        destination_connection.append(
                                            kerio_encoded,
                                            flags_arg,
                                            None,
                                            raw_message,
                                        )
                                    else:
                                        raise

                                if message_id_value:
                                    existing_messages.add(message_id_value)
                                existing_signatures.add(fallback_signature)
                                success_count += 1
                                migrated_account_messages += 1
                                if progress_callback:
                                    progress_callback(1)
                                if copied_callback:
                                    copied_callback(1)
                                if account_progress_callback:
                                    account_progress_callback(1)
                                success_appended = True
                                # UI updates are throttled separately, so no
                                # artificial per-message sleep is needed here.
                                break

                            except Exception as socket_error:
                                if attempt < 2:
                                    time.sleep(1.0)
                                    try:
                                        self._close_connection(source_connection)
                                        source_connection = self.connect_source(email_user, password)
                                        source_connection._encoding = "utf-8"
                                        source_connection.select(imap_encoded_folder, readonly=True)
                                    except Exception:
                                        pass
                                    try:
                                        self._close_connection(destination_connection)
                                        destination_connection = None
                                        for login_value in login_variants:
                                            try:
                                                destination_connection = self.connect_dest(login_value, password)
                                                destination_connection._encoding = "utf-8"
                                                destination_connection.select(kerio_encoded, readonly=True)
                                                break
                                            except Exception:
                                                pass
                                    except Exception:
                                        pass
                                else:
                                    number_text = number.decode("ascii", errors="ignore")
                                    self.log(
                                        f"   [ОШИБКА] Сообщение {number_text} в '{decoded_folder_name}': {socket_error}",
                                        log_file_path,
                                    )

                            if success_appended:
                                break

                        if not success_appended and not self.stop_requested:
                            if progress_callback:
                                progress_callback(1)
                            if error_callback:
                                error_callback(1)

                    kerio_count = 0
                    try:
                        destination_connection._encoding = "utf-8"
                        destination_result, _ = destination_connection.select(kerio_encoded, readonly=True)
                        if destination_result != "OK":
                            destination_connection.select(f'"{kerio_encoded}"', readonly=True)
                        destination_type, destination_numbers = destination_connection.search(None, "ALL")
                        if destination_type == "OK" and destination_numbers[0]:
                            kerio_count = len(destination_numbers[0].split())
                    except Exception:
                        pass

                    audit_line = (
                        f"Папка '{decoded_folder_name}': Ист={folder_total} | "
                        f"Пер={success_count} | Проп={skipped_duplicates} | Kerio={kerio_count}"
                    )
                    self.log(audit_line, log_file_path)

                except Exception as folder_error:
                    self.log(f"Ошибка папки {decoded_folder_name}: {folder_error}", log_file_path)

            destination_size_mb = self.get_mailbox_size_mb(destination_connection, email_user, password)
            self.log(
                f"Итог Kerio: {destination_size_mb} МБ. Перенесено писем: {migrated_account_messages}",
                log_file_path,
            )
            return total_account_messages, migrated_account_messages, source_size_mb, destination_size_mb

        except Exception as error:
            self.log(f"\n[ОШИБКА АККАУНТА]: {error}", log_file_path)
            return total_account_messages, migrated_account_messages, 0.0, 0.0
        finally:
            self._close_connection(source_connection)
            self._close_connection(destination_connection)

    def _count_account_messages(self, email_user, password):
        """Count source messages before migration for an accurate progress bar."""
        temporary_connection = None
        account_messages = 0
        try:
            temporary_connection = self.connect_source(email_user, password)
            folder_names = list_imap_folders(temporary_connection)
            if not folder_names:
                return 0

            for folder_name in folder_names:
                result, _, _selected_name = self._select_imap_folder(temporary_connection, folder_name)
                if result == "OK":
                    search_type, search_data = temporary_connection.search(None, "ALL")
                    if search_type == "OK" and search_data[0]:
                        account_messages += len(search_data[0].split())
            return account_messages
        finally:
            self._close_connection(temporary_connection)

    def run_migration(self, accounts, account_data_for_ui, max_threads):
        try:
            self.log(f"Запуск миграции ({VERSION}). Аккаунтов к обработке: {len(accounts)}")
            total_global_messages = 0
            account_queues = []
            self.log("Предварительный анализ ящиков...")

            for email_user, password in accounts:
                if self.stop_requested:
                    break
                label, progress = account_data_for_ui[email_user]
                self._set_account_row(
                    label,
                    progress,
                    text=f"{email_user}  •  анализ...",
                    status="running",
                )
                try:
                    cached_analysis = self.analysis_cache.get(email_user) if self.use_analysis_cache else None
                    if cached_analysis and cached_analysis.get("ok"):
                        account_messages = int(cached_analysis.get("messages", 0))
                        self.log(f"   - {email_user}: используем итоговый анализ ({account_messages} писем)")
                    else:
                        account_messages = self._count_account_messages(email_user, password)
                    total_global_messages += account_messages
                    account_queues.append((email_user, password, account_messages))
                    max_value = account_messages if account_messages > 0 else 1
                    self._set_account_row(
                        label,
                        progress,
                        text=f"{email_user}  •  {account_messages} писем",
                        maximum=max_value,
                        value=0,
                        status="pending",
                    )
                    self.log(f"   - {email_user}: {account_messages} писем")
                except Exception as error:
                    account_queues.append((email_user, password, 0))
                    self._set_account_row(
                        label,
                        progress,
                        text=f"{email_user}  •  ошибка анализа",
                        maximum=1,
                        value=0,
                        status="error",
                    )
                    self.log(f"   - {email_user}: ошибка ({error})")

            # Do not invent 10 messages per mailbox when the real total is 0.
            # The progress widget still needs a positive maximum internally.
            with self.stats_lock:
                self.total_msgs_cache = total_global_messages
                self.copied_msgs_cache = 0
                self.error_msgs_cache = 0

            global_processed = [0]
            global_copied = [0]
            global_skipped = [0]
            global_errors = [0]
            progress_lock = threading.Lock()

            def progress_callback(count):
                # This callback is intentionally UI-free. The Tk main loop
                # reads the latest value from the shared cache every 250 ms.
                with progress_lock:
                    global_processed[0] += count

            def copied_callback(count):
                with progress_lock:
                    global_copied[0] += count
                    with self.stats_lock:
                        self.copied_msgs_cache = global_copied[0]

            def skipped_callback(count):
                with progress_lock:
                    global_skipped[0] += count
                    with self.stats_lock:
                        self.skipped_msgs_cache = global_skipped[0]

            def error_callback(count=1):
                with progress_lock:
                    global_errors[0] += count
                    with self.stats_lock:
                        self.error_msgs_cache = global_errors[0]

            work_queue = queue.Queue()
            for item in account_queues:
                work_queue.put(item)

            def worker():
                while not self.stop_requested:
                    try:
                        email_user, password, account_total = work_queue.get_nowait()
                    except queue.Empty:
                        return

                    try:
                        label, progress = account_data_for_ui[email_user]
                        self._set_account_row(
                            label,
                            progress,
                            text=f"{email_user}  •  выполняется",
                            status="running",
                        )
                        account_copied = [0]
                        account_lock = threading.Lock()

                        def account_progress_callback(count):
                            with account_lock:
                                account_copied[0] += count
                                current_value = account_copied[0]
                            self._set_account_row(
                                label,
                                progress,
                                value=current_value,
                            )

                        result = self.migrate_account(
                            email_user,
                            password,
                            progress_callback=progress_callback,
                            copied_callback=copied_callback,
                            skipped_callback=skipped_callback,
                            error_callback=error_callback,
                            account_progress_callback=account_progress_callback,
                        )
                        migrated_total, migrated_count, _source_size, _destination_size = result

                        if self.stop_requested:
                            final_text = f"{email_user}  •  остановлен ({account_copied[0]}/{account_total})"
                            final_status = "error"
                        elif account_total and account_copied[0] < account_total:
                            final_text = f"{email_user}  •  частично ({account_copied[0]}/{account_total})"
                            final_status = "error"
                        else:
                            final_text = f"{email_user}  •  готово ({account_copied[0]}/{migrated_total or account_total})"
                            final_status = "done"
                        self._set_account_row(label, progress, text=final_text, status=final_status)
                    except Exception as error:
                        self.log(f"[ОШИБКА ПОТОКА {email_user}]: {error}")
                        self._set_account_row(
                            label,
                            progress,
                            text=f"{email_user}  •  ошибка",
                            status="error",
                        )
                    finally:
                        work_queue.task_done()

            thread_count = min(max_threads, max(1, len(account_queues)))
            threads = []
            for _ in range(thread_count):
                thread = threading.Thread(target=worker, daemon=True)
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()

            self.timer_running = False
            self.process_finished = True
            self.migration_outcome = "stopped" if self.stop_requested else "done"
            status_text = "ОСТАНОВЛЕНО" if self.stop_requested else "ЗАВЕРШЕНА"
            status_color = COLORS["red"] if self.stop_requested else COLORS["green"]
            self.set_status(status_text, status_color)
            self.log(
                f"\n=== МИГРАЦИЯ {status_text} ==="
                f"\nВсего аккаунтов: {len(accounts)}"
                f"\nСкопировано: {global_copied[0]}"
                f"\nПропущено дублей: {global_skipped[0]}"
                f"\nС ошибками: {global_errors[0]}"
            )

            def finish_message():
                messagebox.showinfo(
                    "Миграция завершена",
                    f"Миграция ({VERSION}) завершена.\nСкопировано: {global_copied[0]}\nПропущено дублей: {global_skipped[0]}\nС ошибками: {global_errors[0]}",
                )

            try:
                self.root.after(0, finish_message)
            except tk.TclError:
                pass

        except Exception as error:
            self.timer_running = False
            self.migration_outcome = "error"
            self.set_status("ОШИБКА", COLORS["red"])
            self.log(f"\n[КРИТИЧЕСКАЯ ОШИБКА]: {error}")
            try:
                self.root.after(0, lambda: messagebox.showerror("Ошибка миграции", str(error)))
            except tk.TclError:
                pass
        finally:
            def finish_controls():
                try:
                    self.process_finished = True
                    self.start_btn.config(state=tk.NORMAL)
                    self.stop_btn.config(state=tk.DISABLED)
                    self.test_start_btn.config(state=tk.NORMAL)
                    self.speed_limit_combo.config(state="readonly")
                    self.wizard_back_btn.config(state=tk.NORMAL)
                    self.wizard_next_btn.config(text="Новая миграция", state=tk.NORMAL)
                    outcome_text, outcome_color = {
                        "stopped": ("Остановлено", COLORS["red"]),
                        "error": ("Ошибка", COLORS["red"]),
                    }.get(self.migration_outcome, ("Готово", COLORS["green"]))
                    self.process_status_label.config(text=outcome_text, fg=outcome_color)
                except tk.TclError:
                    pass
            try:
                self.root.after(0, finish_controls)
            except tk.TclError:
                pass

    def on_close(self):
        self.test_stop_event.set()
        self.server_test_stop_event.set()
        self.analysis_stop_event.set()
        if self.timer_running:
            self.stop_requested = True
            self.timer_running = False
        with self.connection_lock:
            connections = list(self.active_connections)
        for connection in connections:
            try:
                connection.logout()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = ImapMigratorApp(root)
    root.mainloop()
