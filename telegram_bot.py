"""
Production Planning Telegram Bot
Trigger your production task automation from your phone via Telegram
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
# CONFIGURATION CONSTANTS
# ============================================================================

# Telegram Configuration (GET FROM BOTFATHER)
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "YOUR_TELEGRAM_USER_ID"))  # Your Telegram user ID

# Authentication & Storage
AUTH_FILE = "auth.json"

# Credentials
AZURE_EMAIL = os.getenv("AZURE_EMAIL", "1135076@delhaize.com")
AZURE_PASSWORD = os.getenv("AZURE_PASSWORD", "Cart2025")

# URLs
BASE_URL = "https://productionplanning-ahq-v2.owex.oliverwyman.com"  # Company selection page
DASHBOARD_URL = f"{BASE_URL}/#/plan-dashboard"
PRODUCTION_TASKS_URL = f"{BASE_URL}/#/production-tasks"

# Timing Configuration
PAGE_TIMEOUT = 30000  # 30 seconds

# Selectors
SELECTORS = {
    # Company selection
    'food_lion_button': 'button.btn-food-lion',
    
    # Dashboard selectors
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
    
    # Login selectors
    'email_input': 'input[type="email"]',
    'password_input': 'input[type="password"]',
    'submit_button': 'input[type="submit"], button[type="submit"]',
    'verification_code_input': 'input[name="otc"], input[type="tel"]',  # Common 2FA input selectors
}

# ============================================================================
# LOGGING SETUP
# ============================================================================

def setup_logging():
    """Configure logging"""
    log_format = '[%(asctime)s %(levelname)s] %(message)s'
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        datefmt='%Y-%m-%d %H:%M:%S',
    )

logger = logging.getLogger(__name__)

# ============================================================================
# PRODUCTION MONITOR CLASS
# ============================================================================

class ProductionMonitor:
    def __init__(self, telegram_chat_id: Optional[int] = None):
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.telegram_chat_id = telegram_chat_id
        self.telegram_app = None
        self.waiting_for_2fa = False
        self.two_fa_code = None
        
    def set_telegram_app(self, app):
        """Set the Telegram app for sending messages"""
        self.telegram_app = app
        
    async def send_telegram(self, message: str):
        """Send message to Telegram"""
        if self.telegram_app and self.telegram_chat_id:
            try:
                await self.telegram_app.bot.send_message(
                    chat_id=self.telegram_chat_id,
                    text=message
                )
            except Exception as e:
                logger.error(f"Error sending Telegram message: {e}")
        logger.info(message)
    
    async def initialize(self):
        """Initialize Playwright browser"""
        await self.send_telegram("🚀 Initializing browser...")
        
        self.playwright = await async_playwright().start()
        
        # Launch browser in headless mode for server deployment
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )
        
        # Check for saved authentication
        auth_file = Path(AUTH_FILE)
        if auth_file.exists():
            logger.info(f"Loading session from {AUTH_FILE}")
            self.context = await self.browser.new_context(storage_state=AUTH_FILE)
        else:
            logger.info("No saved session found")
            self.context = await self.browser.new_context()
        
        self.context.set_default_timeout(PAGE_TIMEOUT)
        self.page = await self.context.new_page()
        
    async def save_session(self):
        """Save browser session"""
        if self.context:
            await self.context.storage_state(path=AUTH_FILE)
            logger.info(f"Session saved to {AUTH_FILE}")
    
    async def handle_login(self):
        """Handle Azure authentication with 2FA support"""
        await self.send_telegram("🔐 Attempting login...")
        
        try:
            # First, go to the main page to select company
            await self.page.goto(BASE_URL, wait_until='networkidle')
            
            # Check if already logged in
            if 'plan-dashboard' in self.page.url:
                await self.send_telegram("✅ Already logged in!")
                return True
            
            # Click Food Lion button if we're on company selection page
            try:
                food_lion_btn = await self.page.wait_for_selector(
                    SELECTORS['food_lion_button'], 
                    timeout=5000
                )
                await self.send_telegram("🏢 Selecting Food Lion...")
                await food_lion_btn.click()
                await asyncio.sleep(2)
            except PlaywrightTimeout:
                # Might already be past this screen
                pass
            
            # Enter email
            await self.send_telegram("📧 Entering email...")
            email_input = await self.page.wait_for_selector(SELECTORS['email_input'], timeout=10000)
            await email_input.fill(AZURE_EMAIL)
            
            # Click submit
            submit_btn = await self.page.query_selector(SELECTORS['submit_button'])
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(2)
            
            # Enter password
            await self.send_telegram("🔑 Entering password...")
            password_input = await self.page.wait_for_selector(SELECTORS['password_input'], timeout=10000)
            await password_input.fill(AZURE_PASSWORD)
            
            # Click submit
            submit_btn = await self.page.query_selector(SELECTORS['submit_button'])
            if submit_btn:
                await submit_btn.click()
                await asyncio.sleep(3)
            
            # Check for 2FA
            await self.send_telegram("📱 Checking for 2FA requirement...")
            
            # Wait a moment to see if 2FA is needed
            try:
                code_input = await self.page.wait_for_selector(
                    SELECTORS['verification_code_input'], 
                    timeout=5000
                )
                
                # 2FA is needed
                await self.send_telegram("🔐 2FA required! Please send me your verification code:")
                self.waiting_for_2fa = True
                
                # Wait for user to provide code (with timeout)
                for _ in range(60):  # Wait up to 2 minutes
                    if self.two_fa_code:
                        break
                    await asyncio.sleep(2)
                
                if not self.two_fa_code:
                    await self.send_telegram("❌ Timeout waiting for 2FA code")
                    return False
                
                # Enter the 2FA code
                await self.send_telegram("⌨️ Entering verification code...")
                await code_input.fill(self.two_fa_code)
                
                # Submit
                submit_btn = await self.page.query_selector(SELECTORS['submit_button'])
                if submit_btn:
                    await submit_btn.click()
                
                self.waiting_for_2fa = False
                self.two_fa_code = None
                
            except PlaywrightTimeout:
                # No 2FA needed, continue
                pass
            
            # Wait for dashboard to load
            await self.page.wait_for_url('**/plan-dashboard', timeout=30000)
            
            await self.send_telegram("✅ Login successful!")
            await self.save_session()
            return True
            
        except PlaywrightTimeout:
            await self.send_telegram("❌ Login timeout")
            return False
        except Exception as e:
            await self.send_telegram(f"❌ Login error: {str(e)}")
            logger.error(f"Login error: {e}", exc_info=True)
            return False
    
    async def check_dashboard(self):
        """Check dashboard and return categories"""
        await self.send_telegram("📊 Checking dashboard...")
        
        try:
            await self.page.goto(DASHBOARD_URL, wait_until='networkidle')
            await self.page.wait_for_selector(SELECTORS['dashboard_cards'], timeout=10000)
            
            cards = await self.page.query_selector_all(SELECTORS['dashboard_cards'])
            
            categories = []
            for card in cards:
                try:
                    title_elem = await card.query_selector(SELECTORS['card_title'])
                    category_name = await title_elem.inner_text() if title_elem else "Unknown"
                    
                    percent_elem = await card.query_selector(SELECTORS['completion_percentage'])
                    completion = await percent_elem.inner_text() if percent_elem else "0"
                    
                    items_left_elem = await card.query_selector(SELECTORS['items_left'])
                    items_left_text = await items_left_elem.inner_text() if items_left_elem else "(0"
                    try:
                        items_left = int(items_left_text.strip('()'))
                    except:
                        items_left = 0
                    
                    categories.append({
                        'name': category_name.strip(),
                        'completion': int(completion.strip()),
                        'items_left': items_left
                    })
                    
                except Exception as e:
                    logger.warning(f"Error parsing card: {e}")
            
            return categories
            
        except Exception as e:
            await self.send_telegram(f"❌ Error checking dashboard: {str(e)}")
            return []
    
    async def navigate_to_production_tasks(self, category_name: str):
        """Navigate to production tasks for category"""
        try:
            viewer_link = await self.page.query_selector(SELECTORS['viewer_nav_link'])
            if viewer_link:
                await viewer_link.click()
                await self.page.wait_for_load_state('networkidle')
            else:
                await self.page.goto(PRODUCTION_TASKS_URL, wait_until='networkidle')
            
            await self.page.wait_for_selector(SELECTORS['plan_selector_buttons'], timeout=10000)
            
            buttons = await self.page.query_selector_all(SELECTORS['plan_selector_buttons'])
            
            for button in buttons:
                button_text = await button.inner_text()
                if button_text.strip() == category_name:
                    await button.click()
                    await self.page.wait_for_load_state('networkidle')
                    await asyncio.sleep(1)
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error navigating: {e}")
            return False
    
    async def process_production_page(self, category_name: str):
        """Process production page and check items"""
        try:
            if not await self.navigate_to_production_tasks(category_name):
                await self.send_telegram(f"❌ Failed to navigate to {category_name}")
                return
            
            await self.send_telegram(f"📝 Processing: {category_name}")
            
            await self.page.wait_for_selector('tbody[data-v-4461cc4c]', timeout=10000)
            await asyncio.sleep(1)
            
            rows = await self.page.query_selector_all(SELECTORS['task_rows'])
            
            items_checked = 0
            items_total = 0
            
            for idx, row in enumerate(rows):
                try:
                    has_th = await row.query_selector('th')
                    if has_th:
                        continue
                    
                    checkbox_cell = await row.query_selector(SELECTORS['item_checkbox_cell'])
                    if not checkbox_cell:
                        continue
                    
                    unchecked_icon = await checkbox_cell.query_selector(SELECTORS['unchecked_icon'])
                    checked_icon = await checkbox_cell.query_selector(SELECTORS['checked_icon'])
                    
                    if unchecked_icon:
                        await unchecked_icon.click()
                        items_checked += 1
                        items_total += 1
                        await asyncio.sleep(0.8)
                    elif checked_icon:
                        items_total += 1
                        
                except Exception as e:
                    continue
            
            await self.send_telegram(f"✅ {category_name}: {items_checked} items checked (total: {items_total})")
            
        except Exception as e:
            await self.send_telegram(f"❌ Error processing {category_name}: {str(e)}")
    
    async def run_full_check(self):
        """Run complete check cycle"""
        try:
            await self.initialize()
            
            if not await self.handle_login():
                await self.send_telegram("❌ Login failed. Aborting.")
                await self.cleanup()
                return
            
            categories = await self.check_dashboard()
            
            if not categories:
                await self.send_telegram("❌ No categories found")
                await self.cleanup()
                return
            
            # Send summary
            summary = "📋 Dashboard Summary:\n"
            for cat in categories:
                summary += f"  • {cat['name']}: {cat['completion']}% ({cat['items_left']} left)\n"
            await self.send_telegram(summary)
            
            # Process incomplete categories
            processed = 0
            for category in categories:
                if category['completion'] < 100 and category['items_left'] > 0:
                    await self.process_production_page(category['name'])
                    processed += 1
            
            if processed == 0:
                await self.send_telegram("🎉 All categories already complete!")
            else:
                await self.send_telegram(f"🎉 Done! Processed {processed} categories.")
            
            await self.cleanup()
            
        except Exception as e:
            await self.send_telegram(f"❌ Error in main process: {str(e)}")
            await self.cleanup()
    
    async def cleanup(self):
        """Cleanup resources"""
        try:
            await self.save_session()
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# ============================================================================
# TELEGRAM BOT HANDLERS
# ============================================================================

active_monitor: Optional[ProductionMonitor] = None

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command"""
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized user")
        return
    
    await update.message.reply_text(
        "👋 Production Monitor Bot\n\n"
        "Commands:\n"
        "/run - Start production check\n"
        "/status - Check bot status\n\n"
        "When 2FA is required, just send me the code!"
    )

async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /run command"""
    global active_monitor
    
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized user")
        return
    
    if active_monitor:
        await update.message.reply_text("⚠️ A check is already running!")
        return
    
    await update.message.reply_text("🚀 Starting production check...")
    
    active_monitor = ProductionMonitor(telegram_chat_id=update.effective_chat.id)
    active_monitor.set_telegram_app(context.application)
    
    # Run in background
    asyncio.create_task(active_monitor.run_full_check())
    
    # Reset active monitor after completion
    await asyncio.sleep(2)
    active_monitor = None

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /status command"""
    if update.effective_user.id != ALLOWED_USER_ID:
        await update.message.reply_text("⛔ Unauthorized user")
        return
    
    if active_monitor:
        await update.message.reply_text("✅ Bot is running a check")
    else:
        await update.message.reply_text("💤 Bot is idle")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text messages (for 2FA codes)"""
    global active_monitor
    
    if update.effective_user.id != ALLOWED_USER_ID:
        return
    
    if active_monitor and active_monitor.waiting_for_2fa:
        code = update.message.text.strip()
        
        # Validate it looks like a code (numbers, 6-8 digits)
        if code.isdigit() and 4 <= len(code) <= 8:
            active_monitor.two_fa_code = code
            await update.message.reply_text("✅ Code received! Continuing...")
        else:
            await update.message.reply_text("⚠️ That doesn't look like a valid code. Please send just the numbers.")

# ============================================================================
# MAIN EXECUTION
# ============================================================================

async def main():
    """Main function to run the bot"""
    setup_logging()
    
    logger.info("Starting Telegram bot...")
    
    # Create application
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("run", run_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # Start bot
    logger.info("Bot is running...")
    await application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    asyncio.run(main())