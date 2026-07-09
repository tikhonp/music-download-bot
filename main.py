import asyncio
import functools
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set
from urllib.parse import urlparse

import requests

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from qobuz_dl.bundle import Bundle
from qobuz_dl.core import QobuzDL
from qobuz_dl.db import handle_download_id
from qobuz_dl.settings import QobuzDLSettings
from qobuz_dl.utils import get_url_info

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------

QOBUZ_URL_PATTERN = re.compile(
    r"https?://(?:www\.)?(?:play\.qobuz\.com|open\.qobuz\.com|qobuz\.com)/"
    r"(?:album|track|playlist|artist|label)/[a-zA-Z0-9\-_]+"
)
APPLE_MUSIC_URL_PATTERN = re.compile(
    r"https?://(?:music\.apple\.com|itunes\.apple\.com)/\S+"
)


class StreamingType(str, Enum):
    QOBUZ = "qobuz"
    APPLE_MUSIC = "apple_music"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PLACEHOLDER_TOKEN = "YOUR_BOT_TOKEN_HERE"


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() == "true"


def _parse_whitelist(raw: str) -> Set[int]:
    users: Set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            users.add(int(part))
        except ValueError:
            raise SystemExit(
                f"Invalid WHITELIST_USERS entry {part!r}: expected integer user IDs"
            )
    return users


@dataclass(frozen=True)
class Config:
    bot_token: str
    whitelist_users: Set[int]
    download_path: str
    proxy_url: Optional[str]
    start_scan_endpoint: str

    qobuz_enabled: bool
    qobuz_email: str
    qobuz_password: str
    qobuz_db: Optional[str]
    qobuz_embed_cover: bool
    qobuz_batch_download_enabled: bool

    apple_music_enabled: bool
    apple_music_download_url: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", _PLACEHOLDER_TOKEN),
            whitelist_users=_parse_whitelist(os.getenv("WHITELIST_USERS", "")),
            download_path=os.getenv("DOWNLOAD_PATH", "./downloads"),
            proxy_url=os.getenv("PROXY_URL") or None,
            start_scan_endpoint=os.getenv("START_SCAN_ENDPOINT", "").strip(),
            qobuz_enabled=_env_bool("QOBUZ_ENABLED", True),
            qobuz_email=os.getenv("QOBUZ_EMAIL", ""),
            qobuz_password=os.getenv("QOBUZ_PASSWORD", ""),
            qobuz_db=os.getenv("QOBUZ_DB") or None,
            qobuz_embed_cover=_env_bool("QOBUZ_EMBED_COVER", True),
            qobuz_batch_download_enabled=_env_bool(
                "QOBUZ_BATCH_DOWNLOAD_ENABLED", True
            ),
            apple_music_enabled=_env_bool("APPLE_MUSIC_ENABLED", True),
            apple_music_download_url=os.getenv("APPLE_MUSIC_DOWNLOAD_URL", "").strip(),
        )

    def validate(self) -> None:
        """Fail fast on misconfiguration; warn on optional gaps."""
        errors: List[str] = []

        if not self.bot_token or self.bot_token == _PLACEHOLDER_TOKEN:
            errors.append("TELEGRAM_BOT_TOKEN is required")
        if self.qobuz_enabled and (not self.qobuz_email or not self.qobuz_password):
            errors.append(
                "QOBUZ_EMAIL and QOBUZ_PASSWORD are required when QOBUZ_ENABLED=true"
            )
        if self.apple_music_enabled and not self.apple_music_download_url:
            errors.append(
                "APPLE_MUSIC_DOWNLOAD_URL is required when APPLE_MUSIC_ENABLED=true"
            )
        if not self.qobuz_enabled and not self.apple_music_enabled:
            errors.append(
                "At least one of QOBUZ_ENABLED / APPLE_MUSIC_ENABLED must be true"
            )

        if errors:
            for err in errors:
                logger.error(err)
            sys.exit(1)

        if not self.whitelist_users:
            logger.warning(
                "No whitelisted users configured. Set WHITELIST_USERS "
                "(e.g. WHITELIST_USERS=123456789,987654321)"
            )
        if not self.qobuz_enabled:
            logger.info("Qobuz downloads disabled via QOBUZ_ENABLED=false")
        if not self.apple_music_enabled:
            logger.info("Apple Music downloads disabled via APPLE_MUSIC_ENABLED=false")
        if not self.start_scan_endpoint:
            logger.warning("No START_SCAN_ENDPOINT configured; rescans will be skipped")
        if not self.proxy_url:
            logger.warning("No PROXY_URL configured")

    def enabled_services(self) -> List[str]:
        services = []
        if self.qobuz_enabled:
            services.append("Qobuz")
        if self.apple_music_enabled:
            services.append("Apple Music")
        return services


# ---------------------------------------------------------------------------
# Apple Music download service (external HTTP API)
# ---------------------------------------------------------------------------


class AppleServiceError(Exception):
    """Raised when the Apple Music download service reports a failure."""


class AppleServiceSync:
    def __init__(self, base_url: str, default_timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = default_timeout

    def start_download(
        self, url: str, fmt: str = "alac", song: bool = False, debug: bool = False
    ) -> str:
        payload = {"url": url, "format": fmt, "song": song, "debug": debug}
        resp = requests.post(
            f"{self.base_url}/download", json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        job_id = resp.json().get("job_id")
        if not job_id:
            raise AppleServiceError("Download service did not return a job_id")
        return job_id

    def get_status(self, job_id: str) -> Dict[str, Any]:
        resp = requests.get(f"{self.base_url}/status/{job_id}", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def wait_for_completion(
        self,
        job_id: str,
        poll_interval: float = 2.0,
        max_wait: float = 3600.0,
        progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """Poll until status is completed/failed, or raise on timeout."""
        deadline = time.time() + max_wait
        while True:
            status = self.get_status(job_id)
            if progress_callback:
                try:
                    progress_callback(status)
                except Exception:  # progress reporting must never break the poll loop
                    logger.debug("progress_callback raised", exc_info=True)
            if status.get("status") in ("completed", "failed"):
                return status
            if time.time() > deadline:
                raise TimeoutError(
                    f"Timed out waiting for job {job_id} after {max_wait}s"
                )
            time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# Download bot
# ---------------------------------------------------------------------------


@dataclass
class DownloadTask:
    url: str
    chat_id: int
    message_id: int
    user_id: int
    streaming_type: StreamingType


class QobuzDownloadBot:
    def __init__(self, config: Config):
        self.config = config
        self.download_path = Path(config.download_path)
        self.download_path.mkdir(parents=True, exist_ok=True)

        self.download_queue: "Queue[DownloadTask]" = Queue()
        self.is_downloading = False

        self.qobuz: Optional[QobuzDL] = None
        if config.qobuz_enabled:
            self._init_qobuz()

        self.apple_service: Optional[AppleServiceSync] = None
        if config.apple_music_enabled:
            self.apple_service = AppleServiceSync(config.apple_music_download_url)

        self.worker_thread = Thread(target=self._download_worker, daemon=True)
        self.worker_thread.start()

    # -- setup --------------------------------------------------------------

    def _configure_qobuz_proxy(self) -> None:
        """Route every qobuz-dl HTTP call through PROXY_URL.

        qobuz-dl creates its own requests Sessions (bundle fetch, auth, and a
        fresh Session per track download) and exposes no proxy setting, so the
        only way to proxy all of them is via the standard *_PROXY env vars,
        which requests honours through trust_env. The internal Apple Music
        service host is excluded so it stays on the direct (docker) network.
        """
        proxy = self.config.proxy_url
        if not proxy:
            return

        for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[var] = proxy

        no_proxy = [os.environ.get("NO_PROXY", "").strip()]
        if self.config.apple_music_download_url:
            host = urlparse(self.config.apple_music_download_url).hostname
            if host:
                no_proxy.append(host)
        no_proxy_value = ",".join(h for h in no_proxy if h)
        if no_proxy_value:
            os.environ["NO_PROXY"] = no_proxy_value
            os.environ["no_proxy"] = no_proxy_value

        logger.info("Routing Qobuz traffic through proxy")

    def _init_qobuz(self) -> None:
        self._configure_qobuz_proxy()

        bundle = Bundle()
        app_id = str(bundle.get_app_id())
        secrets = ",".join(bundle.get_secrets().values())

        settings = None
        if not self.config.qobuz_batch_download_enabled:
            # Single-threaded with a small delay to reduce rate-limit risk.
            settings = QobuzDLSettings(max_workers=1, delay=0.5)

        self.qobuz = QobuzDL(
            directory=str(self.download_path),
            quality=27,  # Max quality
            embed_art=self.config.qobuz_embed_cover,
            downloads_db=self.config.qobuz_db,
            settings=settings,
        )
        self.qobuz.get_tokens()
        self.qobuz.initialize_client(
            self.config.qobuz_email, self.config.qobuz_password, app_id, secrets
        )
        logger.info("Qobuz-dl initialized successfully")

    # -- queue --------------------------------------------------------------

    def is_user_authorized(self, user_id: int) -> bool:
        return user_id in self.config.whitelist_users

    def add_download(self, task: DownloadTask) -> None:
        self.download_queue.put(task)
        logger.info(
            "Queued %s (%s); queue size: %d",
            task.url,
            task.streaming_type.value,
            self.download_queue.qsize(),
        )

    # -- worker -------------------------------------------------------------

    def _download_worker(self) -> None:
        handlers: Dict[StreamingType, Callable[[DownloadTask], None]] = {
            StreamingType.QOBUZ: self._download_qobuz,
            StreamingType.APPLE_MUSIC: self._download_apple_music,
        }
        while True:
            task = self.download_queue.get()
            self.is_downloading = True
            try:
                handler = handlers.get(task.streaming_type)
                if handler is None:
                    logger.error("Unknown streaming type: %s", task.streaming_type)
                else:
                    handler(task)
            except Exception as exc:
                logger.exception("Download failed for %s", task.url)
                self._reply(task, f"❌ Download failed!\n\nURL: {task.url}\n\nError: {exc}")
            finally:
                self.is_downloading = False
                self.download_queue.task_done()

    def _download_qobuz(self, task: DownloadTask) -> None:
        if not self.config.qobuz_enabled or self.qobuz is None:
            logger.warning("Qobuz task received while Qobuz is disabled/uninitialized")
            self._reply(task, "❌ Qobuz downloads are disabled.")
            return

        logger.info("Starting Qobuz download: %s", task.url)

        item_id = None
        try:
            _, item_id = get_url_info(task.url)
        except Exception:
            logger.warning("Could not parse Qobuz item id from %s", task.url, exc_info=True)

        already_downloaded = bool(item_id) and handle_download_id(
            self.qobuz.downloads_db, item_id, add_id=False
        )
        if already_downloaded:
            self._reply(
                task, f"ℹ️ This release has already been downloaded.\n\n{task.url}"
            )
            return

        self.qobuz.handle_url(task.url)

        # Confirm the item landed in the downloads DB (best-effort success check).
        if item_id and not handle_download_id(
            self.qobuz.downloads_db, item_id, add_id=False
        ):
            self._reply(task, f"❌ Download did not complete successfully.\n\n{task.url}")
            return

        self._reply(task, f"✅ Download completed successfully!\n\n{task.url}")
        self.fire_rescan()

    def _download_apple_music(self, task: DownloadTask) -> None:
        if not self.config.apple_music_enabled or self.apple_service is None:
            logger.warning("Apple Music task received while the service is disabled")
            self._reply(task, "❌ Apple Music downloads are disabled.")
            return

        logger.info("Starting Apple Music download: %s", task.url)

        job_id = self.apple_service.start_download(url=task.url, fmt="alac")

        def progress_callback(status: Dict[str, Any]) -> None:
            logger.info(
                "Apple Music progress for %s: %s%%",
                task.url,
                status.get("progress", 0),
            )

        final_status = self.apple_service.wait_for_completion(
            job_id=job_id, progress_callback=progress_callback
        )

        if final_status.get("status") == "completed":
            self._reply(task, f"✅ Download completed successfully!\n\n{task.url}")
            self.fire_rescan()
        else:
            error = final_status.get("error", "Unknown error")
            logger.error("Apple Music download failed for %s: %s", task.url, error)
            self._reply(task, f"❌ Download failed!\n\nURL: {task.url}\n\nError: {error}")

    # -- notifications ------------------------------------------------------

    def _reply(self, task: DownloadTask, text: str) -> None:
        """Send a Telegram reply from the synchronous worker thread.

        The polling Application lives on another event loop, so each
        notification spins up a throwaway Application — see CLAUDE.md.
        """

        async def _send() -> None:
            builder = Application.builder().token(self.config.bot_token)
            if self.config.proxy_url:
                builder = builder.proxy(self.config.proxy_url)
            app = builder.build()
            await app.bot.send_message(
                chat_id=task.chat_id,
                text=text,
                reply_to_message_id=task.message_id,
            )

        try:
            asyncio.run(_send())
        except Exception:
            logger.error("Failed to send Telegram message to %s", task.chat_id, exc_info=True)

    def fire_rescan(self) -> None:
        if not self.config.start_scan_endpoint:
            logger.debug("START_SCAN_ENDPOINT not configured; skipping rescan")
            return
        try:
            resp = requests.get(self.config.start_scan_endpoint, timeout=10)
            resp.raise_for_status()
            logger.info("Rescan triggered successfully")
        except Exception:
            logger.error("Error triggering rescan", exc_info=True)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

config = Config.from_env()
config.validate()
bot_instance = QobuzDownloadBot(config)

Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]


def restricted(handler: Handler) -> Handler:
    """Reject updates from users that are not whitelisted."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or not bot_instance.is_user_authorized(user.id):
            if update.message:
                await update.message.reply_text(
                    "❌ You are not authorized to use this bot."
                )
            return
        await handler(update, context)

    return wrapper


@restricted
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services = config.enabled_services()
    services_text = ", ".join(services) if services else "No services configured"
    await update.message.reply_text(
        "🎵 *Music Download Bot*\n\n"
        f"Send me a link from: {services_text}.\n"
        "I'll add it to the queue and notify you when it's done.\n\n"
        "Commands:\n"
        "/start - Show this message\n"
        "/queue - Show queue status\n"
        "/help - Get help",
        parse_mode="Markdown",
    )


@restricted
async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    queue_size = bot_instance.download_queue.qsize()
    status = "🔄 Downloading..." if bot_instance.is_downloading else "⏸ Idle"
    await update.message.reply_text(
        f"📊 *Queue Status*\n\nStatus: {status}\nItems in queue: {queue_size}",
        parse_mode="Markdown",
    )


@restricted
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    descriptions = {
        "Qobuz": "Qobuz (album, track, playlist, artist, label)",
        "Apple Music": "Apple Music URLs",
    }
    supported = [descriptions[s] for s in config.enabled_services()]
    supported_text = (
        "\n".join(f"• {item}" for item in supported)
        if supported
        else "• No services configured"
    )
    await update.message.reply_text(
        "🎵 *Music Download Bot Help*\n\n"
        "*How to use:*\n"
        "1. Send a supported URL.\n"
        "2. The download will be queued automatically.\n"
        "3. You'll receive a notification when complete.\n\n"
        "*Supported URLs:*\n"
        f"{supported_text}\n\n"
        "*Commands:*\n"
        "/start - Show welcome message\n"
        "/queue - Check queue status\n"
        "/help - Show this help",
        parse_mode="Markdown",
    )


def _extract_urls(text: str, pattern: re.Pattern, enabled: bool) -> List[str]:
    """Return de-duplicated matches in order; empty when the service is disabled."""
    if not enabled:
        return []
    seen: Set[str] = set()
    urls: List[str] = []
    for url in pattern.findall(text):
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


@restricted
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""

    qobuz_urls = _extract_urls(text, QOBUZ_URL_PATTERN, config.qobuz_enabled)
    apple_urls = _extract_urls(text, APPLE_MUSIC_URL_PATTERN, config.apple_music_enabled)

    if not qobuz_urls and not apple_urls:
        if QOBUZ_URL_PATTERN.search(text) and not config.qobuz_enabled:
            await update.message.reply_text(
                "ℹ️ Qobuz downloads are disabled. Provide an Apple Music URL or "
                "enable QOBUZ_ENABLED."
            )
            return
        if APPLE_MUSIC_URL_PATTERN.search(text) and not config.apple_music_enabled:
            await update.message.reply_text(
                "ℹ️ Apple Music downloads are disabled. Provide a Qobuz URL or "
                "enable APPLE_MUSIC_ENABLED."
            )
            return
        services = config.enabled_services()
        supported_text = ", ".join(services) if services else "none"
        await update.message.reply_text(
            f"❓ Please send a supported URL.\n\nSupported sources: {supported_text}."
        )
        return

    for url in qobuz_urls:
        bot_instance.add_download(
            DownloadTask(
                url=url,
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                user_id=update.effective_user.id,
                streaming_type=StreamingType.QOBUZ,
            )
        )
    for url in apple_urls:
        bot_instance.add_download(
            DownloadTask(
                url=url,
                chat_id=update.effective_chat.id,
                message_id=update.message.message_id,
                user_id=update.effective_user.id,
                streaming_type=StreamingType.APPLE_MUSIC,
            )
        )

    total = len(qobuz_urls) + len(apple_urls)
    await update.message.reply_text(
        f"✅ Added {total} download(s) to the queue!\n\n"
        f"Items in queue: {bot_instance.download_queue.qsize()}\n\n"
        "You'll be notified when each download completes."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Update %s caused error: %s", update, context.error)


def main() -> None:
    builder = Application.builder().token(config.bot_token)
    if config.proxy_url:
        builder = builder.proxy(config.proxy_url).get_updates_proxy(config.proxy_url)
    app = builder.build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Starting Music Download Bot...")
    logger.info("Download path: %s", config.download_path)
    logger.info("Qobuz DB path: %s", config.qobuz_db)
    logger.info("Whitelisted users: %s", config.whitelist_users)

    logging.getLogger("httpx").setLevel(logging.WARNING)

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
