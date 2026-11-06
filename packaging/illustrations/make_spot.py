#!/usr/bin/env python3
"""Generate Curslink's 160 × 120 transparent spot illustrations (UI spec §5.4).

Python standard library only. Run from any directory; assets are resolved
relative to this script. Every glyph uses the same device and status geometry.
"""
from pathlib import Path
from html import escape
import xml.etree.ElementTree as ET

OUT = Path(__file__).resolve().parents[2] / 'curslink/assets/illustrations/spot'
SLATE = '#7A8BA6'
NAVY = '#1D3A66'
SKY = '#B6DFFF'
BLUE = '#3FA9FF'
GOOD = '#2EC27E'
WARN = '#E5A50A'
ERROR = '#E5534B'
DEEP = '#1467B8'


def path(d, stroke=SLATE, width=2, fill='none', extra=''):
    return f'<path d="{d}" fill="{fill}" stroke="{stroke}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round" {extra}/>'


def rect(x, y, w, h, rx=4, fill=SLATE, opacity=.16, stroke=SLATE, extra=''):
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" fill-opacity="{opacity}" stroke="{stroke}" stroke-width="2" {extra}/>'


def circle(x, y, r, fill='none', stroke=SLATE, extra=''):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{fill}" stroke="{stroke}" stroke-width="2" {extra}/>'


def group(body, extra=''):
    return f'<g {extra}>{body}</g>'


def screen(x, y, w, h, active=False, stand=True, extra=''):
    body = rect(x, y, w, h, 4, 'url(#screen)' if active else SLATE, 1 if active else .16)
    if active:
        body += f'<rect x="{x+4}" y="{y+4}" width="{w-8}" height="{h-9}" rx="2" fill="{NAVY}"/>'
        body += path(f'M{x+8} {y+h-12}h{max(5,w*.20)}', SKY, 2, extra='opacity=".45"')
    if stand:
        body += path(f'M{x+w/2} {y+h}v7m-9 0h18')
    return group(body, extra)


def laptop(x, y, w, h, active=True, extra=''):
    body = screen(x, y, w, h, active, False)
    body += path(f'M{x-5} {y+h+1}h{w+10}l-4 5H{x-1}z', SLATE, 2, NAVY if active else SLATE, '' if active else 'fill-opacity=".16"')
    body += path(f'M{x+w/2-5} {y+h+2}h10', SKY if active else SLATE, 1.5)
    return group(body, extra)


def pointer(x, y, scale=1):
    return group(path('M0 0l5 25 5-8 9 4 3-6-9-4 7-7z', NAVY, 2, SKY), f'transform="translate({x} {y}) scale({scale})"')


def badge(x, y, kind, color):
    body = circle(x, y, 11, color, 'none')
    shapes = {
        'check': f'M{x-5} {y}l3.5 3.5 6.5-7',
        'cross': f'M{x-3.5} {y-3.5}l7 7m0-7-7 7',
        'clock': f'M{x} {y-4}v4l3 2',
        'arrow': f'M{x} {y-5}v9m-4-4 4 4 4-4',
        'warn': f'M{x} {y-5}v5m0 4h.01',
    }
    if kind == 'clock':
        body += circle(x, y, 6, 'none', '#FFFFFF')
    body += path(shapes[kind], '#FFFFFF', 2)
    return body


def broken_link(y=61):
    return path(f'M65 {y}h8m14 0h8', BLUE, 2, extra='stroke-dasharray="5 5"') + path(f'M80 {y-8}l-4 5m8 7-4 5', SLATE)


def welcome():
    return (path('M43 34C65 8 110 12 124 34', BLUE, 2, extra='stroke-dasharray="5 5"')
            + screen(14, 49, 52, 36) + screen(94, 38, 52, 36, True)
            + pointer(75, 51, 1.05))


def role_host():
    b = screen(43, 20, 74, 49, True)
    b += rect(31, 87, 70, 15, 4)
    for y in [92, 97]:
        b += path(f'M38 {y}h4m5 0h4m5 0h4m5 0h4m5 0h4m5 0h4', SLATE, 1.5)
    b += rect(114, 84, 15, 21, 7) + path('M121.5 88v4', SLATE)
    b += pointer(77, 35, .66)
    return b


def role_agent():
    return (laptop(38, 47, 84, 48)
            + path('M23 65C13 27 57 13 81 37', BLUE, 2, extra='stroke-dasharray="5 5"')
            + pointer(79, 32, .98))


def devices_empty():
    return (laptop(14, 54, 55, 35)
            + path('M77 69h17', BLUE, extra='stroke-dasharray="5 5"')
            + rect(102, 36, 42, 48, 5, extra='stroke-dasharray="5 5"')
            + path('M116 60h14m-7-7v14', SLATE)
            + path('M116 92h14', SLATE))


def searching():
    return (path('M100 27a43 43 0 0 1 38 40', SLATE, extra='opacity=".45"')
            + path('M98 41a29 29 0 0 1 26 27', BLUE, extra='opacity=".65"')
            + path('M96 55a15 15 0 0 1 14 14', BLUE)
            + circle(96, 72, 3, BLUE, 'none')
            + laptop(23, 65, 61, 35)
            + circle(121, 33, 2, SKY, 'none'))


def pairing():
    b = screen(10, 43, 43, 31, True) + laptop(107, 48, 43, 29)
    b += path('M52 59h56', BLUE, extra='stroke-dasharray="5 5"')
    b += path('M80 30l21 9v20c0 15-11 25-21 31-10-6-21-16-21-31V39z', SLATE, 2, 'url(#screen)')
    b += path('M80 36l15 6v17c0 11-7 19-15 25-8-6-15-14-15-25V42z', 'none', 0, NAVY)
    b += path('M74 56v-5a6 6 0 0 1 12 0v5', SKY)
    b += rect(71, 56, 18, 15, 3, SKY, 1, 'none')
    b += path('M80 61v5', NAVY)
    return b


def paired():
    return (screen(13, 35, 53, 37, True) + laptop(96, 45, 52, 35)
            + path('M40 82v8h70', BLUE, extra='stroke-dasharray="5 5"')
            + badge(81, 90, 'check', GOOD))


def calibrate():
    b = screen(8, 29, 39, 27) + screen(61, 17, 39, 27, True) + screen(113, 29, 39, 27)
    b += path('M80 75V56', BLUE)
    b += path('M65 78 35 65m60 13 30-13', SLATE, extra='stroke-dasharray="5 5" opacity=".65"')
    b += circle(80, 87, 11, SLATE, SLATE, 'fill-opacity=".16"')
    b += path('M61 111c1-11 8-16 19-16s18 5 19 16', SLATE, 2, SLATE, 'fill-opacity=".16"')
    b += path('M76 78q4-3 8 0', BLUE)
    return b


def calibrated():
    b = ''
    for x, y in [(8, 44), (61, 28), (114, 44)]:
        b += screen(x, y, 38, 29, True)
        b += path(f'M{x+13} {y+13}l4 4 8-8', '#FFFFFF')
    b += path('M28 90h103', SLATE, extra='stroke-dasharray="5 5"')
    b += badge(80, 90, 'check', GOOD)
    return b


def camera_off():
    b = rect(29, 38, 80, 52, 8)
    b += path('M109 53l21-11v44l-21-11z', SLATE, 2, SLATE, 'fill-opacity=".16"')
    b += circle(67, 64, 15) + circle(67, 64, 7)
    b += path('M29 100 126 22', SLATE, 2)
    b += badge(119, 94, 'cross', ERROR)
    return b


def no_face():
    b = path('M34 43V25h18m56 0h18v18M34 77v18h18m56 0h18V77')
    b += circle(80, 51, 15, SLATE, SLATE, 'fill-opacity=".16" stroke-dasharray="5 5"')
    b += path('M53 90c2-17 12-25 27-25s25 8 27 25', SLATE, 2, SLATE, 'fill-opacity=".16" stroke-dasharray="5 5"')
    b += badge(119, 94, 'warn', WARN)
    return b


def not_linking():
    return (screen(13, 41, 49, 34) + screen(99, 41, 49, 34)
            + broken_link(57) + badge(80, 92, 'cross', ERROR))


def waiting_for_host():
    return (screen(15, 27, 53, 36, extra='opacity=".4"')
            + laptop(95, 59, 51, 34)
            + path('M70 43h26q24 0 24 11', BLUE, extra='stroke-dasharray="5 5"')
            + badge(64, 89, 'clock', WARN))


def connection_lost():
    return (screen(13, 41, 49, 34, True) + laptop(99, 46, 48, 33)
            + broken_link(60) + badge(80, 94, 'warn', WARN))


def app_tile(x, y, size=64):
    s = size / 64
    b = rect(0, 0, 64, 64, 14, NAVY, 1)
    b += rect(10, 24, 23, 19, 3, 'none', 1, BLUE)
    b += rect(33, 16, 23, 19, 3, 'none', 1, BLUE)
    b += path('M21.5 43v5m-5 0h10M44.5 35v5m-5 0h10', BLUE)
    b += pointer(30, 28, .55)
    return group(b, f'transform="translate({x} {y}) scale({s})"')


def update():
    return (app_tile(35, 26, 68) + path('M120 33v31m-8-8 8 8 8-8', BLUE)
            + badge(115, 88, 'arrow', DEEP))


def diagnostics():
    b = path('M48 18h45l19 19v65H48z', SLATE, 2, SLATE, 'fill-opacity=".16"')
    b += path('M93 18v19h19M59 32h21M59 43h17')
    b += path('M35 68h24l7-12 10 28 9-22 6 6h34', BLUE)
    b += path('M59 92h22', SLATE)
    b += badge(113, 98, 'cross', ERROR)
    return b


def background():
    b = rect(15, 23, 130, 18, 5)
    b += path('M24 32h12m7 0h13', SLATE)
    b += rect(107, 26, 22, 12, 3, BLUE, .16, 'none')
    b += path('M111 29h6v5h-6zM119 28h6v5h-6z', BLUE, 1.5)
    b += path('M117 34v2m-4 0h9', BLUE, 1.5)
    b += rect(66, 46, 78, 60, 6, NAVY, 1)
    b += circle(78, 60, 3, GOOD, 'none')
    b += path('M87 60h43', SKY)
    b += path('M73 71h64', SLATE, 1, extra='opacity=".5"')
    b += path('M77 82h43M77 95h26', SKY, 2)
    b += path('M27 55v40h26', SLATE, extra='stroke-dasharray="5 5"')
    return b


ILLUSTRATIONS = {
    'welcome': ('Welcome', 'Two screens joined by an arcing cursor path.', welcome),
    'role-host': ('Host', 'A desktop monitor with a keyboard and mouse.', role_host),
    'role-agent': ('Agent', 'A pointer arriving on a laptop.', role_agent),
    'devices-empty': ('No devices yet', 'A laptop with a dashed link to an empty device.', devices_empty),
    'searching': ('Looking for devices', 'A laptop sending discovery rings into the network.', searching),
    'pairing': ('Pairing', 'Two computers connected through a protective shield and lock.', pairing),
    'paired': ('Paired', 'Two linked devices with a green confirmation check.', paired),
    'calibrate': ('Calibrate gaze', 'A head facing the middle of three screens.', calibrate),
    'calibrated': ('Calibration saved', 'Three screens marked with checks.', calibrated),
    'camera-off': ('Camera unavailable', 'A crossed-out camera with an error badge.', camera_off),
    'no-face': ('No face found', 'A dashed head silhouette inside a camera viewfinder.', no_face),
    'not-linking': ('Not linking', 'Two inactive screens separated by a broken link.', not_linking),
    'waiting-for-host': ('Waiting for the host', 'An agent laptop waiting for a faded host monitor.', waiting_for_host),
    'connection-lost': ('Connection lost', 'A gap between two linked computers and a warning badge.', connection_lost),
    'update': ('Update ready', 'The Curslink app tile with a download arrow.', update),
    'diagnostics': ('Diagnostics', 'A folded report page with a pulse line and error badge.', diagnostics),
    'background': ('Running in the background', 'A menu bar with the Curslink status menu open.', background),
}


def render(title, description, body):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="160" height="120" viewBox="0 0 160 120" role="img" aria-labelledby="title description">
  <title id="title">{escape(title)}</title>
  <desc id="description">{escape(description)}</desc>
  <defs><linearGradient id="screen" x1="0" y1="0" x2="1" y2="1"><stop stop-color="{SKY}"/><stop offset="1" stop-color="{BLUE}"/></linearGradient></defs>
  {body}
</svg>\n'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, (title, description, draw) in ILLUSTRATIONS.items():
        svg = render(title, description, draw())
        ET.fromstring(svg)
        (OUT / f'{name}.svg').write_text(svg, encoding='utf-8')
    print(f'Generated {len(ILLUSTRATIONS)} spot illustrations in {OUT}')


if __name__ == '__main__':
    main()
