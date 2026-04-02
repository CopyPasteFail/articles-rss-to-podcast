"""Send a configurable pipeline failure email using standard-library SMTP."""

from __future__ import annotations

import argparse
import os
import pathlib
import smtplib
import ssl
import sys
from email.message import EmailMessage


LOG_TAIL_LINE_COUNT = 80


def build_failure_email_body(
    *,
    pipeline_id: str,
    exit_code: str,
    log_path: pathlib.Path,
) -> str:
    """Build a concise plaintext email body with the failure log tail."""

    log_tail_lines = []
    if log_path.exists():
        log_tail_lines = log_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-LOG_TAIL_LINE_COUNT:]
    log_tail_text = (
        "\n".join(log_tail_lines) if log_tail_lines else "(log file missing)"
    )
    return (
        f"Pipeline: {pipeline_id}\n"
        f"Exit code: {exit_code}\n"
        f"Log path: {log_path}\n\n"
        "Log tail:\n"
        f"{log_tail_text}\n"
    )


def send_failure_email(
    *,
    pipeline_id: str,
    recipients: list[str],
    subject_prefix: str,
    log_path: pathlib.Path,
    exit_code: str,
    smtp_host: str,
    smtp_port: int,
    smtp_username: str,
    smtp_password: str,
    smtp_from: str,
) -> None:
    """Send the failure email using SMTP STARTTLS authentication."""

    email_message = EmailMessage()
    email_message["Subject"] = f"{subject_prefix} {pipeline_id} failed"
    email_message["From"] = smtp_from
    email_message["To"] = ", ".join(recipients)
    email_message.set_content(
        build_failure_email_body(
            pipeline_id=pipeline_id,
            exit_code=exit_code,
            log_path=log_path,
        )
    )

    tls_context = ssl.create_default_context()
    with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as smtp_client:
        smtp_client.starttls(context=tls_context)
        smtp_client.login(smtp_username, smtp_password)
        smtp_client.send_message(email_message)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for optional workflow failure email delivery."""

    parser = argparse.ArgumentParser(description="Send a pipeline failure email.")
    parser.add_argument("--pipeline-id", required=True)
    parser.add_argument("--subject-prefix", required=True)
    parser.add_argument(
        "--recipients", required=True, help="Comma-separated recipients."
    )
    parser.add_argument("--log-path", required=True)
    parser.add_argument("--exit-code", required=True)
    args = parser.parse_args(argv)

    required_env_names = [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM",
    ]
    missing_env_names = [
        env_name
        for env_name in required_env_names
        if not os.getenv(env_name, "").strip()
    ]
    if missing_env_names:
        missing_text = ", ".join(missing_env_names)
        raise RuntimeError(f"Missing SMTP env vars: {missing_text}")

    recipients = [
        recipient.strip()
        for recipient in args.recipients.split(",")
        if recipient.strip()
    ]
    if not recipients:
        raise RuntimeError("At least one failure email recipient is required.")

    send_failure_email(
        pipeline_id=args.pipeline_id,
        recipients=recipients,
        subject_prefix=args.subject_prefix,
        log_path=pathlib.Path(args.log_path).resolve(),
        exit_code=args.exit_code,
        smtp_host=os.environ["SMTP_HOST"].strip(),
        smtp_port=int(os.environ["SMTP_PORT"].strip()),
        smtp_username=os.environ["SMTP_USERNAME"].strip(),
        smtp_password=os.environ["SMTP_PASSWORD"].strip(),
        smtp_from=os.environ["SMTP_FROM"].strip(),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
