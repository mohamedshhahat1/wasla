# Campaign Header Media — Implementation (DRAFT)

Branch `campaigns-header-media-20261015` at `E:\wasla-campaign-media`, base
`427a0f1` (campaigns remediation head). This draft is replaced by the final
report in Phase 10. A resumed session reads the table below and `git log`, and
continues from the first phase that is not committed.

## Phase status

| Phase | What | State | Commit | Lanes run |
| --- | --- | --- | --- | --- |
| 0 | Baseline at 427a0f1: media headers refused | committed | (this commit) | P07 before; `test_a_media_header_is_refused_at_composition` 1 passed |
| 1 | Migration 0107, models, vocabulary, purge ledger | committed | (this commit) | unit 3,411 passed / 1 skipped; parity + atomicity + purge + invariants (migration-built) 39 passed; docs truth + purge + templates (model-built) 50 passed; alembic fresh/round-trip/check clean |
| 2 | Asset route through the write path | committed | (this commit) | header-media API 148 passed (local + MinIO); campaign/media/conversation/entitlement/purge suites 607 passed + 6 stub failures fixed (route stub lacked `header_media_for`); docs truth + route authorization 34 passed |
| 3 | Composition and preview | committed | (this commit, with Phase 2) | as Phase 2; `test_campaign_templates.py` 82 with docs |
| 4 | Worker: upload once under the lease, header, refresh | committed | (this commit) | `test_campaign_header_media.py` 23 passed model-built; with the API, templates and docs 106 passed migration-built; campaign/outbound/messaging/unit regression 3,680 passed after 3 guard tests acknowledged |
| 5 | Every failure campaign-level | committed | (this commit, with Phase 4) | as Phase 4; client upload classes 14 passed |
| 6 | Retention and lifecycle | committed | (this commit) | lifecycle 11 passed; media/retention/upload/purge suites 294 passed |
| 7 | Observability, invariants, docs, ADR | committed | (this commit) | campaign invariants + oracles 15 passed (migration-built); promtool 75 rules + unit tests SUCCESS, amtool valid; docs truth + unit 3,455 passed |
| 8 | Mutation | committed | 50a9df3 (+ this record) | control 118 passed; M-H01..M-H14: 13 KILLED, M-H02 REDUNDANT GUARD (M-H02+LOCK KILLED, LOCK-alone survives); regression M-C01+M08, M-C12..15, M-C18, M-C24 KILLED; worktree clean |
| 9 | Full lanes, CI jobs, after-probes | not started | | |
| 10 | Final report | not started | | |

## Phase 0 — the "before" (427a0f1, migration-built `wasla_cmh_probe`)

```text
P07 IMAGE header + URL button   refused: ValidationError template_unsupported
                                "This template's header is image; campaigns cannot send media headers yet."
P07 TEXT header payload         header [Ahmed], body [Ahmed], button url index 0 [spring-20]
refusal() by header format      IMAGE / VIDEO / DOCUMENT / LOCATION -> template_unsupported
                                GIF -> None   (found here: a GIF header, which Meta documents for
                                the Marketing Messages API, was NOT refused and would have been
                                sent with no header; closed in Phase 3)
```

## Meta's documentation read before code (2026-10-15)

| Value | Meta | This prompt | Used |
| --- | --- | --- | --- |
| Upload | `POST /{phone-number-id}/media`, `file`, `type`, `messaging_product=whatsapp` | same | same |
| Image | JPEG, PNG, 5 MB, 8-bit RGB/RGBA | JPEG, PNG, 5 MB | same |
| Video | MP4 **and 3GPP**, 16 MB, H.264 + AAC only | MP4, 16 MB | **MP4 and 3GPP** (Meta wins) |
| Document | text, Excel, Word, PowerPoint, PDF, 100 MB | PDF, 16 MB | PDF, 16 MB (`MAX_UPLOAD_BYTES`) |
| Media id lifetime | "Media IDs returned by the API expire after 30 days" | ~30 days | 30 days, refresh at 25 |
| Reuse across messages | not stated on any page read | assumed | assumed; EXTERNAL VERIFICATION |
| Unknown / expired id on a send | no code documented | — | 131053 "Unable to upload the media used in the message" (the only media-in-message code) |
| Header formats | IMAGE, VIDEO, **GIF**, DOCUMENT (+ LOCATION) | IMAGE, VIDEO, DOCUMENT, LOCATION | GIF refused like LOCATION |

Pages: developers.facebook.com/documentation/business-messaging/whatsapp/business-phone-numbers/media.md,
.../templates/template-media.md, .../templates/components.md,
.../messages/document-messages.md, developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes.

## Phase 2-3 notes

* **Found by the tests:** the request-size middleware held `POST /campaigns/header-media`
  to the authenticated default cap, so a deployment would have answered 413 for any
  header file above it. Added to `UPLOAD_ALLOWANCES` (`app/core/limits.py`) with the
  attachment route's cap.
* `read_capped` moved from `conversations.py` to `app/api/uploads.py`; both upload routes
  use it. `MessagingService._store` and the header upload share `storage.write_object`.
* Changed test: `test_a_media_header_is_refused_at_composition` became
  `test_a_media_header_without_its_file_is_refused_at_composition` (IMAGE without a file
  is now `template_parameters`; LOCATION stays `template_unsupported`).
* No campaign route changes fields after create (named transitions only), so
  "header_media_id cannot change after scheduling" is structural: `PATCH` is 405 and the
  audience/schedule bodies refuse an unknown `header_media_id` with 422.

## Phase 4-5 notes

* `WhatsAppClient.upload_media` now classifies every ending (unreachable, throttled,
  credential, connection, refused-with-code, uncertain); all remain `ExternalServiceError`
  subclasses, so the attachment send is unchanged. `MessagingService.upload_header_file`
  reuses the adapter's sender and `prepare` - the one provider upload path.
* **Found while testing:** holding the template row for the whole batch kept it in the
  session's identity map, and the per-send registry check then read a stale status
  (`test_a_template_paused_elsewhere_mid_batch...` failed, 6 sends instead of 2). The
  pre-existing code relied on the row being dropped between reads; the header check now
  reads the template's format without keeping the object.
* Guard tests acknowledged (not weakened): ordering tie-breakers on the three new
  queries; 131053 added to the Meta-code allowlist; the multipart `file` field of the
  new route added to the bounded-field exemptions, as the attachment route's is.
