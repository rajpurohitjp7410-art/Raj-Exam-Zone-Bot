import os
import re
import io
import json
import shutil
import logging
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont
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

# -----------------------------
# Small persistent user profile
# -----------------------------

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
    return bool(update.effective_user and update.effective_user.id in ADMIN_IDS)

# -----------------------------
# Temporary job helpers
# -----------------------------

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
    for path in pdf_paths:
        src = pymupdf.open(path)
        out.insert_pdf(src)
        src.close()
    out.save(output, garbage=4, deflate=True)
    out.close()

def images_to_pdf(image_paths, output):
    out = pymupdf.open()
    for image_path in image_paths:
        imgdoc = pymupdf.open(image_path)
        pdfbytes = imgdoc.convert_to_pdf()
        imgdoc.close()
        imgpdf = pymupdf.open("pdf", pdfbytes)
        out.insert_pdf(imgpdf)
        imgpdf.close()
    out.save(output, garbage=4, deflate=True)
    out.close()

def add_text_watermark(pdf_in, pdf_out, text, opacity=0.25):
    doc = pymupdf.open(pdf_in)
    for page in doc:
        rect = page.rect
        # diagonal-ish watermark is intentionally avoided; keep text level/clean.
        fontsize = max(18, min(48, rect.width / max(12, len(text) * 0.8)))
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
    doc.close()

def add_image_watermark(pdf_in, pdf_out, image_path, opacity=0.20):
    # PyMuPDF inserts the image as a page overlay. The image itself should
    # contain transparency if you want a precise opacity effect.
    doc = pymupdf.open(pdf_in)
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
    doc.close()

def add_link(pdf_in, pdf_out, url, page_no=1, rect=None):
    doc = pymupdf.open(pdf_in)
    page = doc[page_no - 1]
    if rect is None:
        r = page.rect
        rect = pymupdf.Rect(r.width * .10, r.height * .80,
                            r.width * .90, r.height * .90)
    page.insert_link({
        "kind": pymupdf.LINK_URI,
        "from": rect,
        "uri": url,
    })
    doc.save(pdf_out, garbage=4, deflate=True)
    doc.close()

def remove_links(pdf_in, pdf_out):
    doc = pymupdf.open(pdf_in)
    for page in doc:
        links = list(page.get_links())
        for link in links:
            page.delete_link(link)
    doc.save(pdf_out, garbage=4, deflate=True)
    doc.close()

def add_branding_bar(pdf_in, pdf_out, top_text, bottom_text):
    doc = pymupdf.open(pdf_in)
    for page in doc:
        r = page.rect
        bar_h = min(32, r.height * .035)
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
            pymupdf.Rect(0, r.height - bar_h, r.width, r.height),
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
    doc.close()

def add_cover(pdf_in, cover_image, pdf_out):
    cover_doc = pymupdf.open()
    imgdoc = pymupdf.open(cover_image)
    pdfbytes = imgdoc.convert_to_pdf()
    imgdoc.close()
    imgpdf = pymupdf.open("pdf", pdfbytes)
    cover_doc.insert_pdf(imgpdf)
    imgpdf.close()

    src = pymupdf.open(pdf_in)
    cover_doc.insert_pdf(src)
    src.close()

    cover_doc.save(pdf_out, garbage=4, deflate=True)
    cover_doc.close()

def ocr_pdf(pdf_in):
    doc = pymupdf.open(pdf_in)
    chunks = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        try:
            text = pytesseract.image_to_string(img, lang="eng+hin")
        except Exception:
            text = pytesseract.image_to_string(img, lang="eng")
        chunks.append(f"\n--- PAGE {i+1} ---\n{text.strip()}")
    doc.close()
    return "\n".join(chunks).strip()

# -----------------------------
# Menus
# -----------------------------

MAIN_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📚 PDF Tools", callback_data="pdf_tools"),
     InlineKeyboardButton("🖼 Image Tools", callback_data="image_tools")],
    [InlineKeyboardButton("👤 Profile", callback_data="profile"),
     InlineKeyboardButton("⚙️ Preferences", callback_data="preferences")],
    [InlineKeyboardButton("📤 Post Manager", callback_data="post_manager"),
     InlineKeyboardButton("🎁 Invite", callback_data="invite")],
    [InlineKeyboardButton("❓ Help", callback_data="help")],
])

PDF_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("📎 Merge PDFs", callback_data="merge_pdf"),
     InlineKeyboardButton("🔗 Add Link", callback_data="add_link")],
    [InlineKeyboardButton("❌ Remove Links", callback_data="remove_link"),
     InlineKeyboardButton("🖊 Text Watermark", callback_data="add_watermark")],
    [InlineKeyboardButton("🖼 Image Watermark", callback_data="image_watermark"),
     InlineKeyboardButton("🔗 Watermark Link", callback_data="watermark_link")],
    [InlineKeyboardButton("📑 Add Cover", callback_data="set_cover"),
     InlineKeyboardButton("⭐ Branding Bar", callback_data="branding_bar")],
    [InlineKeyboardButton("🔎 PDF OCR", callback_data="pdf_ocr")],
    [InlineKeyboardButton("⬅️ Main Menu", callback_data="main")],
])

IMAGE_MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🖼 Images → PDF", callback_data="image_to_pdf")],
    [InlineKeyboardButton("⬅️ Main Menu", callback_data="main")],
])

# -----------------------------
# Command handlers
# -----------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_profile(update.effective_user)
    await update.message.reply_text(
        f"👋 Welcome to {BRAND_NAME}!\n\n"
        "PDF, image conversion, OCR, watermark, links और branding tools के लिए नीचे menu चुनें.",
        reply_markup=MAIN_MENU,
    )

async def pdf_tools(update, context):
    await send_or_edit(update, "📚 PDF Tools\n\nअपना tool चुनें:", PDF_MENU)

async def image_tools(update, context):
    await send_or_edit(update, "🖼 Image Tools\n\nअपना tool चुनें:", IMAGE_MENU)

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
        "अगले चरण में language, default watermark और branding settings जोड़ सकते हैं.",
        MAIN_MENU,
    )

async def invite(update, context):
    bot_username = (await context.bot.get_me()).username
    link = f"https://t.me/{bot_username}?start=ref_{update.effective_user.id}"
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

async def generic_command(update, context):
    cmd = update.message.text.split()[0].split("@")[0]
    mapping = {
        "/set_cover": "set_cover",
        "/add_link": "add_link",
        "/remove_link": "remove_link",
        "/add_watermark": "add_watermark",
        "/watermark_link": "watermark_link",
        "/branding_bar": "branding_bar",
        "/pdf_ocr": "pdf_ocr",
        "/image_tools": "image_tools",
        "/pdf_tools": "pdf_tools",
        "/profile": "profile",
        "/preferences": "preferences",
        "/invite": "invite",
        "/help": "help",
    }
    action = mapping.get(cmd)
    if action:
        await start_action(update, context, action)

# -----------------------------
# Tool state machine
# -----------------------------

ACTION_INSTRUCTIONS = {
    "merge_pdf": "📎 Merge PDFs\n\nअब एक या एक से अधिक PDF files भेजें.\nजब सभी भेज दें, /done लिखें.",
    "image_to_pdf": "🖼 Images → PDF\n\nअब images भेजें. सभी images मिलने के बाद /done लिखें.",
    "set_cover": "📑 Add Cover\n\nपहले PDF भेजें, फिर cover image भेजें.",
    "add_link": "🔗 Add Link\n\nपहले PDF भेजें. उसके बाद message में URL भेजें.",
    "remove_link": "❌ Remove Links\n\nअब PDF भेजें.",
    "add_watermark": "🖊 Text Watermark\n\nपहले PDF भेजें. उसके बाद watermark text भेजें.",
    "image_watermark": "🖼 Image Watermark\n\nपहले PDF भेजें, फिर watermark image भेजें.",
    "watermark_link": "🔗 Watermark Link\n\nपहले PDF भेजें, फिर URL भेजें. यह link first-page watermark area पर लगाएगा.",
    "branding_bar": "⭐ Branding Bar\n\nपहले PDF भेजें. फिर message में format भेजें:\nTOP | BOTTOM",
    "pdf_ocr": "🔎 PDF OCR\n\nअब PDF भेजें.",
}

async def start_action(update, context, action):
    context.user_data.clear()
    context.user_data["action"] = action
    context.user_data["files"] = []
    await send_or_edit(update, ACTION_INSTRUCTIONS.get(action, "Tool started."), PDF_MENU)

async def done(update, context):
    action = context.user_data.get("action")
    files = context.user_data.get("files", [])
    if action not in ("merge_pdf", "image_to_pdf"):
        await update.message.reply_text("इस tool के लिए /done की जरूरत नहीं है.")
        return
    if not files:
        await update.message.reply_text("पहले files भेजें.")
        return
    work = job_dir(context, update.effective_user.id)
    output = work / ("merged.pdf" if action == "merge_pdf" else "images.pdf")
    try:
        if action == "merge_pdf":
            merge_pdfs(files, output)
        else:
            images_to_pdf(files, output)
        await update.message.reply_document(document=output.open("rb"), caption="✅ तैयार है.")
    except Exception as e:
        log.exception("Tool failed")
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        clear_job_dir(work)
        context.user_data.clear()

async def receive_document(update, context):
    action = context.user_data.get("action")
    if not action:
        return

    doc = update.message.document
    filename = doc.file_name or "file"
    work = job_dir(context, update.effective_user.id)
    path = work / filename

    if action in ("merge_pdf", "pdf_ocr", "remove_link", "add_link",
                   "add_watermark", "image_watermark", "watermark_link",
                   "branding_bar", "set_cover"):
        if not filename.lower().endswith(".pdf") and not (
            action == "set_cover" and filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
        ):
            await update.message.reply_text("⚠️ इस step पर PDF file चाहिए.")
            return

    tg_file = await doc.get_file()
    await tg_file.download_to_drive(path)

    if action in ("merge_pdf", "image_to_pdf"):
        context.user_data.setdefault("files", []).append(str(path))
        await update.message.reply_text(
            f"✅ {filename} received.\nऔर files भेजें या /done लिखें."
        )
        return

    context.user_data["pdf"] = str(path)
    if action == "pdf_ocr":
        await process_pdf_ocr(update, context)
    elif action == "remove_link":
        await process_remove_links(update, context)
    elif action == "add_link":
        await update.message.reply_text("अब URL भेजें, जैसे: https://example.com")
    elif action == "add_watermark":
        await update.message.reply_text("अब watermark text भेजें.")
    elif action == "watermark_link":
        await update.message.reply_text("अब URL भेजें.")
    elif action == "branding_bar":
        await update.message.reply_text("अब भेजें: TOP TEXT | BOTTOM TEXT")
    elif action == "set_cover":
        context.user_data["waiting_for_cover"] = True
        await update.message.reply_text("अब cover image भेजें.")
    elif action == "image_watermark":
        context.user_data["waiting_for_watermark_image"] = True
        await update.message.reply_text("अब watermark image भेजें.")

async def receive_photo(update, context):
    action = context.user_data.get("action")
    if not action:
        return
    work = job_dir(context, update.effective_user.id)
    if action == "image_to_pdf":
        file = await update.message.photo[-1].get_file()
        path = work / f"image_{len(context.user_data.get('files', []))+1}.jpg"
        await file.download_to_drive(path)
        context.user_data.setdefault("files", []).append(str(path))
        await update.message.reply_text("✅ Image received. और images भेजें या /done लिखें.")
    elif action == "set_cover" and context.user_data.get("waiting_for_cover"):
        file = await update.message.photo[-1].get_file()
        path = work / "cover.jpg"
        await file.download_to_drive(path)
        context.user_data["cover"] = str(path)
        await process_set_cover(update, context)
    elif action == "image_watermark" and context.user_data.get("waiting_for_watermark_image"):
        file = await update.message.photo[-1].get_file()
        path = work / "watermark.jpg"
        await file.download_to_drive(path)
        context.user_data["watermark"] = str(path)
        await process_image_watermark(update, context)

async def receive_text(update, context):
    action = context.user_data.get("action")
    if not action:
        return
    text = update.message.text.strip()
    if text == "/done":
        await done(update, context)
        return

    if action == "add_link":
        await process_add_link(update, context, text)
    elif action == "add_watermark":
        await process_text_watermark(update, context, text)
    elif action == "watermark_link":
        await process_watermark_link(update, context, text)
    elif action == "branding_bar":
        await process_branding_bar(update, context, text)
    else:
        await update.message.reply_text("इस tool के लिए अभी file step बाकी है.")

async def process_pdf_ocr(update, context):
    work = job_dir(context, update.effective_user.id)
    try:
        text = ocr_pdf(context.user_data["pdf"])
        txt = work / "ocr.txt"
        txt.write_text(text or "No text detected.", encoding="utf-8")
        await update.message.reply_document(document=txt.open("rb"), caption="✅ OCR complete.")
    except Exception as e:
        log.exception("OCR failed")
        await update.message.reply_text(
            "❌ OCR failed. सुनिश्चित करें कि Tesseract installed है और PDF readable है."
        )
    finally:
        context.user_data.clear()

async def process_remove_links(update, context):
    work = job_dir(context, update.effective_user.id)
    out = work / "links_removed.pdf"
    try:
        remove_links(context.user_data["pdf"], out)
        await update.message.reply_document(document=out.open("rb"), caption="✅ Links removed.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()

async def process_add_link(update, context, url):
    if not re.match(r"^https?://", url, re.I):
        await update.message.reply_text("⚠️ Valid URL भेजें, जैसे https://example.com")
        return
    work = job_dir(context, update.effective_user.id)
    out = work / "link_added.pdf"
    try:
        add_link(context.user_data["pdf"], out, url)
        await update.message.reply_document(document=out.open("rb"), caption="✅ Link added.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()

async def process_text_watermark(update, context, text):
    work = job_dir(context, update.effective_user.id)
    out = work / "watermarked.pdf"
    try:
        add_text_watermark(context.user_data["pdf"], out, text)
        await update.message.reply_document(document=out.open("rb"), caption="✅ Watermark added.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()

async def process_watermark_link(update, context, url):
    if not re.match(r"^https?://", url, re.I):
        await update.message.reply_text("⚠️ Valid URL भेजें.")
        return
    work = job_dir(context, update.effective_user.id)
    out = work / "watermark_link.pdf"
    try:
        add_link(context.user_data["pdf"], out, url)
        await update.message.reply_document(document=out.open("rb"), caption="✅ Link added.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()

async def process_branding_bar(update, context, text):
    parts = [x.strip() for x in text.split("|", 1)]
    if len(parts) != 2:
        await update.message.reply_text("Format: TOP TEXT | BOTTOM TEXT")
        return
    work = job_dir(context, update.effective_user.id)
    out = work / "branded.pdf"
    try:
        add_branding_bar(context.user_data["pdf"], out, parts[0], parts[1])
        await update.message.reply_document(document=out.open("rb"), caption="✅ Branding bar added.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()

async def process_set_cover(update, context):
    work = job_dir(context, update.effective_user.id)
    out = work / "with_cover.pdf"
    try:
        add_cover(context.user_data["pdf"], context.user_data["cover"], out)
        await update.message.reply_document(document=out.open("rb"), caption="✅ Cover added.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()

async def process_image_watermark(update, context):
    work = job_dir(context, update.effective_user.id)
    out = work / "image_watermarked.pdf"
    try:
        add_image_watermark(context.user_data["pdf"], out, context.user_data["watermark"])
        await update.message.reply_document(document=out.open("rb"), caption="✅ Image watermark added.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()

# -----------------------------
# Callback handling
# -----------------------------

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action = q.data

    if action == "main":
        await q.edit_message_text(f"👋 {BRAND_NAME}\n\nMain Menu:", reply_markup=MAIN_MENU)
    elif action == "pdf_tools":
        await q.edit_message_text("📚 PDF Tools\n\nअपना tool चुनें:", reply_markup=PDF_MENU)
    elif action == "image_tools":
        await q.edit_message_text("🖼 Image Tools\n\nअपना tool चुनें:", reply_markup=IMAGE_MENU)
    elif action == "profile":
        await profile(update, context)
    elif action == "preferences":
        await preferences(update, context)
    elif action == "invite":
        await invite(update, context)
    elif action == "help":
        await help_cmd(update, context)
    else:
        await start_action(update, context, action)

async def send_or_edit(update, text, markup):
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, reply_markup=markup)
        except Exception:
            await update.effective_chat.send_message(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)

async def error_handler(update, context):
    log.exception("Unhandled exception", exc_info=context.error)
    try:
        if update and update.effective_chat:
            await update.effective_chat.send_message("❌ कुछ error हुआ. कृपया फिर try करें.")
    except Exception:
        pass

# -----------------------------
# App setup
# -----------------------------

async def post_init(app: Application):
    commands = [
        BotCommand("start", "बोट को चालू करें 🚀"),
        BotCommand("pdf_tools", "PDF tools 📚"),
        BotCommand("image_tools", "Image tools 🖼"),
        BotCommand("profile", "प्रोफाइल 👤"),
        BotCommand("post_manager", "पोस्ट मैनेजर 📤"),
        BotCommand("preferences", "बोट की प्राथमिकताएँ ⚙️"),
        BotCommand("invite", "दोस्तों को invite करें 🎁"),
        BotCommand("help", "सहायता और गाइड ❓"),
        BotCommand("set_cover", "PDF का cover page बदलें 📑"),
        BotCommand("add_link", "PDF में hyperlink जोड़ें 🔗"),
        BotCommand("remove_link", "PDF से links हटाएँ ❌"),
        BotCommand("add_watermark", "Text watermark लगाएँ 📝"),
        BotCommand("watermark_link", "Watermark के साथ link जोड़ें 🔗"),
        BotCommand("branding_bar", "Branding bar set करें ⭐"),
        BotCommand("pdf_ocr", "PDF का text निकालें 🔎"),
    ]
    await app.bot.set_my_commands(commands)

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing. Put it in .env")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("pdf_tools", pdf_tools))
    app.add_handler(CommandHandler("image_tools", image_tools))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("preferences", preferences))
    app.add_handler(CommandHandler("invite", invite))
    app.add_handler(CommandHandler("help", help_cmd))

    # All remaining commands shown in the menu.
    for command in [
        "set_cover", "add_link", "remove_link", "add_watermark",
        "watermark_link", "branding_bar", "pdf_ocr", "post_manager"
    ]:
        app.add_handler(CommandHandler(command, generic_command))

    app.add_handler(CommandHandler("done", done))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.Document.ALL, receive_document))
    app.add_handler(MessageHandler(filters.PHOTO, receive_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text))
    app.add_error_handler(error_handler)

    print(f"{BRAND_NAME} bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
