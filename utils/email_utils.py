import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask_apscheduler import APScheduler
from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from email.mime.application import MIMEApplication
import os
from datetime import datetime
import logging
import os
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATABASE_DIR = BASE_DIR / "database"
DATABASE = str(DATABASE_DIR / "database.db")


def get_wedding_booking_by_id(booking_id):
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM wedding_bookings WHERE id = ?", (booking_id,))
    booking = cursor.fetchone()
    conn.close()
    return dict(booking) if booking else None


def send_email(recipient, subject, body, attachment_path=None):
    sender_email = os.getenv("EVENTFLOW_MAIL_USERNAME")
    sender_password = os.getenv("EVENTFLOW_MAIL_PASSWORD")

    if not sender_email or not sender_password:
        return False, (
            "Email was not sent: configure EVENTFLOW_MAIL_USERNAME "
            "and EVENTFLOW_MAIL_PASSWORD in the environment running Flask."
        )

    if not recipient or not recipient.strip():
        return False, "Email was not sent: the customer email address is missing."

    try:
        message = MIMEMultipart()
        message["From"] = sender_email
        message["To"] = recipient.strip()
        message["Subject"] = subject
        message.attach(MIMEText(body, "plain", "utf-8"))

        if attachment_path:
            if isinstance(attachment_path, tuple):
                filename, file_bytes = attachment_path
            else:
                filename = os.path.basename(attachment_path)
                with open(attachment_path, "rb") as pdf_file:
                    file_bytes = pdf_file.read()

            attachment = MIMEApplication(file_bytes, Name=filename)
            attachment["Content-Disposition"] = f'attachment; filename="{filename}"'
            message.attach(attachment)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(sender_email, sender_password)
            server.send_message(message)

        return True, ""

    except (OSError, smtplib.SMTPException) as exc:
        logger.exception("Email delivery failed.")
        return False, f"Email delivery failed: {type(exc).__name__}: {exc}"


def send_reset_email(email, reset_url):
    subject = "Reset Your Password - Event Management"
    body = f"""Hi,

We received a request to reset your password for your event Management account.

Please click the link below to reset your password:
{reset_url}

If you did not request this, please ignore this email.

Warm regards,  
Event Management Team
"""
    send_email(email, subject, body)


def send_ticket_cancel_email(
    recipient, username, event_title, cancelled, remaining, full_cancel=False
):
    subject = f"Event Booking Cancellation - {event_title}"
    if full_cancel:
        status_line = "Your booking has been fully canceled."
    else:
        status_line = (
            f"You have canceled {cancelled} ticket(s). {remaining} ticket(s) remain."
        )

    body = f"""Hello {username},

This is to confirm that your ticket cancellation for the event "{event_title}" has been processed.

Details:
- Event: {event_title}
- Cancelled Tickets: {cancelled}
- Remaining Tickets: {remaining}
- Status: {"Fully Cancelled" if full_cancel else "Partially Cancelled"}

{status_line}

Thank you,
Event Management Team
"""

    send_email(recipient, subject, body)
