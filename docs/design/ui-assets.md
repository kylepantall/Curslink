# Curslink desktop UI assets

Generated from the draft desktop UI spec dated 6 November 2026. The vector icon redraw follows the two screens and cursor in the installed Curslink icon, with the navy and blue palette from the draft.

| Location | Contents |
|---|---|
| `curslink/assets/` | 1024 px app icon SVG with an 824 px tile, 256 px small icon PNG and 512 px black tray template PNG. The small icon and tray glyph also have editable SVG masters. |
| `curslink/assets/navigation/` | Five 16 px SVG glyphs using `currentColor`. For Qt, resolve it to the active theme colour at load time. |
| `curslink/assets/illustrations/` | Mini map, layout canvas, head plot and sample pairing code card for Windows, macOS and Linux in light and dark. Also includes 180 px switch pop and blink animations. |
| `packaging/icons/` | macOS ICNS, multi-resolution Windows ICO, Linux hicolor PNGs and tray PNGs at 16, 22, 32, 44 and 64 px. |

The macOS tray PNGs are black templates. Set the native image's template flag so the system can tint it. Windows and Linux tray exports include good, warning and off states in light and dark.

## Navigation mapping

| Page | Segoe Fluent Icons | SF Symbols |
|---|---|---|
| Status | E7F4 | display |
| Devices | E772 | laptopcomputer.and.iphone |
| Layout | E8A9 | rectangle.split.2x1 |
| Calibrate | E7B3 | scope |
| Settings | E713 | gearshape |

## Integration

Use the illustration SVGs as visual references for drawing live layouts, head positions and pairing codes. The device names, geometry and code are sample data. macOS variants use the fallback blue accent from the spec. The animated SVGs use CSS and honour reduced motion in a browser. For native Qt widgets, implement motion using the app's animation system.

Regenerate the PNG, ICO and ICNS exports on macOS with Python, Pillow, `sips` and `iconutil`.

```sh
python3 packaging/icons/make_icons.py
```
