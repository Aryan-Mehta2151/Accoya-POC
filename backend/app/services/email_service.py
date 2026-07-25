"""Email service using SMTP."""

import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings


def send_password_reset_email(recipient_email: str, reset_link: str) -> bool:
    """Send a password reset email."""
    settings = get_settings()
    
    try:
        msg = MIMEMultipart()
        msg["From"] = settings.smtp_email
        msg["To"] = recipient_email
        msg["Subject"] = "Reset Your Password - Accoya"
        
        html_body = f"""
        <html>
            <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
                <div style="max-width: 600px; margin: 0 auto;">
                    <h2 style="color: #2c3e50;">Password Reset Request</h2>
                    <p>Hi there,</p>
                    <p>We received a request to reset your password. Click the link below to create a new password:</p>
                    <p style="margin: 30px 0;">
                        <a href="{reset_link}" style="display: inline-block; padding: 12px 30px; background-color: #3498db; color: white; text-decoration: none; border-radius: 5px;">
                            Reset Password
                        </a>
                    </p>
                    <p style="color: #7f8c8d; font-size: 14px;">Or copy and paste this link in your browser:</p>
                    <p style="color: #3498db; word-break: break-all; font-size: 12px;">{reset_link}</p>
                    <p style="color: #7f8c8d; font-size: 14px;">This link expires in 15 minutes.</p>
                    <p style="color: #7f8c8d; font-size: 14px;">If you didn't request this, please ignore this email.</p>
                    <hr style="border: none; border-top: 1px solid #ecf0f1; margin: 20px 0;">
                    <p style="color: #95a5a6; font-size: 12px;">© 2026 Accoya. All rights reserved.</p>
                </div>
            </body>
        </html>
        """
        
        msg.attach(MIMEText(html_body, "html"))
        
        # Send email
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_email, settings.smtp_password)
            server.send_message(msg)
        
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        return False
