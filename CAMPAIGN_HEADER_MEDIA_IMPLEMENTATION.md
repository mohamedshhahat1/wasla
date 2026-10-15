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
| 2 | Asset route through the write path | not started | | |
| 3 | Composition and preview | not started | | |
| 4 | Worker: upload once under the lease, header, refresh | not started | | |
| 5 | Every failure campaign-level | not started | | |
| 6 | Retention and lifecycle | not started | | |
| 7 | Observability, invariants, docs, ADR | not started | | |
| 8 | Mutation | not started | | |
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
