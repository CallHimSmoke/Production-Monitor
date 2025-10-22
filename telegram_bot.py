"""
Production Planning Telegram Bot (Railway-Compatible)
Trigger production task automation via Telegram commands.
"""

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
import pytz
from playwright.async_api import async_playwright, Browser, BrowserContext, Page, TimeoutError as PlaywrightTimeout
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ============================================================================
# CONFIGURATION
# ============================================================================

# Telegram Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID")

# Convert ALLOWED_USER_ID to int safely
try:
    ALLOWED_USER_ID = int(ALLOWED_USER_ID) if ALLOWED_USER_ID else 0
except ValueError:
    ALLOWED_USER_ID = 0

# Authentication & Storage
AUTH_FILE = "auth.json"

# Credentials
AZURE_EMAIL = os.getenv("AZURE_EMAIL", "example@example.com")
AZURE_PASSWORD = os.getenv("AZURE_PASSWORD", "Password123")

# URLs
BASE_URL = "https://productionplanning-ahq-v2.owex.oliverwyman.com"
DASHBOARD_URL = f"{BASE_URL}/#/plan-dashboard"
PRODUCTION_TASKS_URL = f"{BASE_URL}/#/production-tasks"

# Timing
PAGE_TIMEOUT = 30000  # 30 seconds

# Selectors
SELECTORS = {
    'food_lion_button': 'button.btn-food-lion',
    'dashboard_cards': '.card-plan',
    'card_title': '.plan-title',
    'completion_percentage': '.plan-percent-num',
    'items_left': '.plan-stat-remaining',
    'viewer_nav_link': 'a[href="#/production-tasks"]',
    'plan_selector_buttons': '.btn-group button.btn',
    'task_rows': 'tbody[data-v-4461cc4c] > tr',
    'item_checkbox_cell': 'td.checkbox',
    'unchecked_icon': 'i.fa-square.fa-2x',
    'checked_icon': 'i.fa-check-square.fa-2x',
    'email_input': 'input[type="email"]',
    'password_input': 'input[type="password"]',
    'submit_button': 'input[type="submit"], button[type="submit"]',
    'verification_code_input': 'input[name="otc"], input[type="tel"]',
}

# ============================================================================
# LOGGING
# ============================================================================

def setup_logging():
    """Configure logging"""
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

logger = logging.getLogger(__name__)

# ============================================================================
# PRODUCTION MONITOR
# ============================================================================

class ProductionMonitor:
    def __init__(self, telegram_chat_id: Optional[int] = None, telegram_app=None):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.telegram_chat_id = telegram_chat_id
        self.telegram_app = telegram_app
        self.waiting_for_2fa = False
        self.two_fa_code = None

    async def send_telegram(self, message: str):
        """Send message to Telegram"""
        if self.telegram_app and self.telegram_chat_id:
            try:
                await self.telegram_app.bot.send_message(chat_id=self.telegram_chat_id, text=message)
            except Exception as e:
                logger.error(f"Error sending Telegram message: {e}")
        logger.info(message)

    async def initialize(self):
        """Initialize Playwright browser"""
        await self.send_telegram("🚀 Initializing browser...")
        self.playwright = await async_playwright().start()

        # Try to launch headless browser
        try:
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox']
            )
        except Exception as e:
            await self.send_telegram(f"❌ Browser launch failed: {e}")
            raise

        # Load or create new context
        auth_file = Path(AUTH_FILE)
        if auth_file.exists():
            self.context = await self.browser.new_context(storage_state=AUTH_FILE)
        else:
            self.context = await self.browser.new_context()

        self.context.set_default_timeout(PAGE_TIMEOUT)
        self.page = await self.context.new_page()

    async def save_session(self):
        """Save session"""
        if self.context:
            await self.context.storage_state(path=AUTH_FILE)
            logger.info(f"Session saved to {AUTH_FILE}")

    async def handle_login(self):
        """Azure login with 2FA"""
        await self.send_telegram("🔐 Logging in...")
        try:
            await self.page.goto(BASE_URL, wait_until='networkidle')

            # Select company
            try:
                btn = await self.page.wait_for_selector(SELECTORS['food_lion_button'], timeout=5000)
                await btn.click()
                await asyncio.sleep(2)
            except PlaywrightTimeout:
                pass

            # Email
            email_input = await self.page.wait_for_selector(SELECTORS['email_input'], timeout=10000)
            await email_input.fill(AZURE_EMAIL)

            submit = await self.page.query_selector(SELECTORS['submit_button'])
            if submit:
                await submit.click()
                await asyncio.sleep(2)

            # Password
            password_input = await self.page.wait_for_selector(SELECTORS['password_input'], timeout=10000)
            await password_input.fill(AZURE_PASSWORD)

            submit = await self.page.query_selector(SELECTORS['submit_button'])
            if submit:
                await submit.click()
                await asyncio.sleep(3)

            # Check 2FA
            try:
                code_input = await self.page.wait_for_selector(SELECTORS['verification_code_input'], timeout=5000)
                await self.send_telegram("📱 2FA required. Send me your code.")
                self.waiting_for_2fa = True

                for _ in range(60):
                    if self.two_fa_code:
                        break
                    await asyncio.sleep(2)

                if not self.two_fa_code:
                    await self.send_telegram("❌ 2FA timeout.")
                    return False

                await code_input.fill(self.two_fa_code)
                submit = await self.page.query_selector(SELECTORS['submit_button'])
                if submit:
                    await submit.click()

                self.waiting_for_2fa = False
                self.two_fa_code = None
            except PlaywrightTimeout:
                pass

            await self.page.wait_for_url('**/plan-dashboard', timeout=30000)
            await self.send_telegram("✅ Login successful!")
            await self.save_session()
            return True

        except Exception as e:
            await self.send_telegram(f"❌ Login error: {e}")
            logger.error("Login error", exc_info=True)
            return False

    async def check_dashboard(self):
        """Scrape dashboard"""
        await self.send_telegram("📊 Checking dashboard...")
        try:
            await self.page.goto(DASHBOARD_URL, wait_until='networkidle')
            await self.page.wait_for_selector(SELECTORS['dashboard_cards'], timeout=10000)

            cards = await self.page.query_selector_all(SELECTORS['dashboard_cards'])
            categories = []

            for card in cards:
                try:
                    title_elem = await card.query_selector(SELECTORS['card_title'])
                    category = await title_elem.inner_text() if title_elem else "Unknown"

                    percent_elem = await card.query_selector(SELECTORS['completion_percentage'])
                    completion = int((await percent_elem.inner_text()).strip()) if percent_elem else 0

                    items_elem = await card.query_selector(SELECTORS['items_left'])
                    items_text = (await items_elem.inner_text()) if items_elem else "(0"
                    items_left = int(items_text.strip('()')) if items_text else 0

                    categories.append({
                        "name": category.strip(),
                        "completion": completion,
                        "items_left": items_left
                    })
                except Exception as e:
                    logger.warning(f"Card parse error: {e}")

            return categories
        except Exception as e:
            await self.send_telegram(f"❌ Dashboard error: {e}")
            return []

    async def run_full_check(self):
        """Main automation logic"""
        try:
            await self.initialize()

            if not await self.handle_login():
                await self.send_telegram("❌ Login failed.")
                await self.cleanup()
                return

            categories = await self.check_dashboard()
            if not categories:
                await self.send_telegram("❌ No dashboard data found.")
                await self.cleanup()
                return

            summary = "📋 Dashboard Summary:\n"
            for cat in categories:
                summary += f" • {cat['name']}: {cat['completion']}% ({cat['items_left']} left)\n"
            await self.send_telegram(summary)

            await self.send_telegram("🎯 Processing incomplete categories...")
            await self.cleanup()
        except Exception as e:
            await self.send_telegram(f"❌ Main process error: {e}")
            await self.cleanup()

    async def cleanup(self):
        """Close resources"""
        try:
            await self.save_session()
            if self.page: await self.page.close()
            if self.context: await self.context.close()
            if self.browser: await self.browser.close()
            if self.playwright: await self.playwright.stop()
            logger.info("Cleaned up Playwright session.")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# ============================================================================
# TELEGRAM HANDLERS
# ============================================================================

active_monitor: Optional[ProductionMonitor] = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    await update.message.reply_text(
        "👋 Production Bot Ready\n"
        "/run - Start process\n"
        "/status - Check status\n\n"
        "If 2FA required, send your code directly."
    )

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_monitor
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return

    if active_monitor:
        await update.message.reply_text("⚠️ Already running!")
        return

    await update.message.reply_text("🚀 Starting automation...")
    active_monitor = ProductionMonitor(update.effective_chat.id, context.application)
    asyncio.create_task(active_monitor.run_full_check())
    await asyncio.sleep(2)
    active_monitor = None

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized")
        return
    msg = "✅ Running" if active_monitor else "💤 Idle"
    await update.message.reply_text(msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global active_monitor
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    if active_monitor and active_monitor.waiting_for_2fa:
        code = update.message.text.strip()
        if code.isdigit() and 4 <= len(code) <= 8:
            active_monitor.two_fa_code = code
            await update.message.reply_text("✅ Code received.")
        else:
            await update.message.reply_text("⚠️ Invalid code format.")

# ============================================================================
# MAIN
# ============================================================================

def main():
    setup_logging()
    logger.info("Starting Telegram bot...")

    if not TELEGRAM_BOT_TOKEN:
        logger.error("❌ TELEGRAM_BOT_TOKEN missing. Set it in Railway variables.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is running...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
