"""
Email helpers for AMS notifications.

SMTP is still used by the AMS Orchestrator to notify a human when the
Incident Router AI Agent can't confidently route an incident on its own.
Report attachments are sent through Amazon SES.

Two distinct situations, two distinct emails:
  - Low confidence but a candidate group exists -> send_human_review_email()
    includes a one-click Approve link (handled by approval_server.py).
  - Insufficient information (the agent explicitly declined to guess) ->
    send_insufficient_information_email() - there's no suggested group to
    approve, so this is a plain notification asking for manual triage.

Default SMTP settings point at Gmail (smtp.gmail.com:587). Gmail requires
an "App Password" for SMTP once 2FA is enabled on the account:
https://myaccount.google.com/apppasswords
Put your Gmail address in SMTP_USERNAME/SMTP_FROM/SMTP_TO_HUMAN_REVIEW
and the App Password (not your normal Gmail password) in SMTP_PASSWORD.
"""
import logging
import smtplib
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

from config import SmtpSettings

logger = logging.getLogger(__name__)


def _send(settings: SmtpSettings, subject: str, text_body: str, html_body: str, log_label: str) -> bool:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.from_addr or settings.username
    msg["To"] = settings.to_human_review
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    if not settings.host or not settings.to_human_review:
        logger.warning(
            "SMTP not fully configured (host/to_human_review missing); skipping email and logging instead:\n%s",
            text_body,
        )
        return False

    try:
        with smtplib.SMTP(settings.host, settings.port, timeout=15) as server:
            if settings.use_tls:
                server.starttls()
            if settings.username:
                server.login(settings.username, settings.password)
            server.sendmail(msg["From"], [settings.to_human_review], msg.as_string())
        logger.info("Sent %s email to %s", log_label, settings.to_human_review)
        return True
    except smtplib.SMTPAuthenticationError:
        logger.exception(
            "SMTP authentication failed for %s. If this is Gmail, make sure SMTP_PASSWORD is an "
            "App Password (https://myaccount.google.com/apppasswords), not your normal Gmail password.",
            settings.username,
        )
        return False
    except Exception:
        logger.exception("Failed to send %s email", log_label)
        return False


def send_report_email_ses(
    settings,
    recipient: str,
    subject: str,
    text_body: str,
    attachment_path: str,
    html_body: Optional[str] = None,
) -> bool:
    sender = settings.source_email
    if not sender or not recipient:
        logger.warning(
            "SES not fully configured (source_email/recipient missing); skipping report email and logging instead:\n%s",
            text_body,
        )
        return False

    msg = _build_report_message(sender, recipient, subject, text_body, attachment_path, html_body)

    try:
        import boto3

        client = boto3.client("ses", region_name=settings.region_name)
        kwargs = {
            "Source": sender,
            "Destinations": [recipient],
            "RawMessage": {"Data": msg.as_string()},
        }
        if settings.configuration_set:
            kwargs["ConfigurationSetName"] = settings.configuration_set
        client.send_raw_email(**kwargs)
        logger.info("Sent report email through SES to %s with attachment %s", recipient, Path(attachment_path).name)
        return True
    except Exception:
        logger.exception("Failed to send report email through SES")
        return False


def _build_report_message(
    sender: str,
    recipient: str,
    subject: str,
    text_body: str,
    attachment_path: str,
    html_body: Optional[str] = None,
):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient

    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text_body, "plain"))
    if html_body:
        body.attach(MIMEText(html_body, "html"))
    msg.attach(body)

    path = Path(attachment_path)
    with path.open("rb") as handle:
        attachment = MIMEApplication(handle.read(), _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    attachment.add_header("Content-Disposition", "attachment", filename=path.name)
    msg.attach(attachment)
    return msg


def send_human_review_email(
    settings: SmtpSettings,
    ticket_number: str,
    short_description: str,
    suggested_group: str,
    confidence: float,
    rationale: str,
    approve_url: str,
) -> bool:
    """Low confidence but a candidate group exists - includes an Approve link."""
    subject = f"[AMS AI] Approval needed: {ticket_number} -> {suggested_group} (confidence {confidence:.0%})"

    text_body = (
        f"The Incident Router AI Agent produced a low-confidence L2 assignment suggestion.\n\n"
        f"Ticket: {ticket_number}\n"
        f"Short description: {short_description}\n"
        f"Suggested assignment group: {suggested_group}\n"
        f"Confidence score: {confidence:.2f}\n"
        f"Model rationale: {rationale}\n\n"
        f"Approve this assignment: {approve_url}\n"
    )

    html_body = f"""\
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    <p>The <strong>Incident Router AI Agent</strong> produced a low-confidence L2 assignment
    suggestion that needs human review.</p>
    <table cellpadding="6" style="border-collapse: collapse;">
      <tr><td><strong>Ticket</strong></td><td>{ticket_number}</td></tr>
      <tr><td><strong>Short description</strong></td><td>{short_description}</td></tr>
      <tr><td><strong>Suggested group</strong></td><td>{suggested_group}</td></tr>
      <tr><td><strong>Confidence</strong></td><td>{confidence:.0%}</td></tr>
      <tr><td><strong>Rationale</strong></td><td>{rationale}</td></tr>
    </table>
    <p style="margin-top: 20px;">
      <a href="{approve_url}"
         style="background-color:#2e7d32;color:#ffffff;padding:12px 24px;
                text-decoration:none;border-radius:4px;font-weight:bold;display:inline-block;">
        Approve &amp; assign to {suggested_group}
      </a>
    </p>
    <p style="color:#777;font-size:12px;margin-top:24px;">
      This link is single-use and expires automatically. If you don't recognize this
      request, no action is needed - it will simply expire.
    </p>
  </body>
</html>
"""
    return _send(settings, subject, text_body, html_body, log_label=f"approval-request ({ticket_number})")


def send_insufficient_information_email(
    settings: SmtpSettings,
    ticket_number: str,
    short_description: str,
    rationale: str,
) -> bool:
    """
    The Incident Router AI Agent explicitly declined to guess a group
    (insufficient_information=True) rather than fabricate one. There is no
    suggested group here, so there's nothing to "approve" - this is a
    plain heads-up that the incident needs manual L2 assignment.
    """
    subject = f"[AMS AI] Manual routing needed: {ticket_number} (AI has insufficient information)"

    text_body = (
        f"The Incident Router AI Agent did not have enough information to confidently route this "
        f"incident, and deliberately avoided guessing.\n\n"
        f"Ticket: {ticket_number}\n"
        f"Short description: {short_description}\n"
        f"Reason: {rationale}\n\n"
        f"Please assign the correct L2 group manually in ServiceNow."
    )

    html_body = f"""\
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    <p>The <strong>Incident Router AI Agent</strong> did not have enough information to confidently
    route this incident, and deliberately avoided guessing rather than risk a wrong assignment.</p>
    <table cellpadding="6" style="border-collapse: collapse;">
      <tr><td><strong>Ticket</strong></td><td>{ticket_number}</td></tr>
      <tr><td><strong>Short description</strong></td><td>{short_description}</td></tr>
      <tr><td><strong>Reason</strong></td><td>{rationale}</td></tr>
    </table>
    <p style="margin-top: 16px;">Please assign the correct L2 group manually in ServiceNow.</p>
  </body>
</html>
"""
    return _send(settings, subject, text_body, html_body, log_label=f"insufficient-information ({ticket_number})")


def send_remediation_approval_email(
    settings: SmtpSettings,
    ticket_number: str,
    short_description: str,
    job_name: str,
    action: str,
    risk_level: str,
    confidence: float,
    rationale: str,
    failed_checks: list,
    approve_url: str,
    reject_url: str,
    action_parameters: Optional[dict] = None,
) -> bool:
    """
    The Job Remediation AI Agent proposed an action but it failed one or
    more guardrail checks (e.g. risk too high, confidence too low, SOP
    marks it non-auto-resolvable, or a required AWS parameter is
    missing). Includes both Approve and Reject links since executing the
    wrong action against production AWS infrastructure is higher-stakes
    than an L2 routing mistake. Shows the extracted action_parameters
    (e.g. ECS cluster/service name, RDS identifier) so the human can
    verify exactly what will run against AWS before approving.
    """
    params_str = ", ".join(f"{k}={v}" for k, v in (action_parameters or {}).items()) or "(none extracted)"
    subject = f"[AMS AI] Remediation approval needed: {ticket_number} -> {action} (confidence {confidence:.0%})"
    checks_str = ", ".join(failed_checks) if failed_checks else "none"

    text_body = (
        f"The Job Remediation AI Agent proposed a remediation action that needs human approval before "
        f"it can run.\n\n"
        f"Ticket: {ticket_number}\n"
        f"Short description: {short_description}\n"
        f"Job: {job_name}\n"
        f"Proposed action: {action}\n"
        f"AWS parameters: {params_str}\n"
        f"Risk level: {risk_level}\n"
        f"Confidence score: {confidence:.2f}\n"
        f"Guardrail checks that did not pass: {checks_str}\n"
        f"Model rationale: {rationale}\n\n"
        f"Approve and run this action: {approve_url}\n"
        f"Reject (leave for manual remediation): {reject_url}\n"
    )

    html_body = f"""\
<html>
  <body style="font-family: Arial, sans-serif; color: #222;">
    <p>The <strong>Job Remediation AI Agent</strong> proposed a remediation action that needs human
    approval before it runs against production.</p>
    <table cellpadding="6" style="border-collapse: collapse;">
      <tr><td><strong>Ticket</strong></td><td>{ticket_number}</td></tr>
      <tr><td><strong>Short description</strong></td><td>{short_description}</td></tr>
      <tr><td><strong>Job</strong></td><td>{job_name}</td></tr>
      <tr><td><strong>Proposed action</strong></td><td>{action}</td></tr>
      <tr><td><strong>AWS parameters</strong></td><td>{params_str}</td></tr>
      <tr><td><strong>Risk level</strong></td><td>{risk_level}</td></tr>
      <tr><td><strong>Confidence</strong></td><td>{confidence:.0%}</td></tr>
      <tr><td><strong>Guardrail checks not passed</strong></td><td>{checks_str}</td></tr>
      <tr><td><strong>Rationale</strong></td><td>{rationale}</td></tr>
    </table>
    <p style="margin-top: 20px;">
      <a href="{approve_url}"
         style="background-color:#2e7d32;color:#ffffff;padding:12px 24px;
                text-decoration:none;border-radius:4px;font-weight:bold;display:inline-block;margin-right:10px;">
        Approve &amp; run {action}
      </a>
      <a href="{reject_url}"
         style="background-color:#c62828;color:#ffffff;padding:12px 24px;
                text-decoration:none;border-radius:4px;font-weight:bold;display:inline-block;">
        Reject
      </a>
    </p>
    <p style="color:#777;font-size:12px;margin-top:24px;">
      These links are single-use and expire automatically.
    </p>
  </body>
</html>
"""
    return _send(settings, subject, text_body, html_body, log_label=f"remediation-approval ({ticket_number})")
