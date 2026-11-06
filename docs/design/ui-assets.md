# Curslink desktop UI assets

Open `preview.html` from the extracted folder to compare the tray glyph at native size, switch among six illustration themes inspect the spot illustrations on light and dark surfaces, and replay the switch effects.

- `curslink/assets/` contains editable SVG icon masters and the small icon and tray PNG masters.
- `curslink/assets/navigation/` contains five 16 px glyphs. Resolve `currentColor` to the applicable Qt palette colour when loading the SVG.
- `curslink/assets/illustrations/` contains four UI illustrations per theme and two switch effects.
- `curslink/assets/illustrations/spot/` contains the 17 transparent 160 × 120 SVGs from section 5.4. Use the same SVG in light and dark. Render at 96 × 72 for notices, 160 × 120 for cards and empty states, or 240 × 180 for first run. Keep titles and actions in the app UI.
- `packaging/icons/` contains macOS ICNS, Windows ICO, Linux hicolor PNGs and five tray sizes. Mark the monochrome image as a template on macOS.

| Page | Segoe Fluent Icons | SF Symbols |
|---|---|---|
| Status | E7F4 | display |
| Devices | E772 | laptopcomputer.and.iphone |
| Layout | E8A9 | rectangle.split.2x1 |
| Calibrate | E7B3 | scope |
| Settings | E713 | gearshape |

Use the illustration SVGs as drawing references for live layouts, head pose and pairing codes in Qt. They contain sample data. The switch SVGs are browser animations with reduced-motion support. Use native animation for the Qt implementation.

To regenerate platform icon exports, run `python3 packaging/icons/make_icons_macos.py` on macOS (or `packaging/icons/make_icons.py` anywhere, with PySide6) with Pillow installed.

To regenerate the spot SVGs, run `python3 packaging/illustrations/make_spot.py`.
