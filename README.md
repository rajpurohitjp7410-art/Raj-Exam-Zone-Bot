# Raj Exam Zone Telegram Bot

Python starter project for the Raj Exam Zone Telegram bot.

## Included

- `/start`
- `/pdf_tools`
- `/image_tools`
- `/profile`
- `/post_manager` (basic command hook; admin workflow can be expanded)
- `/preferences`
- `/invite`
- `/help`
- `/set_cover`
- `/add_link`
- `/remove_link`
- `/add_watermark`
- `/watermark_link`
- `/branding_bar`
- `/pdf_ocr`
- `/done`

PDF/image features currently implemented:

- Merge multiple PDFs
- Images to PDF
- Add a cover page from an image
- Add/remove PDF links
- Text watermark
- Image watermark
- Branding bars
- PDF OCR
- Basic persistent profile data

## Setup

1. Install Python 3.10+.
2. Create a virtual environment.
3. Install Python packages:

   `pip install -r requirements.txt`

4. Copy `.env.example` to `.env`.
5. Put your BotFather token in `BOT_TOKEN`.
6. For OCR, install the Tesseract OCR engine on the server and make sure it is on PATH. Hindi OCR requires the Hindi Tesseract language data (`hin`) as well.
7. Start:

   `python main.py`

The bot uses polling, so no webhook or domain is required for the first deployment.

## Important

Keep `.env` private. Never paste your BotFather token into a public GitHub repository or chat.

The current implementation is a working foundation. Production upgrades can add:
- admin-only post manager
- queues and progress messages
- file-size limits
- automatic cleanup
- per-user credits
- database storage
- custom watermark opacity/position
- PDF compression/splitting/rotation
- broadcast and scheduled posts
- subscription/payment logic
