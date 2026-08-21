"""The projection's lookup tables and the status ordering rule."""

from app.db.models.conversation import MessageKind, MessageStatus
from app.repositories.conversation_repository import _STATUS_ORDER
from app.services.conversation_service import DELIVERY_STATUSES, MESSAGE_KINDS

# The four statuses Meta reports for a message a business sent.
META_STATUSES = ("sent", "delivered", "read", "failed")


def test_every_meta_status_is_mapped():
    assert set(DELIVERY_STATUSES) == set(META_STATUSES)


def test_delivery_statuses_map_onto_real_message_statuses():
    for status in DELIVERY_STATUSES.values():
        assert isinstance(status, MessageStatus)


def test_message_kinds_map_onto_real_kinds():
    for kind in MESSAGE_KINDS.values():
        assert isinstance(kind, MessageKind)


def test_unsupported_is_never_a_mapping_target():
    """UNSUPPORTED is the fallback, so mapping to it deliberately hides it."""
    assert MessageKind.UNSUPPORTED not in MESSAGE_KINDS.values()


def test_voice_and_audio_share_a_kind():
    """Meta sends both; they are the same thing to us."""
    assert MESSAGE_KINDS["voice"] is MESSAGE_KINDS["audio"]


def test_button_replies_are_interactive():
    assert MESSAGE_KINDS["button"] is MessageKind.INTERACTIVE
    assert MESSAGE_KINDS["interactive"] is MessageKind.INTERACTIVE


def test_every_status_has_an_order():
    for status in MessageStatus:
        assert status in _STATUS_ORDER


def test_outbound_progression_is_strictly_increasing():
    progression = (
        MessageStatus.SENT,
        MessageStatus.DELIVERED,
        MessageStatus.READ,
        MessageStatus.FAILED,
    )
    ranks = [_STATUS_ORDER[status] for status in progression]
    assert ranks == sorted(set(ranks))


def test_arrival_states_share_the_floor():
    """Nothing may look like an advance over a message's starting state."""
    assert _STATUS_ORDER[MessageStatus.RECEIVED] == _STATUS_ORDER[MessageStatus.PENDING]
    assert _STATUS_ORDER[MessageStatus.PENDING] < _STATUS_ORDER[MessageStatus.SENT]
