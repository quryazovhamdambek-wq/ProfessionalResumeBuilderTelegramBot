import sqlite3
import os
from config import config

class Database:
    def __init__(self):
        self.db_path = config.DB_PATH
        self.initialize()

    def get_connection(self):
        return sqlite3.connect(self.db_path)

    def initialize(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Create users table (language ustuni qo'shildi)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    language TEXT DEFAULT 'uz',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Eski bazada language ustuni yo'q bo'lsa, xato bermasligi uchun uni qo'shib qo'yamiz
            try:
                cursor.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'uz'")
            except sqlite3.OperationalError:
                pass 
            
            # Create sessions table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sessions (
                    user_id INTEGER PRIMARY KEY,
                    resume_text TEXT,
                    jd_text TEXT,
                    match_score INTEGER,
                    last_processed_at TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # Create resumes table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS resumes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    original_resume_text TEXT,
                    processed_resume_text TEXT,
                    jd_text TEXT,
                    match_score INTEGER,
                    pdf_path TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            conn.commit()

    def update_user(self, user_id, username, first_name):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Foydalanuvchining tili o'chib ketmasligi uchun INSERT OR REPLACE o'rniga ON CONFLICT ishlatamiz
            cursor.execute('''
                INSERT INTO users (user_id, username, first_name)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name
            ''', (user_id, username, first_name))
            conn.commit()

    # Yangi qo'shilgan funksiya
    def update_user_language(self, user_id, language):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE users SET language = ? WHERE user_id = ?
            ''', (language, user_id))
            conn.commit()

    def update_session(self, user_id, **kwargs):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # Ensure user exists first in sessions
            cursor.execute("INSERT OR IGNORE INTO sessions (user_id) VALUES (?)", (user_id,))
            
            for key, value in kwargs.items():
                cursor.execute(f"UPDATE sessions SET {key} = ? WHERE user_id = ?", (value, user_id))
            conn.commit()

    def get_session(self, user_id):
        with self.get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM sessions WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def save_resume(self, user_id, original_text, processed_text, jd_text, match_score, pdf_path):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO resumes 
                (user_id, original_resume_text, processed_resume_text, jd_text, match_score, pdf_path)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (user_id, original_text, processed_text, jd_text, match_score, pdf_path))
            conn.commit()

db = Database()
