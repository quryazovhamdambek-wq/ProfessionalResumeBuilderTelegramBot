import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_health_check_server():
    server = HTTPServer(('0.0.0.0', 10000), SimpleHTTPRequestHandler)
    server.serve_forever()

# Serverni fonda (background thread) ishga tushirish
threading.Thread(target=run_health_check_server, daemon=True).start()

import os
import asyncio
from telegram import Update, File, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
from config import config
from utils.logger import logger
from database.db import db
from services.resume_parser import resume_parser
from services.jd_analyzer import jd_analyzer
from services.resume_writer import ResumeWriter
from services.latex_generator import latex_generator
from utils.rate_limiter import rate_limiter
from bot.locales import TEXTS

# Global instance
resume_writer = ResumeWriter()

# Define states for JD input
JD_INPUT = 1

class ResumeBot:
    def __init__(self):
        # Create persistent application
        self.app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()
        self.register_handlers()

    def register_handlers(self):
        # /start
        self.app.add_handler(CommandHandler("start", self.start))
        
        # Tilni o'zgartirish tugmasini ushlab olish 
        self.app.add_handler(CallbackQueryHandler(self.set_language, pattern="^lang_"))
        
        # /upload_resume via PDF file handler
        self.app.add_handler(MessageHandler(filters.Document.PDF, self.handle_resume_upload))
        
        # /upload_jd (Conversation for JD text)
        jd_handler = ConversationHandler(
            entry_points=[CommandHandler("upload_jd", self.jd_entry)],
            states={
                JD_INPUT: [MessageHandler(filters.TEXT & (~filters.COMMAND), self.handle_jd_text)]
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            allow_reentry=True
        )
        self.app.add_handler(jd_handler)
        
        # /generate
        self.app.add_handler(CommandHandler("generate", self.generate_pipeline))
        
        # Instructions for non-command text (exclude if it looks like a JD text being pasted)
        # We only trigger this if we are NOT in the JD_INPUT state.
        self.app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), self.unknown_text))

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        db.update_user(user.id, user.username, user.first_name)
        
        # Til tanlash tugmalarini yaratish 🔘
        keyboard = [
            [
                InlineKeyboardButton("🇺🇿 O'zbekcha", callback_data="lang_uz"),
                InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru"),
                InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Foydalanuvchiga til tanlash matni va tugmalarni yuborish 📩
        await update.message.reply_text(
            TEXTS['choose_lang']['uz'],
            reply_markup=reply_markup
        )
        
    async def set_language(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        lang = query.data.split("_")[1] # 'uz', 'ru', yoki 'en'
        user_id = query.from_user.id

        # Ma'lumotlar bazasida foydalanuvchi tilini yangilash
        db.update_user_language(user_id, lang)

        # Xush kelibsiz xabarini tanlangan tilda yuborish
       await query.message.reply_text("Til muvaffaqiyatli tanlandi!")
            

    async def handle_resume_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        document = update.message.document
        user_id = update.effective_user.id
        
        status_msg = await update.message.reply_text("⏳ <b>Parsing your resume...</b>", parse_mode=ParseMode.HTML)
        
        # Clean old files for this user
        file_path = os.path.join(config.TEMP_DIR, f"resume_{user_id}.pdf")
        if os.path.exists(file_path):
            try: os.remove(file_path)
            except: pass

        try:
            # Download file
            file: File = await context.bot.get_file(document.file_id)
            await file.download_to_drive(file_path)
            
            # Parse text
            resume_text = resume_parser.extract_from_pdf(file_path)
            if len(resume_text) < 50:
                raise ValueError("Parsed text is too short. Ensure PDF contains extractable text.")
                
            db.update_session(user_id, resume_text=resume_text)
            
            await status_msg.edit_text("✅ <b>Resume parsed successfully!</b>\nNow send the Job Description with /upload_jd.", parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error parsing resume for user {user_id}: {str(e)}")
            await status_msg.edit_text(f"❌ <b>Failed to parse resume.</b> {str(e)}", parse_mode=ParseMode.HTML)

    async def jd_entry(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📝 <b>Please paste the Job Description text here:</b>", parse_mode=ParseMode.HTML)
        return JD_INPUT

    async def handle_jd_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        jd_text = update.message.text
        user_id = update.effective_user.id
        
        db.update_session(user_id, jd_text=jd_text)
        await update.message.reply_text("✅ <b>Job Description received!</b>\nReady to /generate your resume.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Operation cancelled.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    async def unknown_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        # We avoid responding if it's likely just JD text or accidental input
        if len(update.message.text) > 100:
            return 
            
        await update.message.reply_text("🤔 <b>I didn't understand that.</b>\n\nUpload a PDF resume or use /upload_jd to set JD text.", parse_mode=ParseMode.HTML)

    async def generate_pipeline(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        # Rate Limiting
        allowed, wait_time = await rate_limiter.check_limit(user_id)
        if not allowed:
            await update.message.reply_text(f"🕒 <b>Too fast!</b> Please wait {wait_time:.1f}s before trying again.", parse_mode=ParseMode.HTML)
            return

        session = db.get_session(user_id)
        if not session or not session.get('resume_text') or not session.get('jd_text'):
            await update.message.reply_text("⚠️ <b>Missing Information!</b>\nUpload your resume (PDF) and JD text (/upload_jd) first.", parse_mode=ParseMode.HTML)
            return

        resume_text, jd_text = session['resume_text'], session['jd_text']
        status_msg = await update.message.reply_text("🛠 <b>Initializing Pipeline...</b>", parse_mode=ParseMode.HTML)
        
        async with rate_limiter.semaphore:
            try:
                loop = asyncio.get_event_loop()
                
                # 1. Analyze JD
                await status_msg.edit_text("✨ (1/4) Analyzing Job Description requirements...", parse_mode=ParseMode.HTML)
                jd_analysis = await loop.run_in_executor(None, jd_analyzer.analyze, jd_text)
                
                # 2. Rewrite Resume
                await status_msg.edit_text("✍️ (2/4) Rewriting resume with AI optimization...", parse_mode=ParseMode.HTML)
                rewrite_data = await loop.run_in_executor(None, resume_writer.process, resume_text, jd_analysis, jd_text)
                
                # 3. Generate LaTeX & PDF
                await status_msg.edit_text("📄 (3/4) Generating LaTeX & compiling PDF...", parse_mode=ParseMode.HTML)
                tex_content = await loop.run_in_executor(None, latex_generator.generate_latex, rewrite_data['latex_data'])
                tex_file, pdf_file = await loop.run_in_executor(None, latex_generator.compile_pdf, tex_content, f"resume_opt_{user_id}", rewrite_data['latex_data'])
                
                # 4. Results
                await status_msg.edit_text("🎯 (4/4) Finalizing...", parse_mode=ParseMode.HTML)
                
                score = rewrite_data.get('match_score', 0)
                missing = ", ".join(rewrite_data.get('missing_skills', []))
                
                summary_msg = (
                    f"🏆 <b>Match Score: {score}%</b>\n\n"
                    f"⚠️ <b>Missing Skills/Keywords:</b>\n{missing if missing else 'None detected!'}\n\n"
                    f"📝 <b>AI Summary:</b>\n{rewrite_data.get('summary', '')}"
                )
                
                await update.message.reply_text(summary_msg, parse_mode=ParseMode.HTML)
                
                if pdf_file and os.path.exists(pdf_file):
                    with open(pdf_file, 'rb') as f:
                        await update.message.reply_document(document=f, filename=f"Optimized_Resume.pdf")
                else:
                    await update.message.reply_text("⚠️ <b>PDF Compilation failed.</b> Sending .tex source instead.", parse_mode=ParseMode.HTML)
                    if tex_file and os.path.exists(tex_file):
                        with open(tex_file, 'rb') as f:
                            await update.message.reply_document(document=f, filename=f"Resume_Source.tex")

                # Store result
                db.save_resume(user_id, resume_text, rewrite_data['rewritten_resume'], jd_text, score, pdf_file)
                logger.info(f"Pipeline completed successfully for user {user_id}")
                
            except Exception as e:
                logger.error(f"Pipeline error for user {user_id}: {str(e)}")
                await update.message.reply_text(f"❌ Pipeline failure! {str(e)}")
            finally:
                try:
                    await status_msg.delete()
                except:
                    pass

    def run(self):
        logger.info("Bot starting...")
        self.app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    bot = ResumeBot()
    bot.run()
        
