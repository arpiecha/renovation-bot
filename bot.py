import os
import json
import logging
import base64
import anthropic
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io
import gspread

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_CREDS_JSON = os.environ["GOOGLE_CREDS_JSON"]
GOOGLE_DRIVE_FOLDER_ID = os.environ["GOOGLE_DRIVE_FOLDER_ID"]
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]

CATEGORIES = ["Tiles / Flooring", "Plumbing", "Electrical", "Tools", "Labor", "Other"]

SYSTEM_PROMPT = """You are a receipt analyzer for a home renovation project. 
When given a receipt image, extract the following and respond ONLY with a JSON object (no markdown, no extra text):

{
  "store": "store name",
  "date": "YYYY-MM-DD",
  "total": 0.00,
  "type": "purchase or return",
  "category": "one of: Tiles / Flooring, Plumbing, Electrical, Tools, Labor, Other",
  "items": ["item1", "item2"],
  "notes": "brief note about what was bought"
}

Rules:
- type must be "purchase" or "return"
- category must match exactly one of the options
- If date not visible, use today's date
- If total not clear, use 0.00
- Items should be the main products purchased
- For returns, total should still be positive (type field indicates it's a return)
"""

pending_corrections = {}

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

def upload_to_drive(image_bytes: bytes, filename: str, mime_type: str) -> str:
    drive_service, _ = get_google_services()
    file_metadata = {
        "name": filename,
        "parents": [GOOGLE_DRIVE_FOLDER_ID]
    }
    media = MediaIoBaseUpload(io.BytesIO(image_bytes), mimetype=mime_type)
    file = drive_service.files().create(
        body=file_metadata, media_body=media, fields="id, webViewLink"
    ).execute()
    drive_service.permissions().create(
        fileId=file["id"],
        body={"type": "anyone", "role": "reader"}
    ).execute()
    return file.get("webViewLink", "")

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
    message = client.messages.create(
        model="claude-sonnet-4-20250514",
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
    text = message.content[0].text.strip().replace("```json", "").replace("```", "").strip()
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
        logger.error(f"Error handling photo: {e}")
        await update.message.reply_text(
            "❌ Sorry, I couldn't read that receipt. Try a clearer photo with good lighting."
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
        await query.edit_message_text("💾 Saving receipt...")
        try:
            filename = f"receipt_{receipt['store'].replace(' ', '_')}_{receipt['date']}_{int(datetime.now().timestamp())}.jpg"
            drive_link = upload_to_drive(pending["image_bytes"], filename, pending["mime_type"])
            append_to_sheet(receipt, drive_link)
            sign = "-" if receipt["type"] == "return" else "+"
            await query.edit_message_text(
                f"✅ *Receipt saved!*\n\n"
                f"🏪 {receipt['store']} — {sign}${receipt['total']:.2f}\n"
                f"📂 {receipt['category']}\n"
                f"🗂 [View in Drive]({drive_link})",
                parse_mode="Markdown"
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

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Send me a *photo* of a receipt to get started!\n\nUse /start to see instructions.",
        parse_mode="Markdown"
    )

def main():
    ensure_sheet_headers()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
