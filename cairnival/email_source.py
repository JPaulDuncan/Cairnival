"""Instruction via email.

At each wake the agent polls an IMAP mailbox for unseen mail from allowlisted
senders and turns each message into an instruction file. After the wake it can
reply over SMTP with the specimen it wrote.

Only the allowlist is trusted: mail from anyone else is left untouched (and
unseen-flag restored) so a human can review it.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import smtplib
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.parser import BytesParser
from email import policy

from .config import AgentConfig
from .instructions import Instruction


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _body_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_content().strip()
                except Exception:
                    continue
        return ""
    if msg.get_content_type() == "text/plain":
        try:
            return msg.get_content().strip()
        except Exception:
            return ""
    return ""


def fetch_instructions(cfg: AgentConfig) -> list[Instruction]:
    """Poll IMAP for unseen allowlisted mail; return instructions."""
    if not (cfg.email_enabled and cfg.imap_host and cfg.imap_user):
        return []
    instructions: list[Instruction] = []
    conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port)
    try:
        conn.login(cfg.imap_user, cfg.imap_password)
        conn.select("INBOX")
        status, data = conn.search(None, "UNSEEN")
        if status != "OK":
            return []
        for num in data[0].split():
            status, fetched = conn.fetch(num, "(RFC822)")
            if status != "OK" or not fetched or fetched[0] is None:
                continue
            raw = fetched[0][1]
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            sender_name, sender_addr = email.utils.parseaddr(msg.get("From", ""))
            sender_addr = sender_addr.lower()
            if cfg.email_allowlist and sender_addr not in cfg.email_allowlist:
                # Not ours to act on — put the unseen flag back.
                conn.store(num, "-FLAGS", "\\Seen")
                continue
            subject = _decode(msg.get("Subject")) or "email instruction"
            body = _body_text(msg)
            if not body and not subject:
                continue
            instructions.append(
                Instruction(
                    title=subject[:120],
                    body=body or subject,
                    source="email",
                    sender=sender_addr,
                    reply_to=sender_addr,
                    priority=3,
                )
            )
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    return instructions


def send_reply(cfg: AgentConfig, to_addr: str, subject: str, body: str) -> bool:
    """Send a plain-text reply. Returns False (never raises) on failure so a
    broken SMTP setup cannot wedge the wake cycle."""
    if not (cfg.smtp_host and to_addr):
        return False
    msg = EmailMessage()
    msg["From"] = cfg.smtp_from or cfg.smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30) as smtp:
            smtp.starttls()
            if cfg.smtp_user:
                smtp.login(cfg.smtp_user, cfg.smtp_password)
            smtp.send_message(msg)
        return True
    except Exception:
        return False
