import os
import re
import io
import json
import shutil
import logging
import tempfile
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer

import pymupdf
from PIL import Image
import pytesseract
from dotenv import load_dotenv

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

BRAND_NAME = os.getenv("BRAND_NAME", "Raj Exam Zone")
DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(BRAND_NAME)

PROFILE_FILE = DATA_DIR / "profiles.json"


def load_profiles():
    if not PROFILE_FILE.exists():
        return {}
    try:
        return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_profiles(data):
    PROFILE_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


profiles = load_profiles()


def user_profile(user):
    uid = str(user.id)
    profiles.setdefault(uid, {
        "name": user.full_name,
        "username": user.username or "",
        "credits": 0,
    })
    profiles[uid]["name"] = user.full_name
    profiles[uid]["username"] = user.username or ""
    save_profiles(profiles)
    return profiles[uid]


def is_admin(update: Update):
    return bool(
        update.effective_user
        and update.effective_user.id in ADMIN_IDS
    )


def job_dir(context, user_id):
    root = Path(tempfile.gettempdir()) / "raj_exam_zone" / str(user_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def clear_job_dir(path):
    if path.exists():
        for p in path.iterdir():
            try:
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
            except Exception:
                pass


# -----------------------------
# PDF helpers
# -----------------------------

def merge_pdfs(pdf_paths, output):
    out = pymupdf.open()
    try:
        for path in pdf_paths:
            src = pymupdf.open(path)
            try:
                out.insert_pdf(src)
            finally:
                src.close()
        out.save(output, garbage=4, deflate=True)
    finally:
        out.close()


def images_to_pdf(image_paths, output):
    out = pymupdf.open()
    try:
        for image_path in image_paths:
            imgdoc = pymupdf.open(image_path)
            try:
                pdfbytes = imgdoc.convert_to_pdf()
            finally:
                imgdoc.close()

            imgpdf = pymupdf.open("pdf", pdfbytes)
            try:
                out.insert_pdf(imgpdf)
            finally:
                imgpdf.close()

        out.save(output, garbage=4, deflate=True)
    finally:
        out.close()


def add_text_watermark(pdf_in, pdf_out, text, opacity=0.25):
    doc = pymupdf.open(pdf_in)
    try:
        for page in doc:
            rect = page.rect
            fontsize = max(
                18,
                min(48, rect.width / max(12, len(text) * 0.8))
            )
            box = pymupdf.Rect(
                rect.width * 0.15,
                rect.height * 0.43,
                rect.width * 0.85,
                rect.height * 0.57,
            )
            page.insert_textbox(
                box,
                text,
                fontsize=fontsize,
                fontname="helv",
                color=(0.45, 0.45, 0.45),
                fill_opacity=opacity,
                align=pymupdf.TEXT_ALIGN_CENTER,
                overlay=True,
            )
        doc.save(pdf_out, garbage=4, deflate=True)
    finally:
        doc.close()


def add_image_watermark(pdf_in, pdf_out, image_path, opacity=0.20):
    doc = pymupdf.open(pdf_in)
    try:
        for page in doc:
            r = page.rect
            w = r.width * 0.48
            h = w
            x0 = (r.width - w) / 2
            y0 = (r.height - h) / 2
            page.insert_image(
                pymupdf.Rect(x0, y0, x0 + w, y0 + h),
                filename=str(image_path),
                overlay=True,
            )
        doc.save(pdf_out, garbage=4, deflate=True)
    finally:
        doc.close()


def add_link(pdf_in, pdf_out, url, page_no=1, rect=None):
    doc = pymupdf.open(pdf_in)
    try:
        if page_no < 1 or page_no > len(doc):
            raise ValueError("Invalid page number")

        page = doc[page_no - 1]

        if rect is None:
            r = page.rect
            rect = pymupdf.Rect(
                r.width * 0.10,
                r.height * 0.80,
                r.width * 0.90,
                r.height * 0.90,
            )

        page.insert_link({
            "kind": pymupdf.LINK_URI,
            "from": rect,
            "uri": url,
        })

        doc.save(pdf_out, garbage=4, deflate=True)
    finally:
        doc.close()


def remove_links(pdf_in, pdf_out):
    doc = pymupdf.open(pdf_in)
    try:
        for page in doc:
            links = list(page.get_links())
            for link in links:
                page.delete_link(link)
        doc.save(pdf_out, garbage=4, deflate=True)
    finally:
        doc.close()


def add_branding_bar(pdf_in, pdf_out, top_text, bottom_text):
    doc = pymupdf.open(pdf_in)
    try:
        for page in doc:
            r = page.rect
            bar_h = min(32, r.height * 0.035)

            page.draw_rect(
                pymupdf.Rect(0, 0, r.width, bar_h),
                color=(0.08, 0.08, 0.08),
                fill=(0.08, 0.08, 0.08),
                overlay=True,
            )

            page.insert_text(
                (12, bar_h - 9),
                top_text,
                fontsize=10,
                fontname="helv",
                color=(1, 1, 1),
                overlay=True,
            )

            page.draw_rect(
                pymupdf.Rect(
                    0, r.height - bar_h, r.width, r.height
                ),
                color=(0.08, 0.08, 0.08),
                fill=(0.08, 0.08, 0.08),
                overlay=True,
            )

            page.insert_text(
                (12, r.height - 10),
                bottom_text,
                fontsize=10,
                fontname="helv",
                color=(1, 1, 1),
                overlay=True,
            )

        doc.save(pdf_out, garbage=4, deflate=True)
    finally:
        doc.close()


def add_cover(pdf_in, cover_image, pdf_out):
    cover_doc = pymupdf.open()
    try:
        imgdoc = pymupdf.open(cover_image)
        try:
            pdfbytes = imgdoc.convert_to_pdf()
        finally:
            imgdoc.close()

        imgpdf = pymupdf.open("pdf", pdfbytes)
        try:
            cover_doc.insert_pdf(imgpdf)
        finally:
            imgpdf.close()

        src = pymupdf.open(pdf_in)
        try:
            cover_doc.insert_pdf(src)
        finally:
            src.close()

        cover_doc.save(pdf_out, garbage=4, deflate=True)
    finally:
        cover_doc.close()


def ocr_pdf(pdf_in):
    doc = pymupdf.open(pdf_in)
    chunks = []

    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(
                matrix=pymupdf.Matrix(1.5, 1.5),
                alpha=False
            )
            img = Image.open(io.BytesIO(pix.tobytes("png")))

            try:
                text = pytesseract.image_to_string(
                    img,
                    lang="eng+hin"
                )
            except Exception:
                text = pytesseract.image_to_string(
                    img,
                    lang="eng"
                )

            chunks.append(
                f"\n--- PAGE {i + 1} ---\n{text.strip()}"
            )
    finally:
        doc.close()

    return "\n".join(chunks).strip()


# -----------------------------
# Menus
# -----------------------------

MAIN_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton(
            "📚 PDF Tools",
            callback_data="pdf_tools"
        ),
        InlineKeyboardButton(
            "🖼 Image Tools",
            callback_data="image_tools"
        ),
    ],
    [
        InlineKeyboardButton(
            "👤 Profile",
            callback_data="profile"
        ),
        InlineKeyboardButton(
            "⚙️ Preferences",
            callback_data="preferences"
        ),
    ],
    [
        InlineKeyboardButton(
            "📤 Post Manager",
            callback_data="post_manager"
        ),
        InlineKeyboardButton(
            "🎁 Invite",
            callback_data="invite"
        ),
    ],
    [
        InlineKeyboardButton(
            "❓ Help",
            callback_data="help"
        ),
    ],
])


PDF_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton(
            "📎 Merge PDFs",
            callback_data="merge_pdf"
        ),
        InlineKeyboardButton(
            "🔗 Add Link",
            callback_data="add_link"
        ),
    ],
    [
        InlineKeyboardButton(
            "❌ Remove Links",
            callback_data="remove_link"
        ),
        InlineKeyboardButton(
            "🖊 Text Watermark",
            callback_data="add_watermark"
        ),
    ],
    [
        InlineKeyboardButton(
            "🖼 Image Watermark",
            callback_data="image_watermark"
        ),
        InlineKeyboardButton(
            "🔗 Watermark Link",
            callback_data="watermark_link"
        ),
    ],
    [
        InlineKeyboardButton(
            "📑 Add Cover",
            callback_data="set_cover"
        ),
        InlineKeyboardButton(
            "⭐ Branding Bar",
            callback_data="branding_bar"
        ),
    ],
    [
        InlineKeyboardButton(
            "🔎 PDF OCR",
            callback_data="pdf_ocr"
        ),
    ],
    [
        InlineKeyboardButton(
            "⬅️ Main Menu",
            callback_data="main"
        ),
    ],
])


IMAGE_MENU = InlineKeyboardMarkup([
    [
        InlineKeyboardButton(
            "🖼 Images → PDF",
            callback_data="image_to_pdf"
        ),
    ],
    [
        InlineKeyboardButton(
            "⬅️ Main Menu",
            callback_data="main"
        ),
    ],
])


# -----------------------------
# Utility message helper
# -----------------------------

async def send_or_edit(update, text, reply_markup=None):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=reply_markup
            )
        except Exception:
            await update.callback_query.message.reply_text(
                text,
                reply_markup=reply_markup
            )
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=reply_markup
        )


# -----------------------------
# Command handlers
# -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_profile(update.effective_user)

    await update.message.reply_text(
        f"👋 Welcome to {BRAND_NAME}!\n\n"
        "PDF, image conversion, OCR, watermark, links "
        "और branding tools के लिए नीचे menu चुनें.",
        reply_markup=MAIN_MENU,
    )


async def pdf_tools(update, context):
    await send_or_edit(
        update,
        "📚 PDF Tools\n\nअपना tool चुनें:",
        PDF_MENU
    )


async def image_tools(update, context):
    await send_or_edit(
        update,
        "🖼 Image Tools\n\nअपना tool चुनें:",
        IMAGE_MENU
    )


async def profile(update, context):
    p = user_profile(update.effective_user)

    await send_or_edit(
        update,
        f"👤 Profile\n\n"
        f"Name: {p['name']}\n"
        f"Username: @{p['username'] or '—'}\n"
        f"User ID: {update.effective_user.id}\n"
        f"Credits: {p['credits']}",
        MAIN_MENU,
    )


async def preferences(update, context):
    await send_or_edit(
        update,
        "⚙️ Preferences\n\n"
        "इस starter version में profile data automatically save होता है.\n"
        "Language, default watermark और branding settings "
        "बाद में जोड़ी जा सकती हैं.",
        MAIN_MENU,
    )


async def invite(update, context):
    bot_username = (await context.bot.get_me()).username
    link = (
        f"https://t.me/{bot_username}"
        f"?start=ref_{update.effective_user.id}"
    )

    await send_or_edit(
        update,
        f"🎁 Invite\n\nअपना referral link:\n{link}",
        MAIN_MENU,
    )


async def help_cmd(update, context):
    await send_or_edit(
        update,
        "❓ Help\n\n"
        "1. /pdf_tools → PDF tools\n"
        "2. /image_tools → Image tools\n"
        "3. /pdf_ocr → PDF से text\n"
        "4. /add_watermark → text watermark\n"
        "5. /add_link → PDF link\n"
        "6. /remove_link → PDF links हटाएँ\n"
        "7. /branding_bar → top/bottom branding\n\n"
        "किसी tool के बाद bot जो file मांगे, वही upload करें.",
        MAIN_MENU,
    )


# -----------------------------
# Tool state machine
# -----------------------------

ACTION_INSTRUCTIONS = {
    "merge_pdf":
        "📎 Merge PDFs\n\n"
        "अब एक या एक से अधिक PDF files भेजें.\n"
        "जब सभी भेज दें, /done लिखें.",

    "image_to_pdf":
        "🖼 Images → PDF\n\n"
        "अब images भेजें. सभी images मिलने के बाद /done लिखें.",

    "set_cover":
        "📑 Add Cover\n\n"
        "पहले PDF भेजें, फिर cover image भेजें.",

    "add_link":
        "🔗 Add Link\n\n"
        "पहले PDF भेजें. उसके बाद message में URL भेजें.",

    "remove_link":
        "❌ Remove Links\n\nअब PDF भेजें.",

    "add_watermark":
        "🖊 Text Watermark\n\n"
        "पहले PDF भेजें. उसके बाद watermark text भेजें.",

    "image_watermark":
        "🖼 Image Watermark\n\n"
        "पहले PDF भेजें, फिर watermark image भेजें.",

    "watermark_link":
        "🔗 Watermark Link\n\n"
        "पहले PDF भेजें, फिर URL भेजें.",

    "branding_bar":
        "⭐ Branding Bar\n\n"
        "पहले PDF भेजें. फिर message में format भेजें:\n"
        "TOP | BOTTOM",

    "pdf_ocr":
        "🔎 PDF OCR\n\nअब PDF भेजें.",
}


async def start_action(update, context, action):
    context.user_data.clear()
    context.user_data["action"] = action
    context.user_data["files"] = []

    await send_or_edit(
        update,
        ACTION_INSTRUCTIONS.get(
            action,
            "Tool started."
        ),
        PDF_MENU
    )


async def done(update, context):
    action = context.user_data.get("action")
    files = context.user_data.get("files", [])

    if action not in ("merge_pdf", "image_to_pdf"):
        await update.message.reply_text(
            "इस tool के लिए /done की जरूरत नहीं है."
        )
        return

    if not files:
        await update.message.reply_text(
            "पहले files भेजें."
        )
        return

    work = job_dir(
        context,
        update.effective_user.id
    )

    output = work / (
        "merged.pdf"
        if action == "merge_pdf"
        else "images.pdf"
    )

    try:
        if action == "merge_pdf":
            merge_pdfs(files, output)
        else:
            images_to_pdf(files, output)

        with output.open("rb") as f:
            await update.message.reply_document(
                document=f,
                caption="✅ तैयार है."
            )

    except Exception as e:
        log.exception("Tool failed")
        await update.message.reply_text(
            f"❌ Error: {e}"
        )

    finally:
        clear_job_dir(work)
        context.user_data.clear()


async def receive_document(update, context):
    action = context.user_data.get("action")

    if not action:
        return

    doc = update.message.document
    filename = doc.file_name or "file"

    work = job_dir(
        context,
        update.effective_user.id
    )

    safe_name = Path(filename).name
    path = work / safe_name

    pdf_actions = {
        "merge_pdf",
        "pdf_ocr",
        "remove_link",
        "add_link",
        "add_watermark",
        "image_watermark",
        "watermark_link",
        "branding_bar",
        "set_cover",
    }

    if action in pdf_actions:
        is_pdf = filename.lower().endswith(".pdf")
        is_cover_image = (
            action == "set_cover"
            and filename.lower().endswith(
                (".png", ".jpg", ".jpeg", ".webp")
            )
        )

        if not is_pdf and not is_cover_image:
            await update.message.reply_text(
                "⚠️ इस step पर PDF file चाहिए."
            )
            return

    tg_file = await doc.get_file()
    await tg_file.download_to_drive(path)

    if action in ("merge_pdf", "image_to_pdf"):
        context.user_data.setdefault(
            "files", []
        ).append(str(path))

        await update.message.reply_text(
            f"✅ {filename} received.\n"
            "और files भेजें या /done लिखें."
        )
        return

    context.user_data["pdf"] = str(path)

    if action == "pdf_ocr":
        await process_pdf_ocr(update, context)

    elif action == "remove_link":
        await process_remove_links(update, context)

    elif action == "add_link":
        await update.message.reply_text(
            "अब URL भेजें, जैसे: https://example.com"
        )

    elif action == "add_watermark":
        await update.message.reply_text(
            "अब watermark text भेजें."
        )

    elif action == "watermark_link":
        await update.message.reply_text(
            "अब URL भेजें."
        )

    elif action == "branding_bar":
        await update.message.reply_text(
            "अब भेजें: TOP TEXT | BOTTOM TEXT"
        )

    elif action == "set_cover":
        context.user_data["waiting_for_cover"] = True
        await update.message.reply_text(
            "अब cover image भेजें."
        )

    elif action == "image_watermark":
        context.user_data["waiting_for_watermark_image"] = True
        await update.message.reply_text(
            "अब watermark image भेजें."
        )


async def receive_photo(update, context):
    action = context.user_data.get("action")

    if not action:
        return

    work = job_dir(
        context,
        update.effective_user.id
    )

    if action == "image_to_pdf":
        file = await update.message.photo[-1].get_file()

        path = work / (
            f"image_"
            f"{len(context.user_data.get('files', [])) + 1}.jpg"
        )

        await file.download_to_drive(path)

        context.user_data.setdefault(
            "files", []
        ).append(str(path))

        await update.message.reply_text(
            "✅ Image received. और images भेजें या /done लिखें."
        )

    elif (
        action == "set_cover"
        and context.user_data.get("waiting_for_cover")
    ):
        file = await update.message.photo[-1].get_file()
        path = work / "cover.jpg"

        await file.download_to_drive(path)

        context.user_data["cover"] = str(path)

        await process_set_cover(update, context)

    elif (
        action == "image_watermark"
        and context.user_data.get(
            "waiting_for_watermark_image"
        )
    ):
        file = await update.message.photo[-1].get_file()
        path = work / "watermark.jpg"

        await file.download_to_drive(path)

        context.user_data["watermark"] = str(path)

        await process_image_watermark(update, context)


async def receive_text(update, context):
    action = context.user_data.get("action")

    if not action:
        return

    text = update
