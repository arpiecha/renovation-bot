# Renovation Receipt Tracker Bot — Setup Guide

## Files in this package
- `bot.py` — the main bot code
- `requirements.txt` — Python dependencies
- `railway.toml` — Railway deployment config

---

## Step 1: Get your Telegram bot token
1. Open Telegram → search @BotFather
2. Send `/newbot`
3. Follow prompts → copy your token

---

## Step 2: Get your Anthropic API key
1. Go to https://console.anthropic.com
2. API Keys → Create Key → copy it

---

## Step 3: Set up Google Drive & Sheets

### Create a Google Cloud service account:
1. Go to https://console.cloud.google.com
2. Create a new project (e.g. "Renovation Bot")
3. Enable these APIs:
   - Google Drive API
   - Google Sheets API
4. Go to IAM & Admin → Service Accounts → Create
5. Give it a name, click Done
6. Click the service account → Keys → Add Key → JSON
7. Download the JSON file — this is your GOOGLE_CREDS_JSON

### Create your Google Drive folder:
1. Go to Google Drive
2. Create a folder called "Renovation Receipts"
3. Right-click → Share → add your service account email (from the JSON file, field "client_email")
4. Give it Editor access
5. Open the folder → copy the ID from the URL:
   `https://drive.google.com/drive/folders/THIS_PART_IS_THE_ID`

### Create your Google Sheet:
1. Go to Google Sheets → create a new blank sheet
2. Share it with your service account email (Editor access)
3. Copy the Sheet ID from the URL:
   `https://docs.google.com/spreadsheets/d/THIS_PART_IS_THE_ID/edit`

---

## Step 4: Deploy to Railway

1. Go to https://railway.app → sign up with GitHub
2. New Project → Deploy from GitHub repo
   (upload these files to a GitHub repo first, or use Railway's CLI)
3. Add these Environment Variables in Railway dashboard:

| Variable | Value |
|---|---|
| TELEGRAM_TOKEN | your telegram bot token |
| ANTHROPIC_API_KEY | your anthropic api key |
| GOOGLE_CREDS_JSON | paste the entire contents of your service account JSON file |
| GOOGLE_DRIVE_FOLDER_ID | your drive folder ID |
| GOOGLE_SHEET_ID | your google sheet ID |

4. Deploy — Railway will install dependencies and start the bot automatically.

---

## Step 5: Test it!
1. Open Telegram → find your bot
2. Send `/start`
3. Send a photo of a receipt
4. Confirm or correct the details
5. Check your Google Sheet and Drive folder!

---

## Troubleshooting
- Bot not responding? Check Railway logs for errors
- Google auth failing? Make sure the service account has Editor access to both the folder and sheet
- Receipt misread? Use the "Fix category" or "Fix type" buttons — corrections are applied instantly
