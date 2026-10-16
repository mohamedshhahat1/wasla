# Campaigns page - the API contract

**Status: the backend is implemented; no frontend exists yet.** This is what the
campaigns page is built against: for every element of the page design
(`docs/ui/campaigns-page-mockup.html` in the owner's workspace, read on
2026-10-16), the route that serves it, what to send, what comes back, who may
use it, which rate bucket it costs, its states, and what the page must not
claim. Behaviour in depth: [CAMPAIGNS.md](CAMPAIGNS.md); every field and code:
[API.md](API.md); decisions: ADR-145..ADR-150 in [DECISIONS.md](../DECISIONS.md).
The OpenAPI document (`/openapi.json` where docs are enabled) has every request
and response model, for generating a client.

## Ground rules

- **Base** `/api/v1`, a bearer access token whose workspace (`tid`) is the
  active one. The workspace never comes from a path or a body.
- **Roles.** *member* = any workspace member; *admin* = owner or admin. A member
  calling an admin route gets `403`; another workspace's resource is `404`.
- **Rate buckets.** **C** - campaigns, segments and templates together:
  `RATE_LIMIT_CAMPAIGN_PER_MINUTE`, 30 a minute per workspace. **W** - every
  other workspace route: 300 a minute. **P** - public, per client address. A
  `429` carries `Retry-After`; wait that long, do not retry at once.
- **Errors** are `{error: {code, message, details?, request_id?}}`. Show
  `message` to a person; branch on `code`.
- **Times** are UTC ISO-8601 with an offset; wherever the page shows a time to
  a person there is a `..._local` twin in the workspace's own zone, with its
  offset. Show the local one; never convert in the browser with the browser's
  zone (the viewer may be elsewhere).
- **Money** is an exact decimal string (`"0.2468"`). Never parse it into a float
  for arithmetic.
- **Lists** are keyset-paged: pass `next_cursor` back as `cursor`; stop when it is
  null, not on a short page.
- **Versions.** A draft and a segment carry `version`; an edit sends it back as
  `If-Match: "<version>"` and gets `409 ..._version_conflict` with the current
  one when somebody else edited first. Reload and let the person redo the edit;
  never retry blindly.

## The polling budget

Bucket **C** is 30 requests a minute for the whole workspace - every open tab,
every person - shared with templates. The page's plan:

| What | When | Cost |
| --- | --- | --- |
| `GET /campaigns/progress?ids=` (the visible rows, up to 20) | every `poll_after_seconds` (10 while any is live, 60 otherwise), with `If-None-Match` | 6 a minute at most |
| `GET /campaigns/summary` | on page load and when the tab regains focus | rare |
| `GET /campaigns`, a campaign, its statistics, recipients, activity | on navigation | rare |
| everything else | on a person's action | rare |

Poll only while the page is visible; stop when hidden. On `429`, back off to
`Retry-After` and resume at `poll_after_seconds`. A `304` costs a request too.
There is no WebSocket or Server-Sent Events stream (ADR-149).

## Element by element

### 1. Search by name (PG-01)

| | |
| --- | --- |
| Route | `GET /campaigns?q=&status=&account_id=&limit=&cursor=` |
| Request | `q` at most 100 characters; combine with `status` (repeatable) and `account_id` |
| Response | `{items: [CampaignRead], next_cursor}` |
| Who / bucket | member / C |
| States | empty: "No campaign matches"; `422` for a `q` over 100 |
| Must not claim | a fuzzy or ranked search - it is a literal, case-insensitive "contains"; `%` and `_` are characters |

### 2. Duplicate (PG-02)

| | |
| --- | --- |
| Route | `POST /campaigns/{id}/duplicate` |
| Response | `201 CampaignRead` - a draft "`<name> (copy)`", `audience_ready: false`, the rules in `audience` |
| Who / bucket | admin / C |
| States | `422` with create's codes when the template was withdrawn or the number disabled since (`template_category`, `template_not_synced`, ...) |
| Must not claim | that the copy has recipients, a schedule or results - the page writes its audience again from the copied rules (`POST /campaigns/{copy}/audience` with `audience`'s filters) |

### 3. Edit draft (PG-03)

| | |
| --- | --- |
| Route | `PATCH /campaigns/{id}` with `If-Match: "<version>"` |
| Request | any of `name`, `description`, `account_id`, `template_id`, `variables`, `header_variables`, `button_parameters`, `body_parameters`, `header_parameters`, `header_media_id`, `messages_per_minute` - only those sent change |
| Response | `CampaignRead` with the new `version` (also the `ETag`) and `audience_cleared` |
| Who / bucket | admin / C |
| States | `409 campaign_not_editable` (no longer a draft - hide the button), `409 campaign_version_conflict` (`details.version`), `422 campaign_version_required`, `422` with create's composition codes |
| Must not claim | that the audience survived a change of number, template, values or header file - when `audience_cleared` is true, say so and offer "write the audience again" |

### 4. Delete draft (PG-04)

| | |
| --- | --- |
| Route | `DELETE /campaigns/{id}` -> `204` |
| Who / bucket | admin / C |
| States | `409 campaign_not_deletable` - a scheduled campaign is cancelled first; a campaign anybody was sent is never deletable |
| Must not claim | that the header file is deleted - it stays for retention |

### 5. Send test to myself (PG-09)

| | |
| --- | --- |
| Route | `POST /campaigns/{id}/test-send` `{contact_id}` -> `201 MessageRead` |
| Choosing the contact | the admin's own contact on the campaign's number: the picker (`GET /contacts?account_id=&q=<their number>`) |
| Who / bucket | admin / C |
| States | `422 contact_has_no_conversation` ("Message this WhatsApp number from your phone first, then try again"), `422 contact_opted_out`, `409 campaign_not_testable` (cancelled), `409 portfolio_limit_reached` (`details.available_at`), `422 messaging_limit_unknown`, `429 test_send_limit` (`details.reset_at`; ten a day per workspace) |
| Must not claim | that the test counts in the campaign's statistics or uses the campaign's daily allowance - it does neither; it **does** count in Meta's messaging limit outside the service window |

### 6. Per-contact variables (PER-01 .. PER-05)

| | |
| --- | --- |
| Composing | `body_parameters` / `header_parameters`: one entry per placeholder, `{source, value?, key?, fallback?}`; `source` is `constant` (with `value`), `contact.display_name`, `lead.name`, `lead.interest`, `lead.status`, `lead.budget`, `lead.custom_fields` (with `key`) - every non-constant needs a `fallback` (at most 60 characters) |
| Preview | `POST /campaigns/{id}/preview-messages` `{contact_ids: [<= 5]}` -> `{previews: [{contact_id, header, body, buttons, fallbacks: {body: [i], header: [i]}}]}` |
| Who / bucket | admin / C |
| States | `422 parameters_conflict`, `parameter_value_required`, `parameter_fallback_required`, `parameter_fallback_too_long`, `parameter_key_unknown`, `parameter_source_not_allowed` (a button), `404` for a contact outside the audience |
| Must not claim | a **first name** - the design's `{contact.first_name}` does not exist; the contact has a WhatsApp display name, which may be a full name, a nickname or empty. Label the button "name". Buttons and the header image are the same for everybody. Values are frozen when the audience is written: an edit of a contact afterwards does not change what this campaign sends |

### 7. Saved segments (PG-06)

| | |
| --- | --- |
| Routes | `GET /campaign-segments` (member), `POST` / `PATCH` (`If-Match`) / `DELETE` (admin) |
| Request | `{name, description?, rules: {last_inbound_within_days?, lead_statuses?}, account_id?}` |
| Using one | `segment_id` on `POST /campaigns/audience/preview` and `POST /campaigns/{id}/audience`; other filters sent with it only narrow |
| Bucket | C |
| States | `409 segment_name_taken`, `409 segment_limit_reached` (100), `409 segment_version_conflict`, `422 segment_account_mismatch`, `422` for contact ids |
| Must not claim | that editing a segment updates campaigns that used it - each campaign keeps the rules it copied |

### 8. Quiet hours (TZ-03)

| | |
| --- | --- |
| Route | `GET` (member) / `PUT` (admin) `/workspace/campaign-settings` - `{quiet_hours_enabled, quiet_hours_start, quiet_hours_end, timezone, utc_offset}` |
| Bucket | W |
| States | `422 quiet_hours_empty_window`; seconds refused |
| Must not claim | a per-campaign "respect quiet hours" checkbox - the design shows one, but quiet hours are the **workspace's** setting and bind every campaign. Show it read-only in the composer ("Quiet hours 22:00-09:00 Cairo apply") with a link to the settings |

### 9. Cost estimate (COST-02)

| | |
| --- | --- |
| Route | `GET /campaigns/{id}/cost-estimate` |
| Response | `{label: "estimate", currency, total, per_market, recipients_counted, excluded, rate_card, computed_at, reason, assumptions}` |
| Who / bucket | admin / C |
| States | `total: null, reason: "no_rate_card"` - show "No price list yet", never a number |
| Must not claim | a price: show "≈ up to 2.47 USD, paid to Meta", never "will cost". Meta charges only delivered messages and bills the business directly; Wasla charges nothing for messages. Never compute a price in the page from a constant (the design's `0.0644`) |

### 10. CSV export (PG-14)

| | |
| --- | --- |
| Route | `GET /campaigns/{id}/recipients/export.csv?status=&reason=` - a file download |
| Who / bucket | admin / C |
| States | `429 export_limit` (`details.reset_at`; five an hour per workspace), `403` for a member - hide the button |
| Must not claim | that the export is a live view: it is the list at the moment of the request |

### 11. Activity log (PG-05)

| | |
| --- | --- |
| Route | `GET /campaigns/{id}/activity?cursor=&limit=` |
| Response | `{items: [{id, action, occurred_at, occurred_at_local, actor: {type: "user"|"system", id, name}, metadata}], next_cursor}` |
| Who / bucket | member / C |
| `action` | `created`, `updated` (`metadata.fields`), `duplicated_from`, `duplicated_to` (`copy_id`), `audience_set` (`recipients`, `filters`), `scheduled`, `resumed`, `paused` (`reason`), `cancelled`, `deleted`, `waiting` (`reason`, `resumes_at`), `completed`, `failed` (`reason`), `test_sent`, `recipients_exported` (`rows`) |
| Must not claim | who a test went to, or any contact - the log never carries one |

### 12. Live updates (PG-12)

| | |
| --- | --- |
| Route | `GET /campaigns/progress?ids=<1..20>` with `If-None-Match` |
| Response | `{items: [{id, status, waiting_reason, resumes_at, resumes_at_local, counts: {sent, delivered, read, failed, skipped, pending, replied}, eta, updated_at}], poll_after_seconds}`; `304` when nothing changed |
| Who / bucket | member / C (see the polling budget) |
| States | `422` for more than 20 ids; `404` if one is not this workspace's - drop it and ask again |
| Must not claim | "real time": the design's "Updates live" is a poll every 10 seconds |

### 13. Replied count (PG-10)

| | |
| --- | --- |
| Where | `GET /campaigns/{id}/statistics` -> `replied`, `opted_out_after`; the summary; progress counts |
| Must not claim | that a reply is always to this campaign - it is a message on the campaign's number within 72 hours, credited to the customer's **latest** campaign there. Never estimate it (the design's `read × 0.11`) |

### 14. Reasons breakdown (PG-10)

| | |
| --- | --- |
| Where | `statistics.reasons: [{status, reason_code, provider_code, count}]` - every recipient once; sums to `total` |
| Must not claim | that a **skipped** person "failed": skipped people were never sent (opted out, capped, deadline passed...); failed ones were attempted |

### 15. ETA (PG-13)

| | |
| --- | --- |
| Where | `CampaignRead.eta` and progress: `{estimated_completion_at, estimated_completion_at_local, basis: [rate, quiet_hours, daily_limit, contact_cap, deadline], confidence, reason}` |
| States | null time with `reason` (`draft`, `paused`, `failed`, `cancelled`, `completed`, `limit_unknown`) - show nothing, not "0 min" |
| Must not claim | a guaranteed time; show "about ... (estimate)". Never compute it in the page from `pending / rate` |

### 16. Daily limit counter (LIM-07)

| | |
| --- | --- |
| Route | `GET /whatsapp/accounts/{account_id}/messaging-limit` |
| Response | `{tier, known, messaging_limit, checked_at, source, campaign_share, campaign_budget, used_last_24h, campaign_available_now, next_capacity_at, next_capacity_at_local, shared_with_other_numbers, shared_with_other_workspaces}` |
| Who / bucket | member / W |
| States | `known: false` - "Meta's limit for this number has not been read yet; campaigns on it wait"; `messaging_limit: null` with `known: true` - unlimited |
| Must not claim | that the count is this workspace's alone when `shared_with_other_workspaces` is true, nor whose the rest is - it is the whole business portfolio's |

### 17. Spread over several days (LIM-04 .. LIM-06)

| | |
| --- | --- |
| Where | automatic; `delivery_plan` on the schedule response and `GET /campaigns/{id}/delivery-plan` (admin): `{days: [{local_date, first_send_at_local, last_send_at_local, recipients}], estimated_completion_at, notices}`; `send_until` on schedule |
| Notices | `starts_after_quiet_hours`, `spreads_over_days`, `limit_shared_with_other_numbers`, `limit_unknown`, `contact_cap_defers`, `plan_exceeds_ttl`, `deadline_leaves_recipients` |
| Must not claim | that people beyond the first day were refused - they are waiting; show "Day 2: about 200 people" |

### 18. Contact picker (PG-07)

| | |
| --- | --- |
| Route | `GET /contacts?q=&account_id=&eligible_for_campaign=&limit=<=50&cursor=` |
| Response | `{items: [{id, display_name, wa_id, lead, last_inbound_at, opted_out_at, eligible_for_campaign, ineligible_reason}], next_cursor}` |
| Who / bucket | member / W |
| States | `422 account_required` for `eligible_for_campaign` without a number |
| Must not claim | that a picked person will be sent - the audience step still applies the base population (opted-out people are excluded) |

### 19. Four-step composer (PG-08)

1. `POST /campaigns` with an `Idempotency-Key` the page generates once per
   composer session (a retry returns the same draft; another body with that key
   is `409 idempotency_key_reused`).
2. `POST /campaigns/{id}/audience` (filters or `segment_id`) - or, before it,
   `POST /campaigns/audience/preview` to show the count.
3. `GET /campaigns/{id}/delivery-plan` and `GET /campaigns/{id}/cost-estimate`;
   `POST .../preview-messages` and `POST .../test-send` as the person wishes.
4. `POST /campaigns/{id}/schedule` `{scheduled_at?, send_until?}` - with offsets.

A draft whose audience step failed stays a draft with `audience_ready: false`;
list it with "Continue" and "Delete". Steps 2-4 are admin, bucket C.

### 20. KPI cards (PG-11)

| | |
| --- | --- |
| Route | `GET /campaigns/summary?from=&to=&account_id=` (member, C) |
| Response | `{campaigns_by_status, recipients: {sent, delivered, read, failed, skipped, held, replied, opted_out_after, pending}, rates: {delivery, read, reply}, window: {from, to, from_local, to_local, timezone}}` |
| Mapping | "Sending now" = `campaigns_by_status.running`, "Scheduled" = `scheduled`, "Need attention" = `paused` + `failed`, "Messages sent (30 days)" = `recipients.sent` |
| Must not claim | more than the window says: every count is over campaigns **created** in `[from, to)` (default: the last 30 days in the workspace's zone). For "Sending now" across all time, ask with an early `from` |

### 21. Time zone at onboarding (TZ-01, TZ-02)

| | |
| --- | --- |
| Routes | `GET /timezones?q=` (public, P) - `{tzdata_version, suggested, timezones: [{id, label, utc_offset, offset_minutes, region}]}`, cacheable (`ETag`); `POST /auth/register` and `POST /workspaces` require `timezone`; `PATCH /workspace` changes it (admin) |
| States | `422 timezone_unknown` |
| Must not claim | that changing the zone moves scheduled campaigns - a `scheduled_at` is an instant; only where quiet hours fall moves |

### The design's existing elements

| Element | Route |
| --- | --- |
| Campaign list, status filter | `GET /campaigns?status=` (member, C) |
| One campaign, Setup panel | `GET /campaigns/{id}` |
| Delivery funnel (Audience, Sent, Delivered, Read) | `GET /campaigns/{id}/statistics` - `total`, `sent`, `delivered`, `read` |
| Recipients table and its status filter | `GET /campaigns/{id}/recipients?status=&reason=&cursor=` |
| Send / Pause / Resume / Cancel | `POST .../schedule`, `.../pause`, `.../schedule` again, `.../cancel` (admin) |
| Template picker | `GET /templates` (approved marketing templates only can be composed) |
| Header image upload | `POST /campaigns/header-media` (multipart) |
| Message preview | `POST .../preview-messages` |

## Waiting and stop reasons, as the page should say them

| Code | English | Egyptian Arabic |
| --- | --- | --- |
| `waiting_reason: quiet_hours` | Paused for quiet hours - continues at {resumes_at_local} | واقفة في ساعات الهدوء - هتكمل الساعة {resumes_at_local} |
| `waiting_reason: daily_limit` | Reached today's WhatsApp sending limit - continues at {resumes_at_local} | وصلت لحد واتساب النهارده - هتكمل {resumes_at_local} |
| `waiting_reason: contact_cap` | The rest already got a marketing message in the last 24 hours - continues at {resumes_at_local} | الباقيين وصلتهم رسالة تسويقية في آخر ٢٤ ساعة - هتكمل {resumes_at_local} |
| `waiting_reason: limit_unknown` | Waiting for WhatsApp to tell us this number's limit | مستنيين واتساب يقولنا حد الرقم ده |
| `admin_paused` | Paused by {actor} | اتوقفت بواسطة {actor} |
| `workspace_suspended` | Paused: the workspace is suspended | واقفة: مساحة العمل موقوفة |
| `workspace_deleted` | Stopped: the workspace was deleted | اتوقفت: مساحة العمل اتمسحت |
| `subscription_suspended` | Paused: the subscription is unpaid | واقفة: الاشتراك مش مدفوع |
| `template_removed` / `template_not_approved` / `template_withdrawn` | Stopped: WhatsApp no longer allows this template | اتوقفت: واتساب مبقاش سامح بالقالب ده |
| `template_category` | Stopped: the template is no longer a marketing template | اتوقفت: القالب مبقاش تسويقي |
| `template_parameters_refused` | Stopped: WhatsApp refused the message's values | اتوقفت: واتساب رفض قيم الرسالة |
| `connection_disabled` | Stopped: the WhatsApp number is turned off | اتوقفت: رقم الواتساب مقفول |
| `credential_refused` / `connection_refused` | Stopped: WhatsApp refused the number's access - reconnect it | اتوقفت: واتساب رفض صلاحية الرقم - اربطه تاني |
| `provider_unreachable` | Paused: WhatsApp could not be reached | واقفة: مقدرناش نوصل لواتساب |
| `repeated_batch_failure` | Paused after repeated errors | واقفة بعد أخطاء متكررة |
| `header_media_unavailable` | Paused: the header image is missing - upload it again | واقفة: صورة الرأس مش موجودة - ارفعها تاني |
| `header_media_refused` | Stopped: WhatsApp refused the header file | اتوقفت: واتساب رفض ملف الرأس |
| `meta_quality_review` | Paused: WhatsApp is reviewing this template's quality | واقفة: واتساب بيراجع جودة القالب |

Recipient reasons (`reason_code`) for the "Why recipients were not reached"
card: `opted_out` (asked not to receive marketing), `contact_gone`,
`other_connection`, `identity_changed`, `channel_not_in_plan`,
`recipient_opted_out_at_provider` (stopped marketing in WhatsApp),
`marketing_limit_per_user` (WhatsApp's own limit for that person),
`undeliverable`, `delivery_unconfirmed` (may have arrived - never repeated),
`attempts_exhausted`, `template_parameters_refused`, `send_window_closed` (the
deadline passed), `contact_frequency_cap` (already had a marketing message that
day), and `unrecorded`.

## Error codes a campaign screen can meet

| Status | Code | When |
| --- | --- | --- |
| 402 | `plan_limit_exceeded`, `channel_not_in_plan` | Scheduling past the plan's campaign allowance, or a channel the plan lacks |
| 403 | `permission_denied`, `marketing_template_admin_only` | A member on an admin route |
| 404 | `not_found` | Another workspace's resource, or none |
| 409 | `campaign_not_editable`, `campaign_not_deletable`, `campaign_version_conflict`, `idempotency_key_reused`, `campaign_not_testable`, `portfolio_limit_reached`, `contact_frequency_cap`, `segment_name_taken`, `segment_limit_reached`, `segment_version_conflict` | See the elements above |
| 422 | `scheduled_at_needs_offset`, `send_until_needs_offset`, `send_until_before_start`, `messaging_limit_unknown`, `template_parameters`, `template_not_synced`, `template_unsupported`, `template_category`, `header_media_type`, `header_media_too_large`, `header_media_not_stored`, `parameters_conflict`, `parameter_value_required`, `parameter_fallback_required`, `parameter_fallback_too_long`, `parameter_key_unknown`, `parameter_source_not_allowed`, `campaign_version_required`, `segment_version_required`, `segment_account_mismatch`, `contact_has_no_conversation`, `contact_opted_out`, `recipient_opted_out`, `account_required`, `summary_window_needs_offset`, `timezone_unknown`, `quiet_hours_empty_window`, `validation_error` | See [API.md](API.md) |
| 429 | `rate_limited`, `test_send_limit`, `export_limit` | Bucket C exhausted, or the daily tests / hourly exports |

## What the page must never claim

- **A guaranteed send time.** ETAs and plans are estimates; quiet hours, Meta's
  limit and the per-customer cap move them.
- **A guaranteed price.** The cost is an upper-bound estimate of what Meta may
  bill the business; Wasla charges nothing for messages.
- **That "Read" means the person read it.** It means WhatsApp reported a read
  receipt, which people can turn off.
- **That a reply is always to this campaign.** It is credited to the customer's
  latest campaign on that number within 72 hours.
- **That a skipped person "failed".** Skipped people were never sent.
- **That a person was reached because they were picked or are in a segment.**
  Opt-outs, the cap and the deadline still apply at send time.
- **A first name** for `contact.display_name`.
- **A per-campaign quiet-hours switch.** Quiet hours are the workspace's.
- **Live updates.** The page polls.
