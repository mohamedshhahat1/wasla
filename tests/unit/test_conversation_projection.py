"""The projection's lookup tables and the status ordering rule."""

from app.db.models.conversation import MessageKind, MessageStatus
from app.repositories.conversation_repository import _STATUS_ORDER
from app.services.conversation_service import DELIVERY_STATUSES, MESSAGE_KINDS

# The four statuses Meta reports for a message a business sent.
META_STATUSES = ("sent", "delivered", "read", "failed")


def test_every_meta_status_is_mapped() -> None:
    assert set(DELIVERY_STATUSES) == set(META_STATUSES)


def test_delivery_statuses_map_onto_real_message_statuses() -> None:
    for status in DELIVERY_STATUSES.values():
        assert isinstance(status, MessageStatus)


def test_message_kinds_map_onto_real_kinds() -> None:
    for kind in MESSAGE_KINDS.values():
        assert isinstance(kind, MessageKind)


def test_unsupported_is_never_a_mapping_target() -> None:
    """UNSUPPORTED is the fallback, so mapping to it deliberately hides it."""
    assert MessageKind.UNSUPPORTED not in MESSAGE_KINDS.values()


def test_template_is_never_a_mapping_target() -> None:
    """Templates are outbound only.

    A customer cannot send one, so an inbound payload mapping to TEMPLATE would
    mean Wasla had misread the traffic.
    """
    assert MessageKind.TEMPLATE not in MESSAGE_KINDS.values()


def test_voice_and_audio_share_a_kind() -> None:
    """Meta sends both; they are the same thing to us."""
    assert MESSAGE_KINDS["voice"] is MESSAGE_KINDS["audio"]


def test_button_replies_are_interactive() -> None:
    assert MESSAGE_KINDS["button"] is MessageKind.INTERACTIVE
    assert MESSAGE_KINDS["interactive"] is MessageKind.INTERACTIVE


def test_every_status_has_an_order() -> None:
    for status in MessageStatus:
        assert status in _STATUS_ORDER


def test_outbound_progression_is_strictly_increasing() -> None:
    progression = (
        MessageStatus.SENT,
        MessageStatus.DELIVERED,
        MessageStatus.READ,
        MessageStatus.FAILED,
    )
    ranks = [_STATUS_ORDER[status] for status in progression]
    assert ranks == sorted(set(ranks))


def test_arrival_states_share_the_floor() -> None:
    """Nothing may look like an advance over a message's starting state."""
    assert _STATUS_ORDER[MessageStatus.RECEIVED] == _STATUS_ORDER[MessageStatus.PENDING]
    assert _STATUS_ORDER[MessageStatus.PENDING] < _STATUS_ORDER[MessageStatus.SENT]
