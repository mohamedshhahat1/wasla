# Wasla's design language

The rules every screen follows, from FE-RD on (ADR-195). Screens compose components; components
read role tokens; only `src/tokens.css` holds raw values. A later look changes `tokens.css` and the
components here — the screens follow.

## Palette: one blue family and white

| Step     | Hex     | Step     | Hex     |
| -------- | ------- | -------- | ------- |
| blue-50  | #F4F8FE | blue-600 | #1C5FC2 |
| blue-100 | #E4EEFC | blue-700 | #174D9E |
| blue-200 | #C9DCF8 | blue-800 | #153E7D |
| blue-300 | #9DBFF0 | blue-900 | #122F5E |
| blue-400 | #5F97E3 | blue-950 | #0A1A35 |
| blue-500 | #2F74D6 | white    | #FFFFFF |

The scale is read only by the role tokens in `tokens.css`; components, stories and apps use role
tokens (a unit test fails otherwise). Greys are blue-greys of the same hue: `text-muted` #4B5C7A,
`border` #D3E0F2, `border-strong` #7088AE. The focus ring is #3B82F6.

## Three roles by visible area: 60 / 30 / 10

| Role      | Share  | Tokens                                                                                                                       | Where                                                                                               |
| --------- | ------ | ---------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| Dominant  | ≈ 60 % | `surface`, `surface-raised` (white), `canvas`, `surface-sunken` (blue-50)                                                    | page background, cards, forms, dialogs                                                              |
| Secondary | ≈ 30 % | light: `secondary-subtle` (blue-100), `border`, `border-strong`, `selection`; dark: `brand` (blue-900), `console` (blue-800) | structure: the brand panel and bands, field boundaries, chips, selected rows, quiet-button hovers   |
| Accent    | ≈ 10 % | `primary` (blue-600), `primary-hover` (700), `primary-pressed` (800), `focus`, `info`                                        | the one main action per view, links, focus, the "on" state of switches, checkboxes, radios and tabs |
| Text      | apart  | `text` (blue-950), `text-muted`, `on-brand`, `on-brand-muted`, `on-primary`, `text-inverse`                                  | words                                                                                               |
| Status    | small  | `danger`, `warning`, `success` and their `-subtle`                                                                           | a status only - an error, a warning, a success, a destructive button                                |

`tokens.roles.json` gives every colour token exactly one role. `scripts/measure-colour-roles.mjs`
classifies the pixels of a screenshot by those roles (nearest token in OKLab, ΔE 0.04). Bands: the
average over the measured screens at each width - dominant 55–68 %, secondary 24–36 %, accent
6–12 %; every screen - dominant ≥ 45 %, accent ≤ 15 %, status ≤ 3 % without an error, text and
anti-aliasing ≤ 25 % of all pixels. frontend.yml measures the signed-out screens on every push.

Design a new screen's composition before building it: where the secondary sits (a band, a panel,
the field boundaries) and which single action carries the accent. The frame every screen already has:

- the header is the dark-blue structural band (`brand`), the language switcher in a white pill on it;
- the footer is the action blue - the one brand band the accent may carry (taste-skill) - with white
  links (`Link tone="inverse"`); dark and blue bands carry `data-brand-surface`, which turns the focus
  ring light (`focus-inverse`, 3:1 on every band);
- auth screens put the brand panel beside the form (0.8 of its width), a band above it on a phone;
- empty and error cards hold their icon on a light-blue band.

Measured at FE-RD's end (27 screens, en and ar): 1280 - 65.0 / 25.7 / 9.2; 390 - 63.2 / 26.7 / 9.6.

### Status colours

Red, amber and green are the only colours outside the family, because a red error drawn in blue
would mislead (WCAG 1.4.1). They appear only for a status, on their `-subtle` backgrounds, always
with words and an icon, never as decoration. Information is blue.

### The console

`[data-app="console"]` sets `brand` to `console` (blue-800): the console's top band is its own
blue, so staff always know where they are. The action blue is the same in both apps.

### Contrast (computed by `test/tokens.test.ts`)

Text 4.5:1; boundaries, indicators and focus 3:1; a disabled label kept ≥ 3:1. The lowest pairs:
`border-strong` on `canvas` 3.38, `focus` on `canvas` 3.45, `text-muted` on `secondary-subtle` 5.76,
`primary` on `secondary-subtle` 5.18. `blue-400` is never text or a boundary on white (2.99); the
focus ring always sits on a 2 px offset of the surface, never directly on the action blue (1.65).

## Type, space, shape, depth

- IBM Plex Sans and IBM Plex Sans Arabic, self-hosted, 400 / 500 / 600. Latin line height 1.5,
  Arabic 1.7, headings 1.2 with a slight negative tracking. Sizes 13 / 14 / 16 / 18 / 20 / 24 / 32 /
  40; nothing under 13 px; body 16 px (no zoom on focus in iOS Safari). Sentence case; no tracked
  capitals; no eyebrow above a heading; emphasis by weight, never by colour.
- Spacing 2 / 4 / 8 / 12 / 16 / 20 / 24 / 32 / 40 / 48 / 64 / 96.
- One radius family: 6 (small), 8 (controls), 12 (cards), 16 (panels, dialogs).
- Shadows are blue-950 at low alpha, with an offset and a soft blur; never black, never a halo.

## Buttons

| Variant     | Look                                                | Use                                                  |
| ----------- | --------------------------------------------------- | ---------------------------------------------------- |
| `primary`   | the action blue, white text                         | the one main action of a view                        |
| `secondary` | white, a 3:1 blue-grey outline, light blue on hover | every other action                                   |
| `ghost`     | no fill, blue text, light blue on hover             | quiet actions inside a group (the language switcher) |
| `danger`    | the status red                                      | destructive actions only                             |

Every state has a look: rest, hover (colour, at once), pressed (`scale(0.97)` and
the pressed blue), focus-visible (the 2 px ring at once), disabled (blue-50 with muted text, ≥ 3:1),
busy (`busy` prop: disabled, `aria-busy`, a turning ring before the unchanged label). Targets are at
least 40 × 40 (`--spacing-target`), fields 44 px tall. `Link` is underlined, thicker on hover and
focus - colour is never the only cue.

## Composites

`AuthLayout` (the form on white and the dark-blue `BrandPanel` at the inline end; a band above the
form on a phone), `BrandPanel` (the brand's one line on blue-900 with a CSS dot pattern), `Card`
(one card recipe: white, a hairline, 12 px radius, a small shadow), `PageHeader` (the h1, a line, the
actions), `Wordmark` (the name as text - there is no logo yet), `EmptyState`, `ErrorState`,
`FormField`. None holds product logic or calls the API.

## Motion

| Token            | Value                           | For                                |
| ---------------- | ------------------------------- | ---------------------------------- |
| `--motion-press` | 120 ms                          | button press                       |
| `--motion-fast`  | 150 ms                          | hover colour, tooltips, every exit |
| `--motion-base`  | 200 ms                          | popovers, menus, toasts entering   |
| `--motion-slow`  | 240 ms                          | dialogs entering                   |
| `--ease-out`     | cubic-bezier(0.23, 1, 0.32, 1)  | entering and leaving, press        |
| `--ease-in-out`  | cubic-bezier(0.77, 0, 0.175, 1) | something moving on screen         |

Only `transform` and `opacity` animate. Colours change at once - a label is never caught
between two colour pairs (axe checks a screen the moment it changes). Popovers, menus, selects and
tooltips fade in where they open - nothing moves their box, because their placement is measured the
moment they open; dialogs enter by scale alone (never part-transparent) and fade out; toasts slide in
from the inline end. Exits are faster than entries; nothing goes over 300 ms; nothing starts from
`scale(0)`; never ease-in. Focus rings, text, typing, arrowing through a menu or a radio group never
animate; nothing loops but the busy ring; nothing animates on page load. Under
`prefers-reduced-motion: reduce` every transition is 0 s and no animation runs (a Playwright test
asserts it, with a control that shows the same parts animating otherwise).

## The signed-in app (FE-3, ADR-196)

**The frame.** The dark-blue top bar (`brand`) carries the wordmark, the workspace switcher (an outlined button on the
band), the live-updates mark and the account menu; it scrolls with the page, so it never covers a focused field. From
1024 px the navigation is a light-blue side band (`secondary-subtle`) at the inline start, 256 px (`--spacing-nav`);
below that it is a sheet from the inline start (mirrored in RTL) behind a menu button. The page sits on the canvas;
each page opens with its heading on a light-blue band (the secondary's other half); the action-blue footer stays the
one brand band. The current navigation entry is a white "selected" row with blue words and `aria-current="page"`.

**Navigation shows only what exists** - the registry in `apps/web/lib/navigation.ts`; never a disabled entry, a
"coming soon" or a link to a screen a later stage builds.

**Gates.** A full state (suspended, access withdrawn) replaces the page with `StatusState` - an amber icon on the
light-blue band, a heading that takes focus, words, one action. Everything else is a `Banner` under the top bar: one
line, its status colour only for a status (payment owed: red for owners and admins; a billing issue: amber for
members; capacity, allowance, a subscription that ended: amber), information in blue (trial ending, live updates
paused). At most two banners show; the rest sit behind one "n more" disclosure. A new banner is announced once,
politely; one that stays is never repeated. No amount, price or invoice appears.

**Badges** are a small pill with words and an icon (`Badge`), never colour alone. **Stat cards** (`StatCard`) are a
label, a number in Latin digits inside `<bdi>`, and an optional line. **Meters** (`Meter`, `role="meter"`) show used
of total in words beside the bar; over the limit is said in words. **Checklists** (`Checklist`) give every step its
state in words.

**Secrets typed into a form** (an access token): a password field with show / hide, no autocomplete, no spell check,
never prefilled; the request is a plain call (never a cached mutation); the form resets after success.

**Irreversible actions** use `ConfirmDialog` with something to type (releasing a number: its last four digits);
reversible ones a plain confirmation dialog.

**Motion.** The sheet enters with `--motion-slow` and leaves with `--motion-fast`; banners are placed, not slid; the
live mark is a still dot - nothing loops but the busy ring.

**Measured at FE-3's end** (39 screens per width, en and ar, the signed-in screens included): 1280 - 62.3 / 29.9 /
7.6; 390 - 64.0 / 27.1 / 8.3. Gate states are measured apart (status ≤ 8 %): the payment banner 4.1–6.3 %, the
suspended state 0.3–0.5 %.

**Cards are not landmarks.** A card is a block with an `h2`; only the frame's header, navigation, main and footer are
landmarks (a region per card is noise for screen readers - and crashed Firefox's axe run, FEF-25).

| Skill (FE-3) | Decided |
| --- | --- |
| ui-ux-pro-max | the shell's navigation pattern, banners in words and icons, the secret field, the typed confirmation, the pre-delivery checklist |
| taste-skill | where the secondary sits (bar, side navigation, heading bands), one primary per view |
| emil-design-eng | the sheet's motion; banners placed, not slid; a still live mark |
| impeccable | the audits before (17/20) and after (19/20) the fixes |

## Right to left

Logical properties only (the lint forbids physical sides). The brand panel sits at the inline end
by order, so it is on the left in Arabic; icons in the flip list mirror; numbers, addresses, slugs
and codes inside Arabic text are in `<bdi>`. Every composition is checked in Arabic at 390 and 1280.

## Made with

The four Claude Code skills the owner named (FE-RD, 2026-11-12), each for its own part:

| Skill                                     | SKILL.md SHA-256 | Decided                                                                                                                                                                             |
| ----------------------------------------- | ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| ui-ux-pro-max                             | 43d1c5ad…861f7d  | the role strategy ("navy structure, one action blue"), the UX rules for forms, focus, targets and reduced motion, the pre-delivery checklist                                        |
| taste-skill (design-taste-frontend 2.5.1) | dfd1190a…09721f  | one system (one button pair, one card, one radius family), contrast-led hierarchy, 13 px floor, the single brand band, the marketing hero in the first screen                       |
| emil-design-eng                           | f6655603…c7a9    | every motion value above: curves, durations, press scale, origin-aware popovers, exits faster, what never animates                                                                  |
| impeccable 4.8.2                          | e0565c6c…f4b48   | the audits before and after (Operate mode for the apps), the craft floor: browser surfaces (selection, caret, accent colour), no eyebrows, no coloured side borders, offset shadows |

What a skill proposed and this system does not take (another font, an animation library, GSAP,
stock photographs, a second hue, a dark theme now) is listed in FE_BLUE_REDESIGN.md.
