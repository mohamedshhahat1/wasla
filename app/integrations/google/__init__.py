"""Google as an identity provider.

Two pieces, deliberately separate. `oidc` turns a string that claims to come
from Google into claims that provably did, and knows nothing about Wasla's
users. `client` performs the authorization-code exchange. Neither knows what a
session is; that decision belongs to the service layer.
"""

from app.integrations.google.oidc import (
    GOOGLE_ISSUERS,
    GOOGLE_JWKS_URL,
    ID_TOKEN_ALGORITHM,
    GoogleIdentityClaims,
    GoogleIdTokenVerifier,
    GoogleKeyRing,
    GoogleKeysUnavailableError,
    GoogleTokenInvalidError,
)

__all__ = [
    "GOOGLE_ISSUERS",
    "GOOGLE_JWKS_URL",
    "ID_TOKEN_ALGORITHM",
    "GoogleIdTokenVerifier",
    "GoogleIdentityClaims",
    "GoogleKeyRing",
    "GoogleKeysUnavailableError",
    "GoogleTokenInvalidError",
]
