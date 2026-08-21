# WhatsApp Integration

**Status: Planned** — no WhatsApp code exists yet. See [../TASKS.md](../TASKS.md) phase 3.

Scope: WhatsApp Business Cloud API integration, webhooks, and messaging compliance.

## Account model

Per tenant, one or more WhatsApp Business phone numbers depending on plan. Stored fields: `tenant_id`, `phone_number_id`, `business_account_id`, `display_phone_number`, access token metadata, `status`, webhook configuration metadata, timestamps. Tenant credentials are stored securely rather than as one global token; secrets never live in code.

## Tenant resolution

```
phone_number_id -> whatsapp_account -> tenant_id -> conversation -> agent
```

The tenant is never inferred from the customer's phone number.

## Webhook contract

1. Verify the Meta subscription challenge.
2. Validate the `X-Hub-Signature-256` signature against the raw request body.
3. Parse the payload and extract message and status events.
4. Resolve the account and tenant.
5. Persist events idempotently using WhatsApp message/event IDs.
6. Create or update the conversation.
7. Enqueue asynchronous processing.
8. Return quickly.

AI and media work never runs inside the webhook request. Meta retries are expected, so duplicate deliveries must not produce duplicate processing or duplicate outbound messages. Rate limiting must not drop Meta retries.

## Client abstraction

A single async httpx-based client owns all Graph API calls, with a configurable API version: `send_text`, `send_image`, `send_document`, `send_video`, `send_audio`, `send_location`, `send_button`, `send_list`, `send_template`. Raw HTTP calls are never scattered through services.

## Supported message types

Text, image, document, PDF, audio, video, location, interactive buttons, interactive lists, and templates. Inbound delivery statuses and read receipts update stored message state.

## Messaging policy

Outside the 24-hour customer service window, only approved templates may be sent. Templates carry `tenant_id`, `name`, `language`, `category`, `status`, and components. Campaigns and follow-ups must respect opt-in, template approval, and rate limits; uncontrolled bulk sending is not implemented. Related: [AI_AGENTS.md](AI_AGENTS.md), [CRM.md](CRM.md).
