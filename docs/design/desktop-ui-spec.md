# Curslink desktop UI spec

The colours, sizes, type and illustrations behind the redesign
([design page](https://claude.ai/artifact/QkrfqSjSnjR6paWYZd5FsC)). The Qt app
reads its theme from these values. When a value changes, change it here and in
`curslink/gui/theme.py` together.

Status: **draft, awaiting Kyle's approval** (6 Nov 2026).

---

## 1. Platforms and themes

| Platform | Look | Window backdrop | Title bar |
|---|---|---|---|
| Windows 11 | Fluent 2 | Mica (`DwmSetWindowAttribute`, `DWMWA_SYSTEMBACKDROP_TYPE = 38`, value `2`). Plain `--win` colour on Windows 10. | System caption buttons, 40 px tall, 16 px app icon and title on the left |
| macOS | Glass (vibrancy) | `NSVisualEffectView`, material `.sidebar` for the sidebar and `.menu` for menus and popovers, `.underWindowBackground` for the window | Unified with the content: traffic lights at x 20 px, y 20 px, no title text |
| Linux | GNOME / Adwaita | Solid `--win` (no blur on X11 or most Wayland compositors) | Client-side header bar, 46 px tall, split: sidebar header and content header |

**Theme setting:** `System` (default), `Light`, `Dark`. System follows
`QStyleHints.colorScheme()` and changes live when the OS switches.

---

## 2. Colours

Each table is one theme. Token names match the design page's CSS variables.

### 2.1 Windows 11

| Token | Use | Light | Dark |
|---|---|---|---|
| `win` | Window base | `#F3F3F3` | `#202020` |
| `mica` | Window base over Mica (86 to 88% opacity) | `rgba(243,243,243,.86)` | `rgba(32,32,32,.88)` |
| `content` | Content pane over Mica | `rgba(255,255,255,.50)` | `rgba(255,255,255,.03)` |
| `card` | Cards and setting rows | `#FFFFFF` | `#2B2B2B` |
| `card2` | Inset areas (log, step list) | `#F9F9F9` | `#323232` |
| `stroke` | Card and divider lines | `#E5E5E5` | `#1D1D1D` |
| `ink` | Primary text | `#1B1B1B` | `#FFFFFF` |
| `ink2` | Secondary text | `#5F5F5F` | `#C5C5C5` |
| `ink3` | Disabled text, placeholders | `#8A8A8A` | `#9A9A9A` |
| `accent` | Primary buttons, toggles on, focus | `#005FB8` | `#60CDFF` |
| `accent-ink` | Text on accent | `#FFFFFF` | `#000000` |
| `accent-soft` | Selected and info backgrounds | `#E3EEF9` | `#1F3A4A` |
| `ctl` | Button and field fill | `#FFFFFF` | `#2D2D2D` |
| `ctl-stroke` | Button and field border | `#D1D1D1` | `#3A3A3A` |
| `ctl-bottom` | Text field bottom border (Fluent) | `#8A8A8A` | `#9A9A9A` |
| `navsel` | Selected nav item | `rgba(0,0,0,.055)` | `rgba(255,255,255,.06)` |
| `plot` | Layout canvas and head plot | `#F5F7FA` | `#1C1C1C` |

### 2.2 macOS

| Token | Use | Light | Dark |
|---|---|---|---|
| `win` | Window tint over vibrancy | `rgba(246,246,248,.92)` | `rgba(30,30,32,.90)` |
| `side` | Floating sidebar tint | `rgba(255,255,255,.42)` | `rgba(60,60,67,.36)` |
| `card` | Grouped rows | `rgba(255,255,255,.78)` | `rgba(255,255,255,.06)` |
| `card2` | Inset areas | `rgba(0,0,0,.035)` | `rgba(255,255,255,.05)` |
| `stroke` | Hairlines (0.5 px) | `rgba(0,0,0,.08)` | `rgba(255,255,255,.09)` |
| `ink` | Primary text | `#1D1D1F` | `#F5F5F7` |
| `ink2` | Secondary text | `#6E6E73` | `#A1A1A6` |
| `ink3` | Disabled, placeholders | `#8E8E93` | `#8E8E93` |
| `accent` | System accent (use the user's accent colour when Qt reports one) | `#007AFF` | `#0A84FF` |
| `accent-ink` | Text on accent | `#FFFFFF` | `#FFFFFF` |
| `accent-soft` | Selected backgrounds | `rgba(0,122,255,.12)` | `rgba(10,132,255,.22)` |
| `ctl` | Button and field fill | `#FFFFFF` | `rgba(255,255,255,.08)` |
| `ctl-stroke` | Button and field border | `rgba(0,0,0,.14)` | `rgba(255,255,255,.14)` |
| `navsel` | Selected sidebar item | `rgba(0,0,0,.08)` | `rgba(255,255,255,.10)` |
| `plot` | Canvas and plot | `rgba(0,0,0,.04)` | `rgba(0,0,0,.25)` |

### 2.3 Linux (GNOME / Adwaita)

| Token | Use | Light | Dark |
|---|---|---|---|
| `win` | Window and content | `#FAFAFB` | `#222226` |
| `side` | Sidebar | `#EBEBED` | `#2E2E32` |
| `card` | Boxed lists | `#FFFFFF` | `#36363A` |
| `card2` | Inset areas | `#F6F6F7` | `#2E2E32` |
| `stroke` | Card edge and dividers | `rgba(0,0,6,.07)` | `rgba(0,0,0,.36)` |
| `ink` | Primary text | `#1E1E22` | `#FFFFFF` |
| `ink2` | Secondary text | `rgba(0,0,6,.60)` | `rgba(255,255,255,.70)` |
| `ink3` | Disabled, placeholders | `rgba(0,0,6,.45)` | `rgba(255,255,255,.50)` |
| `accent` | Accent | `#3584E4` | `#78AEED` |
| `accent-ink` | Text on accent | `#FFFFFF` | `#0B1A2B` |
| `accent-soft` | Selected backgrounds | `rgba(53,132,228,.14)` | `rgba(120,174,237,.18)` |
| `ctl` | Button and field fill (no border) | `rgba(0,0,6,.06)` | `rgba(255,255,255,.10)` |
| `navsel` | Selected sidebar row | `rgba(0,0,6,.10)` | `rgba(255,255,255,.10)` |
| `plot` | Canvas and plot | `#F2F2F4` | `#1C1C1F` |

### 2.4 Status colours

Used for pills, the status dot and the log. Kept apart from the accent.

| Meaning | Platform | Light text / fill | Dark text / fill |
|---|---|---|---|
| Good (connected, linking) | Windows | `#0F7B0F` / `#DFF6DD` | `#6CCB5F` / `#253A22` |
| | macOS | `#248A3D` / `rgba(52,199,89,.16)` | `#30D158` / `rgba(48,209,88,.16)` |
| | Linux | `#1B7F3B` / `rgba(46,194,126,.16)` | `#8FF0A4` / `rgba(38,162,105,.25)` |
| Warning (waiting, paused) | Windows | `#9D5D00` / `#FFF4CE` | `#FCE100` / `#433519` |
| | macOS | `#A05A00` / `rgba(255,159,10,.18)` | `#FFB340` / `rgba(255,159,10,.18)` |
| | Linux | `#8F5A00` / `rgba(229,165,10,.20)` | `#F8E45C` / `rgba(205,147,9,.25)` |
| Error (stopped, unpair) | Windows | `#C42B1C` / `#FDE7E9` | `#FF99A4` / `#442726` |
| | macOS | `#D70015` / `rgba(255,59,48,.14)` | `#FF6961` / `rgba(255,69,58,.18)` |
| | Linux | `#C01C28` / `rgba(224,27,36,.12)` | `#FF7B63` / `rgba(192,28,40,.30)` |
| Offline | all | `ink2` on `card2` with a 1 px `stroke` ring | same |

Status dot (sidebar footer, tray): good `#2EC27E`, warning `#E5A50A`, off `ink3`.

### 2.5 Brand

| Name | Hex | Use |
|---|---|---|
| Navy 900 | `#0F1B2D` | Icon tile (dark end) |
| Navy 700 | `#1D3A66` | Icon tile (light end) |
| Link blue | `#3FA9FF` | Switch animation, icon screens |
| Sky | `#B6DFFF` | Icon pointer highlight |

### 2.6 Contrast rules

* Text in a field is always `ink` on `ctl`. Never white on a light field. Placeholder text is `ink3`.
* Every `ink` / `card` pair above passes WCAG AA (4.5:1). `ink2` passes 4.5:1 on `card` and `win`.
* Text on `accent` uses `accent-ink`. In dark mode on Windows and Linux the accent is light, so that text is black or near-black.

---

## 3. Type

| Platform | UI font | Mono font |
|---|---|---|
| Windows | Segoe UI Variable Text, then Segoe UI | Cascadia Mono, then Consolas |
| macOS | SF Pro (system font, `.AppleSystemUIFont`) | SF Mono, then Menlo |
| Linux | Cantarell or Adwaita Sans (whatever the desktop sets), then Noto Sans | Source Code Pro, then DejaVu Sans Mono |

| Style | Windows | macOS | Linux | Weight |
|---|---|---|---|---|
| Page title | 28 px | 26 px | 24 px | 600 / 700 / 700 |
| Big status ("Work laptop") | 26 px | 26 px | 26 px | 600 |
| Card heading, group label | 14 px | 13 px (`ink2`) | 14 px | 600 / 600 / 700 |
| Body, row title | 14 px | 13.5 px | 14 px | 400 |
| Row detail, small | 12.5 px | 12.5 px | 12.5 px | 400 |
| Eyebrow (uppercase, 0.06 em tracking) | 11.5 px | 11.5 px | 11.5 px | 600 |
| Pairing code (0.14 em tracking) | 38 px mono | 38 px mono | 38 px mono | 500 |
| Mono (log, angles, paths) | 12 to 12.5 px | same | same | 400 |
| Title bar text | 12 px | none | 14 px bold (header bar) | 400 |

---

## 4. Sizes

### 4.1 Window

| | Value |
|---|---|
| Default size | 1080 × 700 px (Status); agent window 1080 × 560 |
| Minimum size | 760 × 520 px |
| Sidebar width | 232 px Windows and Linux, 240 px macOS (floating, 8 px inset, 14 px radius) |
| Content padding | 24 px top, 32 px sides (macOS 30 px top) |
| Content max width | 860 px (Linux centres it) |
| Gap between cards | 16 px |
| Window corner radius | 8 px Windows, 16 px macOS (system), 14 px Linux |

### 4.2 Radius

| Element | Windows | macOS | Linux |
|---|---|---|---|
| Card / group | 8 | 12 | 12 |
| Setting row (Windows draws each row as its own card, 3 px apart) | 6 | — | — |
| Button, field, nav item | 4 | 7 (nav 8) | 8 |
| Pill | full | full | full |
| Menu, toast | 8 | 12 (toast 18) | 12 |

### 4.3 Controls

| Control | Windows | macOS | Linux |
|---|---|---|---|
| Button height / padding | 32 / 0 14 px | 28 / 0 12 px, 13 px text | 34 / 0 14 px, bold, no border |
| Small button | 28 / 0 10 px | same | same |
| Text field height | 32 px, 1 px border, bottom border `ctl-bottom`, 2 px `accent` bottom when focused | 26 px, focus ring 3.5 px `accent` at 45% | 34 px, no border, focus ring 2 px `accent` at 60% |
| Drop-down | as field, chevron 7 px | as field, accent stepper square 16 px | as field |
| Toggle (track) | 40 × 20, 1 px `ink2` border off, knob 12 px | 38 × 22, knob 20 px white with shadow | 48 × 26, knob 20 px |
| Segmented control | 2 px inset, item padding 4 × 14 px | grey track `rgba(120,120,128,.14)`, raised white thumb | as Windows, no border |
| Slider | 4 px track, 18 px thumb | same, white thumb | same |
| Nav item height | 36 px, 3 × 16 px accent bar when selected | 30 px | 38 px |
| Setting row min height | 64 px | 56 px | 56 px |
| Pill | 2 × 10 px padding, 12 px semibold | same | same |
| Row icon | 20 px, 1.5 px stroke (Windows only) | none | none |

### 4.4 Tray and notifications

| | Value |
|---|---|
| Tray glyph sizes | 16, 22, 32, 44, 64 px |
| Status dot on glyph | radius 20% of size, at bottom right, with a cut-out ring 8% of size (Windows and Linux only) |
| macOS menu bar | template image (black, the system tints it), no dot |
| Menu min width | 300 px, 4 px padding, item 6 × 10 px |
| Toast | 360 px wide, 36 px icon |

---

## 5. Illustrations and icons

### 5.1 App icon

Files in `curslink/assets` (masters) and `packaging/icons` (generated by
`packaging/icons/make_icons.py`):

| File | What | Sizes |
|---|---|---|
| `curslink.svg` | The icon on Apple's grid (tile is the middle 824 of 1024) | any |
| `curslink-small.png` | Simplified icon for 16 to 24 px | 256 px master |
| `tray.png` | Black tray glyph | 512 px master |
| `curslink.icns` | macOS | 16 to 1024 |
| `curslink.ico` | Windows (tile scaled to 94% of the square) | 16 to 256 |
| `png/curslink-N.png` | Linux hicolor | 16 to 512 |

### 5.2 Navigation glyphs

16 px, 1.6 px stroke, round caps and joins, `currentColor` (macOS: `accent`).
The SVG paths, on a 16 × 16 view box:

| Page | Path |
|---|---|
| Status | `<rect x="1.5" y="2.5" width="13" height="9" rx="1.5"/><path d="M5.5 14h5M8 11.5V14"/>` |
| Devices | `<rect x="1.5" y="3" width="8.5" height="6.5" rx="1"/><rect x="11.5" y="5" width="3" height="7" rx="1"/><path d="M3.5 12h5"/>` |
| Layout | `<rect x="1.5" y="3.5" width="6" height="5" rx="1"/><rect x="8.5" y="5.5" width="6" height="7" rx="1"/>` |
| Calibrate | `<circle cx="8" cy="8" r="5.5"/><circle cx="8" cy="8" r="2"/><path d="M8 1v2M8 13v2M1 8h2M13 8h2"/>` |
| Settings | `<circle cx="8" cy="8" r="2.2"/><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4"/>` |

On Windows, use the Segoe Fluent Icons font instead when it's installed:
Status `E7F4`, Devices `E772`, Layout `E8A9`, Calibrate `E7B3`, Settings `E713`.
On macOS, SF Symbols: `display`, `laptopcomputer.and.iphone`,
`rectangle.split.2x1`, `scope`, `gearshape`.

### 5.3 Drawn illustrations

| Illustration | Where | Spec |
|---|---|---|
| Screen mini map | Status hero | 150 × 96 px area. Each screen is a rect with 5 px radius and 2 px `ink3` border on `card2`. The active one has an `accent` border, `accent-soft` fill, a 4 px `accent` glow at 18%, and a 10 px `accent` dot that pulses (6 to 10 px halo, 2.4 s, off with reduced motion). |
| Layout canvas | Layout | 300 px tall, `plot` fill with a 16 px dot grid (`stroke` in light, `rgba(255,255,255,.08)` in dark). Screens: 6 px radius, 2 px border. This computer `ink3` on `card`; other devices `#8E6BD8` at 60% border and 14% fill; selected `accent` with 4 px glow. A connected edge is a 4 px good-colour bar on the shared edge. |
| Head plot | Calibrate | 250 px tall, `plot` fill, 1 px `stroke` axes through the centre. Ranges: yaw ±50°, pitch ±32°. Targets 26 × 18 px, 4 px radius, 2 px `ink2` border, good colour when recorded. Your head: 30 px `accent` ring with an 14 px dot. |
| Pairing code card | Devices | `accent-soft` card, code in 38 px mono split 3 + 3. |
| Switch animation | On the target screen | 180 px square, colour `#3FA9FF`, 380 ms. Pop: ring grows from 10 px to 76 px radius, stroke 9 px to 1 px, with a 7 px dot fading out. Blink: 34 px ring, 6 px stroke, plus 9 px dot, flashed twice. |

---

## 6. Motion

| What | Duration | Easing |
|---|---|---|
| Toggle knob | 120 ms | ease out |
| Page switch | 150 ms cross-fade (none with reduced motion) | ease out |
| Status dot pulse | 2.4 s loop | ease in-out |
| Switch animation | 380 ms | cubic ease out |

---

## 7. Behaviour that goes with the look

* Before set-up (first run), the sidebar has no pages or link status: it shows the app icon (96 px), "Curslink" (20 px bold) and "Version x.y.z" (13 px, `ink2`), centred.
* After set-up, Curslink starts in the tray or menu bar. The window only opens when you choose Open Curslink, on hosts and agents alike.
* The first time it starts hidden, it shows one notification: "Curslink is running in the background".
* macOS hides the Dock icon while the window is closed (`NSApplicationActivationPolicyAccessory`) and shows it again when the window opens.
* With no tray (GNOME without the AppIndicator extension), it says so once and keeps a small window open.
* Cameras are picked by name (with a "Built in", "USB", "Continuity" or "Virtual" note); the config keeps the index and the name, and falls back to the name if the index moves.
