"""WhatsApp's webhook signature: Meta's, shared by every Meta product.

Kept under this name for the callers written against it. The implementation is
`app.integrations.meta.signature` (OMNI-010).
"""

from __future__ import annotations

from app.integrations.meta.signature import (
    SIGNATURE_HEADER,
    SIGNATURE_PREFIX,
    compute_signature,
    verify_signature,
)

__all__ = ["SIGNATURE_HEADER", "SIGNATURE_PREFIX", "compute_signature", "verify_signature"]
