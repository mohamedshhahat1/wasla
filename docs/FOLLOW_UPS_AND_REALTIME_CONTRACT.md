# Follow-ups, header media and the realtime inbox - the API contract

**Status: the backend is implemented; no frontend exists yet.** This is what the
follow-up settings card, the conversation's follow-up panel, the inbox's live
updates, the template header library, the inbox's template composer, the
campaign composer's per-recipient files and the date-time picker are built
against. Behaviour in depth: [CRM.md](CRM.md) "Follow-ups",
[CAMPAIGNS.md](CAMPAIGNS.md), [MEDIA.md](MEDIA.md); every field and code:
[API.md](API.md); decisions: ADR-151..ADR-156 in [DECISIONS.md](../DECISIONS.md).
The campaigns page's own contract is [CAMPAIGNS_PAGE_CONTRACT.md](CAMPAIGNS_PAGE_CONTRACT.md),
whose ground rules (base path, roles, buckets, error shape, `_local` times)
apply here unchanged.

Buckets: **W** - the workspace's 300 a minute; **C** - campaigns, segments and
templates, 30 a minute; **S** - the event stream, bounded by open streams
instead (5 a person, 100 a workspace, `429 stream_limit`).

## 1. The follow-up settings card ("إعدادات المتابعة")

| | |
| --- | --- |
| Read | `GET /workspace/follow-up-settings` (member, W) |
| Response | `{enabled, channel_types, available_channel_types, steps: [{delay_minutes, guidance}], summary: {enabled, steps_count}, version, updated_at, updated_by}` |
| Save | `PUT /workspace/follow-up-settings` (owner or admin, W), header `If-Match: <version>`, body `{enabled, channel_types, steps: [{delay_minutes, guidance?}]}` - the whole card every time |
| Toggle "تفعيل المتابعة" | `enabled` |
| Platform chips | `channel_types`, a subset of `available_channel_types` (today `["whatsapp"]`); grey out every chip not in `available_channel_types` - Facebook and Instagram until Wasla has their adapters |
| Reminders "التذكيرات" | `steps`, in order. "بعد [n] دقائق" sends `delay_minutes: n`; "بعد [n] ساعات" sends `n × 60`. 5 minutes to 23 hours (1,380). "+ إضافة تذكير" is disabled at 5 rows |
| Header line | `summary`: "مفعّل · خطوة واحدة" = `{enabled: true, steps_count: 1}`; "متوقف" when `enabled` is false |
| A new workspace | opens `enabled: true`, `["whatsapp"]`, one step of 5 minutes, `version` 1 |
| `version: 0` | the workspace has no settings: show the card off and save it to create them |
| Errors | `422 follow_up_steps_required` (on with no reminder), `follow_up_channels_required`, `channel_type_unavailable`, `follow_up_delay_out_of_range`, `follow_up_too_many_steps`, `follow_up_guidance_too_long`; `422 follow_up_settings_version_required` (no `If-Match`); `409 follow_up_settings_changed` (someone saved first: reload, then re-apply); `403` for a member |
| Effects to state | turning off, removing a platform or deleting a reminder stops the follow-ups waiting on it at once; editing a delay changes the next ones, not one already waiting |
| Must not claim | that a reminder will go at exactly that minute (the worker sweeps every 30 s, and the AI may decide not to send); that reminders reach a customer whose 24-hour window will have closed - they never do |

## 2. A conversation's follow-ups, and "stop follow-ups"

| | |
| --- | --- |
| State | `GET /conversations/{id}` → `follow_up: {state: none \| scheduled \| ended, step_index, steps_total, due_at, due_at_local, ended_reason, ended_at}` (null in the list) |
| History | `GET /conversations/{id}/follow-ups` (member, W, cursor-paged, latest due first): `{id, origin: one_off \| sequence, step_index, status, requested_at, scheduled_at, scheduled_at_local, sent_at, cancelled_at, cancelled_reason, skip_reason, deferred_reason, text, message_id, created_by_kind}` - `text` only for a sent one |
| Stop | `DELETE /conversations/{id}/follow-up` (any member who may reply, W) → the state; ends the run `stopped_by_person`, audited; with nothing waiting it changes nothing |
| Shown | "Reminder 1 of 2 at 03:05" from `step_index`, `steps_total`, `due_at_local`; a waiting template follow-up with `deferred_reason` shows why it waits and keeps `requested_at` as what was asked |
| Must not claim | the text of a reminder before it is sent (the AI writes it when it is due, and may skip); the model's reasoning for a skip (never stored) |

## 3. The inbox's live updates

| Step | |
| --- | --- |
| 1. Ticket | `POST /events/ticket` (member, W) → `201 {ticket, expires_at}`. Single use, 60 s; ask right before opening. Never log it or put it in analytics |
| 2. Open | `new EventSource("/api/v1/events/stream?ticket=" + ticket)` (S). `401` → ask a new ticket. `429 stream_limit` → five tabs already have one: poll instead |
| 3. Listen | events below; `id:` on each; `: keep-alive` every 15 s |
| 4. On an event | fetch what it names through the REST route (`GET /conversations/{id}`, `GET /conversations/{id}/messages?after=<last id held>`, `GET /campaigns/progress?ids=`); never render from the event |
| 5. Reconnect | the browser reconnects itself (`retry: 3000`) and sends `Last-Event-ID`; a new ticket is needed - on `error`, close the EventSource, ask a ticket, open again with `?ticket=...&lastEventId=<last id seen>` |
| 6. `resync` | the stream could not replay everything since your id: re-read with `GET /conversations?updated_since=<when you last were complete>` and replace what you hold |
| 7. `closed` | `{reason}` then the response ends. `token_changed` → sign in again; `membership_revoked` / `workspace_unavailable` → leave the workspace's views; `workspace_switched` → the person opened another workspace: stop this one's inbox; `server_restart` → reconnect after a short random delay |
| Fallback | without a stream (or while reconnecting), poll `GET /conversations?updated_since=` with `If-None-Match`, every `poll_after_seconds` (30; 60 while a stream is open) |

| Event | Data | Fetch |
| --- | --- | --- |
| `message.created` | `{conversation_id, message_id, direction, channel_type, created_at}` | the conversation and `messages?after=` |
| `message.status` | `{conversation_id, message_id, status}` | the message's status in the thread |
| `conversation.updated` | `{conversation_id, fields: [status \| owner \| assignee \| priority]}` | `GET /conversations/{id}` |
| `follow_up.updated` | `{conversation_id, state}` | `GET /conversations/{id}` (its `follow_up`) |
| `campaign.changed` | `{campaign_id}` | `GET /campaigns/progress?ids=` |

Must not claim: that an event means this person may read its content (fetch
it; the REST route authorizes); that the inbox is complete after `resync`
until it has re-read; that events arrive in a guaranteed order across
conversations.

## 4. The template header library

| | |
| --- | --- |
| Upload | `POST /template-header-media` (owner or admin, W), multipart `file` → `{id, kind: image | video | document, mime_type, size_bytes, filename, created_at}\| video \| document, mime_type, byte_size, filename, created_at}`; the bytes decide the kind; GIF and oversize files are refused as for a campaign header |
| List | `GET /template-header-media?kind=&cursor=&limit=` (member, W) |
| Delete | `DELETE /template-header-media/{id}` (owner or admin, W); `409 header_media_in_use` while a live campaign, a recipient of one or a waiting follow-up names it |
| Must not claim | that a deleted file can be recovered; any Meta media id (Wasla's id is all the API shows) |

## 5. A person's template with a media header (the inbox)

| | |
| --- | --- |
| Route | `POST /conversations/{id}/messages/template` (member, W) with `header_media_id` (a library file of the template header's kind) and the body / button parameters |
| Errors | `422 header_media_required` (an IMAGE / VIDEO / DOCUMENT header and no file), `header_media_kind_mismatch`, `header_media_must_be_uploaded` (a raw `image.id` / `link` in components is no longer accepted); `404` another workspace's file; `409 portfolio_limit_reached` with `details: {available_at, available_at_local}` when the business portfolio is at Meta's limit |
| Notice | `notices: ["messaging_limit_unknown"]` on a 2xx sent while Wasla has not read the number's limit |
| Must not claim | that a refused template will go later by itself - the person decides; say when (`available_at_local`) |

A person's one-off **template follow-up** takes the same `header_media_id`
(`POST /follow-ups`), checked when it is scheduled and uploaded when it goes.

## 6. Campaign composer: a file per recipient

| | |
| --- | --- |
| Fields | `header_media_mode: campaign \| per_recipient`; `header_media_id` (the default file, required); `header_media_rules: [{when: {field, key?, equals}, header_media_id}]` - first match wins, `field` a PER-01 source (`contact.display_name`, `lead.*`) |
| Limits | at most 20 distinct files (`422 header_media_too_many_files`); every file of this workspace (`404`) and of the template header's kind (`422 header_media_kind_mismatch`) |
| Preview | `POST /campaigns/{id}/preview-messages` → each person's `header_media_id` |
| Must not claim | that a person's file follows later changes to their lead: it is chosen when the audience is written |

## 7. Picking a date and time in the workspace's zone

`GET /workspace/local-time?at=YYYY-MM-DDTHH:MM` (member, W) →
`{at_local, at_utc, timezone, offset, ambiguous, nonexistent, alternatives}`.
Send the returned `at_local` to the route that wants an aware time (a campaign's
schedule, a follow-up's `scheduled_at`). In the hour clocks go back,
`ambiguous: true`: offer both `alternatives` (the first is chosen by default).
In the hour clocks skip, `nonexistent: true`: `at_local` is the first valid
instant after it - say so. An offset or seconds in `at` is `422
local_time_invalid`. Never compute an offset in the browser.

## The sentences to show

| Code | English | العربية |
| --- | --- | --- |
| `scheduled` | Reminder {n} of {total} at {time} | التذكير {n} من {total} الساعة {time} |
| `completed` | All reminders were sent | اتبعتت كل التذكيرات |
| `customer_replied` | The customer replied - reminders stopped | العميل رد — المتابعة وقفت |
| `human_took_over` | A colleague took the conversation - reminders stopped | زميل استلم المحادثة — المتابعة وقفت |
| `conversation_closed` | The conversation was closed | المحادثة اتقفلت |
| `opted_out` | The customer asked not to be messaged | العميل طلب ميتبعتلوش رسايل |
| `lead_closed` | The lead was won or lost | العميل اتقفل (اتكسب أو اتخسر) |
| `purchased` | The AI judged the customer has bought or will buy | الذكاء الاصطناعي شاف إن العميل اشترى أو هيشتري |
| `ai_skipped` / `concluded` / `declined` / `waiting_on_business` / `inappropriate` / `other` | The AI decided a reminder would not help | الذكاء الاصطناعي شاف إن التذكير مش هيفيد |
| `window_closing` | The next reminder would fall after WhatsApp's 24 hours - it was not scheduled | المتابعة دي هتبقى بعد 24 ساعة من آخر رسالة للعميل — واتساب مش بيسمح |
| `window_closed` | WhatsApp's 24 hours had nearly passed - not sent | الـ 24 ساعة بتاعة واتساب كانت قربت تخلص — متبعتتش |
| `ai_unavailable` | The AI could not write the reminder | الذكاء الاصطناعي مقدرش يكتب التذكير |
| `guardrail` | The AI's text was not safe to send | الرسالة اللي كتبها الذكاء الاصطناعي مكانتش مناسبة تتبعت |
| `allowance_exhausted` | The workspace's AI allowance is used up | رصيد الذكاء الاصطناعي خلص |
| `not_served` | The workspace is suspended | مساحة العمل موقوفة |
| `settings_changed` | The follow-up settings changed | إعدادات المتابعة اتغيرت |
| `sequence_restarted` | Started again after a new reply | بدأت من الأول بعد رد جديد |
| `replaced_by_person` | Replaced by a colleague's follow-up | اتبدلت بمتابعة من زميل |
| `stopped_by_person` | Stopped by a colleague | زميل وقفها |
| `channel_unavailable` | This platform cannot be followed up | المنصة دي مينفعش يتعمل عليها متابعة |
| `failed` | Sending failed | الإرسال فشل |
| `deferred_reason: quiet_hours` | Waiting for quiet hours to end | مستني وقت الهدوء يخلص |
| `deferred_reason: portfolio_limit` | Waiting for WhatsApp's daily limit to free up | مستني حد واتساب اليومي يفضى |

## What the pages must never claim

- That a follow-up will certainly be sent at the minute shown.
- That the AI's text is known before it is sent.
- That a stream event means its content is readable by this person - fetch it.
- That the inbox is complete without a re-read after `resync`.
- That reminders can reach Facebook or Instagram customers - not until Wasla has those adapters.
