# Brand: Salamander

The proposed name for the trader agent and its desk (#85). This file is the written
reference; the visual specimen is `clients/desk/brand/salamander.html`.

## Name

- **Salamander** in writing and in the wordmark. **Sal** in speech, the launcher and the terminal.
- Line: **slow gold**, set under the name in mono caps, in blue flame. "From volatile gold
  to slow gold" when there is room.
- The wordmark sets **SAL** in spot yellow and **AMANDER** in salt.

## Why the name fits

- Named for Salamandra Hermética (Alqvimia A0027): "We are able to live and function in
  the fire of transformation." The agent lives in the fire of the market; RiskGuard is its
  creative control.
- **SAL** is the alchemists' salt, the fixed principle. The name holds the fixed inside the
  fire: the model is volatile, RiskGuard and the journal are fixed.
- No trading or fintech product uses the name (search, 2026-09-30).

## Mark

A fire salamander seen from above. Its body is an S, for Salamander and Sal. Four limbs, a
blunt round head, and gold spots on the spine: the fixed moments along the way.

| File | Use |
|---|---|
| `clients/desk/brand/salamander-mark.svg` | Default mark |
| `clients/desk/brand/salamander-mark-gold.svg` | Gold on skin: launcher and app icon |
| `clients/desk/brand/salamander-mark-mono.svg` | Skin on salt: light backgrounds |
| `clients/desk/brand/salamander-icon.png` | Launcher icon, 512px |
| `clients/desk/brand/make_mark.py` | Draws the mark; regenerate from here, never edit the SVGs by hand |

The mark holds at 40px and 20px.

## Colour

Colour carries meaning. The tokens below are the CSS custom properties the desk uses.

| Name | Hex | Desk token | Meaning |
|---|---|---|---|
| Skin | `#0D0B0A` | `--base` | The ground. Glossy, warm black. |
| Surface | `#161311` | `--surface` | Panels. |
| Raised | `#211C18` | `--overlay` | Raised panels and overlays. |
| Salt | `#EFE9DF` | text | Text. What stays after the fire. |
| Spot | `#F5B82E` | `--accent` | Fixed: accepted, held, earned. The one primary action. |
| Blue flame | `#62B6FF` | `--live` | Live and volatile: the running loop, proposals, the connection. |
| Blue flame text | `#9FD0FF` | `--live-text` | Blue flame at text contrast. |
| Scorch | `#EF5A4C` | `--red` | Failures only. |

Losses read in ash (muted salt) with a minus sign, not in scorch.

## Type

| Role | Face | Use |
|---|---|---|
| Display | Big Shoulders Display | Wordmark, headings, big numbers. Condensed, made for the City of Chicago, where the research comes from. |
| Interface | Hanken Grotesk | Body and controls. Readable at 14 to 15px, tabular figures for every number that moves. |
| Instrument | Fragment Mono | Symbols, catalog numbers, settings. |

All three ship in `clients/desk/fonts/` under the SIL Open Font License (`OFL.txt`).

## Rules

- **Yellow fixes, blue burns.** Never use yellow for something still in motion or blue for
  something settled. Red never decorates.
- **One yellow button.** Each screen has at most one primary action. Everything else is an
  outline.
- **Plain words.** "Held BTC/USD", "Would buy $2,000 of ETH". No codes on screen; catalog
  numbers only in sources.
- **Original.** No borrowed palettes, faces or icons from other products.
