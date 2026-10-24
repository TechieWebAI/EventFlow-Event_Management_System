from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from flask import (
    Flask,
    Response,
    flash,
    g,
    get_flashed_messages,
    make_response,
    redirect,
    render_template,
    render_template_string,
    request,
    send_file,
    session,
    url_for,
)
from flask_apscheduler import APScheduler
from flask_mail import Message
from io import BytesIO, StringIO
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from urllib.parse import unquote_plus
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
from utils.auth_utils import (
    generate_reset_token,
    get_user_by_email,
    update_user_password,
    verify_token,
    verify_password,
)
from utils.email_utils import (
    send_email,
    send_reset_email,
    send_ticket_cancel_email,
)
import csv
import io
import json
import os
import requests
import smtplib
import sqlite3
import tempfile
import textwrap
import urllib.parse
import uuid
from pathlib import Path
import math
from dotenv import load_dotenv

app = Flask(__name__)
load_dotenv()
app.secret_key = os.environ["EVENTFLOW_SECRET_KEY"]

BASE_DIR = Path(__file__).resolve().parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE_DIR.mkdir(exist_ok=True)
DATABASE = str(DATABASE_DIR / "database.db")


# ---------- DATABASE CONNECTION ----------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()


def is_admin():
    """Check the logged-in user's current role in the database."""
    user_id = session.get("user_id")

    if user_id is None:
        return False

    db = get_db()
    user = db.execute(
        "SELECT role FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()

    return user is not None and user["role"] == "admin"


# ---------- ROUTES ----------
@app.route("/")
def index():
    db = get_db()
    # Query for events in each category
    tech_events = db.execute(
        "SELECT * FROM events WHERE category = 'Tech' AND is_deleted = 0"
    ).fetchall()

    art_events = db.execute(
        "SELECT * FROM events WHERE category = 'Art' AND is_deleted = 0"
    ).fetchall()

    fitness_events = db.execute(
        "SELECT * FROM events WHERE category = 'Fitness' AND is_deleted = 0"
    ).fetchall()

    culture_events = db.execute(
        "SELECT * FROM events WHERE category = 'Culture' AND is_deleted = 0"
    ).fetchall()

    # 1. Admin-marked popular events
    manual_popular = db.execute("""
        SELECT * FROM events
        WHERE is_popular = 1 AND is_deleted = 0
    """).fetchall()

    # 2. Most booked events
    dynamic_popular = db.execute("""
        SELECT events.*, COUNT(bookings.id) AS booking_count
        FROM events
        LEFT JOIN bookings ON events.id = bookings.event_id
        WHERE events.is_deleted = 0
        GROUP BY events.id
        ORDER BY booking_count DESC
        LIMIT 4
    """).fetchall()

    # 3. Merge both — ensuring uniqueness by ID
    popular_dict = {}
    for event in manual_popular:
        popular_dict[event["id"]] = dict(event)
        popular_dict[event["id"]]["is_popular"] = 1  # Editor Pick

    for event in dynamic_popular:
        if event["id"] not in popular_dict:
            popular_dict[event["id"]] = dict(event)
        popular_dict[event["id"]]["booking_count"] = event["booking_count"]

    combined_popular = list(popular_dict.values())

    return render_template(
        "index.html",
        tech_events=tech_events,
        art_events=art_events,
        fitness_events=fitness_events,
        culture_events=culture_events,
        combined_popular=combined_popular,
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        password_raw = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not username or not email or not password_raw:
            flash("Complete all required fields.")
            return redirect(url_for("signup"))

        if len(password_raw) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("signup"))

        if len(password_raw) > 128:
            flash("Password must be 128 characters or fewer.")
            return redirect(url_for("signup"))

        if password_raw != confirm_password:
            flash("Passwords do not match.")
            return redirect(url_for("signup"))

        db = get_db()
        try:
            db.execute(
                "INSERT INTO users (username, email, password) VALUES (?, ?, ?)",
                (username, email, generate_password_hash(password_raw)),
            )
            db.commit()
            flash("Signup successful. Please log in.")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            db.rollback()
            flash("That email or username is already registered.")
            return redirect(url_for("signup"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        user = get_user_by_email(email)

        if user and verify_password(user["password"], password):
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            flash("Login successful.")
            return redirect(url_for("index"))
        else:
            flash("Invalid email or password.")
            return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out successfully.")
    return redirect(url_for("index"))


def book_event(event_id):
    if "user_id" not in session:
        flash("You must be logged in to book an event.")
        return redirect(url_for("login"))

    return redirect(url_for("start_booking", event_id=event_id))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"]
        user = get_user_by_email(email)
        if user:
            token = generate_reset_token(user["id"])
            reset_url = url_for("reset_password", token=token, _external=True)
            send_reset_email(user["email"], reset_url)
            print(f"🔗 Reset link: {reset_url}")  # Replace with real email latera
            flash("📧 A reset link has been sent to your email.")
        else:
            flash("❌ No account found with that email.")
    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user_id = verify_token(token)

    if not user_id:
        flash("This reset link is invalid or expired.")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if len(new_password) < 8:
            flash("Password must be at least 8 characters.")
            return render_template("reset_password.html")

        if len(new_password) > 128:
            flash("Password must be 128 characters or fewer.")
            return render_template("reset_password.html")

        if new_password != confirm_password:
            flash("Passwords do not match.")
            return render_template("reset_password.html")

        db = get_db()
        db.execute(
            "UPDATE users SET password = ? WHERE id = ?",
            (generate_password_hash(new_password), user_id),
        )
        db.commit()

        flash("Your password has been reset. Please log in.")
        return redirect(url_for("login"))

    return render_template("reset_password.html")


@app.route("/profile")
def profile():
    if "user_id" not in session:
        flash("Please login first.")
        return redirect(url_for("login"))

    db = get_db()

    # Fetch user info
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()

    # Event bookings with status and ticket quantity
    bookings = db.execute(
        """
        SELECT
            bookings.id AS booking_id,
            events.id AS event_id,
            events.title,
            events.start_date,
            events.end_date,
            events.location,
            events.event_date,
            events.time,
            bookings.booking_date,
            bookings.ticket_quantity,
            bookings.original_ticket_quantity,
            bookings.status
        FROM bookings
        JOIN events
            ON bookings.event_id = events.id
        WHERE bookings.user_id = ?
        ORDER BY bookings.booking_date DESC
    """,
        (session["user_id"],),
    ).fetchall()

    wedding_rows = db.execute(
        """
        SELECT
            wb.id,
            wb.user_id,
            wb.theme_id,
            wt.name AS theme,
            wt.image_url AS theme_image_url,
            wb.wedding_date,
            wb.booking_date,
            wb.guest_count,
            wb.booked_on,
            wb.status,
            wb.venue,
            wb.wedding_location,
            wb.wedding_days,
            wb.selected_events,
            wb.per_event_customization,
            wb.general_services,
            wb.preferences,
            wb.optional_addons,
            wb.contact_note,
            wb.catering_preferences
        FROM wedding_bookings AS wb
        LEFT JOIN wedding_themes AS wt ON wt.id = wb.theme_id
        WHERE wb.user_id = ?
        ORDER BY wb.booked_on DESC, wb.id DESC
    """,
        (session["user_id"],),
    ).fetchall()

    status_labels = {
        "new_request": "New Request",
        "under_review": "Under Review",
        "changes_requested": "Changes Requested",
        "confirmed": "Confirmed",
        "declined": "Declined / Rejected",
        "cancelled": "Cancelled",
    }

    wedding_bookings = []

    for row in wedding_rows:
        w = dict(row)

        try:
            legacy = json.loads(w.get("catering_preferences") or "{}")
            if not isinstance(legacy, dict):
                legacy = {}
        except (TypeError, ValueError):
            legacy = {}

        def read_json_list(column, legacy_key):
            raw = w.get(column)
            if raw:
                try:
                    value = json.loads(raw)
                    if isinstance(value, list):
                        return value
                except (TypeError, ValueError):
                    pass

            value = legacy.get(legacy_key, [])
            return value if isinstance(value, list) else []

        raw_events = w.get("selected_events")
        try:
            events = (
                json.loads(raw_events)
                if raw_events
                else legacy.get("selected_events", [])
            )
        except (TypeError, ValueError):
            events = legacy.get("selected_events", [])
        w["selected_events_list"] = events if isinstance(events, list) else []

        raw_customization = w.get("per_event_customization")
        try:
            customization = (
                json.loads(raw_customization)
                if raw_customization
                else legacy.get("event_customization", {})
            )
        except (TypeError, ValueError):
            customization = legacy.get("event_customization", {})
        w["per_event_customization_data"] = (
            customization if isinstance(customization, dict) else {}
        )

        w["general_services_list"] = read_json_list("general_services", "services")
        w["optional_addons_list"] = read_json_list("optional_addons", "addons")

        w["wedding_location_display"] = (
            str(
                w.get("wedding_location") or legacy.get("wedding_location") or ""
            ).strip()
            or "Not provided"
        )
        w["wedding_days_display"] = (
            str(w.get("wedding_days") or legacy.get("wedding_days") or "").strip()
            or "Not provided"
        )
        w["preferences_display"] = (
            str(w.get("preferences") or legacy.get("preferences") or "").strip()
            or "Not provided"
        )
        w["contact_note_display"] = (
            str(w.get("contact_note") or legacy.get("contact_note") or "").strip()
            or "No additional note"
        )

        w["status_key"] = normalize_wedding_status(w.get("status")) or "legacy"
        w["status_label"] = status_labels.get(w["status_key"], "Needs Review")

        wedding_bookings.append(w)

    return render_template(
        "profile.html",
        user=user,
        bookings=bookings,
        wedding_bookings=wedding_bookings,
    )


@app.route("/my-wedding-bookings/<int:booking_id>/pdf")
def customer_wedding_plan_pdf(booking_id):
    if "user_id" not in session:
        flash("Please log in to view your wedding plan.")
        return redirect(url_for("login"))

    db = get_db()
    booking = db.execute(
        """
        SELECT user_id, status
        FROM wedding_bookings
        WHERE id = ?
        """,
        (booking_id,),
    ).fetchone()

    if booking is None or booking["user_id"] != session["user_id"]:
        flash("Wedding booking not found.")
        return redirect(url_for("profile"))

    if normalize_wedding_status(booking["status"]) != "confirmed":
        flash("The wedding plan PDF is available after the booking is confirmed.")
        return redirect(url_for("profile"))

    try:
        pdf_bytes, _, _ = build_wedding_booking_pdf(booking_id)

        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=request.args.get("download") == "1",
            download_name=f"wedding_plan_WF-{booking_id:04d}.pdf",
        )
    except Exception:
        app.logger.exception(
            "Could not create customer PDF for wedding booking %s",
            booking_id,
        )
        flash("The wedding plan PDF could not be generated.")
        return redirect(url_for("profile"))


@app.route("/cancel_tickets/<int:booking_id>", methods=["GET", "POST"])
def cancel_tickets(booking_id):

    if "user_id" not in session:
        flash("Please log in first.")
        return redirect(url_for("login"))

    db = get_db()

    booking = db.execute(
        """
        SELECT
            b.*,
            e.title AS event_title,
            e.event_date,
            e.location,
            u.username,
            u.email
        FROM bookings b
        JOIN events e
            ON b.event_id = e.id
        JOIN users u
            ON b.user_id = u.id
        WHERE b.id = ?
          AND b.user_id = ?
    """,
        (booking_id, session["user_id"]),
    ).fetchone()

    # ---------------------------------------------------------
    # BOOKING DOES NOT EXIST
    # ---------------------------------------------------------

    if not booking:
        flash("Booking not found.")
        return redirect(url_for("profile"))

    current_status = (booking["status"] or "").lower()

    # ---------------------------------------------------------
    # ALREADY CANCELLED
    # ---------------------------------------------------------

    if current_status == "cancelled":
        flash("This booking has already been cancelled.")
        return redirect(url_for("profile"))

    # ---------------------------------------------------------
    # COMPLETED EVENT
    # ---------------------------------------------------------

    if current_status == "completed":
        flash("Completed bookings cannot be cancelled.")
        return redirect(url_for("profile"))

    # NOTHING IS CANCELLED HERE.
    if request.method == "GET":

        return render_template("cancel_tickets.html", booking=booking)

    # ACTUAL CANCELLATION HAPPENS HERE.
    # =========================================================

    try:

        cancel_qty = int(request.form.get("cancel_quantity", 0))

    except (ValueError, TypeError):

        flash("Please enter a valid number of tickets.")
        return redirect(request.url)

    current_quantity = int(booking["ticket_quantity"] or 0)

    # ---------------------------------------------------------
    # VALIDATION
    # ---------------------------------------------------------

    if current_quantity <= 0:

        flash("There are no tickets available to cancel.")
        return redirect(url_for("profile"))

    if cancel_qty <= 0:

        flash("Please select at least 1 ticket.")
        return redirect(request.url)

    if cancel_qty > current_quantity:

        flash(f"You can cancel a maximum of " f"{current_quantity} ticket(s).")

        return redirect(request.url)

    remaining = current_quantity - cancel_qty

    # =========================================================
    # FULL CANCELLATION
    # =========================================================

    if remaining == 0:

        db.execute(
            """
            UPDATE bookings
            SET ticket_quantity = 0,
                status = 'cancelled'
            WHERE id = ?
        """,
            (booking_id,),
        )

        db.execute(
            """
            UPDATE events
            SET available_tickets =
                available_tickets + ?
            WHERE id = ?
        """,
            (cancel_qty, booking["event_id"]),
        )

        db.commit()

        # Optional existing email function
        try:

            send_ticket_cancel_email(
                recipient=booking["email"],
                username=booking["username"],
                event_title=booking["event_title"],
                cancelled=cancel_qty,
                remaining=0,
                full_cancel=True,
            )

        except Exception:

            pass

        flash(f"All {cancel_qty} ticket(s) cancelled successfully.")

        return redirect(url_for("profile"))

    # =========================================================
    # PARTIAL CANCELLATION
    # =========================================================

    db.execute(
        """
        UPDATE bookings
        SET ticket_quantity = ?,
            status = 'active'
        WHERE id = ?
    """,
        (remaining, booking_id),
    )

    db.execute(
        """
        UPDATE events
        SET available_tickets =
            available_tickets + ?
        WHERE id = ?
    """,
        (cancel_qty, booking["event_id"]),
    )

    db.commit()

    # Optional existing email function
    try:

        send_ticket_cancel_email(
            recipient=booking["email"],
            username=booking["username"],
            event_title=booking["event_title"],
            cancelled=cancel_qty,
            remaining=remaining,
            full_cancel=False,
        )

    except Exception:

        pass

    flash(f"{cancel_qty} ticket(s) cancelled. " f"{remaining} ticket(s) remain.")

    return redirect(url_for("profile"))


@app.route("/admin/bookings", methods=["GET"])
def admin_bookings():
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()

    query = """
        SELECT
            users.username,
            users.email,
            events.title AS event_title,
            events.start_date,
            events.end_date,
            events.location,
            events.event_date,
            bookings.booking_date,
            bookings.ticket_quantity,
            bookings.original_ticket_quantity,
            bookings.status
        FROM bookings
        JOIN users ON bookings.user_id = users.id
        JOIN events ON bookings.event_id = events.id
    """

    filters = []
    params = []

    # =====================================================
    # BOOKING DATE FILTER
    # =====================================================

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    if start_date:
        filters.append("DATE(bookings.booking_date) >= ?")
        params.append(start_date)

    if end_date:
        filters.append("DATE(bookings.booking_date) <= ?")
        params.append(end_date)

    # =====================================================
    # STATUS FILTER
    # =====================================================

    status_filter = request.args.get("status", "all").strip().lower()

    if status_filter == "active":

        filters.append("""
            bookings.status != 'cancelled'
            AND (
                bookings.original_ticket_quantity IS NULL
                OR bookings.ticket_quantity >= bookings.original_ticket_quantity
            )
        """)

    elif status_filter == "partial":

        filters.append("""
            bookings.status != 'cancelled'
            AND bookings.original_ticket_quantity IS NOT NULL
            AND bookings.ticket_quantity < bookings.original_ticket_quantity
        """)

    elif status_filter == "cancelled":

        filters.append("""
            bookings.status = 'cancelled'
        """)

    # =====================================================
    # APPLY FILTERS
    # =====================================================

    if filters:
        query += " WHERE " + " AND ".join(filters)

    query += """
        ORDER BY bookings.booking_date DESC
    """

    bookings = db.execute(query, params).fetchall()

    return render_template(
        "admin_bookings.html",
        bookings=bookings,
        selected_status=status_filter,
        start_date=start_date,
        end_date=end_date,
    )


@app.route("/admin/export_csv")
def export_bookings_csv():
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()

    query = """
        SELECT
            users.username,
            users.email,
            events.title AS event_title,
            events.start_date,
            events.end_date,
            events.location,
            events.event_date,
            bookings.booking_date,
            bookings.ticket_quantity,
            bookings.original_ticket_quantity,
            bookings.status
        FROM bookings
        JOIN users ON bookings.user_id = users.id
        JOIN events ON bookings.event_id = events.id
    """

    filters = []
    params = []

    # =====================================================
    # BOOKING DATE FILTER
    # =====================================================

    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    if start_date:
        filters.append("DATE(bookings.booking_date) >= ?")
        params.append(start_date)

    if end_date:
        filters.append("DATE(bookings.booking_date) <= ?")
        params.append(end_date)

    # =====================================================
    # STATUS FILTER
    # =====================================================

    status_filter = request.args.get("status", "all").strip().lower()

    if status_filter == "active":

        filters.append("""
            bookings.status != 'cancelled'
            AND (
                bookings.original_ticket_quantity IS NULL
                OR bookings.ticket_quantity >= bookings.original_ticket_quantity
            )
        """)

    elif status_filter == "partial":

        filters.append("""
            bookings.status != 'cancelled'
            AND bookings.original_ticket_quantity IS NOT NULL
            AND bookings.ticket_quantity < bookings.original_ticket_quantity
        """)

    elif status_filter == "cancelled":

        filters.append("""
            bookings.status = 'cancelled'
        """)

    # =====================================================
    # APPLY FILTERS
    # =====================================================

    if filters:
        query += " WHERE " + " AND ".join(filters)

    query += """
        ORDER BY bookings.booking_date DESC
    """

    bookings = db.execute(query, params).fetchall()

    # =====================================================
    # CREATE CSV
    # =====================================================

    output = StringIO()

    writer = csv.writer(output)

    writer.writerow(
        [
            "Username",
            "Email",
            "Event",
            "Start Date",
            "End Date",
            "Location",
            "Event Date",
            "Booking Date",
            "Tickets",
            "Original Tickets",
            "Status",
        ]
    )

    for b in bookings:

        if b["status"] == "cancelled":

            display_status = "Cancelled"

        elif (
            b["original_ticket_quantity"] is not None
            and b["ticket_quantity"] < b["original_ticket_quantity"]
        ):

            display_status = "Partially cancelled"

        else:

            display_status = "Active"

        writer.writerow(
            [
                b["username"],
                b["email"],
                b["event_title"],
                b["start_date"],
                b["end_date"],
                b["location"],
                b["event_date"],
                b["booking_date"],
                b["ticket_quantity"],
                b["original_ticket_quantity"] or b["ticket_quantity"],
                display_status,
            ]
        )

    output.seek(0)

    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment;filename=bookings.csv"},
    )


@app.route("/upload_event", methods=["POST"])
def upload_event():
    image = request.files["image"]
    filename = image.filename
    image.save(os.path.join("static/images", filename))
    # Save filename in DB or event.image = filename


UPLOAD_FOLDER = "static/images"
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


@app.route("/admin/add_event", methods=["GET", "POST"])
def add_event():
    if not is_admin():
        flash("Admin access only.", "error")
        return redirect(url_for("login"))

    current_date = datetime.now().date().isoformat()

    def render_add_event():
        return render_template(
            "add_event.html",
            current_date=current_date,
        )

    def show_error(message):
        flash(message, "error")
        # Render Add Event directly so the error appears on this page.
        return render_add_event()

    if request.method == "GET":
        return render_add_event()

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    location = request.form.get("location", "").strip()
    category = request.form.get("category", "").strip()
    start_date = request.form.get("start_date", "").strip()
    end_date = request.form.get("end_date", "").strip()
    event_date = request.form.get("event_date", "").strip()
    event_time = request.form.get("time", "").strip()

    if not all(
        [
            title,
            description,
            location,
            category,
            start_date,
            end_date,
            event_date,
            event_time,
        ]
    ):
        return show_error("Please complete all required event fields.")

    allowed_categories = {"Tech", "Art", "Fitness", "Culture", "Concerts"}
    if category not in allowed_categories:
        return show_error("Please select a valid event category.")

    try:
        start_day = datetime.strptime(start_date, "%Y-%m-%d").date()
        end_day = datetime.strptime(end_date, "%Y-%m-%d").date()
        event_day = datetime.strptime(event_date, "%Y-%m-%d").date()
    except ValueError:
        return show_error("Enter valid event, start, and end dates.")

    today = datetime.now().date()

    if start_day < today:
        return show_error("Start date cannot be in the past.")

    if end_day < start_day:
        return show_error("End date cannot be before the start date.")

    # The event date must be within the schedule, including both endpoints.
    if not start_day <= event_day <= end_day:
        return show_error(
            "Event date must be between the start date and end date, inclusive."
        )

    try:
        capacity = int(request.form.get("capacity", ""))
        price = float(request.form.get("price", ""))
    except ValueError:
        return show_error("Enter a whole-number ticket capacity and a valid price.")

    if capacity < 1:
        return show_error("Ticket capacity must be at least 1.")

    if not math.isfinite(price) or price < 0:
        return show_error("Ticket price must be zero or greater.")

    image = request.files.get("image")

    if not image or not image.filename:
        return show_error("Please choose an event image.")

    if not allowed_file(image.filename):
        return show_error("Image must be JPG, PNG, or WEBP.")

    filename = secure_filename(image.filename)
    if not filename:
        return show_error("The selected image filename is invalid.")

    image_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    db = get_db()

    try:
        image.save(image_path)

        is_popular = 1 if request.form.get("is_popular") == "1" else 0

        db.execute(
            """
            INSERT INTO events (
                title, description, start_date, end_date, location,
                category, image, capacity, price, time, archived,
                available_tickets, is_popular, event_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                title,
                description,
                start_date,
                end_date,
                location,
                category,
                filename,
                capacity,
                price,
                event_time,
                0,
                capacity,
                is_popular,
                event_date,
            ),
        )
        db.commit()

    except Exception:
        db.rollback()

        if os.path.exists(image_path):
            os.remove(image_path)

        app.logger.exception("Could not create the event.")
        return show_error("The event could not be saved. Please try again.")

    flash("Event added successfully.", "success")
    return redirect(url_for("view_events"))


@app.route("/admin/edit_event/<int:event_id>", methods=["GET", "POST"])
def edit_event(event_id):
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()
    event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    if event is None:
        flash("Event not found.", "error")
        return redirect(url_for("view_events"))

    current_date = datetime.now().date().isoformat()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        start_date = request.form.get("start_date", "").strip()
        end_date = request.form.get("end_date", "").strip()
        location = request.form.get("location", "").strip()
        category = request.form.get("category", "").strip()
        time = request.form.get("time", "").strip()
        capacity_value = request.form.get("capacity", "").strip()
        price_value = request.form.get("price", "").strip()
        event_date = request.form.get("event_date", "").strip()

        def show_error(message):
            flash(message, "error")
            return render_template(
                "edit_event.html",
                event=event,
                current_date=datetime.now().date().isoformat(),
            )

        if not all(
            [
                title,
                description,
                start_date,
                end_date,
                event_date,
                location,
                category,
                time,
                capacity_value,
                price_value,
            ]
        ):
            return show_error("Please complete all required event fields.")

        try:
            start_day = datetime.strptime(start_date, "%Y-%m-%d").date()

            end_day = datetime.strptime(end_date, "%Y-%m-%d").date()

            event_day = datetime.strptime(event_date, "%Y-%m-%d").date()

        except ValueError:
            return show_error("Enter valid event, start, and end dates.")

        # EXACTLY THE SAME DATE RULES AS ADD EVENT
        today = datetime.now().date()

        if start_day < today:
            return show_error("Start date cannot be in the past.")

        if end_day < start_day:
            return show_error("End date cannot be before the start date.")

        if not start_day <= event_day <= end_day:
            return show_error(
                "Event date must be between the start date and end date, inclusive."
            )

        try:
            capacity = int(capacity_value)
            price = float(price_value)
        except ValueError:
            return show_error("Enter a whole-number ticket capacity and a valid price.")

        import math

        if capacity < 1:
            return show_error("Ticket capacity must be at least 1.")

        if not math.isfinite(price) or price < 0:
            return show_error("Ticket price must be zero or greater.")

        db.execute(
            """
            UPDATE events
            SET title = ?,
                description = ?,
                start_date = ?,
                end_date = ?,
                location = ?,
                category = ?,
                time = ?,
                capacity = ?,
                price = ?,
                event_date = ?
            WHERE id = ?
            """,
            (
                title,
                description,
                start_date,
                end_date,
                location,
                category,
                time,
                capacity,
                price,
                event_date,
                event_id,
            ),
        )

        db.commit()

        flash("Event updated successfully.")
        return redirect(url_for("view_events"))

    return render_template("edit_event.html", event=event, current_date=current_date)


@app.route("/admin/delete_event/<int:event_id>")
def delete_event(event_id):
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()
    deletion_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE events SET is_deleted = 1, deleted_at = ? WHERE id = ?",
        (deletion_time, event_id),
    )
    db.commit()
    flash("Event moved to trash. It can be restored within 30 days.")
    return redirect(url_for("view_events"))


@app.route("/admin/trashed_events")
def trashed_events():
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()

    # Full datetime comparison
    one_month_ago = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

    events = db.execute(
        """
        SELECT * FROM events
        WHERE is_deleted = 1
          AND deleted_at > ?
          AND (archived IS NULL OR archived = 0)
    """,
        (one_month_ago,),
    ).fetchall()

    return render_template("trashed_events.html", events=events)


@app.route("/admin/restore_event/<int:event_id>")
def restore_event(event_id):
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()
    event = db.execute(
        "SELECT id, deleted_at FROM events WHERE id = ? AND is_deleted = 1",
        (event_id,),
    ).fetchone()

    if not event:
        flash("Event not found or not in deleted state.")
        return redirect(url_for("view_events"))

    deleted_at_value = event["deleted_at"]

    try:
        # Accepts both YYYY-MM-DD and YYYY-MM-DD HH:MM:SS.
        deleted_at = datetime.fromisoformat(str(deleted_at_value).strip())
    except (TypeError, ValueError):
        app.logger.warning(
            "Cannot restore event %s: invalid deleted_at value %r",
            event_id,
            deleted_at_value,
        )
        flash("This event has an invalid deletion date and could not be restored.")
        return redirect(url_for("view_events"))

    age = datetime.now() - deleted_at

    if timedelta(0) <= age <= timedelta(days=30):
        db.execute(
            "UPDATE events SET is_deleted = 0, deleted_at = NULL WHERE id = ?",
            (event_id,),
        )
        db.commit()
        flash("Event restored successfully.")
    else:
        flash("Cannot restore. 30-day recovery period has expired.")

    return redirect(url_for("view_events"))


class Config:
    SCHEDULER_API_ENABLED = True


app.config.from_object(Config())
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()


def cleanup_old_deleted_events():
    with app.app_context():  # <-- required!
        db = get_db()
        cutoff_date = (datetime.now() - timedelta(days=30)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        print("Cleanup running at:", datetime.now(), "| Cutoff:", cutoff_date)

        rows = db.execute(
            "SELECT * FROM events WHERE is_deleted = 1 AND deleted_at <= ?",
            (cutoff_date,),
        ).fetchall()
        print("Events found to delete:", len(rows))

        # db.execute("DELETE FROM events WHERE is_deleted = 1 AND deleted_at <= ?", (cutoff_date,))
        # db.commit()


@app.route("/admin/run_cleanup")
def run_cleanup():
    cleanup_old_deleted_events()
    return "Cleanup run"


# Schedule it to run every 60 seconds
@scheduler.task("interval", id="cleanup_task", seconds=60)
def scheduled_cleanup():
    cleanup_old_deleted_events()


@app.route("/admin/archive_event/<int:event_id>")
def archive_event(event_id):
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()
    db.execute("UPDATE events SET archived = 1 WHERE id = ?", (event_id,))
    db.commit()
    print(f"Archived event with id: {event_id}")
    flash("Event hidden from Trash page.")
    return redirect(url_for("trashed_events"))


@app.route("/admin/dashboard")
def admin_dashboard():
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()

    # =========================================================
    # CORE METRICS
    # =========================================================

    total_events = db.execute("""
        SELECT COUNT(*)
        FROM events
        WHERE COALESCE(is_deleted, 0) = 0
    """).fetchone()[0] or 0

    upcoming_events_count = db.execute("""
        SELECT COUNT(*)
        FROM events
        WHERE COALESCE(is_deleted, 0) = 0
          AND DATE(COALESCE(event_date, start_date)) >= DATE('now')
    """).fetchone()[0] or 0

    total_bookings = db.execute("""
        SELECT COUNT(*)
        FROM bookings
        WHERE LOWER(COALESCE(status, 'active')) != 'cancelled'
    """).fetchone()[0] or 0

    tickets_booked = db.execute("""
        SELECT COALESCE(SUM(ticket_quantity), 0)
        FROM bookings
        WHERE LOWER(COALESCE(status, 'active')) != 'cancelled'
    """).fetchone()[0] or 0

    registered_users = db.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE LOWER(username) != 'admin'
    """).fetchone()[0] or 0

    booking_value = db.execute("""
        SELECT COALESCE(
            SUM(
                COALESCE(b.ticket_quantity, 0) *
                COALESCE(e.price, 0)
            ),
            0
        )
        FROM bookings b
        JOIN events e ON b.event_id = e.id
        WHERE LOWER(COALESCE(b.status, 'active')) != 'cancelled'
    """).fetchone()[0] or 0

    # =========================================================
    # UPCOMING EVENTS
    # =========================================================

    upcoming_events = db.execute("""
        SELECT
            e.id,
            e.title,
            e.category,
            e.location,
            e.event_date,
            e.start_date,
            e.end_date,
            e.capacity,
            e.available_tickets,
            e.price,
            COALESCE(
                (
                    SELECT SUM(b.ticket_quantity)
                    FROM bookings b
                    WHERE b.event_id = e.id
                      AND LOWER(COALESCE(b.status, 'active')) != 'cancelled'
                ),
                0
            ) AS tickets_booked
        FROM events e
        WHERE COALESCE(e.is_deleted, 0) = 0
          AND DATE(COALESCE(e.event_date, e.start_date)) >= DATE('now')
        ORDER BY DATE(COALESCE(e.event_date, e.start_date)) ASC
        LIMIT 5
    """).fetchall()

    # =========================================================
    # RECENT BOOKINGS
    # =========================================================

    recent_bookings = db.execute("""
        SELECT
            b.id,
            b.booking_date,
            b.ticket_quantity,
            b.status,
            u.username,
            u.email,
            e.title AS event_title,
            e.category,
            e.price
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        JOIN events e ON b.event_id = e.id
        ORDER BY datetime(b.booking_date) DESC
        LIMIT 6
    """).fetchall()

    # =========================================================
    # EVENT PERFORMANCE
    # =========================================================

    event_performance = db.execute("""
        SELECT
            e.id,
            e.title,
            e.category,
            e.capacity,
            e.price,
            COALESCE(
                SUM(
                    CASE
                        WHEN LOWER(COALESCE(b.status, 'active')) != 'cancelled'
                        THEN COALESCE(b.ticket_quantity, 0)
                        ELSE 0
                    END
                ),
                0
            ) AS tickets_booked,
            COALESCE(
                SUM(
                    CASE
                        WHEN LOWER(COALESCE(b.status, 'active')) != 'cancelled'
                        THEN COALESCE(b.ticket_quantity, 0) * COALESCE(e.price, 0)
                        ELSE 0
                    END
                ),
                0
            ) AS revenue
        FROM events e
        LEFT JOIN bookings b ON e.id = b.event_id
        WHERE COALESCE(e.is_deleted, 0) = 0
        GROUP BY e.id
        ORDER BY tickets_booked DESC
        LIMIT 5
    """).fetchall()

    # =========================================================
    # CATEGORY OVERVIEW
    # =========================================================

    raw_categories = db.execute("""
        SELECT category, COUNT(*) AS event_count
        FROM events
        WHERE COALESCE(is_deleted, 0) = 0
        GROUP BY category
        ORDER BY event_count DESC
    """).fetchall()

    category_map = {
        "Technology": 0,
        "Art": 0,
        "Culture": 0,
        "Fitness": 0,
        "Concerts": 0,
    }

    for row in raw_categories:
        category = (row["category"] or "").strip().lower()
        count = row["event_count"] or 0

        if "tech" in category:
            category_map["Technology"] += count
        elif "art" in category:
            category_map["Art"] += count
        elif "culture" in category:
            category_map["Culture"] += count
        elif "fitness" in category:
            category_map["Fitness"] += count
        elif "concert" in category or "music" in category:
            category_map["Concerts"] += count

    category_total = sum(category_map.values())

    category_overview = []

    for name, count in category_map.items():
        percentage = round((count / category_total) * 100, 1) if category_total else 0

        category_overview.append(
            {"name": name, "count": count, "percentage": percentage}
        )

    # =========================================================
    # BOOKING ACTIVITY — LAST 7 DAYS
    # =========================================================

    booking_activity = db.execute("""
        SELECT
            DATE(booking_date) AS booking_day,
            COUNT(*) AS booking_count,
            COALESCE(SUM(ticket_quantity), 0) AS ticket_count
        FROM bookings
        WHERE LOWER(COALESCE(status, 'active')) != 'cancelled'
          AND DATE(booking_date) >= DATE('now', '-6 days')
        GROUP BY DATE(booking_date)
        ORDER BY DATE(booking_date)
    """).fetchall()

    activity_map = {
        row["booking_day"]: {
            "bookings": row["booking_count"],
            "tickets": row["ticket_count"],
        }
        for row in booking_activity
    }

    booking_chart = []

    for i in range(6, -1, -1):
        day = datetime.now().date() - timedelta(days=i)
        key = day.strftime("%Y-%m-%d")

        booking_chart.append(
            {
                "label": day.strftime("%a"),
                "date": key,
                "bookings": activity_map.get(key, {}).get("bookings", 0),
                "tickets": activity_map.get(key, {}).get("tickets", 0),
            }
        )

    booking_chart_max = max([item["bookings"] for item in booking_chart] or [1])

    # =========================================================
    # WEDDING ACTIVITY
    # =========================================================

    wedding_stats = db.execute("""
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(guest_count), 0) AS guests,
            SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) = 'confirmed'
                    THEN 1 ELSE 0
                END
            ) AS confirmed,
            SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) = 'pending'
                    THEN 1 ELSE 0
                END
            ) AS pending,
            SUM(
                CASE
                    WHEN LOWER(COALESCE(status, '')) = 'cancelled'
                    THEN 1 ELSE 0
                END
            ) AS cancelled
        FROM wedding_bookings
    """).fetchone()

    # =========================================================
    # QUICK SUMMARY
    # =========================================================

    trashed_events_count = db.execute("""
        SELECT COUNT(*)
        FROM events
        WHERE COALESCE(is_deleted, 0) = 1
    """).fetchone()[0] or 0

    # =========================================================
    # CATEGORY DONUT GRADIENT
    # =========================================================

    category_colors = ["#c97835", "#6d8fb3", "#789b88", "#9a7b9e", "#b28b58"]

    gradient_parts = []
    current_percentage = 0

    for index, category in enumerate(category_overview):
        if category["percentage"] <= 0:
            continue

        next_percentage = current_percentage + category["percentage"]

        gradient_parts.append(
            f"{category_colors[index]} " f"{current_percentage}% " f"{next_percentage}%"
        )

        current_percentage = next_percentage

    if gradient_parts:
        category_gradient = "conic-gradient(" + ", ".join(gradient_parts) + ")"
    else:
        category_gradient = "conic-gradient(#d9d9d9 0% 100%)"

    return render_template(
        "admin_dashboard.html",
        total_events=total_events,
        upcoming_events_count=upcoming_events_count,
        total_bookings=total_bookings,
        tickets_booked=tickets_booked,
        registered_users=registered_users,
        booking_value=booking_value,
        upcoming_events=upcoming_events,
        recent_bookings=recent_bookings,
        event_performance=event_performance,
        category_overview=category_overview,
        category_gradient=category_gradient,
        booking_chart=booking_chart,
        booking_chart_max=booking_chart_max,
        wedding_stats=wedding_stats,
        trashed_events_count=trashed_events_count,
    )


@app.route("/admin/view_events")
def view_events():
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()

    # Check whether the newer cancellation-history column exists.
    booking_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(bookings)").fetchall()
    }

    has_original_quantity = "original_ticket_quantity" in booking_columns

    events = db.execute("""
        SELECT *
        FROM events
        WHERE is_deleted = 0
        ORDER BY
            CASE
                WHEN event_date IS NULL OR event_date = ''
                THEN start_date
                ELSE event_date
            END ASC,
            id DESC
    """).fetchall()

    today = datetime.now().date()

    event_operations = []

    for event in events:

        # --------------------------------------------------
        # Event status
        # --------------------------------------------------
        event_date_value = event["event_date"] or event["start_date"]

        try:
            event_date_obj = datetime.strptime(event_date_value, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            event_date_obj = None

        if event_date_obj:
            if event_date_obj < today:
                event_status = "Past"
            elif event_date_obj == today:
                event_status = "Live"
            else:
                event_status = "Upcoming"
        else:
            event_status = "Upcoming"

        # --------------------------------------------------
        # Booking information
        # --------------------------------------------------
        if has_original_quantity:
            booking_rows = db.execute(
                """
                SELECT
                    ticket_quantity,
                    original_ticket_quantity,
                    status
                FROM bookings
                WHERE event_id = ?
            """,
                (event["id"],),
            ).fetchall()
        else:
            booking_rows = db.execute(
                """
                SELECT
                    ticket_quantity,
                    ticket_quantity AS original_ticket_quantity,
                    status
                FROM bookings
                WHERE event_id = ?
            """,
                (event["id"],),
            ).fetchall()

        original_booked = 0
        active_tickets = 0
        cancelled_tickets = 0
        active_bookings = 0
        cancelled_bookings = 0

        for booking in booking_rows:

            original_qty = int(booking["original_ticket_quantity"] or 0)

            current_qty = int(booking["ticket_quantity"] or 0)

            cancelled_qty = max(original_qty - current_qty, 0)

            original_booked += original_qty
            active_tickets += current_qty
            cancelled_tickets += cancelled_qty

            booking_status = (booking["status"] or "").lower()

            if booking_status == "cancelled":
                cancelled_bookings += 1
            elif current_qty > 0:
                active_bookings += 1

        capacity = int(event["capacity"] or 0)

        available_tickets = max(capacity - active_tickets, 0)

        occupancy = round((active_tickets / capacity) * 100) if capacity > 0 else 0

        booking_value = original_booked * float(event["price"] or 0)

        event_operations.append(
            {
                "id": event["id"],
                "title": event["title"],
                "description": event["description"],
                "category": event["category"],
                "location": event["location"],
                "image": event["image"],
                "start_date": event["start_date"],
                "end_date": event["end_date"],
                "event_date": event["event_date"],
                "time": event["time"],
                "price": float(event["price"] or 0),
                "capacity": capacity,
                "original_booked": original_booked,
                "cancelled_tickets": cancelled_tickets,
                "active_tickets": active_tickets,
                "available_tickets": available_tickets,
                "active_bookings": active_bookings,
                "cancelled_bookings": cancelled_bookings,
                "occupancy": occupancy,
                "booking_value": booking_value,
                "status": event_status,
            }
        )

    total_events = len(event_operations)

    upcoming_count = sum(
        1 for event in event_operations if event["status"] == "Upcoming"
    )

    live_count = sum(1 for event in event_operations if event["status"] == "Live")

    past_count = sum(1 for event in event_operations if event["status"] == "Past")

    return render_template(
        "view_events.html",
        events=event_operations,
        total_events=total_events,
        upcoming_count=upcoming_count,
        live_count=live_count,
        past_count=past_count,
    )


@app.route("/admin/event_details/<int:event_id>")
def admin_event_details(event_id):
    if not is_admin():
        flash("Admin access only.")
        return redirect(url_for("login"))

    db = get_db()

    # ---------------------------------------------------------
    # Get event
    # ---------------------------------------------------------
    event = db.execute(
        """
        SELECT *
        FROM events
        WHERE id = ?
    """,
        (event_id,),
    ).fetchone()

    if not event:
        flash("Event not found.")
        return redirect(url_for("view_events"))

    # ---------------------------------------------------------
    # Detect booking history column
    # ---------------------------------------------------------
    booking_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(bookings)").fetchall()
    }

    has_original_quantity = "original_ticket_quantity" in booking_columns

    # ---------------------------------------------------------
    # Booking data
    # ---------------------------------------------------------
    if has_original_quantity:
        bookings = db.execute(
            """
            SELECT
                bookings.id,
                bookings.booking_date,
                bookings.ticket_quantity,
                bookings.original_ticket_quantity,
                bookings.status,
                users.username,
                users.email
            FROM bookings
            JOIN users
                ON bookings.user_id = users.id
            WHERE bookings.event_id = ?
            ORDER BY bookings.booking_date DESC
        """,
            (event_id,),
        ).fetchall()
    else:
        bookings = db.execute(
            """
            SELECT
                bookings.id,
                bookings.booking_date,
                bookings.ticket_quantity,
                bookings.ticket_quantity AS original_ticket_quantity,
                bookings.status,
                users.username,
                users.email
            FROM bookings
            JOIN users
                ON bookings.user_id = users.id
            WHERE bookings.event_id = ?
            ORDER BY bookings.booking_date DESC
        """,
            (event_id,),
        ).fetchall()

    # ---------------------------------------------------------
    # Calculate totals
    # ---------------------------------------------------------
    original_booked = 0
    active_tickets = 0
    cancelled_tickets = 0
    active_bookings = 0
    cancelled_bookings = 0

    customer_bookings = []

    for booking in bookings:

        original_qty = int(booking["original_ticket_quantity"] or 0)

        current_qty = int(booking["ticket_quantity"] or 0)

        cancelled_qty = max(original_qty - current_qty, 0)

        original_booked += original_qty
        active_tickets += current_qty
        cancelled_tickets += cancelled_qty

        booking_status = (booking["status"] or "").lower()

        if booking_status == "cancelled":
            cancelled_bookings += 1
            display_status = "Cancelled"
        else:
            active_bookings += 1
            display_status = "Active"

        customer_bookings.append(
            {
                "id": booking["id"],
                "username": booking["username"],
                "email": booking["email"],
                "booking_date": booking["booking_date"],
                "original_quantity": original_qty,
                "current_quantity": current_qty,
                "cancelled_quantity": cancelled_qty,
                "status": display_status,
            }
        )

    capacity = int(event["capacity"] or 0)

    available_tickets = max(capacity - active_tickets, 0)

    booking_rate = round((active_tickets / capacity) * 100) if capacity else 0

    booking_value = original_booked * float(event["price"] or 0)

    # ---------------------------------------------------------
    # Booking activity by date
    # ---------------------------------------------------------
    activity_rows = db.execute(
        """
        SELECT
            DATE(booking_date) AS booking_day,
            SUM(ticket_quantity) AS tickets
        FROM bookings
        WHERE event_id = ?
        GROUP BY DATE(booking_date)
        ORDER BY booking_day ASC
    """,
        (event_id,),
    ).fetchall()

    booking_activity = [
        {"date": row["booking_day"], "tickets": int(row["tickets"] or 0)}
        for row in activity_rows
    ]

    return render_template(
        "admin_event_details.html",
        event=event,
        original_booked=original_booked,
        cancelled_tickets=cancelled_tickets,
        active_tickets=active_tickets,
        available_tickets=available_tickets,
        active_bookings=active_bookings,
        cancelled_bookings=cancelled_bookings,
        booking_rate=booking_rate,
        booking_value=booking_value,
        customer_bookings=customer_bookings,
        booking_activity=booking_activity,
    )


@app.route("/event/<int:event_id>")
def event_detail(event_id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Get event details
    cursor.execute("SELECT * FROM events WHERE id=?", (event_id,))
    event = cursor.fetchone()

    # Calculate booked tickets
    cursor.execute(
        "SELECT SUM(ticket_quantity) FROM bookings WHERE event_id=?", (event_id,)
    )
    booked = cursor.fetchone()[0] or 0

    available_tickets = event["capacity"] - booked

    # Convert time to 12-hour format
    if event and event["time"]:
        try:
            time_24 = datetime.strptime(event["time"], "%H:%M")
            time_12 = time_24.strftime("%I:%M %p")
        except:
            time_12 = event["time"]
    else:
        time_12 = "N/A"

    from_page = request.args.get("from_page")

    conn.close()
    return render_template(
        "event_detail.html",
        event=event,
        time_12=time_12,
        available_tickets=available_tickets,
        from_page=from_page,
    )


def auto_mark_expired_events():
    db = get_db()
    today = datetime.today().date().isoformat()

    # Mark events as deleted if their end_date has passed and they are not already deleted
    db.execute(
        """
        UPDATE events
        SET is_deleted = 1,
            deleted_at = ?
        WHERE end_date < ? AND is_deleted = 0
    """,
        (today, today),
    )
    db.commit()


@app.route("/tech")
def tech_page():
    auto_mark_expired_events()
    db = get_db()
    tech_events = db.execute(
        "SELECT * FROM events WHERE category = ? AND is_deleted = 0", ("Tech",)
    ).fetchall()
    return render_template("tech.html", tech_events=tech_events)


@app.route("/art")
def art_page():
    auto_mark_expired_events()
    db = get_db()
    art_events = db.execute(
        "SELECT * FROM events WHERE category = ? AND is_deleted = 0", ("Art",)
    ).fetchall()
    return render_template("art.html", art_events=art_events)


@app.route("/culture")
def culture_page():
    auto_mark_expired_events()
    db = get_db()
    culture_events = db.execute(
        "SELECT * FROM events WHERE category = ? AND is_deleted = 0", ("Culture",)
    ).fetchall()
    return render_template("culture.html", culture_events=culture_events)


@app.route("/fitness")
def fitness_page():
    auto_mark_expired_events()
    db = get_db()
    fitness_events = db.execute(
        "SELECT * FROM events WHERE category = ? AND is_deleted = 0", ("Fitness",)
    ).fetchall()
    return render_template("fitness.html", fitness_events=fitness_events)


# =========================================================
# CONCERT EVENTS
# =========================================================
@app.route("/concerts")
def concerts_page():
    auto_mark_expired_events()
    db = get_db()

    concert_events = db.execute(
        "SELECT * FROM events WHERE category = ? AND is_deleted = 0", ("Concerts",)
    ).fetchall()

    return render_template("concerts.html", concert_events=concert_events)


# Step 1: Confirm Personal Info & Event Details
@app.route("/start_booking/<int:event_id>", methods=["GET", "POST"])
def start_booking(event_id):
    session.pop("booking", None)  # 🔄 Clear previous
    session.modified = True
    if "user_id" not in session:
        flash("Please login first.")
        return redirect(url_for("login"))

    db = get_db()
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (session["user_id"],)
    ).fetchone()
    event = db.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    if request.method == "POST":
        session["booking"] = {"event_id": event_id, "user_id": session["user_id"]}
        return redirect(url_for("ticket_quantity"))

    return render_template("booking_step1_info.html", user=user, event=event, step=1)


@app.route("/ticket_quantity", methods=["GET", "POST"])
def ticket_quantity():
    # Step 1: Ensure user is in the booking flow
    if "booking" not in session:
        flash("Please start the booking process.")
        return redirect(url_for("index"))

    # Step 2: Get event info from DB
    db = get_db()
    event = db.execute(
        "SELECT * FROM events WHERE id = ?", (session["booking"]["event_id"],)
    ).fetchone()

    if not event:
        flash("Event not found.")
        return redirect(url_for("index"))

    # Step 3: Handle POST (form submission)
    if request.method == "POST":

        if request.form.get("action") == "back":
            return redirect(
                url_for("start_booking", event_id=session["booking"]["event_id"])
            )

        try:
            quantity = int(request.form.get("quantity"))
        except (TypeError, ValueError):
            flash("Invalid ticket quantity.")
            return redirect(url_for("ticket_quantity"))

        # Handle possible None in available_tickets
        available_tickets = event["available_tickets"]
        if available_tickets is None:
            flash("Ticket availability not configured for this event.")
            return redirect(url_for("index"))

        if quantity > available_tickets:
            flash(f"Only {available_tickets} tickets available.")
            return redirect(url_for("ticket_quantity"))

        # Save booking details in session
        session["booking"]["ticket_quantity"] = quantity
        session["booking"]["total_price"] = quantity * event["price"]
        session.modified = True  # ✅ Force Flask to save session changes

        return redirect(url_for("payment_method"))

    # Step 4: Render the template for ticket selection
    return render_template("booking_step2_quantity.html", event=event, step=2)


PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com"
# PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
# PAYPAL_SECRET = os.getenv("PAYPAL_SECRET")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_SECRET = os.getenv("PAYPAL_SECRET")


def get_paypal_access_token():
    if not PAYPAL_CLIENT_ID or not PAYPAL_SECRET:
        raise RuntimeError(
            "PayPal credentials are missing. Set PAYPAL_CLIENT_ID "
            "and PAYPAL_SECRET in the PowerShell window running Flask."
        )

    try:
        response = requests.post(
            f"{PAYPAL_API_BASE}/v1/oauth2/token",
            headers={
                "Accept": "application/json",
                "Accept-Language": "en_US",
            },
            data={"grant_type": "client_credentials"},
            auth=(PAYPAL_CLIENT_ID, PAYPAL_SECRET),
            timeout=20,
        )
        result = response.json()
    except requests.RequestException as exc:
        app.logger.exception("Could not connect to PayPal.")
        raise RuntimeError("Could not connect to PayPal.") from exc
    except ValueError as exc:
        raise RuntimeError(
            f"PayPal returned an unreadable response (HTTP {response.status_code})."
        ) from exc

    access_token = result.get("access_token")
    if response.ok and access_token:
        return access_token

    # Show PayPal's error, but never log or display your credentials.
    reason = (
        result.get("error_description")
        or result.get("message")
        or result.get("error")
        or "PayPal did not provide an access token."
    )
    app.logger.error(
        "PayPal token request failed (HTTP %s): %s",
        response.status_code,
        reason,
    )
    raise RuntimeError(
        f"PayPal authentication failed (HTTP {response.status_code}): {reason}"
    )


@app.route("/payment_method", methods=["GET", "POST"])
def payment_method():
    if "booking" not in session:
        flash("Please start the booking process.")
        return redirect(url_for("index"))

    db = get_db()
    event = db.execute(
        "SELECT * FROM events WHERE id = ?", (session["booking"]["event_id"],)
    ).fetchone()
    total_price_inr = session["booking"].get("total_price", event["price"])
    session["booking"]["total_price"] = total_price_inr  # Store for later use
    session.modified = True

    # Convert INR to USD (for sandbox use only)
    inr_to_usd_rate = 0.012  # Approx conversion rate for testing
    total_price_usd = round(total_price_inr * inr_to_usd_rate, 2)

    if request.method == "POST":

        # ✅ Handle back button
        if request.form.get("action") == "back":
            return redirect(url_for("ticket_quantity"))

        access_token = get_paypal_access_token()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }

        payment_payload = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "amount": {
                        "currency_code": "USD",  # PayPal Sandbox prefers USD
                        "value": f"{total_price_usd:.2f}",
                    }
                }
            ],
            "application_context": {
                "return_url": url_for("payment_success", _external=True),
                "cancel_url": url_for("payment_cancel", _external=True),
            },
        }

        response = requests.post(
            f"{PAYPAL_API_BASE}/v2/checkout/orders",
            headers=headers,
            json=payment_payload,
        )
        order = response.json()

        if response.status_code == 201 and "links" in order:
            for link in order["links"]:
                if link["rel"] == "approve":
                    session["booking"]["paypal_order_id"] = order["id"]
                    session.modified = True
                    return redirect(link["href"])

        flash("Failed to create PayPal order.")
        return redirect(url_for("payment_method"))

    return render_template(
        "booking_step3_payment.html", step=3, total_price=total_price_inr
    )


@app.route("/payment_success")
def payment_success():
    booking = session.get("booking")
    if not booking or "paypal_order_id" not in booking:
        flash("Payment verification failed.")
        return redirect(url_for("index"))

    access_token = get_paypal_access_token()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    order_id = booking["paypal_order_id"]

    capture_url = f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture"
    response = requests.post(capture_url, headers=headers)
    capture_result = response.json()

    if "status" in capture_result and capture_result["status"] == "COMPLETED":
        session["booking"]["payment_method"] = "PayPal"
        session.modified = True
        return redirect(url_for("confirm_booking"))
    else:
        flash("Payment was not successful. Please try again.")
        return redirect(url_for("payment_method"))


@app.route("/payment_cancel")
def payment_cancel():
    flash("Payment was cancelled.")
    return redirect(url_for("payment_method"))


@app.route("/confirm_booking")
def confirm_booking():
    booking_data = session.get("booking")
    if not booking_data:
        flash("Booking session expired or not started.")
        return redirect(url_for("index"))

    required_keys = ["user_id", "event_id", "ticket_quantity", "payment_method"]
    if not all(k in booking_data for k in required_keys):
        flash("Incomplete booking data. Please restart the booking process.")
        return redirect(
            url_for("start_booking", event_id=booking_data.get("event_id", 1))
        )

    db = get_db()

    # Fetch event and user details for email
    event = db.execute(
        "SELECT * FROM events WHERE id = ?", (booking_data["event_id"],)
    ).fetchone()
    user = db.execute(
        "SELECT * FROM users WHERE id = ?", (booking_data["user_id"],)
    ).fetchone()

    # Insert the booking
    db.execute(
        "INSERT INTO bookings (user_id, event_id, ticket_quantity, original_ticket_quantity, payment_method, booking_date) VALUES (?, ?, ?, ?, ?, ?)",
        (
            booking_data["user_id"],
            booking_data["event_id"],
            booking_data["ticket_quantity"],
            booking_data["ticket_quantity"],
            booking_data["payment_method"],
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )

    db.execute(
        "UPDATE events SET available_tickets = available_tickets - ? WHERE id = ?",
        (booking_data["ticket_quantity"], booking_data["event_id"]),
    )
    db.commit()

    # ✅ Send email confirmation
    subject = f"Booking Confirmed: {event['title']}"
    body = f"""Hi {user['username']},

Thank you for booking with us!

Here are your booking details:

Event: {event['title']}
Date: {event['event_date']}
Location: {event['location']}
Tickets: {booking_data['ticket_quantity']}
Paid via: {booking_data['payment_method']}
Booked on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

We look forward to seeing you there!

Best regards,  
Event Management Team
"""
    send_email(user["email"], subject, body)

    # Clear session
    session.pop("booking", None)

    flash("Booking successful! Confirmation email sent.")
    return redirect(url_for("profile", step=4))


# wedding planning
@app.route("/wedding-themes")
def wedding_themes():
    db = get_db()
    themes = db.execute("SELECT * FROM wedding_themes").fetchall()
    return render_template("wedding_themes.html", themes=themes)


@app.route("/theme/<int:theme_id>")
def view_theme(theme_id):
    db = get_db()
    theme = db.execute(
        "SELECT * FROM wedding_themes WHERE id = ?", (theme_id,)
    ).fetchone()
    events = db.execute(
        "SELECT * FROM theme_events WHERE theme_id = ?", (theme_id,)
    ).fetchall()
    return render_template("theme_detail.html", theme=theme, events=events)


@app.route("/book-theme/<int:theme_id>", methods=["GET", "POST"])
def book_theme(theme_id):

    # ---------------------------------------------------------
    # LOGIN CHECK — UNCHANGED
    # ---------------------------------------------------------
    if "user_id" not in session:
        flash("Please log in to submit a wedding plan.")
        return redirect(url_for("login"))

    db = get_db()

    # ---------------------------------------------------------
    # GET SELECTED WEDDING THEME — UNCHANGED
    # ---------------------------------------------------------
    theme = db.execute(
        """
        SELECT *
        FROM wedding_themes
        WHERE id = ?
        """,
        (theme_id,),
    ).fetchone()

    if not theme:
        flash("Wedding theme not found.")
        return redirect(url_for("wedding_themes"))

    # ---------------------------------------------------------
    # EXISTING EVENT LIST — UNCHANGED
    # ---------------------------------------------------------
    events = [
        {"id": 1, "event_name": "Mehendi"},
        {"id": 2, "event_name": "Sangeet"},
        {"id": 3, "event_name": "Haldi"},
        {"id": 4, "event_name": "Reception"},
    ]

    # ---------------------------------------------------------
    # POST = SUBMIT WEDDING PLAN
    # ---------------------------------------------------------
    if request.method == "POST":

        user_id = session["user_id"]

        # BASIC WEDDING DETAILS
        wedding_date = request.form.get("wedding_date", "").strip()
        guest_count = request.form.get("guest_count", "").strip()
        wedding_location = request.form.get("wedding_location", "").strip()
        venue = request.form.get("venue", "").strip()
        wedding_days = request.form.get("wedding_days", "").strip()

        # CONTACT DETAILS
        # Keep the existing field names expected by this route.
        user_name = request.form.get("full_name", "").strip()
        user_email = request.form.get("email", "").strip()
        user_contact = request.form.get("phone", "").strip()

        contact_method = request.form.get("contact_method", "").strip()
        contact_note = request.form.get("contact_note", "").strip()

        # WEDDING EVENTS
        selected_events = request.form.getlist("events")

        # EVENT CUSTOMIZATION
        ceremony_services = request.form.getlist("ceremony_services")

        # GENERAL SERVICES
        selected_services = request.form.getlist("services")

        # PREFERENCES
        preferences = request.form.get("preferences", "").strip()

        # OPTIONAL ADD-ONS
        selected_addons = request.form.getlist("addons")

        # KEEP EXISTING BRIDE/GROOM BEHAVIOR
        bride_name = ""
        groom_name = ""

        # -----------------------------------------------------
        # SAVE MULTIPLE VALUES AS JSON ARRAYS IN THEIR COLUMNS
        # -----------------------------------------------------
        per_event_customization = {"Wedding Ceremony": ceremony_services}

        # Preserve the existing JSON structure for other code
        # that may already read catering_preferences.
        customization_data = {
            "wedding_location": wedding_location,
            "wedding_days": wedding_days,
            "selected_events": selected_events,
            "event_customization": per_event_customization,
            "services": selected_services,
            "addons": selected_addons,
            "preferences": preferences,
            "contact_method": contact_method,
            "contact_note": contact_note,
        }

        catering_json = json.dumps(customization_data)

        # BOOKING TIMESTAMP — UNCHANGED
        booked_on = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        booking_date = booked_on

        # -----------------------------------------------------
        # INSERT BOOKING WITH NEW INDIVIDUAL COLUMNS
        # -----------------------------------------------------
        db.execute(
            """
            INSERT INTO wedding_bookings (
                user_id,
                theme_id,
                wedding_date,
                booking_date,
                guest_count,
                venue,
                full_name,
                email,
                phone,
                catering_preferences,
                booked_on,
                bride_name,
                groom_name,
                wedding_location,
                wedding_days,
                selected_events,
                per_event_customization,
                general_services,
                preferences,
                optional_addons,
                preferred_contact_method,
                contact_note
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                user_id,
                theme_id,
                wedding_date,
                booking_date,
                guest_count,
                venue,
                user_name,
                user_email,
                user_contact,
                catering_json,
                booked_on,
                bride_name,
                groom_name,
                wedding_location,
                wedding_days,
                json.dumps(selected_events),
                json.dumps(per_event_customization),
                json.dumps(selected_services),
                preferences,
                json.dumps(selected_addons),
                contact_method,
                contact_note,
            ),
        )

        # SAVE — UNCHANGED
        db.commit()

        # SUCCESS MESSAGE — UNCHANGED
        flash(
            "Your wedding planning request has been submitted successfully. "
            "Please wait for admin confirmation."
        )

        # PROFILE REDIRECT — UNCHANGED
        return redirect(url_for("profile"))

    # GET = SHOW PAGE — UNCHANGED
    return render_template("book_theme.html", theme=theme, events=events)


# ============================================================
# EVENTFLOW — WEDDING BOOKING STATUS WORKFLOW
# ============================================================

WEDDING_STATUS_LABELS = {
    "new_request": "New Request",
    "under_review": "Under Review",
    "changes_requested": "Changes Requested",
    "confirmed": "Confirmed",
    "declined": "Declined",
    "cancelled": "Cancelled",
}

# Map legacy values to the current workflow.
WEDDING_STATUS_ALIASES = {
    "pending": "new_request",
    "new": "new_request",
    "new_request": "new_request",
    "under review": "under_review",
    "under_review": "under_review",
    "changes requested": "changes_requested",
    "changes_requested": "changes_requested",
    "confirmed": "confirmed",
    "declined": "declined",
    "cancelled": "cancelled",
    "canceled": "cancelled",
}

WEDDING_STATUS_TRANSITIONS = {
    "new_request": {
        "under_review",
        "changes_requested",
        "confirmed",
        "declined",
        "cancelled",
    },
    "under_review": {
        "changes_requested",
        "confirmed",
        "declined",
        "cancelled",
    },
    "changes_requested": {
        "under_review",
        "confirmed",
        "declined",
        "cancelled",
    },
    "confirmed": {"cancelled"},
    "declined": set(),
    "cancelled": set(),
}


def normalize_wedding_status(value):
    """Return the canonical status for a stored legacy/current value."""
    if not value:
        return "new_request"

    return WEDDING_STATUS_ALIASES.get(
        str(value).strip().lower(),
        None,
    )


from datetime import date, timedelta


@app.route("/admin-wedding-bookings", methods=["GET"])
def admin_wedding_bookings():
    if not is_admin():
        flash("You must be an admin to manage wedding bookings.", "error")
        return redirect(url_for("login"))

    today = date.today()
    today_iso = today.isoformat()

    # Initialize chart data before try, so the error fallback can use it too.
    trend_months = [today.replace(day=1)]
    for _ in range(5):
        trend_months.append((trend_months[-1] - timedelta(days=1)).replace(day=1))
    trend_months.reverse()

    wedding_trend = {
        "labels": [month.strftime("%b") for month in trend_months],
        "requests": [0] * 6,
        "confirmed": [0] * 6,
    }

    trend_index = {
        month.strftime("%Y-%m"): index for index, month in enumerate(trend_months)
    }

    status_labels = {
        "new_request": "New Request",
        "under_review": "Under Review",
        "changes_requested": "Changes Requested",
        "confirmed": "Confirmed",
        "declined": "Declined",
        "cancelled": "Cancelled",
        "legacy": "Needs Review",
    }

    status_order = [
        "new_request",
        "under_review",
        "changes_requested",
        "confirmed",
        "declined",
        "cancelled",
    ]

    def parse_wedding_date(value):
        if not value:
            return None
        try:
            return date.fromisoformat(str(value).strip()[:10])
        except (TypeError, ValueError):
            return None

    def decode_booking_json(value, fallback):
        if value in (None, ""):
            return fallback
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback

    db = get_db()

    try:
        rows = db.execute("""
            SELECT
                wb.id,
                wb.user_id,
                wb.theme_id,
                wb.booking_date,
                wb.guest_count,
                wb.status,
                wb.booked_on,
                wb.venue,
                wb.theme,
                wb.full_name,
                wb.email AS booking_email,
                wb.phone,
                wb.catering_preferences,
                wb.bride_name,
                wb.groom_name,
                wb.wedding_date,
                wb.wedding_location,
                wb.wedding_days,
                wb.selected_events,
                wb.per_event_customization,
                wb.general_services,
                wb.preferences,
                wb.optional_addons,
                wb.preferred_contact_method,
                wb.contact_note,
                u.username,
                u.email AS account_email,
                wt.name AS saved_theme_name
            FROM wedding_bookings AS wb
            LEFT JOIN users AS u ON u.id = wb.user_id
            LEFT JOIN wedding_themes AS wt ON wt.id = wb.theme_id
            ORDER BY wb.id DESC
        """).fetchall()

        bookings = []

        for row in rows:
            item = dict(row)

            # Older bookings may have these values only in
            # catering_preferences, so keep that compatibility fallback.
            try:
                legacy_customization = json.loads(
                    item.get("catering_preferences") or "{}"
                )
                if not isinstance(legacy_customization, dict):
                    legacy_customization = {}
            except (TypeError, ValueError):
                legacy_customization = {}

            item["selected_events_list"] = decode_booking_json(
                item.get("selected_events"),
                legacy_customization.get("selected_events", []),
            )
            if not isinstance(item["selected_events_list"], list):
                item["selected_events_list"] = []

            item["per_event_customization_data"] = decode_booking_json(
                item.get("per_event_customization"),
                legacy_customization.get("event_customization", {}),
            )
            if not isinstance(item["per_event_customization_data"], dict):
                item["per_event_customization_data"] = {}

            item["general_services_list"] = decode_booking_json(
                item.get("general_services"),
                legacy_customization.get("services", []),
            )
            if not isinstance(item["general_services_list"], list):
                item["general_services_list"] = []

            item["optional_addons_list"] = decode_booking_json(
                item.get("optional_addons"),
                legacy_customization.get("addons", []),
            )
            if not isinstance(item["optional_addons_list"], list):
                item["optional_addons_list"] = []

            item["wedding_location_display"] = (
                str(
                    item.get("wedding_location")
                    or legacy_customization.get("wedding_location")
                    or ""
                ).strip()
                or "Not provided"
            )

            item["wedding_days_display"] = (
                str(
                    item.get("wedding_days")
                    or legacy_customization.get("wedding_days")
                    or ""
                ).strip()
                or "Not provided"
            )

            try:
                item["wedding_days_count"] = max(
                    1, min(int(item.get("wedding_days") or 1), 14)
                )
            except (TypeError, ValueError):
                item["wedding_days_count"] = 1

            item["preferences_display"] = (
                str(
                    item.get("preferences")
                    or legacy_customization.get("preferences")
                    or ""
                ).strip()
                or "No preferences provided"
            )

            item["preferred_contact_method_display"] = (
                str(
                    item.get("preferred_contact_method")
                    or legacy_customization.get("contact_method")
                    or ""
                ).strip()
                or "Not specified"
            )

            item["contact_note_display"] = (
                str(
                    item.get("contact_note")
                    or legacy_customization.get("contact_note")
                    or ""
                ).strip()
                or "No additional note"
            )

            # Keep wedding date separate from request submission date.
            item["wedding_date_value"] = (
                str(item.get("wedding_date") or "").strip()[:10] or None
            )

            parsed_date = parse_wedding_date(item["wedding_date_value"])

            item["wedding_date_display"] = (
                parsed_date.strftime("%d %b %Y") if parsed_date else "Not provided"
            )

            item["customer_name"] = (
                str(item.get("full_name") or "").strip()
                or str(item.get("username") or "").strip()
                or "Customer"
            )

            item["customer_email"] = (
                str(item.get("booking_email") or "").strip()
                or str(item.get("account_email") or "").strip()
                or "No email provided"
            )

            item["theme_name"] = (
                str(item.get("saved_theme_name") or "").strip()
                or str(item.get("theme") or "").strip()
                or "Wedding plan"
            )

            canonical_status = normalize_wedding_status(item.get("status"))
            item["status_key"] = canonical_status or "legacy"
            item["status_label"] = status_labels[item["status_key"]]

            item["date_object"] = parsed_date
            item["is_upcoming"] = (
                item["status_key"] == "confirmed"
                and parsed_date is not None
                and parsed_date > today
            )
            item["is_today"] = (
                item["status_key"] == "confirmed" and parsed_date == today
            )
            item["is_past_confirmed"] = (
                item["status_key"] == "confirmed"
                and parsed_date is not None
                and parsed_date < today
            )

            current_status = item["status_key"]
            item["allowed_statuses"] = (
                sorted(
                    WEDDING_STATUS_TRANSITIONS.get(current_status, set()),
                    key=lambda value: (
                        status_order.index(value) if value in status_order else 999
                    ),
                )
                if current_status != "legacy"
                else []
            )

            bookings.append(item)

        # Count requests by the month they were submitted.
        for booking in bookings:
            submitted_date = parse_wedding_date(booking.get("booked_on"))
            if submitted_date is None:
                continue

            index = trend_index.get(submitted_date.strftime("%Y-%m"))
            if index is None:
                continue

            wedding_trend["requests"][index] += 1

            # This counts requests that are currently confirmed,
            # grouped by the month they were originally submitted.
            if booking["status_key"] == "confirmed":
                wedding_trend["confirmed"][index] += 1

        # Count bookings by wedding theme.
        theme_counts = {}
        for booking in bookings:
            theme_name = booking["theme_name"]
            theme_counts[theme_name] = theme_counts.get(theme_name, 0) + 1

        theme_bookings = [
            {"name": name, "count": count}
            for name, count in sorted(
                theme_counts.items(),
                key=lambda item: (-item[1], item[0].casefold()),
            )
        ]

        theme_booking_total = sum(theme["count"] for theme in theme_bookings)

        # Keep the existing dashboard totals.
        stats = {
            "total": len(bookings),
            "new_request": sum(b["status_key"] == "new_request" for b in bookings),
            "under_review": sum(b["status_key"] == "under_review" for b in bookings),
            "changes_requested": sum(
                b["status_key"] == "changes_requested" for b in bookings
            ),
            "confirmed": sum(b["status_key"] == "confirmed" for b in bookings),
            "declined": sum(b["status_key"] == "declined" for b in bookings),
            "cancelled": sum(b["status_key"] == "cancelled" for b in bookings),
            "upcoming": sum(b["is_upcoming"] for b in bookings),
            "ongoing": sum(b["is_today"] for b in bookings),
            "completed_by_date": sum(b["is_past_confirmed"] for b in bookings),
            "guests": sum(max(0, int(b.get("guest_count") or 0)) for b in bookings),
        }

        stats["attention"] = (
            stats["new_request"] + stats["under_review"] + stats["changes_requested"]
        )

        upcoming_confirmed = sorted(
            [b for b in bookings if b["is_upcoming"]],
            key=lambda b: b["date_object"],
        )
        next_wedding = upcoming_confirmed[0] if upcoming_confirmed else None

        if next_wedding:
            next_wedding["days_until"] = (next_wedding["date_object"] - today).days

        # Keep the existing search and status filters for the request list.
        search_query = request.args.get("q", "").strip()[:120]
        selected_status = request.args.get("status", "all").strip().lower()

        if selected_status != "all":
            normalized_filter = normalize_wedding_status(selected_status)
            if normalized_filter:
                selected_status = normalized_filter
            elif selected_status != "legacy":
                selected_status = "invalid"

        filtered_bookings = bookings

        if search_query:
            query_lower = search_query.casefold()
            filtered_bookings = [
                b
                for b in filtered_bookings
                if query_lower in str(b["id"]).casefold()
                or query_lower in b["customer_name"].casefold()
                or query_lower in b["customer_email"].casefold()
                or query_lower in b["theme_name"].casefold()
                or query_lower in str(b.get("venue") or "").casefold()
                or query_lower
                in str(b.get("wedding_location_display") or "").casefold()
                or query_lower in str(b.get("phone") or "").casefold()
                or query_lower
                in str(b.get("preferred_contact_method_display") or "").casefold()
            ]

        if selected_status not in ("all", "invalid"):
            filtered_bookings = [
                b for b in filtered_bookings if b["status_key"] == selected_status
            ]
        elif selected_status == "invalid":
            filtered_bookings = []

        # Build a Sunday-first calendar using wedding dates.
        calendar_month = request.args.get("month", today.strftime("%Y-%m"))

        try:
            month_start = date.fromisoformat(calendar_month + "-01")
        except ValueError:
            month_start = today.replace(day=1)
            calendar_month = month_start.strftime("%Y-%m")

        calendar_title = month_start.strftime("%B %Y")
        previous_month = (month_start - timedelta(days=1)).replace(day=1)
        next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)

        calendar_prev = previous_month.strftime("%Y-%m")
        calendar_next = next_month.strftime("%Y-%m")

        selected_date = request.args.get("date", today_iso)

        try:
            selected_date_object = date.fromisoformat(selected_date)
        except ValueError:
            selected_date_object = today
            selected_date = today_iso

        first_calendar_day = month_start - timedelta(
            days=(month_start.weekday() + 1) % 7
        )

        calendar_days = []
        for offset in range(42):
            calendar_date = first_calendar_day + timedelta(days=offset)
            day_bookings = []

            for booking in bookings:
                wedding_start = booking.get("date_object")
                if wedding_start is None:
                    continue

                wedding_end = wedding_start + timedelta(
                    days=booking["wedding_days_count"]
                )

                if wedding_start <= calendar_date < wedding_end:
                    day_bookings.append(booking)

            calendar_days.append(
                {
                    "iso": calendar_date.isoformat(),
                    "day": calendar_date.day,
                    "in_month": calendar_date.month == month_start.month,
                    "bookings": day_bookings,
                }
            )

        selected_day_bookings = [
            booking
            for booking in bookings
            if booking.get("date_object") is not None
            and booking["date_object"]
            <= selected_date_object
            < booking["date_object"] + timedelta(days=booking["wedding_days_count"])
        ]

        selected_date_display = selected_date_object.strftime("%d %b %Y")

        requested_booking_id = request.args.get("selected", "").strip()
        selected_booking = next(
            (
                booking
                for booking in bookings
                if str(booking["id"]) == requested_booking_id
            ),
            None,
        )

        if not request.args.get("selected") and not request.args.get("date"):
            selected_booking = next(
                (
                    booking
                    for booking in bookings
                    if booking["status_key"]
                    in (
                        "new_request",
                        "under_review",
                        "changes_requested",
                    )
                ),
                bookings[0] if bookings else None,
            )
        elif not requested_booking_id and selected_day_bookings:
            selected_booking = selected_day_bookings[0]

        upcoming_cutoff = today + timedelta(days=90)

        # Confirmed weddings happening within the next 90 days.
        upcoming_bookings = sorted(
            (
                booking
                for booking in bookings
                if booking["is_upcoming"] and booking["date_object"] <= upcoming_cutoff
            ),
            key=lambda booking: booking["date_object"],
        )

        # ALL confirmed future weddings, including those within 90 days.
        confirmed_bookings = sorted(
            (booking for booking in bookings if booking["is_upcoming"]),
            key=lambda booking: booking["date_object"],
        )

        # Confirmed weddings whose dates have passed.
        completed_bookings = sorted(
            (booking for booking in bookings if booking["is_past_confirmed"]),
            key=lambda booking: booking["date_object"],
            reverse=True,
        )

        return render_template(
            "admin_wedding_bookings.html",
            bookings=filtered_bookings,
            stats=stats,
            next_wedding=next_wedding,
            today=today_iso,
            search_query=search_query,
            selected_status=selected_status,
            status_labels=status_labels,
            status_order=status_order,
            calendar_month=calendar_month,
            calendar_title=calendar_title,
            calendar_prev=calendar_prev,
            calendar_next=calendar_next,
            calendar_days=calendar_days,
            selected_date=selected_date,
            selected_date_display=selected_date_display,
            selected_day_bookings=selected_day_bookings,
            selected_booking=selected_booking,
            upcoming_bookings=upcoming_bookings,
            confirmed_bookings=confirmed_bookings,
            completed_bookings=completed_bookings,
            theme_bookings=theme_bookings,
            theme_booking_total=theme_booking_total,
            wedding_trend=wedding_trend,
        )

    except Exception:
        app.logger.exception("Could not load the Manage Weddings dashboard")
        flash(
            "Wedding dashboard could not be loaded. " "Check the application log.",
            "error",
        )

        return render_template(
            "admin_wedding_bookings.html",
            bookings=[],
            stats={
                "total": 0,
                "new_request": 0,
                "under_review": 0,
                "changes_requested": 0,
                "confirmed": 0,
                "declined": 0,
                "cancelled": 0,
                "upcoming": 0,
                "ongoing": 0,
                "completed_by_date": 0,
                "guests": 0,
                "attention": 0,
            },
            next_wedding=None,
            today=today_iso,
            search_query="",
            selected_status="all",
            status_labels=status_labels,
            status_order=status_order,
            calendar_month=today.strftime("%Y-%m"),
            calendar_title=today.strftime("%B %Y"),
            calendar_prev=today.strftime("%Y-%m"),
            calendar_next=today.strftime("%Y-%m"),
            calendar_days=[],
            selected_date=today_iso,
            selected_date_display=today.strftime("%d %b %Y"),
            selected_day_bookings=[],
            selected_booking=None,
            upcoming_bookings=[],
            confirmed_bookings=[],
            completed_bookings=[],
            theme_bookings=[],
            theme_booking_total=0,
            wedding_trend=wedding_trend,
        )


@app.route(
    "/admin-update-booking-status/<int:booking_id>",
    methods=["POST"],
)
def admin_update_booking_status(booking_id):
    # Only admins can update wedding booking statuses.
    if not is_admin():
        flash(
            "You must be an admin to manage wedding bookings.",
            "error",
        )
        return redirect(url_for("login"))

    # Keep the admin on the page where the status change was made.
    # The destination is chosen from fixed Flask endpoints, not a URL
    # supplied by the browser.
    def status_redirect():
        if request.form.get("return_to") == "detail":
            return redirect(
                url_for(
                    "admin_wedding_request_detail",
                    booking_id=booking_id,
                    q=request.form.get("return_q", ""),
                    from_date=request.form.get("return_from_date", ""),
                    to_date=request.form.get("return_to_date", ""),
                    status=request.form.get("return_status", "all"),
                    theme_id=request.form.get("return_theme_id", ""),
                )
            )

        if request.form.get("return_to") == "requests":
            return redirect(
                url_for(
                    "admin_wedding_requests",
                    q=request.form.get("return_q", ""),
                    from_date=request.form.get("return_from_date", ""),
                    to_date=request.form.get("return_to_date", ""),
                    status=request.form.get("return_status", "all"),
                    theme_id=request.form.get("return_theme_id", ""),
                )
            )

        return redirect(url_for("admin_wedding_bookings"))

    # Reject missing or invalid status values.
    raw_status = request.form.get("status")
    requested_status = normalize_wedding_status(raw_status)

    if requested_status is None:
        flash("Invalid wedding booking status.", "error")
        return status_redirect()

    db = get_db()

    try:
        booking = db.execute(
            """
            SELECT id, status
            FROM wedding_bookings
            WHERE id = ?
            """,
            (booking_id,),
        ).fetchone()

        if booking is None:
            flash("Wedding booking not found.", "error")
            return status_redirect()

        current_status = normalize_wedding_status(booking["status"])

        # Do not silently overwrite an unknown legacy status.
        if current_status is None:
            flash(
                "This booking has an unrecognized legacy status. "
                "Review it before changing its status.",
                "error",
            )
            return status_redirect()

        if requested_status == current_status:
            flash(
                f"Booking is already " f"{WEDDING_STATUS_LABELS[current_status]}.",
                "info",
            )
            return status_redirect()

        allowed_statuses = WEDDING_STATUS_TRANSITIONS.get(
            current_status,
            set(),
        )

        # Validate the transition on the server.
        # Hiding an option in HTML alone is not security.
        if requested_status not in allowed_statuses:
            flash(
                f"Cannot change "
                f"{WEDDING_STATUS_LABELS[current_status]} "
                f"to {WEDDING_STATUS_LABELS[requested_status]}.",
                "error",
            )
            return status_redirect()

        # Update only if the status has not changed since we read it.
        # This helps prevent overwriting a concurrent admin update.
        cursor = db.execute(
            """
            UPDATE wedding_bookings
            SET status = ?
            WHERE id = ? AND status IS ?
            """,
            (
                requested_status,
                booking_id,
                booking["status"],
            ),
        )

        if cursor.rowcount != 1:
            db.rollback()
            flash(
                "This booking was updated by another admin. " "Refresh and try again.",
                "error",
            )
            return status_redirect()

        db.commit()

        flash(
            f"Wedding booking #{booking_id} updated to "
            f"{WEDDING_STATUS_LABELS[requested_status]}.",
            "success",
        )

    except Exception:
        db.rollback()
        app.logger.exception(
            "Failed to update wedding booking status for ID %s",
            booking_id,
        )
        flash(
            "The booking status could not be updated. " "Check the application log.",
            "error",
        )

    return status_redirect()


@app.route("/admin-wedding-requests", methods=["GET"])
def admin_wedding_requests():
    if not is_admin():
        flash("You must be an admin to manage wedding bookings.", "error")
        return redirect(url_for("login"))

    import calendar
    from datetime import date

    today = date.today()

    # Default range starts exactly three calendar months ago.
    month_index = today.year * 12 + today.month - 1 - 3
    default_year, zero_based_month = divmod(month_index, 12)
    default_month = zero_based_month + 1
    default_day = min(
        today.day,
        calendar.monthrange(default_year, default_month)[1],
    )
    default_from_date = date(
        default_year,
        default_month,
        default_day,
    ).isoformat()

    def get_date_filter(parameter, default_value):
        if parameter not in request.args:
            return default_value

        value = request.args.get(parameter, "").strip()
        if not value:
            return ""

        try:
            return date.fromisoformat(value).isoformat()
        except ValueError:
            return default_value

    from_date = get_date_filter("from_date", default_from_date)
    to_date = get_date_filter("to_date", today.isoformat())
    search_query = request.args.get("q", "").strip()[:120]
    selected_status = request.args.get("status", "all").strip().lower()
    selected_theme = request.args.get("theme_id", "").strip()

    if selected_status == "rejected":
        selected_status = "declined"

    status_labels = {
        "new_request": "New Request",
        "under_review": "Under Review",
        "changes_requested": "Change Requested",
        "confirmed": "Confirmed",
        "declined": "Declined / Rejected",
        "cancelled": "Cancelled",
        "legacy": "Needs Review",
    }

    status_order = [
        "new_request",
        "under_review",
        "changes_requested",
        "confirmed",
        "declined",
        "cancelled",
    ]

    if selected_status not in ("all", "legacy"):
        normalized_status = normalize_wedding_status(selected_status)
        if normalized_status:
            selected_status = normalized_status
        else:
            selected_status = "all"

    if selected_theme and not selected_theme.isdigit():
        selected_theme = ""

    db = get_db()

    themes = db.execute("""
        SELECT id, name
        FROM wedding_themes
        ORDER BY name
        """).fetchall()

    where_parts = []
    parameters = []

    # booked_on is the request-submission date. booking_date is retained
    # as a compatibility fallback for older records.
    submitted_date_sql = """
        substr(
            COALESCE(
                NULLIF(wb.booked_on, ''),
                NULLIF(wb.booking_date, '')
            ),
            1,
            10
        )
    """

    if from_date:
        where_parts.append(f"{submitted_date_sql} >= ?")
        parameters.append(from_date)

    if to_date:
        where_parts.append(f"{submitted_date_sql} <= ?")
        parameters.append(to_date)

    if selected_theme:
        where_parts.append("wb.theme_id = ?")
        parameters.append(int(selected_theme))

    where_sql = ""
    if where_parts:
        where_sql = "WHERE " + " AND ".join(where_parts)

    rows = db.execute(
        f"""
        SELECT
            wb.id,
            wb.theme_id,
            wb.booking_date,
            wb.booked_on,
            wb.wedding_date,
            wb.guest_count,
            wb.status,
            wb.venue,
            wb.theme,
            wb.full_name,
            wb.email AS booking_email,
            u.username,
            u.email AS account_email,
            wt.name AS saved_theme_name
        FROM wedding_bookings AS wb
        LEFT JOIN users AS u ON u.id = wb.user_id
        LEFT JOIN wedding_themes AS wt ON wt.id = wb.theme_id
        {where_sql}
        ORDER BY
            COALESCE(
                NULLIF(wb.booked_on, ''),
                NULLIF(wb.booking_date, '')
            ) DESC,
            wb.id DESC
        """,
        parameters,
    ).fetchall()

    bookings = []

    for row in rows:
        item = dict(row)

        item["customer_name"] = (
            str(item.get("full_name") or "").strip()
            or str(item.get("username") or "").strip()
            or "Customer"
        )

        item["customer_email"] = (
            str(item.get("booking_email") or "").strip()
            or str(item.get("account_email") or "").strip()
            or "No email provided"
        )

        item["theme_name"] = (
            str(item.get("saved_theme_name") or "").strip()
            or str(item.get("theme") or "").strip()
            or "Wedding plan"
        )

        item["status_key"] = normalize_wedding_status(item.get("status")) or "legacy"
        item["status_label"] = status_labels.get(item["status_key"], "Needs Review")

        wedding_date_value = str(item.get("wedding_date") or "").strip()[:10]
        try:
            item["wedding_date_display"] = date.fromisoformat(
                wedding_date_value
            ).strftime("%d %b %Y")
        except ValueError:
            item["wedding_date_display"] = "Not provided"

        submitted_value = str(
            item.get("booked_on") or item.get("booking_date") or ""
        ).strip()
        item["submitted_date"] = submitted_value[:10]

        try:
            item["submitted_display"] = date.fromisoformat(
                item["submitted_date"]
            ).strftime("%d %b %Y")
        except ValueError:
            item["submitted_display"] = submitted_value or "Not provided"

        bookings.append(item)

    if selected_status == "legacy":
        bookings = [item for item in bookings if item["status_key"] == "legacy"]
    elif selected_status != "all":
        bookings = [item for item in bookings if item["status_key"] == selected_status]

    if search_query:
        search_lower = search_query.casefold()
        bookings = [
            item
            for item in bookings
            if search_lower in str(item["id"]).casefold()
            or search_lower in item["customer_name"].casefold()
            or search_lower in item["customer_email"].casefold()
            or search_lower in item["theme_name"].casefold()
            or search_lower in str(item.get("venue") or "").casefold()
        ]

    return render_template(
        "admin_wedding_requests.html",
        bookings=bookings,
        themes=themes,
        search_query=search_query,
        from_date=from_date,
        to_date=to_date,
        selected_status=selected_status,
        selected_theme=selected_theme,
        status_labels=status_labels,
        status_order=status_order,
    )


@app.route("/admin-wedding-requests/<int:booking_id>", methods=["GET"])
def admin_wedding_request_detail(booking_id):
    if not is_admin():
        flash("You must be an admin to manage wedding bookings.", "error")
        return redirect(url_for("login"))

    import json
    from datetime import date

    db = get_db()
    row = db.execute(
        """
        SELECT
            wb.*,
            u.username,
            u.email AS account_email,
            wt.name AS saved_theme_name
        FROM wedding_bookings AS wb
        LEFT JOIN users AS u ON u.id = wb.user_id
        LEFT JOIN wedding_themes AS wt ON wt.id = wb.theme_id
        WHERE wb.id = ?
        """,
        (booking_id,),
    ).fetchone()

    if row is None:
        flash("Wedding booking not found.", "error")
        return redirect(url_for("admin_wedding_requests"))

    booking = dict(row)

    theme_row = db.execute(
        "SELECT * FROM wedding_themes WHERE id = ?",
        (booking.get("theme_id"),),
    ).fetchone()

    theme_data = dict(theme_row) if theme_row else {}

    image_column = next(
        (
            column
            for column in theme_data
            if "image" in column.casefold() or "photo" in column.casefold()
        ),
        None,
    )

    booking["theme_image_value"] = (
        theme_data.get(image_column) if image_column else None
    )

    image_value = str(booking.get("theme_image_value") or "").strip()

    if image_value.startswith(("http://", "https://")):
        booking["theme_image_url"] = image_value
    elif image_value:
        image_path = image_value.replace("\\", "/").lstrip("/")
        if image_path.startswith("static/"):
            image_path = image_path[len("static/") :]

        # Theme images saved as a bare filename are in static/images.
        if "/" not in image_path:
            image_path = "images/" + image_path

        booking["theme_image_url"] = url_for(
            "static",
            filename=image_path,
        )
    else:
        booking["theme_image_url"] = None

    def read_list(column, legacy_key):
        value = booking.get(column)
        if value not in (None, ""):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except (TypeError, ValueError):
                return []

        try:
            legacy = json.loads(booking.get("catering_preferences") or "{}")
        except (TypeError, ValueError):
            legacy = {}

        result = legacy.get(legacy_key, [])
        return result if isinstance(result, list) else []

    try:
        legacy = json.loads(booking.get("catering_preferences") or "{}")
        if not isinstance(legacy, dict):
            legacy = {}
    except (TypeError, ValueError):
        legacy = {}

    booking["customer_name"] = (
        str(booking.get("full_name") or "").strip()
        or str(booking.get("username") or "").strip()
        or "Customer"
    )
    booking["customer_email"] = (
        str(booking.get("email") or "").strip()
        or str(booking.get("account_email") or "").strip()
        or "No email provided"
    )
    booking["theme_name"] = (
        str(booking.get("saved_theme_name") or "").strip()
        or str(booking.get("theme") or "").strip()
        or "Wedding plan"
    )

    wedding_date_value = str(booking.get("wedding_date") or "").strip()[:10]
    try:
        booking["wedding_date_display"] = date.fromisoformat(
            wedding_date_value
        ).strftime("%d %b %Y")
    except ValueError:
        booking["wedding_date_display"] = "Not provided"

    submitted_value = str(
        booking.get("booked_on") or booking.get("booking_date") or ""
    ).strip()
    booking["submitted_display"] = submitted_value or "Not provided"

    booking["selected_events_list"] = read_list("selected_events", "selected_events")
    booking["general_services_list"] = read_list("general_services", "services")
    booking["optional_addons_list"] = read_list("optional_addons", "addons")

    customization = booking.get("per_event_customization")
    if customization:
        try:
            customization = json.loads(customization)
        except (TypeError, ValueError):
            customization = {}
    else:
        customization = legacy.get("event_customization", {})
    booking["per_event_customization_data"] = (
        customization if isinstance(customization, dict) else {}
    )

    booking["wedding_location_display"] = (
        str(
            booking.get("wedding_location") or legacy.get("wedding_location") or ""
        ).strip()
        or "Not provided"
    )
    booking["wedding_days_display"] = (
        str(booking.get("wedding_days") or legacy.get("wedding_days") or "").strip()
        or "Not provided"
    )
    booking["preferences_display"] = (
        str(booking.get("preferences") or legacy.get("preferences") or "").strip()
        or "Not provided"
    )
    booking["preferred_contact_display"] = (
        str(
            booking.get("preferred_contact_method")
            or legacy.get("contact_method")
            or ""
        ).strip()
        or "Not provided"
    )
    booking["contact_note_display"] = (
        str(booking.get("contact_note") or legacy.get("contact_note") or "").strip()
        or "No customer note"
    )
    booking["budget_display"] = (
        str(legacy.get("budget") or "").strip() or "Not provided"
    )

    status_labels = {
        "new_request": "New Request",
        "under_review": "Under Review",
        "changes_requested": "Change Requested",
        "confirmed": "Confirmed",
        "declined": "Declined / Rejected",
        "cancelled": "Cancelled",
        "legacy": "Needs Review",
    }
    status_order = [
        "new_request",
        "under_review",
        "changes_requested",
        "confirmed",
        "declined",
        "cancelled",
    ]

    booking["status_key"] = normalize_wedding_status(booking.get("status")) or "legacy"
    booking["status_label"] = status_labels[booking["status_key"]]
    booking["allowed_statuses"] = sorted(
        WEDDING_STATUS_TRANSITIONS.get(booking["status_key"], set()),
        key=lambda value: (status_order.index(value) if value in status_order else 999),
    )

    return render_template(
        "admin_wedding_request_detail.html",
        booking=booking,
        status_labels=status_labels,
        q=request.args.get("q", ""),
        from_date=request.args.get("from_date", ""),
        to_date=request.args.get("to_date", ""),
        selected_status=request.args.get("status", "all"),
        selected_theme=request.args.get("theme_id", ""),
    )


def build_wedding_booking_pdf(booking_id):
    db = get_db()

    row = db.execute(
        """
        SELECT
            wb.*,
            u.username,
            u.email AS account_email,
            wt.name AS saved_theme_name
        FROM wedding_bookings AS wb
        LEFT JOIN users AS u ON u.id = wb.user_id
        LEFT JOIN wedding_themes AS wt ON wt.id = wb.theme_id
        WHERE wb.id = ?
        """,
        (booking_id,),
    ).fetchone()

    if row is None:
        raise ValueError("Wedding booking not found.")

    booking = dict(row)

    def parse_json(value, fallback):
        if not value:
            return fallback
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback

    legacy = parse_json(booking.get("catering_preferences"), {})
    if not isinstance(legacy, dict):
        legacy = {}

    def selected_json(column, legacy_key, expected_type):
        value = booking.get(column)
        parsed = parse_json(value, None) if value else None

        if not isinstance(parsed, expected_type):
            parsed = legacy.get(legacy_key, [])

        return parsed if isinstance(parsed, expected_type) else expected_type()

    name = (
        str(booking.get("full_name") or "").strip()
        or str(booking.get("username") or "").strip()
        or "Customer"
    )
    recipient = (
        str(booking.get("email") or "").strip()
        or str(booking.get("account_email") or "").strip()
    )
    theme_name = (
        str(booking.get("saved_theme_name") or "").strip()
        or str(booking.get("theme") or "").strip()
        or "Wedding plan"
    )

    events = selected_json("selected_events", "selected_events", list)
    services = selected_json("general_services", "services", list)
    addons = selected_json("optional_addons", "addons", list)

    customization = selected_json(
        "per_event_customization",
        "event_customization",
        dict,
    )

    preferences = (
        booking.get("preferences") or legacy.get("preferences") or "Not provided"
    )
    location = (
        booking.get("wedding_location")
        or legacy.get("wedding_location")
        or "Not provided"
    )
    wedding_days = (
        booking.get("wedding_days") or legacy.get("wedding_days") or "Not provided"
    )
    contact_method = (
        booking.get("preferred_contact_method")
        or legacy.get("contact_method")
        or "Not provided"
    )
    contact_note = (
        booking.get("contact_note") or legacy.get("contact_note") or "No customer note"
    )
    budget = booking.get("budget") or legacy.get("budget") or "Not provided"

    def pdf_safe(value):
        if value is None or value == "":
            value = "Not provided"

        if isinstance(value, (list, tuple)):
            value = ", ".join(str(part) for part in value) or "Not provided"
        elif isinstance(value, dict):
            value = (
                ", ".join(f"{key}: {val}" for key, val in value.items())
                or "Not provided"
            )
        else:
            value = str(value)

        # Helvetica in ReportLab does not support every Unicode character.
        return value.replace("₹", "INR ").encode("latin-1", "replace").decode("latin-1")

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=letter)
    page_width, page_height = letter
    y = page_height - 52

    def draw_line(value, bold=False, indent=0):
        nonlocal y

        value = pdf_safe(value)
        wrapped = textwrap.wrap(value, width=88) or [""]

        for line in wrapped:
            if y < 55:
                pdf.showPage()
                y = page_height - 52

            pdf.setFont("Helvetica-Bold" if bold else "Helvetica", 10)
            pdf.setFillColor(HexColor("#20252b"))
            pdf.drawString(48 + indent, y, line)
            y -= 15

    def draw_section(title):
        nonlocal y

        if y < 85:
            pdf.showPage()
            y = page_height - 52

        y -= 7
        pdf.setFillColor(HexColor("#b98216"))
        pdf.setFont("Helvetica-Bold", 12)
        pdf.drawString(48, y, pdf_safe(title))
        y -= 19

    pdf.setFillColor(HexColor("#b98216"))
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(48, y, "Wedding Booking Plan")
    y -= 25

    pdf.setFillColor(HexColor("#555555"))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(48, y, f"Booking reference: WF-{booking_id:04d}")
    y -= 23

    draw_section("Customer Information")
    draw_line(f"Name: {name}")
    draw_line(f"Email: {recipient}")
    draw_line(f"Phone: {booking.get('phone') or 'Not provided'}")
    draw_line(f"Preferred contact: {contact_method}")
    draw_line(f"Customer note: {contact_note}")

    draw_section("Wedding Details")
    draw_line(f"Wedding date: {booking.get('wedding_date') or 'Not provided'}")
    draw_line(f"Wedding days: {wedding_days}")
    draw_line(f"Venue: {booking.get('venue') or 'Not provided'}")
    draw_line(f"Location: {location}")
    draw_line(f"Guest count: {booking.get('guest_count') or 'Not provided'}")
    draw_line(f"Budget: {budget}")

    draw_section("Selected Theme")
    draw_line(theme_name)

    draw_section("Selected Events")
    if events:
        for event in events:
            draw_line(f"• {event}", indent=8)
    else:
        draw_line("Not provided")

    draw_section("Event-specific Services")
    if customization:
        for event_name, event_services in customization.items():
            draw_line(f"{event_name}: {event_services}", indent=8)
    else:
        draw_line("Not provided")

    draw_section("General Services")
    draw_line(services)

    draw_section("Optional Add-ons")
    draw_line(addons)

    draw_section("Customer Preferences")
    draw_line(preferences)

    draw_section("Booking Status")
    draw_line(booking.get("status") or "Not provided")

    pdf.save()
    output.seek(0)

    return output.getvalue(), recipient, name


@app.route("/admin/preview_pdf/<int:booking_id>")
def preview_pdf_page(booking_id):
    if not is_admin():
        flash("You must be an admin to view this wedding plan.", "error")
        return redirect(url_for("login"))

    db = get_db()
    booking = db.execute(
        """
        SELECT id, full_name, status
        FROM wedding_bookings
        WHERE id = ?
        """,
        (booking_id,),
    ).fetchone()

    if booking is None:
        flash("Wedding booking not found.", "error")
        return redirect(url_for("admin_wedding_requests"))

    return render_template(
        "admin_wedding_plan_preview.html",
        booking=booking,
        booking_status_key=normalize_wedding_status(booking["status"]),
        q=request.args.get("q", ""),
        from_date=request.args.get("from_date", ""),
        to_date=request.args.get("to_date", ""),
        selected_status=request.args.get("status", "all"),
        selected_theme=request.args.get("theme_id", ""),
    )


@app.route("/admin/preview_pdf/<int:booking_id>/file")
def admin_wedding_plan_pdf_file(booking_id):
    if not is_admin():
        return "Unauthorized", 403

    try:
        pdf_bytes, _, _ = build_wedding_booking_pdf(booking_id)

        return send_file(
            BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=request.args.get("download") == "1",
            download_name=f"wedding_plan_WF-{booking_id:04d}.pdf",
        )
    except ValueError:
        return "Wedding booking not found.", 404
    except Exception as exc:
        app.logger.exception(
            "Could not generate PDF for wedding booking %s",
            booking_id,
        )
        return f"PDF generation failed: {type(exc).__name__}: {exc}", 500


@app.route(
    "/admin/wedding-booking/<int:booking_id>/send-plan",
    methods=["POST"],
)
def admin_send_wedding_plan(booking_id):
    if not is_admin():
        flash("You must be an admin to send a wedding plan.", "error")
        return redirect(url_for("login"))

    def return_to_detail():
        return redirect(
            url_for(
                "admin_wedding_request_detail",
                booking_id=booking_id,
                q=request.form.get("q", ""),
                from_date=request.form.get("from_date", ""),
                to_date=request.form.get("to_date", ""),
                status=request.form.get("status", "all"),
                theme_id=request.form.get("theme_id", ""),
            )
        )

    db = get_db()
    booking_row = db.execute(
        "SELECT status FROM wedding_bookings WHERE id = ?",
        (booking_id,),
    ).fetchone()

    if booking_row is None:
        flash("Wedding booking not found.", "error")
        return redirect(url_for("admin_wedding_requests"))

    if normalize_wedding_status(booking_row["status"]) != "confirmed":
        flash("Confirm this booking before sending its plan.", "warning")
        return return_to_detail()

    try:
        pdf_bytes, customer_email, customer_name = build_wedding_booking_pdf(booking_id)

        sent, error_message = send_wedding_plan_email(
            recipient=customer_email,
            subject=f"Confirmed wedding plan WF-{booking_id:04d}",
            body=(
                f"Hello {customer_name},\n\n"
                f"Your wedding booking WF-{booking_id:04d} is confirmed. "
                "Your submitted wedding plan is attached as a PDF.\n\n"
                "EventFlow Wedding Management"
            ),
            filename=f"wedding_plan_WF-{booking_id:04d}.pdf",
            pdf_bytes=pdf_bytes,
        )

        if sent:
            flash("The confirmed plan PDF was emailed to the customer.", "success")
        else:
            flash(error_message, "error")

    except Exception:
        app.logger.exception(
            "Could not prepare wedding plan email for booking %s",
            booking_id,
        )
        flash(
            "The PDF could not be prepared. Check the Flask terminal for the error.",
            "error",
        )

    return return_to_detail()


def send_wedding_plan_email(recipient, subject, body, filename, pdf_bytes):

    sender_email = os.getenv("EVENTFLOW_MAIL_USERNAME")
    sender_password = os.getenv("EVENTFLOW_MAIL_PASSWORD")

    app.logger.warning(
        "Wedding email config check: username_present=%s password_present=%s",
        bool(sender_email),
        bool(sender_password),
    )

    if not recipient:
        return False, "No customer email address was found for this booking."

    if not sender_email or not sender_password:
        return False, (
            "Email was not sent: EVENTFLOW_MAIL_USERNAME or "
            "EVENTFLOW_MAIL_PASSWORD is missing."
        )

    try:
        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = recipient
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain", "utf-8"))

        attachment = MIMEApplication(pdf_bytes, Name=filename)
        attachment["Content-Disposition"] = f'attachment; filename="{filename}"'
        message.attach(attachment)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender_email, sender_password)
            server.sendmail(sender_email, recipient, message.as_string())

        return True, ""

    except Exception as exc:
        app.logger.exception("Wedding plan email failed")
        return False, f"{type(exc).__name__}: {exc}"


@app.template_filter("url_encode")
def url_encode_filter(s):
    if s is None:
        return ""
    return urllib.parse.quote_plus(s)


if __name__ == "__main__":
    app.run(debug=True)
