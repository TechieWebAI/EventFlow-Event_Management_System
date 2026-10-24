import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer
from pathlib import Path
import os
from dotenv import load_dotenv

# Config
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

SECRET_KEY = os.environ["EVENTFLOW_SECRET_KEY"]
SECURITY_PASSWORD_SALT = os.environ["EVENTFLOW_PASSWORD_RESET_SALT"]

serializer = URLSafeTimedSerializer(SECRET_KEY)

DATABASE_DIR = BASE_DIR / "database"
DATABASE = str(DATABASE_DIR / "database.db")


def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def get_user_by_email(email):
    db = get_db()
    return db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()


def verify_password(stored_hash, input_password):
    """Verify a password against its stored password hash safely."""
    return check_password_hash(stored_hash, input_password)


def hash_password(password):
    return generate_password_hash(password)  # Uses scrypt, PBKDF2, etc.

def update_user_password(user_id, new_password):
    conn = get_db()
    hashed_password = hash_password(new_password)
    conn.execute(
        "UPDATE users SET password = ? WHERE id = ?", (hashed_password, user_id)
    )
    conn.commit()
    conn.close()


def generate_reset_token(user_id):
    return serializer.dumps(user_id, salt=SECURITY_PASSWORD_SALT)


def verify_token(token, expiration=3600):
    try:
        return serializer.loads(token, salt=SECURITY_PASSWORD_SALT, max_age=expiration)
    except Exception:
        return None
