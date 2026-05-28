from __future__ import annotations

import smtplib
from email.message import EmailMessage

from crypto_monitor.config import Settings
from crypto_monitor.models import Digest
from crypto_monitor.retry import retry_call


class EmailDelivery:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def configured(self) -> bool:
        return bool(self.settings.smtp_host and self.settings.smtp_from)

    def send(self, digest: Digest, recipients: list[str]) -> None:
        if not self.configured():
            raise RuntimeError("SMTP is not configured")
        if not recipients:
            raise ValueError("No email recipients provided")

        message = EmailMessage()
        message["Subject"] = f"Сводка цифровых активов за {digest.digest_date}"
        message["From"] = self.settings.smtp_from
        message["To"] = ", ".join(recipients)
        message.set_content(digest.plain_text)
        message.add_alternative(digest.html, subtype="html")

        def _send_once() -> None:
            with smtplib.SMTP(self.settings.smtp_host, self.settings.smtp_port, timeout=30) as smtp:
                if self.settings.smtp_use_tls:
                    smtp.starttls()
                if self.settings.smtp_username and self.settings.smtp_password:
                    smtp.login(self.settings.smtp_username, self.settings.smtp_password)
                smtp.send_message(message)

        retry_call(_send_once, attempts=3, base_delay_seconds=2.0, retry_exceptions=(OSError,))
