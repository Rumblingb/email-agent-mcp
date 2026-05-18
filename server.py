"""
Email Agent MCP Server
=======================
An MCP (Model Context Protocol) server providing email tools for AI agents.
Uses IMAP for reading/searching and SMTP for sending, with Gmail app password support.
"""

from __future__ import annotations

import email
import imaplib
import os
import re
import smtplib
from dataclasses import dataclass
from email.header import decode_header
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parsedate_to_datetime
from typing import Optional

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, CallToolResult

# ── Configuration ───────────────────────────────────────────────────────────

@dataclass
class EmailConfig:
    """Email configuration from environment variables."""
    address: str
    password: str
    smtp_server: str
    smtp_port: int
    imap_server: str
    imap_port: int

    @classmethod
    def from_env(cls) -> "EmailConfig":
        address = os.environ.get("EMAIL_ADDRESS", "")
        password = os.environ.get("EMAIL_PASSWORD", "")
        smtp_server = os.environ.get("EMAIL_SMTP_SERVER", "smtp.gmail.com")
        smtp_port = int(os.environ.get("EMAIL_SMTP_PORT", "587"))
        imap_server = os.environ.get("EMAIL_IMAP_SERVER", "imap.gmail.com")
        imap_port = int(os.environ.get("EMAIL_IMAP_PORT", "993"))

        if not address or not password:
            raise ValueError(
                "EMAIL_ADDRESS and EMAIL_PASSWORD must be set. "
                "For Gmail, use an App Password (not your regular password)."
            )

        return cls(
            address=address,
            password=password,
            smtp_server=smtp_server,
            smtp_port=smtp_port,
            imap_server=imap_server,
            imap_port=imap_port,
        )


# ── Email Helpers ───────────────────────────────────────────────────────────

def _decode_header_value(header_value: bytes | str | None) -> str:
    """Decode an email header (handles encoded words like =?UTF-8?Q? etc)."""
    if header_value is None:
        return ""
    decoded_parts = decode_header(header_value)
    parts: list[str] = []
    for part_bytes, charset in decoded_parts:
        if isinstance(part_bytes, bytes):
            try:
                charset = charset or "utf-8"
                parts.append(part_bytes.decode(charset, errors="replace"))
            except (LookupError, UnicodeDecodeError):
                parts.append(part_bytes.decode("utf-8", errors="replace"))
        else:
            parts.append(str(part_bytes))
    return " ".join(parts)


def _parse_email_body(msg: email.message.Message) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        parts: list[str] = []
        for part in msg.walk():
            content_type = part.get_content_type()
            if content_type == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        parts.append(payload.decode(charset, errors="replace"))
                    except (LookupError, UnicodeDecodeError):
                        parts.append(payload.decode("utf-8", errors="replace"))
            elif content_type == "text/html" and not parts:
                # Only use HTML if we haven't found a text/plain part
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset, errors="replace")
                        # Strip basic HTML tags for plain text fallback
                        body = re.sub(r"<[^>]+>", "", body)
                        body = re.sub(r"\s+", " ", body).strip()
                        parts.append(body)
                    except (LookupError, UnicodeDecodeError):
                        pass
        return "\n".join(parts) if parts else "(no plain text content)"
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except (LookupError, UnicodeDecodeError):
                return payload.decode("utf-8", errors="replace")
        return "(empty body)"


def _get_attachments_metadata(msg: email.message.Message) -> list[dict]:
    """Extract attachment metadata from an email message."""
    attachments: list[dict] = []
    if msg.is_multipart():
        for part in msg.walk():
            content_disposition = str(part.get("Content-Disposition", ""))
            if "attachment" in content_disposition.lower():
                filename = _decode_header_value(part.get_filename())
                if not filename:
                    filename = "unnamed"
                attachments.append({
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "size": len(part.get_payload(decode=True) or b""),
                })
    return attachments


def _build_search_criteria(query: str) -> str:
    """Convert Gmail-style search query to IMAP SEARCH criteria.

    Supports:
        from:someone@example.com  -> FROM "someone@example.com"
        subject:meeting           -> SUBJECT "meeting"
        before:2024/01/01        -> BEFORE "01-Jan-2024"
        after:2024/01/01         -> SINCE "01-Jan-2024"
        has:attachment            -> X-GM-RAW "has:attachment"
        is:unread                 -> UNSEEN
        is:read                   -> SEEN
    """
    # If it looks like a raw Gmail search, use X-GM-RAW
    gmail_patterns = re.findall(r'(from|subject|to|before|after|has|is):', query.lower())
    if gmail_patterns:
        # Use Gmail's X-GM-RAW extension for complex queries
        return f'X-GM-RAW "{query.replace(chr(34), chr(92) + chr(34))}"'

    # Fallback: use query as a simple text search (body/subject)
    return f'TEXT "{query}"'


def _parse_date_to_imap(date_str: str) -> str:
    """Convert a date string to IMAP format (DD-Mon-YYYY)."""
    from datetime import datetime
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return dt.strftime("%d-%b-%Y")
        except ValueError:
            continue
    return date_str


def _connect_imap(config: EmailConfig) -> imaplib.IMAP4_SSL:
    """Connect and login to IMAP server."""
    imap = imaplib.IMAP4_SSL(config.imap_server, config.imap_port)
    imap.login(config.address, config.password)
    return imap


def _fetch_emails(
    imap: imaplib.IMAP4_SSL,
    message_ids: list[bytes],
    max_results: int = 50,
) -> list[dict]:
    """Fetch email data for a list of message IDs."""
    results: list[dict] = []
    for msg_id in message_ids[:max_results]:
        try:
            status, data = imap.fetch(msg_id, "(RFC822)")
            if status != "OK" or not data or not data[0]:
                continue

            raw_email = data[0][1]
            if isinstance(raw_email, bytes):
                msg = email.message_from_bytes(raw_email)
            else:
                msg = email.message_from_string(raw_email)

            message_id = msg.get("Message-ID", "").strip()
            subject = _decode_header_value(msg.get("Subject", ""))
            sender = _decode_header_value(msg.get("From", ""))
            recipient = _decode_header_value(msg.get("To", ""))
            date_raw = msg.get("Date", "")
            date_str = str(date_raw)

            results.append({
                "id": msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                "message_id": message_id,
                "subject": subject or "(no subject)",
                "from": sender,
                "to": recipient,
                "date": date_str,
                "snippet": "",  # populated on full read
            })
        except Exception:
            continue

    return results


# ── MCP Server ──────────────────────────────────────────────────────────────

class EmailAgentServer:
    """Email Agent MCP server with email tools."""

    def __init__(self, config: EmailConfig):
        self.config = config
        self.server = Server("email-agent")

        # Register tools
        self.server.list_tools()(self._list_tools)
        self.server.call_tool()(self._call_tool)

    def get_server(self) -> Server:
        return self.server

    async def _list_tools(self) -> list[Tool]:
        return [
            Tool(
                name="email_search",
                description=(
                    "Search emails by sender, subject, or content. "
                    "Uses Gmail-style query format.\n\n"
                    "Examples:\n"
                    '  "from:someone@example.com"\n'
                    '  "subject:meeting"\n'
                    '  "from:alice@example.com subject:invoice after:2024/01/01"\n'
                    '  "has:attachment is:unread"\n\n'
                    "Returns a list of matching emails with subject, sender, and date."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query in Gmail format (e.g., 'from:user@example.com subject:hello')",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results (default: 20, max: 100)",
                            "default": 20,
                        },
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="email_read",
                description=(
                    "Read the full content of a specific email by its ID. "
                    "Returns the email body (plain text), sender, recipients, subject, date, "
                    "and metadata about any attachments (filename, type, size)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "message_id": {
                            "type": "string",
                            "description": "The message ID (sequence number or UID) of the email to read",
                        },
                    },
                    "required": ["message_id"],
                },
            ),
            Tool(
                name="email_send",
                description=(
                    "Send an email via SMTP. Supports plain text body and optional CC. "
                    "Uses the configured email account (EMAIL_ADDRESS) as the sender. "
                    "Works with Gmail, Outlook, Yahoo, and any SMTP server."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Recipient email address (single address, use comma for multiple)",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject line",
                        },
                        "body": {
                            "type": "string",
                            "description": "Email body content (plain text)",
                        },
                        "cc": {
                            "type": "string",
                            "description": "CC recipient(s), comma-separated if multiple (optional)",
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            ),
            Tool(
                name="email_list_inbox",
                description=(
                    "List recent emails from the inbox (or a specific label/folder). "
                    "Returns email summaries with subject, sender, date, and a snippet."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of emails to return (default: 20, max: 100)",
                            "default": 20,
                        },
                        "label": {
                            "type": "string",
                            "description": (
                                "Folder/label to list from. Defaults to 'INBOX'. "
                                "Examples: 'INBOX', '[Gmail]/Sent Mail', '[Gmail]/Spam', 'INBOX/Projects'"
                            ),
                        },
                    },
                    "required": [],
                },
            ),
            Tool(
                name="email_draft",
                description=(
                    "Save an email as a draft in the Drafts folder. "
                    "The email is composed with the given recipient, subject, and body, "
                    "then saved as an unsent draft via IMAP."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "to": {
                            "type": "string",
                            "description": "Recipient email address",
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject line",
                        },
                        "body": {
                            "type": "string",
                            "description": "Email body content (plain text)",
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            ),
        ]

    async def _call_tool(self, name: str, arguments: dict) -> list[TextContent]:
        try:
            if name == "email_search":
                result = self._email_search(
                    query=arguments["query"],
                    max_results=min(arguments.get("max_results", 20), 100),
                )
            elif name == "email_read":
                result = self._email_read(message_id=arguments["message_id"])
            elif name == "email_send":
                result = self._email_send(
                    to=arguments["to"],
                    subject=arguments["subject"],
                    body=arguments["body"],
                    cc=arguments.get("cc"),
                )
            elif name == "email_list_inbox":
                result = self._email_list_inbox(
                    max_results=min(arguments.get("max_results", 20), 100),
                    label=arguments.get("label"),
                )
            elif name == "email_draft":
                result = self._email_draft(
                    to=arguments["to"],
                    subject=arguments["subject"],
                    body=arguments["body"],
                )
            else:
                raise ValueError(f"Unknown tool: {name}")

            return [TextContent(type="text", text=result)]
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {type(e).__name__}: {e}")]

    # ── Tool Implementations ──────────────────────────────────────────────

    def _email_search(self, query: str, max_results: int = 20) -> str:
        """Search emails using Gmail-style query."""
        config = self.config
        imap = _connect_imap(config)

        try:
            imap.select("INBOX")

            criteria = _build_search_criteria(query)
            status, message_ids = imap.search(None, criteria)

            if status != "OK":
                return f"Search failed: {status}"

            ids = message_ids[0].split() if message_ids[0] else []
            if not ids:
                return "No results found."

            results = _fetch_emails(imap, ids, max_results=max_results)

            if not results:
                return "No results found."

            lines = [f"Found {len(results)} result(s):", ""]
            for i, email_data in enumerate(results, 1):
                lines.append(f"{i}. Subject: {email_data['subject']}")
                lines.append(f"   From:    {email_data['from']}")
                lines.append(f"   Date:    {email_data['date']}")
                lines.append(f"   ID:      {email_data['id']}")
                lines.append("")

            return "\n".join(lines)
        finally:
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass

    def _email_read(self, message_id: str) -> str:
        """Read full email content."""
        config = self.config
        imap = _connect_imap(config)

        try:
            imap.select("INBOX")

            # Try fetching by sequence number or UID
            try:
                status, data = imap.fetch(message_id.encode(), "(RFC822)")
            except Exception:
                # Try UID fetch
                status, data = imap.uid("FETCH", message_id, "(RFC822)")

            if status != "OK" or not data or not data[0]:
                return f"Could not read message {message_id}. It may have been deleted or the ID is invalid."

            raw_email = data[0][1]
            if isinstance(raw_email, bytes):
                msg = email.message_from_bytes(raw_email)
            else:
                msg = email.message_from_string(raw_email)

            subject = _decode_header_value(msg.get("Subject", "(no subject)"))
            sender = _decode_header_value(msg.get("From", "unknown"))
            recipients = _decode_header_value(msg.get("To", ""))
            cc = _decode_header_value(msg.get("Cc", ""))
            date_str = str(msg.get("Date", "unknown"))
            body = _parse_email_body(msg)
            attachments = _get_attachments_metadata(msg)

            lines = [
                f"Subject: {subject}",
                f"From:    {sender}",
                f"To:      {recipients}",
            ]
            if cc:
                lines.append(f"CC:      {cc}")
            lines.append(f"Date:    {date_str}")
            lines.append(f"ID:      {message_id}")
            if attachments:
                lines.append(f"Attachments: {len(attachments)}")
                for att in attachments:
                    size_kb = att["size"] / 1024
                    lines.append(f"  - {att['filename']} ({att['content_type']}, {size_kb:.1f} KB)")
            else:
                lines.append("Attachments: none")
            lines.append("")
            lines.append("─" * 60)
            lines.append(body)

            return "\n".join(lines)
        finally:
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass

    def _email_send(self, to: str, subject: str, body: str, cc: str | None = None) -> str:
        """Send an email via SMTP."""
        config = self.config
        msg = MIMEMultipart("alternative")
        msg["From"] = config.address
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc

        msg.attach(MIMEText(body, "plain", "utf-8"))

        # Collect all recipients
        all_recipients = [r.strip() for r in to.split(",") if r.strip()]
        if cc:
            all_recipients.extend(r.strip() for r in cc.split(",") if r.strip())

        with smtplib.SMTP(config.smtp_server, config.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(config.address, config.password)
            smtp.sendmail(config.address, all_recipients, msg.as_string())

        cc_msg = f" (CC: {cc})" if cc else ""
        return f"Email sent successfully to {to}{cc_msg}. Subject: {subject}"

    def _email_list_inbox(self, max_results: int = 20, label: str | None = None) -> str:
        """List recent inbox messages."""
        config = self.config
        folder = label or "INBOX"
        imap = _connect_imap(config)

        try:
            status, _ = imap.select(folder)
            if status != "OK":
                return f"Could not select folder: {folder}"

            status, message_ids = imap.search(None, "ALL")
            if status != "OK" or not message_ids[0]:
                return f"Inbox ({folder}) is empty."

            ids = message_ids[0].split()
            # Get the most recent messages
            recent_ids = ids[-max_results:]

            results = _fetch_emails(imap, recent_ids, max_results=max_results)

            if not results:
                return f"No messages found in {folder}."

            lines = [f"📬 {folder} — Most recent {len(results)} message(s):", ""]
            for i, email_data in enumerate(reversed(results), 1):
                snippet_preview = email_data.get("snippet", "")
                lines.append(f"{i}. Subject: {email_data['subject']}")
                lines.append(f"   From:    {email_data['from']}")
                lines.append(f"   Date:    {email_data['date']}")
                lines.append(f"   ID:      {email_data['id']}")
                lines.append("")

            return "\n".join(lines)
        finally:
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass

    def _email_draft(self, to: str, subject: str, body: str) -> str:
        """Save a draft to the Drafts folder."""
        config = self.config

        msg = EmailMessage()
        msg["From"] = config.address
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)

        imap = _connect_imap(config)
        try:
            # Try common Gmail drafts folder first
            drafts_folders = ["[Gmail]/Drafts", "Drafts", "INBOX.Drafts", "Draft"]
            drafts_folder = "Drafts"

            for folder in drafts_folders:
                status, _ = imap.select(folder)
                if status == "OK":
                    drafts_folder = folder
                    break

            # Append the message to the drafts folder
            imap.append(
                drafts_folder,
                "\\Draft",
                None,
                msg.as_bytes(),
            )

            return f"Draft saved successfully in '{drafts_folder}'. To: {to}, Subject: {subject}"
        finally:
            try:
                imap.close()
                imap.logout()
            except Exception:
                pass


# ── Entry Point ─────────────────────────────────────────────────────────────

async def main() -> None:
    """Run the Email Agent MCP server."""
    config = EmailConfig.from_env()
    email_server = EmailAgentServer(config)
    server = email_server.get_server()

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream=read_stream,
            write_stream=write_stream,
            initialization_options={"serverName": "email-agent", "serverVersion": "1.0.0"},
        )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
