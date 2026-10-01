"""The channel-neutral contracts every messaging adapter speaks (ADR-117 to ADR-121).

Nothing in this package imports a provider. An adapter - WhatsApp's today -
translates its provider's webhooks into `InboundEvent`s, answers policy
questions through a `ChannelPolicy`, and sends through the same delivery
protocol every channel shares. The business layer above (ingestion, the inbox,
AI turns, CRM, follow-ups, analytics) reads only these types.
"""
