"""Outgoing mail: SMTP when SMTP_HOST is set, otherwise the console. Tests read the outbox."""

from __future__ import annotations

import logging
import re
import smtplib
from collections import deque
from dataclasses import dataclass
from email.message import EmailMessage

from traceloom.config import Settings

log = logging.getLogger("traceloom.mail")

_API_SECRET = re.compile(r"tl-sk-[A-Za-z0-9_\-]+")


@dataclass(frozen=True)
class Message:
    to: str
    subject: str
    body: str


def scrub(text: str, settings: Settings) -> str:
    """Remove credentials from anything that is logged."""
    text = _API_SECRET.sub("[redacted]", text)
    for secret in (settings.smtp_password, settings.secret_key):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


class Mailer:
    def __init__(self, settings: Settings, outbox_size: int = 200) -> None:
        self.settings = settings
        self.outbox: deque[Message] = deque(maxlen=outbox_size)

    def send(self, to: str, subject: str, body: str) -> bool:
        message = Message(to=to, subject=subject, body=body)
        self.outbox.append(message)
        if not self.settings.smtp_host:
            # Console fallback (FR-A2): the message, never credentials.
            log.info(
                "mail (no SMTP_HOST) to=%s subject=%r\n%s",
                to,
                scrub(subject, self.settings),
                scrub(body, self.settings),
            )
            return True
        try:
            self._send_smtp(message)
        except (OSError, smtplib.SMTPException):
            log.exception("sending mail to %s failed", to)
            return False
        return True

    def _send_smtp(self, message: Message) -> None:
        s = self.settings
        mail = EmailMessage()
        mail["From"], mail["To"], mail["Subject"] = s.smtp_from, message.to, message.subject
        mail.set_content(message.body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            if s.smtp_starttls:
                smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(mail)
