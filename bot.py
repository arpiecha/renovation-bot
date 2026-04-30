import os
import json
import logging
import base64
import anthropic
from datetime import datetime
import threading
import schedule
import time
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
import dropbox


logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
DROPBOX_ACCESS_TOKEN = os.environ["DROPBOX_ACCESS_TOKEN"]

GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

CATEGORIES = ["Materials", "Labor", "Mortgage", "MISC"]

SYSTEM_PROMPT = """You are a receipt analyzer for a home renovation project.
When given a receipt image, extract the following and respond ONLY with a JSON object (no markdown, no extra text):

{
  "store": "store name",
  "date": "YYYY-MM-DD",
  "total": 0.00,
  "type": "purchase or return",
  "category": "one of: Materials, Labor, Mortgage, MISC",
  "items": ["item1", "item2"],
  "notes": "brief note about what was bought"
}

Rules:
- type must be "purchase" or "return"
- category must match exactly one of: Materials, Labor, Mortgage, MISC
- If date not visible, use today's date
- If total not clear, use 0.00
- Items should be the main products purchased
- For returns, total should still be positive (type field indicates it's a return)

Category guide:
- Materials: any physical supplies, tools, hardware, tiles, lumber, paint, plumbing parts, electrical parts, etc.
- Labor: contractor payments, installation fees, service charges, any work done by a person
- Mortgage: mortgage payments, interest, property taxes, insurance related to the property
- MISC: anything that doesn't clearly fit above — permits, fees, cleaning, disposal, etc.
"""

pending_corrections = {}
reminders = {}  # {chat_id: [{"name": "ComEd", "day": 15}]}

async def chatid(update, context):
    chat_id = update.message.chat_id
    await update.message.reply_text("Your Chat ID is: " + str(chat_id) + "\n\nSave this — you will need it for bill reminders.")

def get_google_services():
    creds_info = json.loads(GOOGLE_CREDS_JSON)
    scopes = [
        "https://www.googleapis.com/auth/drive",
        "https://www.googleapis.com/auth/spreadsheets"
    ]
    creds = Credentials.from_service_account_info(creds_info, scopes=scopes)
    drive_service = build("drive", "v3", credentials=creds)
    sheets_service = build("sheets", "v4", credentials=creds)
    return drive_service, sheets_service

def upload_to_dropbox(image_bytes: bytes, filename: str) -> str:
    try:
        dbx = dropbox.Dropbox(DROPBOX_ACCESS_TOKEN)
        path = f"/renovation_receipts/{filename}.jpg"
        dbx.files_upload(image_bytes, path, mode=dropbox.files.WriteMode.overwrite)
        # Try to create shared link, if it already exists get the existing one
        try:
            link_result = dbx.sharing_create_shared_link_with_settings(path)
            url = link_result.url
        except dropbox.exceptions.ApiError as e:
            # Link already exists, get it
            links = dbx.sharing_list_shared_links(path=path, direct_only=True)
            url = links.links[0].url if links.links else ""
        # Convert to direct view link
        url = url.replace("?dl=0", "?dl=1").replace("www.dropbox.com", "dl.dropboxusercontent.com")
        return url
    except Exception as e:
        logger.warning(f"Dropbox upload failed: {e}")
        return "Photo not saved"

def append_to_sheet(receipt: dict, drive_link: str):
    _, sheets_service = get_google_services()
    amount = -receipt["total"] if receipt["type"] == "return" else receipt["total"]
    row = [
        receipt["date"],
        receipt["store"],
        receipt["category"],
        receipt["type"].capitalize(),
        amount,
        ", ".join(receipt.get("items", [])),
        receipt.get("notes", ""),
        drive_link
    ]
    sheets_service.spreadsheets().values().append(
        spreadsheetId=GOOGLE_SHEET_ID,
        range="Sheet1!A:H",
        valueInputOption="USER_ENTERED",
        body={"values": [row]}
    ).execute()

def ensure_sheet_headers():
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range="Sheet1!A1:H1"
        ).execute()
        if not result.get("values"):
            headers = [["Date", "Store", "Category", "Type", "Amount ($)", "Items", "Notes", "Receipt Photo"]]
            sheets_service.spreadsheets().values().update(
                spreadsheetId=GOOGLE_SHEET_ID,
                range="Sheet1!A1:H1",
                valueInputOption="USER_ENTERED",
                body={"values": headers}
            ).execute()
    except Exception as e:
        logger.error(f"Error ensuring headers: {e}")

async def analyze_receipt(image_bytes: bytes, mime_type: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    b64 = base64.standard_b64encode(image_bytes).decode("utf-8")
    logger.info(f"Sending image to Claude, size: {len(image_bytes)} bytes, mime: {mime_type}")
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": b64}},
                {"type": "text", "text": "Analyze this receipt."}
            ]
        }]
    )
    raw = message.content[0].text
    logger.info(f"Claude response: {raw}")
    text = raw.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(text)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👷 *Renovation Receipt Tracker*\n\n"
        "Send me a photo of any receipt and I'll:\n"
        "• Read and categorize it automatically\n"
        "• Save the image to Google Drive\n"
        "• Log it to your spreadsheet\n\n"
        "Categories I track:\n"
        "🟦 Tiles / Flooring\n"
        "🔵 Plumbing\n"
        "🟡 Electrical\n"
        "🟣 Tools\n"
        "🩷 Labor\n"
        "⚫ Other\n\n"
        "Just send a photo to get started!",
        parse_mode="Markdown"
    )

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Got it! Analyzing your receipt...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        image_bytes = await file.download_as_bytearray()
        image_bytes = bytes(image_bytes)
        receipt = await analyze_receipt(image_bytes, "image/jpeg")
        chat_id = update.message.chat_id
        pending_corrections[chat_id] = {
            "receipt": receipt,
            "image_bytes": image_bytes,
            "mime_type": "image/jpeg",
            "message_id": update.message.message_id
        }
        sign = "-" if receipt["type"] == "return" else "+"
        emoji = "↩️" if receipt["type"] == "return" else "🛒"
        summary = (
            f"{emoji} *Receipt detected*\n\n"
            f"🏪 Store: {receipt['store']}\n"
            f"📅 Date: {receipt['date']}\n"
            f"💰 Amount: {sign}${receipt['total']:.2f}\n"
            f"📂 Category: {receipt['category']}\n"
            f"🛍 Items: {', '.join(receipt.get('items', [])) or 'N/A'}\n"
            f"📝 Notes: {receipt.get('notes', 'N/A')}\n\n"
            f"Is this correct?"
        )
        keyboard = [
            [InlineKeyboardButton("✅ Looks good, save it!", callback_data="confirm")],
            [InlineKeyboardButton("✏️ Fix category", callback_data="fix_category")],
            [InlineKeyboardButton("✏️ Fix type (purchase/return)", callback_data="fix_type")],
            [InlineKeyboardButton("❌ Discard", callback_data="discard")]
        ]
        await update.message.reply_text(
            summary,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error handling photo: {type(e).__name__}: {e}", exc_info=True)
        await update.message.reply_text(
            f"❌ Debug error: {type(e).__name__}: {str(e)[:300]}"
        )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    data = query.data

    if chat_id not in pending_corrections:
        await query.edit_message_text("Session expired. Please send the photo again.")
        return

    pending = pending_corrections[chat_id]
    receipt = pending["receipt"]

    if data == "confirm":
        await query.edit_message_text("💾 Saving entry...")
        try:
            filename = f"receipt_{receipt['store'].replace(' ', '_')}_{receipt['date']}_{int(datetime.now().timestamp())}"
            if pending["image_bytes"]:
                drive_link = upload_to_dropbox(pending["image_bytes"], filename)
                photo_line = f"\n🗂 [View photo]({drive_link})"
            else:
                drive_link = "Manual entry - no photo"
                photo_line = "\n📝 Manual entry — no photo"
            append_to_sheet(receipt, drive_link)
            sign = "-" if receipt["type"] == "return" else "+"
            await query.edit_message_text(
                f"✅ *Entry saved!*\n\n"
                f"🏪 {receipt['store']} — {sign}${receipt['total']:.2f}\n"
                f"📂 {receipt['category']}"
                f"{photo_line}",
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
            del pending_corrections[chat_id]
        except Exception as e:
            logger.error(f"Save error: {e}")
            await query.edit_message_text(f"❌ Error saving: {str(e)}\n\nCheck your Google credentials.")

    elif data == "fix_category":
        buttons = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in CATEGORIES]
        await query.edit_message_text(
            "Choose the correct category:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    elif data.startswith("cat_"):
        new_cat = data[4:]
        pending_corrections[chat_id]["receipt"]["category"] = new_cat
        keyboard = [[InlineKeyboardButton("✅ Save now", callback_data="confirm")],
                    [InlineKeyboardButton("✏️ Fix type", callback_data="fix_type")]]
        await query.edit_message_text(
            f"Category updated to: *{new_cat}*\n\nReady to save?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "fix_type":
        keyboard = [
            [InlineKeyboardButton("🛒 Purchase", callback_data="type_purchase")],
            [InlineKeyboardButton("↩️ Return", callback_data="type_return")]
        ]
        await query.edit_message_text("Is this a purchase or a return?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("type_"):
        new_type = data[5:]
        pending_corrections[chat_id]["receipt"]["type"] = new_type
        keyboard = [[InlineKeyboardButton("✅ Save now", callback_data="confirm")],
                    [InlineKeyboardButton("✏️ Fix category", callback_data="fix_category")]]
        await query.edit_message_text(
            f"Type updated to: *{new_type}*\n\nReady to save?",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data == "discard":
        del pending_corrections[chat_id]
        await query.edit_message_text("🗑 Receipt discarded.")


async def parse_manual_entry(text: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        system="""You are a receipt parser. The user will describe a purchase in natural language.
Extract details and respond ONLY with a JSON object, no markdown, no extra text:
{
  "store": "store or payee name",
  "date": "YYYY-MM-DD",
  "total": 0.00,
  "type": "purchase or return",
  "category": "one of: Materials, Labor, Mortgage, MISC",
  "items": ["item1"],
  "notes": "brief note"
}
Category guide:
- Materials: supplies, tools, hardware, tiles, lumber, paint, plumbing/electrical parts
- Labor: contractor payments, installation fees, service charges
- Mortgage: mortgage payments, interest, property taxes, insurance
- MISC: permits, fees, cleaning, disposal, anything else
If date not mentioned use today. If store not mentioned use 'Cash Payment'.""",
        messages=[{"role": "user", "content": text}]
    )
    raw = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    return json.loads(raw)

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 5:
        await update.message.reply_text(
            "📸 Send a photo of a receipt, or describe a cash payment like:\n\n"
            "_'Paid contractor $500 for labor today'_\n"
            "_'Home Depot $85 for pipes'_",
            parse_mode="Markdown"
        )
        return

    # Check if it's a cancel reminder request
    if any(kw in text.lower() for kw in ["cancel", "remove", "delete", "stop"]) and any(kw in text.lower() for kw in ["remind", "reminder", "bill", "comed", "nicor", "mortgage"]):
        await handle_cancel_reminder(update, text)
        return

    # Check if it's a reminder request
    reminder_keywords = ["remind", "reminder", "due", "bill", "pay", "mortgage", "comed", "nicor"]
    if any(kw in text.lower() for kw in reminder_keywords) and any(word in text.lower() for word in ["remind", "due", "every month", "monthly", "th", "st", "nd", "rd"]):
        await handle_reminder_request(update, text)
        return

    await update.message.reply_text("💭 Parsing your entry...")
    try:
        receipt = await parse_manual_entry(text)
        chat_id = update.message.chat_id
        pending_corrections[chat_id] = {
            "receipt": receipt,
            "image_bytes": None,
            "mime_type": None,
            "message_id": update.message.message_id
        }
        sign = "-" if receipt["type"] == "return" else "+"
        emoji = "↩️" if receipt["type"] == "return" else "💵"
        summary = (
            f"{emoji} *Manual entry detected*\n\n"
            f"🏪 Store: {receipt['store']}\n"
            f"📅 Date: {receipt['date']}\n"
            f"💰 Amount: {sign}${receipt['total']:.2f}\n"
            f"📂 Category: {receipt['category']}\n"
            f"🛍 Items: {', '.join(receipt.get('items', [])) or 'N/A'}\n"
            f"📝 Notes: {receipt.get('notes', 'N/A')}\n\n"
            f"Is this correct?"
        )
        keyboard = [
            [InlineKeyboardButton("✅ Looks good, save it!", callback_data="confirm")],
            [InlineKeyboardButton("✏️ Fix category", callback_data="fix_category")],
            [InlineKeyboardButton("✏️ Fix type (purchase/return)", callback_data="fix_type")],
            [InlineKeyboardButton("❌ Discard", callback_data="discard")]
        ]
        await update.message.reply_text(
            summary,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Error parsing manual entry: {e}")
        await update.message.reply_text(
            "❌ Couldn't parse that. Try something like:\n\n"
            "_'Paid contractor $500 for labor'_\n"
            "_'Home Depot $85 pipes and fittings'_",
            parse_mode="Markdown"
        )

async def handle_reminder_request(update, text: str):
    chat_id = update.message.chat_id
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=200,
        system="""Extract bill reminder info from the user message. Respond ONLY with JSON:
{"name": "bill name", "day": 15}
Day is the day of the month it is due. Name should be short like "ComEd", "Nicor", "Mortgage".""",
        messages=[{"role": "user", "content": text}]
    )
    raw = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
    parsed = json.loads(raw)
    
    if chat_id not in reminders:
        reminders[chat_id] = []
    
    # Remove existing reminder with same name
    reminders[chat_id] = [r for r in reminders[chat_id] if r["name"].lower() != parsed["name"].lower()]
    reminders[chat_id].append(parsed)
    save_reminders_to_sheet(chat_id, reminders[chat_id])
    
    await update.message.reply_text(
        f"✅ Got it! I'll remind you to pay *{parsed['name']}* on the *{parsed['day']}th of every month*.",
        parse_mode="Markdown"
    )

def save_reminders_to_sheet(chat_id, bills):
    try:
        _, sheets_service = get_google_services()
        # Clear existing reminders sheet and rewrite
        sheets_service.spreadsheets().values().clear(
            spreadsheetId=GOOGLE_SHEET_ID, range="Reminders!A:D"
        ).execute()
        headers = [["Bill Name", "Due Day", "Next Due Date", "Chat ID"]]
        rows = []
        for bill in bills:
            # Calculate next due date
            now = datetime.now()
            due_day = bill["day"]
            if now.day <= due_day:
                next_due = now.replace(day=due_day).strftime("%Y-%m-%d")
            else:
                if now.month == 12:
                    next_due = now.replace(year=now.year+1, month=1, day=due_day).strftime("%Y-%m-%d")
                else:
                    next_due = now.replace(month=now.month+1, day=due_day).strftime("%Y-%m-%d")
            rows.append([bill["name"], bill["day"], next_due, str(chat_id)])
        sheets_service.spreadsheets().values().update(
            spreadsheetId=GOOGLE_SHEET_ID,
            range="Reminders!A1",
            valueInputOption="USER_ENTERED",
            body={"values": headers + rows}
        ).execute()
    except Exception as e:
        logger.warning(f"Could not save reminders to sheet: {e}")

def load_reminders_from_sheet():
    try:
        _, sheets_service = get_google_services()
        result = sheets_service.spreadsheets().values().get(
            spreadsheetId=GOOGLE_SHEET_ID, range="Reminders!A2:D"
        ).execute()
        rows = result.get("values", [])
        loaded = {}
        for row in rows:
            if len(row) >= 4:
                chat_id = int(row[3])
                if chat_id not in loaded:
                    loaded[chat_id] = []
                loaded[chat_id].append({"name": row[0], "day": int(row[1])})
        return loaded
    except Exception as e:
        logger.warning(f"Could not load reminders from sheet: {e}")
        return {}

async def send_reminders(app):
    today = datetime.now().day
    for chat_id, bills in reminders.items():
        for bill in bills:
            if bill["day"] == today:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"🔔 *Reminder: {bill['name']} is due today!*\n\nDon't forget to pay and log it.",
                    parse_mode="Markdown"
                )

def run_scheduler(app):
    import asyncio
    schedule.every().day.at("09:00").do(lambda: asyncio.run(send_reminders(app)))
    while True:
        schedule.run_pending()
        time.sleep(60)

async def handle_cancel_reminder(update, text: str):
    chat_id = update.message.chat_id
    if chat_id not in reminders or not reminders[chat_id]:
        await update.message.reply_text("You have no active reminders to cancel.")
        return

    # Use AI to figure out which bill to cancel
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=100,
        system='Extract the bill name the user wants to cancel. Respond ONLY with the bill name, nothing else. Example: "ComEd" or "Nicor" or "Mortgage".',
        messages=[{"role": "user", "content": text}]
    )
    bill_name = message.content[0].text.strip()

    before = len(reminders[chat_id])
    reminders[chat_id] = [r for r in reminders[chat_id] if r["name"].lower() != bill_name.lower()]
    after = len(reminders[chat_id])

    if before > after:
        save_reminders_to_sheet(chat_id, reminders[chat_id])
        await update.message.reply_text("🗑 Reminder for " + bill_name + " has been cancelled.")
    else:
        # Show active reminders
        names = ", ".join([r["name"] for r in reminders[chat_id]]) or "none"
        await update.message.reply_text("Couldn't find a reminder for " + bill_name + ". Active reminders: " + names)

def main():
    ensure_sheet_headers()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("chatid", chatid))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    # Load reminders from sheet on startup
    global reminders
    reminders = load_reminders_from_sheet()
    logger.info(f"Loaded {sum(len(v) for v in reminders.values())} reminders from sheet")

    # Start reminder scheduler in background
    scheduler_thread = threading.Thread(target=run_scheduler, args=(app,), daemon=True)
    scheduler_thread.start()
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
