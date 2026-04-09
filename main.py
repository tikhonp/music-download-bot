import os
import re
import sys
import asyncio
import logging
from pathlib import Path
from typing import Callable, Optional, Dict, Any, Set
from dataclasses import dataclass
from queue import Queue
from threading import Thread
import requests
import time

import httpx

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from qobuz_dl.core import QobuzDL # type: ignore[import-untyped]
from qobuz_dl.utils import get_url_info # type: ignore[import-untyped]
from qobuz_dl.db import handle_download_id # type: ignore[import-untyped]


# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
WHITELIST_USERS = set(map(int, os.getenv("WHITELIST_USERS", "").split(","))) if os.getenv("WHITELIST_USERS") else set()
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "./downloads")
PROXY_URL = os.getenv("PROXY_URL", "")

OS_CONFIG = os.path.join(os.environ["HOME"], ".config")
CONFIG_PATH = os.getenv("CONFIG_PATH", os.path.join(OS_CONFIG, "qobuz-dl"))
QOBUZ_DB = os.path.join(CONFIG_PATH, "qobuz_dl.db")

QOBUZ_EMAIL = os.getenv("QOBUZ_EMAIL", "")
QOBUZ_PASSWORD = os.getenv("QOBUZ_PASSWORD", "")

# Qobuz URL pattern
QOBUZ_URL_PATTERN = re.compile(
    r'https?://(?:www\.)?(?:play\.qobuz\.com|open\.qobuz\.com|qobuz\.com)/'
    r'(?:album|track|playlist|artist|label)/[a-zA-Z0-9\-_]+'
)

# Apple Music URL pattern
APPLE_MUSIC_URL_PATTERN = re.compile(
    r'https?://(?:music\.apple\.com|itunes\.apple\.com)/\S+'
)

START_SCAN_ENDPOINT = os.getenv("START_SCAN_ENDPOINT", "")

APPLE_MUSIC_DOWNLOAD_URL=os.getenv("APPLE_MUSIC_DOWNLOAD_URL", "")

# # QobuzDL does not have a proxy param so i set env vers for it
# os.environ['HTTP_PROXY'] = PROXY_URL
# os.environ['HTTPS_PROXY'] = PROXY_URL
# OR NOT???

if not APPLE_MUSIC_DOWNLOAD_URL:
    logger.warning("No APPLE_MUSIC_DOWNLOAD_URL configured. Set APPLE_MUSIC_DOWNLOAD_URL environment variable if needed.")

if not START_SCAN_ENDPOINT:
    logger.warning("No START_SCAN_ENDPOINT configured. Set START_SCAN_ENDPOINT environment variable if needed.")

if not BOT_TOKEN or BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
    logger.error("Please set TELEGRAM_BOT_TOKEN environment variable")
    sys.exit(1)

if not WHITELIST_USERS:
    logger.warning("No whitelisted users configured. Set WHITELIST_USERS environment variable.")
    logger.warning("Example: WHITELIST_USERS=123456789,987654321")

if not QOBUZ_EMAIL or not QOBUZ_PASSWORD:
    logger.error("Please set QOBUZ_EMAIL and QOBUZ_PASSWORD environment variables")
    sys.exit(1)

if not PROXY_URL:
    logger.warning("No proxy configured. Set PROXY_URL environment variable if needed.")

class AppleServiceSync:
    def __init__(self, base_url: str, default_timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = default_timeout

    def start_download(self, url: str, fmt: str = "alac", song: bool = False, debug: bool = False) -> str:
        payload = {"url": url, "format": fmt, "song": song, "debug": debug}
        resp = requests.post(f"{self.base_url}/download", json=payload, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        return data["job_id"]

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
        """Poll until status is completed/failed or timeout. Call progress_callback(status_dict) on each poll."""
        start = time.time()
        while True:
            status = self.get_status(job_id)
            if progress_callback:
                try:
                    progress_callback(status)
                except Exception:
                    pass
            s = status.get("status")
            if s in ("completed", "failed"):
                return status
            if (time.time() - start) > max_wait:
                raise TimeoutError(f"Timeout waiting for job {job_id} (waited {max_wait}s)")
            time.sleep(poll_interval)

@dataclass
class DownloadTask:
    url: str
    chat_id: int
    message_id: int
    user_id: int
    streaming_type: str # "qobuz" or "apple_music"


class QobuzDownloadBot:
    def __init__(self, token: str, whitelist: Set[int], download_path: str):
        self.token = token
        self.whitelist = whitelist
        self.download_path = Path(download_path)
        self.download_path.mkdir(parents=True, exist_ok=True)
        
        self.download_queue: Queue = Queue()
        self.is_downloading = False
        
        # Initialize Qobuz-dl
        self.qobuz = None
        self._init_qobuz()

        # Initialize Apple Music
        self.apple_service = AppleServiceSync(base_url=APPLE_MUSIC_DOWNLOAD_URL)
        
        # Start download worker thread
        self.worker_thread = Thread(target=self._download_worker, daemon=True)
        self.worker_thread.start()
    
    def _init_qobuz(self):
        """Initialize Qobuz-dl instance"""
        # logging.getLogger("qobuz_dl").setLevel(logging.WARNING)
        self.qobuz = QobuzDL(
            directory=str(self.download_path),
            quality=27,  # Max quality
            embed_art=True,
            downloads_db=QOBUZ_DB,
        )
        self.qobuz.get_tokens()
        self.qobuz.initialize_client(QOBUZ_EMAIL, QOBUZ_PASSWORD, self.qobuz.app_id, self.qobuz.secrets)
        logger.info("Qobuz-dl initialized successfully")
    
    def _download_worker(self):
        """Worker thread that processes download queue"""
        while True:
            task: DownloadTask = self.download_queue.get()
            
            if task.streaming_type == "apple_music":
                try:
                    self.is_downloading = True
                    logger.info(f"Starting Apple Music download: {task.url}")

                    job_id = self.apple_service.start_download(url=task.url, fmt="alac", song=False, debug=False)
                    
                    def progress_callback(status: Dict[str, Any]):
                        progress = status.get("progress", 0)
                        logger.info(f"Apple Music download progress for {task.url}: {progress}%")
                    
                    final_status = self.apple_service.wait_for_completion(
                        job_id=job_id,
                        progress_callback=progress_callback
                    )

                    if final_status.get("status") == "completed":
                        # Send success message
                        asyncio.run(self._send_success_message(task))
                        self.fire_rescan()
                    else:
                        error_msg = final_status.get("error", "Unknown error")
                        logger.error(f"Apple Music download failed for {task.url}: {error_msg}")
                        asyncio.run(self._send_error_message(task, error_msg))
                
                except Exception as e:
                    logger.error(f"Apple Music download failed for {task.url}: {e}")
                    asyncio.run(self._send_error_message(task, str(e)))
                
                finally:
                    self.is_downloading = False
                    self.download_queue.task_done()

            elif task.streaming_type == "qobuz":
                try:
                    self.is_downloading = True
                    logger.info(f"Starting Qobuz download: {task.url}")


                    _, item_id = get_url_info(task.url)
                    if handle_download_id(self.qobuz.downloads_db, item_id, add_id=False):
                        asyncio.run(self._send_release_already_downloaded_message(task))
                        continue
                    
                    # Download using Qobuz-dl
                    self.qobuz.handle_url(task.url)

                    if not handle_download_id(self.qobuz.downloads_db, item_id, add_id=False):
                        asyncio.run(self._send_error_message(task, "Download did not complete successfully."))
                    else:
                        # Send success message
                        asyncio.run(self._send_success_message(task))
                        self.fire_rescan()
                    
                except Exception as e:
                    logger.error(f"Download failed for {task.url}: {e}")
                    asyncio.run(self._send_error_message(task, str(e)))
                
                finally:
                    self.is_downloading = False
                    self.download_queue.task_done()
            else:
                logger.error(f"Unknown streaming type: {task.streaming_type}")
                self.download_queue.task_done()

    async def _send_release_already_downloaded_message(self, task: DownloadTask):
        """Send message indicating release is already downloaded"""
        try:
            app = Application.builder().proxy(PROXY_URL).token(self.token).build()
            await app.bot.send_message(
                chat_id=task.chat_id,
                text=f"ℹ️ This release has already been downloaded.\n\n{task.url}",
                reply_to_message_id=task.message_id
            )
        except Exception as e:
            logger.error(f"Failed to send already downloaded message: {e}")
    
    async def _send_success_message(self, task: DownloadTask):
        """Send success notification"""
        try:
            app = Application.builder().proxy(PROXY_URL).token(self.token).build()
            await app.bot.send_message(
                chat_id=task.chat_id,
                text=f"✅ Download completed successfully!\n\n{task.url}",
                reply_to_message_id=task.message_id
            )
        except Exception as e:
            logger.error(f"Failed to send success message: {e}")
    
    async def _send_error_message(self, task: DownloadTask, error: str):
        """Send error notification"""
        try:
            app = Application.builder().proxy(PROXY_URL).token(self.token).build()
            await app.bot.send_message(
                chat_id=task.chat_id,
                text=f"❌ Download failed!\n\n"
                     f"URL: {task.url}\n\n"
                     f"Error: {error}",
                reply_to_message_id=task.message_id
            )
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")
    
    def is_user_authorized(self, user_id: int) -> bool:
        """Check if user is in whitelist"""
        return user_id in self.whitelist
    
    def add_download(self, task: DownloadTask):
        """Add download task to queue"""
        self.download_queue.put(task)
        logger.info(f"Added to queue: {task.url} (Queue size: {self.download_queue.qsize()})")

    def fire_rescan(self):
        """Trigger a rescan via the configured endpoint"""
        try:
            response = httpx.get(START_SCAN_ENDPOINT, timeout=10)
            if response.status_code == 200:
                logger.info("Rescan triggered successfully.")
            else:
                logger.error(f"Failed to trigger rescan. Status code: {response.status_code}")
        except Exception as e:
            logger.error(f"Error triggering rescan: {e}")

bot_instance = QobuzDownloadBot(BOT_TOKEN, WHITELIST_USERS, DOWNLOAD_PATH)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    user_id = update.effective_user.id
    
    if not bot_instance.is_user_authorized(user_id):
        await update.message.reply_text(
            "❌ You are not authorized to use this bot."
        )
        return
    
    await update.message.reply_text(
        "🎵 *Qobuz Download Bot*\n\n"
        "Send me a Qobuz link (album, track, playlist, artist, or label) "
        "and I'll download it for you!\n\n"
        "Commands:\n"
        "/start - Show this message\n"
        "/queue - Show queue status\n"
        "/help - Get help",
        parse_mode='Markdown'
    )

async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /queue command"""
    user_id = update.effective_user.id
    
    if not bot_instance.is_user_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    queue_size = bot_instance.download_queue.qsize()
    status = "🔄 Downloading..." if bot_instance.is_downloading else "⏸ Idle"
    
    await update.message.reply_text(
        f"📊 *Queue Status*\n\n"
        f"Status: {status}\n"
        f"Items in queue: {queue_size}",
        parse_mode='Markdown'
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command"""
    user_id = update.effective_user.id
    
    if not bot_instance.is_user_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    await update.message.reply_text(
        "🎵 *Qobuz Download Bot Help*\n\n"
        "*How to use:*\n"
        "1. Send any Qobuz URL (album, track, playlist, etc.)\n"
        "2. The download will be queued automatically\n"
        "3. You'll receive a notification when complete\n\n"
        "*Supported URLs:*\n"
        "• Albums\n"
        "• Tracks\n"
        "• Playlists\n"
        "• Artists\n"
        "• Labels\n\n"
        "*Commands:*\n"
        "/start - Show welcome message\n"
        "/queue - Check queue status\n"
        "/help - Show this help",
        parse_mode='Markdown'
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming messages with Qobuz URLs"""
    user_id = update.effective_user.id
    
    # Check authorization
    if not bot_instance.is_user_authorized(user_id):
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return
    
    text = update.message.text
    
    qobuz_urls = QOBUZ_URL_PATTERN.findall(text)
    apple_music_urls = APPLE_MUSIC_URL_PATTERN.findall(text)
    
    if not qobuz_urls and not apple_music_urls:
        await update.message.reply_text(
            "❓ Please send a valid Qobuz URL.\n\n"
            "Example: https://play.qobuz.com/album/..."
        )
        return
    
    # Queue each URL
    for url in qobuz_urls:
        task = DownloadTask(
            url=url,
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            user_id=user_id,
            streaming_type="qobuz"
        )
        bot_instance.add_download(task)

    for url in apple_music_urls:
        task = DownloadTask(
            url=url,
            chat_id=update.effective_chat.id,
            message_id=update.message.message_id,
            user_id=user_id,
            streaming_type="apple_music"
        )
        bot_instance.add_download(task)
    
    queue_size = bot_instance.download_queue.qsize()
    await update.message.reply_text(
        f"✅ Added {len(qobuz_urls+apple_music_urls)} download(s) to queue!\n\n"
        f"Queue position: #{queue_size}\n\n"
        f"You'll be notified when the download completes."
    )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors"""
    logger.error(f"Update {update} caused error {context.error}")

def main():
    """Start the bot"""

    app = (Application
           .builder()
           .token(BOT_TOKEN)
           .proxy(PROXY_URL)
           .get_updates_proxy(PROXY_URL)
           .build())

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logger.info("Starting Qobuz Telegram Bot...")
    logger.info(f"Download path: {DOWNLOAD_PATH}")
    logger.info(f"Qobuz DB path: {QOBUZ_DB}")
    logger.info(f"Whitelisted users: {WHITELIST_USERS}")

    logging.getLogger("httpx").setLevel(logging.WARNING)

    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
