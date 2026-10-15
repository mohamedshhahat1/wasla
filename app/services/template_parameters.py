"""What a template needs at send time, and the components a send carries (CMP-05, ADR-137).

A campaign checked only the body's placeholder count and sent a body component
alone. A template whose header or buttons need a parameter was then refused by
Meta for every recipient - 132000, 132012 or 131008 - one at a time, three
attempts each, and the campaign ended `completed` with everyone `failed` and no
reason (CAMPAIGNS_OUTBOUND_MESSAGING_AUDIT.md section 10, P06/P07).

**The spec is derived from the components the registry already stores**, never
kept as a second copy: a sync writes Meta's component list unedited
(`WhatsAppTemplate.components`), and this reads it. A row with no components
was never synced with them; its needs are unknown and a campaign on it is
refused until the next sync, rather than guessed at.

Shapes follow Meta's documentation, read 2026-10-13:

- the stored components - "Message templates" / "Components"
  (developers.facebook.com/docs/whatsapp/business-management-api/message-templates/components):
  `HEADER` with `format` TEXT, IMAGE, VIDEO, DOCUMENT or LOCATION; `BODY`;
  `FOOTER`; `BUTTONS` with `buttons[]` of type QUICK_REPLY, URL (a dynamic URL
  ends in `{{1}}`), PHONE_NUMBER, COPY_CODE and others;
- the send - "Send message templates"
  (developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates):
  `{"type": "header" | "body", "parameters": [...]}` and
  `{"type": "button", "sub_type": "url" | "copy_code", "index": "0", "parameters": [...]}`;
  a named placeholder's parameter carries `parameter_name`.

**Media headers** (ADR-144, which amends ADR-137's deferral): an IMAGE, VIDEO or
DOCUMENT header is filled with a file the workspace uploaded to Wasla - one
per campaign, of the kind the header names - and sent as the id Meta gave the
campaign worker's upload of it: `{"type": "header", "parameters": [{"type":
"image", "image": {"id": ...}}]}`, `video` alike, `document` with its
`filename` (Meta, "Template media" and "Document messages", read 2026-10-15).
A LOCATION header carries no file and is still refused (DEFERRED), and so is
GIF - Meta's Marketing Messages API format - and any format this does not know:
a refusal before anyone is written to, never a send without the header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

from app.core.exceptions import ValidationError
from app.db.models.whatsapp_template import PLACEHOLDER

#: Stable error codes a campaign composition is refused with.
TEMPLATE_NOT_SYNCED: Final = "template_not_synced"
TEMPLATE_PARAMETERS: Final = "template_parameters"
TEMPLATE_UNSUPPORTED: Final = "template_unsupported"

#: Meta's codes for a template send whose parameters do not fit the template:
#: 132000 (parameter count), 132012 (parameter format), 131008 (a required
#: parameter missing). "Fix the request" - the same for every recipient, so a
#: campaign stops on the first (error-codes page, read 2026-10-13; EXT-CMP).
TEMPLATE_PARAMETER_CODES: Final = frozenset({132000, 132012, 131008})

#: Header formats filled with a file the workspace uploaded (HM-03). The
#: format in lower case is the asset's kind and the parameter's `type`.
FILE_HEADER_FORMATS: Final = frozenset({"IMAGE", "VIDEO", "DOCUMENT"})
#: Every header format a campaign can fill: none, text, or a file.
SENDABLE_HEADER_FORMATS: Final = FILE_HEADER_FORMATS | {"TEXT"}

#: Button types that never take a send-time parameter. A URL button needs one
#: only when its URL ends in a placeholder; COPY_CODE always does.
STATIC_BUTTONS: Final = frozenset({"QUICK_REPLY", "PHONE_NUMBER", "VOICE_CALL"})


@dataclass(frozen=True, slots=True)
class ButtonNeed:
    """One button that needs a parameter at send time."""

    index: int
    sub_type: str


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """Everything a send of this template must supply."""

    body: tuple[str, ...] = ()
    body_named: bool = False
    #: `None` for no header; TEXT; a file format; or one composition refuses.
    header_format: str | None = None
    header: tuple[str, ...] = ()
    header_named: bool = False
    buttons: tuple[ButtonNeed, ...] = ()
    #: Button types this module does not know how to fill.
    unsupported_buttons: tuple[str, ...] = field(default_factory=tuple)


def _names(text: Any) -> tuple[tuple[str, ...], bool]:
    """A text's placeholders in send order, and whether they are named.

    Positional placeholders are sent in numeric order; named ones in order of
    first appearance. Distinct: `{{1}} ... {{1}}` is one parameter.
    """
    if not isinstance(text, str):
        return (), False
    seen: list[str] = []
    for match in PLACEHOLDER.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    if all(name.isdigit() for name in seen):
        return tuple(sorted(seen, key=int)), False
    return tuple(seen), True


def parameter_spec(components: list[Any] | None) -> ParameterSpec | None:
    """What a template's stored components need at send time, or None if unknown."""
    if not isinstance(components, list):
        return None
    body: tuple[str, ...] = ()
    body_named = False
    header_format: str | None = None
    header: tuple[str, ...] = ()
    header_named = False
    buttons: list[ButtonNeed] = []
    unsupported: list[str] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        kind = str(component.get("type", "")).strip().upper()
        if kind == "BODY":
            body, body_named = _names(component.get("text"))
        elif kind == "HEADER":
            header_format = str(component.get("format", "TEXT")).strip().upper() or "TEXT"
            if header_format == "TEXT":
                header, header_named = _names(component.get("text"))
        elif kind == "BUTTONS":
            raw = component.get("buttons")
            for index, button in enumerate(raw if isinstance(raw, list) else []):
                if not isinstance(button, dict):
                    continue
                button_type = str(button.get("type", "")).strip().upper()
                if button_type == "URL":
                    if PLACEHOLDER.search(str(button.get("url", ""))):
                        buttons.append(ButtonNeed(index=index, sub_type="url"))
                elif button_type == "COPY_CODE":
                    buttons.append(ButtonNeed(index=index, sub_type="copy_code"))
                elif button_type not in STATIC_BUTTONS:
                    unsupported.append(button_type or "UNKNOWN")
    return ParameterSpec(
        body=body,
        body_named=body_named,
        header_format=header_format,
        header=header,
        header_named=header_named,
        buttons=tuple(buttons),
        unsupported_buttons=tuple(unsupported),
    )


@dataclass(frozen=True, slots=True)
class HeaderFileParameter:
    """The file a media header is sent with: Meta's id for it, and a document's name."""

    kind: str
    provider_media_id: str
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class CampaignParameters:
    """What a campaign supplies: body values, header values, button values by index.

    `header_file_kind` is the kind of the header file composition was given
    (`image`, `video`, `document`), or None; `header_file` is what a send
    carries for it, once the worker holds Meta's id.
    """

    body: tuple[str, ...] = ()
    header: tuple[str, ...] = ()
    buttons: dict[int, str] = field(default_factory=dict)
    header_file_kind: str | None = None
    header_file: HeaderFileParameter | None = None

    def as_dict(self) -> dict[str, Any] | None:
        """The record kept on the campaign (`campaigns.template_parameters`), body excluded."""
        if not self.header and not self.buttons:
            return None
        return {
            "header": list(self.header),
            "buttons": [{"index": index, "value": value} for index, value in self.buttons.items()],
        }

    @classmethod
    def from_campaign(
        cls,
        variables: list[str] | None,
        stored: dict[str, Any] | None,
        *,
        header_file_kind: str | None = None,
        header_file: HeaderFileParameter | None = None,
    ) -> CampaignParameters:
        stored = stored or {}
        buttons = {
            int(entry["index"]): str(entry["value"])
            for entry in stored.get("buttons", [])
            if isinstance(entry, dict) and "index" in entry and "value" in entry
        }
        return cls(
            body=tuple(variables or ()),
            header=tuple(str(value) for value in stored.get("header", [])),
            buttons=buttons,
            header_file_kind=header_file_kind,
            header_file=header_file,
        )


def refusal(spec: ParameterSpec | None, supplied: CampaignParameters) -> ValidationError | None:
    """Why a campaign cannot send this template with these parameters, or None.

    Every component is named in the sentence a person reads, and the error
    code is stable for a program.
    """
    if spec is None:
        return ValidationError(
            "This template's components are not known yet. Sync the templates, then try again.",
            error_code=TEMPLATE_NOT_SYNCED,
        )
    if spec.header_format is not None and spec.header_format not in SENDABLE_HEADER_FORMATS:
        # LOCATION (deferred), GIF, or a format Meta adds tomorrow: refused,
        # never sent without its header.
        return ValidationError(
            f"This template's header is {spec.header_format.lower()}; campaigns cannot send "
            "that kind of header.",
            error_code=TEMPLATE_UNSUPPORTED,
        )
    if spec.header_format in FILE_HEADER_FORMATS:
        wanted = spec.header_format.lower()
        if supplied.header_file_kind is None:
            return ValidationError(
                f"This template's header is {article(wanted)} {wanted}; choose a header file.",
                error_code=TEMPLATE_PARAMETERS,
            )
        if supplied.header_file_kind != wanted:
            return ValidationError(
                f"This template's header is {article(wanted)} {wanted}; the header file is "
                f"{article(supplied.header_file_kind)} {supplied.header_file_kind}.",
                error_code=TEMPLATE_PARAMETERS,
            )
    elif supplied.header_file_kind is not None:
        return ValidationError(
            "This template's header takes no file; remove the header file.",
            error_code=TEMPLATE_PARAMETERS,
        )
    if spec.unsupported_buttons:
        return ValidationError(
            "This template has buttons a campaign cannot fill: "
            + ", ".join(sorted(set(spec.unsupported_buttons)))
            + ".",
            error_code=TEMPLATE_UNSUPPORTED,
        )
    if len(supplied.body) != len(spec.body):
        return ValidationError(
            f"This template's body expects {len(spec.body)} variable(s); "
            f"{len(supplied.body)} were supplied.",
            error_code=TEMPLATE_PARAMETERS,
        )
    if len(supplied.header) != len(spec.header):
        return ValidationError(
            f"This template's header expects {len(spec.header)} variable(s); "
            f"{len(supplied.header)} were supplied.",
            error_code=TEMPLATE_PARAMETERS,
        )
    needed = {need.index for need in spec.buttons}
    given = set(supplied.buttons)
    if needed - given:
        return ValidationError(
            "This template's button(s) "
            + ", ".join(str(index) for index in sorted(needed - given))
            + " need a value.",
            error_code=TEMPLATE_PARAMETERS,
        )
    if given - needed:
        return ValidationError(
            "This template has no button needing a value at "
            + ", ".join(str(index) for index in sorted(given - needed))
            + ".",
            error_code=TEMPLATE_PARAMETERS,
        )
    return None


def article(noun: str) -> str:
    """'a' or 'an', for a sentence a person reads."""
    return "an" if noun[:1] in "aeiou" else "a"


def _parameter(name: str, value: str, *, named: bool) -> dict[str, Any]:
    parameter: dict[str, Any] = {"type": "text", "text": value}
    if named:
        parameter["parameter_name"] = name
    return parameter


def send_components(
    spec: ParameterSpec, supplied: CampaignParameters
) -> list[dict[str, Any]] | None:
    """The `components` of a template send, in Meta's shape, for every part the spec needs.

    None when the template needs nothing: an empty component list is not the
    same as none, and Meta refuses the former on a template with no
    placeholders.
    """
    components: list[dict[str, Any]] = []
    if spec.header_format in FILE_HEADER_FORMATS:
        # One file for every recipient: the id the campaign's upload returned.
        header_file = supplied.header_file
        if header_file is None:
            raise ValueError("A media header needs Meta's id for the campaign's header file.")
        media: dict[str, Any] = {"id": header_file.provider_media_id}
        if header_file.kind == "document" and header_file.filename:
            media["filename"] = header_file.filename
        components.append(
            {
                "type": "header",
                "parameters": [{"type": header_file.kind, header_file.kind: media}],
            }
        )
    if spec.header:
        components.append(
            {
                "type": "header",
                "parameters": [
                    _parameter(name, value, named=spec.header_named)
                    for name, value in zip(spec.header, supplied.header, strict=True)
                ],
            }
        )
    if spec.body:
        components.append(
            {
                "type": "body",
                "parameters": [
                    _parameter(name, value, named=spec.body_named)
                    for name, value in zip(spec.body, supplied.body, strict=True)
                ],
            }
        )
    for need in spec.buttons:
        value = supplied.buttons[need.index]
        parameter: dict[str, Any] = (
            {"type": "coupon_code", "coupon_code": value}
            if need.sub_type == "copy_code"
            else {"type": "text", "text": value}
        )
        components.append(
            {
                "type": "button",
                "sub_type": need.sub_type,
                "index": str(need.index),
                "parameters": [parameter],
            }
        )
    return components or None


__all__ = [
    "FILE_HEADER_FORMATS",
    "SENDABLE_HEADER_FORMATS",
    "TEMPLATE_NOT_SYNCED",
    "TEMPLATE_PARAMETERS",
    "TEMPLATE_PARAMETER_CODES",
    "TEMPLATE_UNSUPPORTED",
    "ButtonNeed",
    "CampaignParameters",
    "HeaderFileParameter",
    "ParameterSpec",
    "parameter_spec",
    "refusal",
    "send_components",
]
