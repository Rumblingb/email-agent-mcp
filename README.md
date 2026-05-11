# Email Agent MCP Server

An MCP (Model Context Protocol) server that gives AI agents email access via IMAP and SMTP. Works with Gmail, Outlook, Yahoo, and any standard email provider that supports app passwords.

## Features

| Tool | Description |
|------|-------------|
| `email_search(query, max_results)` | Search emails by sender, subject, content (Gmail-style query syntax) |
| `email_read(message_id)` | Read full email content with attachment metadata |
| `email_send(to, subject, body, cc?)` | Send emails via SMTP with optional CC |
| `email_list_inbox(max_results, label?)` | List recent emails from inbox or any label/folder |
| `email_draft(to, subject, body)` | Save an email as a draft |

## Quick Start

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Configure

Set these environment variables:

```bash
export EMAIL_ADDRESS="your.email@gmail.com"
export EMAIL_PASSWORD="your-app-password"
```

For custom SMTP/IMAP (optional):

```bash
export EMAIL_SMTP_SERVER="smtp.gmail.com"    # default
export EMAIL_SMTP_PORT="587"                  # default
export EMAIL_IMAP_SERVER="imap.gmail.com"     # default
export EMAIL_IMAP_PORT="993"                  # default
```

### 3. Run

```bash
python server.py
```

## Gmail Setup (App Password)

Gmail requires an **App Password** — your regular password won't work.

1. Go to [Google Account Security](https://myaccount.google.com/security)
2. Enable **2-Step Verification** (required for app passwords)
3. Go to **App passwords** (search in Google Account settings)
4. Select **Mail** and your device, then **Generate**
5. Copy the 16-character password — use this as `EMAIL_PASSWORD`

> **Note:** If you have a Google Workspace account, your admin must enable IMAP access.

## Other Providers

| Provider | SMTP Server | SMTP Port | IMAP Server | IMAP Port |
|----------|-------------|-----------|-------------|-----------|
| Gmail | smtp.gmail.com | 587 | imap.gmail.com | 993 |
| Outlook/Hotmail | smtp.office365.com | 587 | outlook.office365.com | 993 |
| Yahoo Mail | smtp.mail.yahoo.com | 587 | imap.mail.yahoo.com | 993 |
| GMX | smtp.gmx.com | 587 | imap.gmx.com | 993 |
| Zoho | smtp.zoho.com | 587 | imap.zoho.com | 993 |

## Search Query Syntax

Uses Gmail-style search format:

```
"from:alice@example.com subject:invoice after:2024/01/01"
"has:attachment is:unread"
"from:someone@example.com"
"subject:meeting"
```

Supported keywords: `from:`, `subject:`, `to:`, `before:`, `after:`, `has:attachment`, `is:unread`, `is:read`

## License

MIT

## Pricing

$19/month — [Subscribe on Stripe](https://buy.stripe.com/dRm6oJ4Hd2Jugek0wz1oI0m)

---

Built for AI agents. No OAuth required — just an email and app password.
