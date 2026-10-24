import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "database.db"

SCHEMA = r"""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user'
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date TEXT NOT NULL,
    location TEXT NOT NULL,
    image TEXT NOT NULL,
    price INTEGER,
    capacity INTEGER,
    contact_info INTEGER,
    time TEXT,
    is_deleted INTEGER DEFAULT 0,
    deleted_at TEXT,
    available_tickets INTEGER,
    is_popular INTEGER NOT NULL DEFAULT 0,
    event_date DATE,
    archived INTEGER DEFAULT 0,
    original_ticket_quantity TEXT,
    booking_start_date TEXT,
    booking_end_date TEXT
);

CREATE TABLE IF NOT EXISTS wedding_themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    image_url TEXT,
    total_price INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    event_id INTEGER,
    booking_date TEXT,
    ticket_quantity INTEGER,
    payment_method TEXT,
    status TEXT DEFAULT 'Pending',
    original_ticket_quantity INTEGER,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE TABLE IF NOT EXISTS theme_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL,
    event_name TEXT NOT NULL,
    decoration TEXT,
    catering TEXT,
    photography TEXT,
    staff TEXT,
    guest_limit INTEGER,
    price INTEGER,
    image_url TEXT,
    total_price INTEGER DEFAULT 0,
    FOREIGN KEY(theme_id) REFERENCES wedding_themes(id)
);

CREATE TABLE IF NOT EXISTS wedding_bookings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    theme_id INTEGER NOT NULL,
    booking_date TEXT NOT NULL,
    guest_count INTEGER,
    status TEXT DEFAULT 'Pending',
    booked_on DATETIME,
    venue TXET,
    theme TXET,
    full_name TEXT,
    email TEXT,
    phone TEXT,
    catering_preferences TEXT,
    bride_name TEXT,
    groom_name TEXT,
    wedding_date TEXT,
    wedding_location TEXT,
    wedding_days INTEGER,
    selected_events TEXT,
    per_event_customization TEXT,
    general_services TEXT,
    preferences TEXT,
    optional_addons TEXT,
    preferred_contact_method TEXT,
    contact_note TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(theme_id) REFERENCES wedding_themes(id)
);

CREATE TABLE IF NOT EXISTS wedding_guests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL,
    guest_name TEXT NOT NULL,
    guest_email TEXT NOT NULL,
    rsvp_status TEXT DEFAULT 'Pending',
    token TEXT,
    FOREIGN KEY(booking_id) REFERENCES wedding_bookings(id)
);
"""

def initialize_database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Never initialize over an existing database.
    if DB_PATH.exists():
        raise SystemExit(
            f"STOP: {DB_PATH} already exists. "
            "This initializer is only for a new database. "
            "Back up and test with a separate empty database instead."
        )

    connection = sqlite3.connect(DB_PATH)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA)
        connection.commit()
        print(f"Database schema created successfully: {DB_PATH}")
    except Exception:
        connection.rollback()
        if DB_PATH.exists():
            DB_PATH.unlink()
        raise
    finally:
        connection.close()

if __name__ == "__main__":
    initialize_database()
