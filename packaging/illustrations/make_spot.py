"""Draws the in-app spot illustrations into curslink/assets/illustrations/spot.

    python packaging/illustrations/make_spot.py

Brand palette on a transparent background, so one file reads on light and
dark. 160 x 120 view box. See docs/design/desktop-ui-spec.md, section 5.4.
"""
import os
import sys
from pathlib import Path

OUT = sys.argv[1] if len(sys.argv) > 1 else str(Path(__file__).resolve().parents[2] / "curslink" / "assets" / "illustrations" / "spot")
os.makedirs(OUT, exist_ok=True)

N1 = "#7A8BA6"           # outline (slate, works on both themes)
N2 = "#7A8BA6" ; N2O = ".16"  # neutral fill
ACC = "#3FA9FF"; DEEP = "#1467B8"; SKY = "#B6DFFF"; NAVY = "#1D3A66"
GOOD = "#2EC27E"; WARN = "#E5A50A"; BAD = "#E5534B"

DEFS = f'''<defs>
<linearGradient id="scr" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{SKY}"/><stop offset="1" stop-color="{ACC}"/></linearGradient>
<linearGradient id="tile" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#24497F"/><stop offset="1" stop-color="#0F1B2D"/></linearGradient>
</defs>'''

def screen(x, y, w, h, active=False, dashed=False, faded=False):
    if active:
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="url(#scr)" fill-opacity=".9" stroke="{ACC}" stroke-width="2.5"/>'
                f'<rect x="{x+4}" y="{y+4}" width="{w-8}" height="{h-8}" rx="3" fill="{NAVY}" fill-opacity=".85"/>')
    dash = ' stroke-dasharray="4 4"' if dashed else ""
    op = ' opacity=".55"' if faded else ""
    fill = "none" if dashed else N2
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" fill="{fill}" fill-opacity="{N2O}" stroke="{N1}" stroke-width="2"{dash}{op}/>'

def stand(cx, y, faded=False):
    op = ' opacity=".55"' if faded else ""
    return f'<path d="M{cx} {y}v7M{cx-9} {y+8}h18" stroke="{N1}" stroke-width="2" stroke-linecap="round"{op}/>'

def laptop(x, y, w, active=False, dashed=False, faded=False):
    h = round(w * 0.62)
    s = screen(x, y, w, h, active, dashed, faded)
    op = ' opacity=".55"' if faded else ""
    dash = ' stroke-dasharray="4 4"' if dashed else ""
    base = f'<path d="M{x-6} {y+h+2}h{w+12}l-4 6h-{w+4}z" fill="{N2}" fill-opacity=".3" stroke="{N1}" stroke-width="2" stroke-linejoin="round"{op}{dash}/>'
    return s + base

def pointer(x, y, s=1.0):
    # arrow tip at (x, y)
    return (f'<path transform="translate({x} {y}) scale({s})" d="M0 0l0 17l4.6-4.3l3.3 7.4l3.2-1.4l-3.3-7.2l6.4-.3z" '
            f'fill="#FFFFFF" stroke="{NAVY}" stroke-width="1.6" stroke-linejoin="round"/>')

def arc(d, dashed=False, color=ACC, width=3):
    dash = ' stroke-dasharray="5 5"' if dashed else ""
    return f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linecap="round"{dash}/>'

def badge(cx, cy, kind):
    color = {"good": GOOD, "warn": WARN, "bad": BAD, "acc": DEEP}[kind]
    mark = {"good": f'<path d="M{cx-5} {cy}l3.5 3.5l6.5-7" fill="none" stroke="#fff" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/>',
            "warn": f'<path d="M{cx} {cy-5.5}v6.5" stroke="#fff" stroke-width="2.6" stroke-linecap="round"/><circle cx="{cx}" cy="{cy+5}" r="1.6" fill="#fff"/>',
            "bad": f'<path d="M{cx-4.5} {cy-4.5}l9 9M{cx+4.5} {cy-4.5}l-9 9" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>',
            "acc": f'<path d="M{cx} {cy-6}v12M{cx-6} {cy}h12" stroke="#fff" stroke-width="2.4" stroke-linecap="round"/>'}[kind]
    return f'<circle cx="{cx}" cy="{cy}" r="11" fill="{color}"/>{mark}'

def sparkle(x, y, r=4, color=ACC, op="1"):
    return (f'<path d="M{x} {y-r}Q{x} {y} {x+r} {y}Q{x} {y} {x} {y+r}Q{x} {y} {x-r} {y}Q{x} {y} {x} {y-r}z" fill="{color}" opacity="{op}"/>')

def shadow(cx, cy, rx):
    return f'<ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="4" fill="{N1}" fill-opacity=".14"/>'

def head(cx, cy, r=13, dashed=False, color=None):
    c = color or N1
    dash = ' stroke-dasharray="4 4"' if dashed else ""
    fill = "none" if dashed else N2
    return (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{fill}" fill-opacity=".3" stroke="{c}" stroke-width="2"{dash}/>'
            f'<path d="M{cx-r*1.7} {cy+r*2.5}q0-{r*1.3} {r*1.7}-{r*1.3}q{r*1.7} 0 {r*1.7} {r*1.3}" fill="{fill}" fill-opacity=".3" stroke="{c}" stroke-width="2" stroke-linecap="round"{dash}/>')

def camera(cx, cy, w=46, h=32):
    x, y = cx - w/2, cy - h/2
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="{N2}" fill-opacity=".2" stroke="{N1}" stroke-width="2"/>'
            f'<circle cx="{cx}" cy="{cy}" r="{h*0.3}" fill="{NAVY}" stroke="{N1}" stroke-width="2"/><circle cx="{cx+2}" cy="{cy-2}" r="2" fill="{SKY}"/>'
            f'<path d="M{cx+w/2} {cy-5}l9-5v20l-9-5z" fill="{N2}" fill-opacity=".2" stroke="{N1}" stroke-width="2" stroke-linejoin="round"/>')

ART = {}

ART["welcome"] = ("Two screens linked by the pointer",
    shadow(80, 104, 62) + screen(14, 46, 62, 42) + stand(45, 88) + screen(84, 26, 64, 44, active=True) + stand(116, 70)
    + arc("M38 66C60 66 92 58 110 44") + pointer(108, 38) + sparkle(138, 16, 5) + sparkle(24, 30, 3.5, SKY) + sparkle(150, 84, 3, ACC, ".6"))

ART["role-host"] = ("A computer with the keyboard and mouse",
    shadow(80, 106, 58) + screen(38, 14, 84, 54, active=True) + stand(80, 68)
    + f'<rect x="30" y="86" width="74" height="16" rx="4" fill="{ACC}" fill-opacity=".18" stroke="{ACC}" stroke-width="2"/>'
    + "".join(f'<rect x="{36+i*10}" y="91" width="6" height="2.5" rx="1" fill="{ACC}"/>' for i in range(6))
    + f'<rect x="114" y="82" width="15" height="22" rx="7.5" fill="{ACC}" fill-opacity=".18" stroke="{ACC}" stroke-width="2"/><path d="M121.5 82v7" stroke="{ACC}" stroke-width="2"/>')

ART["role-agent"] = ("A laptop that the pointer arrives on",
    shadow(92, 106, 54) + laptop(54, 30, 76, active=True) + arc("M12 40C30 26 52 34 74 52", dashed=True) + pointer(74, 50) + sparkle(20, 66, 3.5, SKY))

ART["devices-empty"] = ("No devices paired yet",
    shadow(80, 106, 64) + laptop(18, 40, 54) + screen(100, 36, 46, 34, dashed=True) + stand(123, 70, faded=True)
    + arc("M58 54C74 40 92 40 104 50", dashed=True, color=N1, width=2) + badge(141, 34, "acc"))

ART["searching"] = ("Looking for devices on the network",
    shadow(80, 106, 40) + laptop(56, 56, 48)
    + "".join(f'<path d="M{80-r} {54-r*0.15}A{r} {r} 0 0 1 {80+r} {54-r*0.15}" fill="none" stroke="{ACC}" stroke-width="3" stroke-linecap="round" opacity="{op}"/>' for r, op in ((16, "1"), (30, ".6"), (44, ".3")))
    + sparkle(30, 30, 3, SKY) + sparkle(132, 24, 3.5, ACC, ".7"))

ART["pairing"] = ("Two devices sharing a pairing key",
    shadow(80, 106, 66) + laptop(14, 46, 44) + screen(108, 40, 42, 32) + stand(129, 72)
    + arc("M52 58H68", dashed=True, color=N1, width=2) + arc("M92 58H110", dashed=True, color=N1, width=2)
    + f'<path d="M80 34l16 6v12c0 11-7 18-16 22c-9-4-16-11-16-22V40z" fill="{NAVY}" stroke="{ACC}" stroke-width="2.5" stroke-linejoin="round"/>'
    + f'<circle cx="80" cy="51" r="4.5" fill="{SKY}"/><path d="M80 54v8" stroke="{SKY}" stroke-width="3" stroke-linecap="round"/>')

ART["paired"] = ("Devices paired",
    shadow(80, 106, 66) + laptop(14, 46, 44) + screen(108, 40, 42, 32, active=True) + stand(129, 72)
    + arc("M52 58C72 46 90 46 110 56") + badge(80, 34, "good"))

ART["calibrate"] = ("Look at each screen in turn",
    screen(10, 18, 40, 28) + screen(60, 8, 40, 28, active=True) + screen(110, 18, 40, 28)
    + arc("M80 76L80 42", dashed=True) + arc("M74 78L36 50", dashed=True, color=N1, width=2) + arc("M86 78L124 50", dashed=True, color=N1, width=2)
    + head(80, 88, 11) + f'<circle cx="80" cy="22" r="4" fill="{SKY}"/>')

ART["calibrated"] = ("Calibration saved",
    screen(10, 22, 40, 28) + screen(60, 12, 40, 28) + screen(110, 22, 40, 28)
    + "".join(f'<path d="M{x-6} {y}l4 4l8-8" fill="none" stroke="{GOOD}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>' for x, y in ((30, 36), (80, 26), (130, 36)))
    + head(80, 90, 11) + badge(104, 86, "good"))

ART["camera-off"] = ("The camera can't be opened",
    shadow(78, 100, 44) + camera(74, 60, 56, 38) + f'<path d="M38 28L118 94" stroke="{BAD}" stroke-width="3.5" stroke-linecap="round"/>' + badge(124, 34, "bad"))

ART["no-face"] = ("No face in view",
    "".join(f'<path d="{d}" fill="none" stroke="{N1}" stroke-width="2.5" stroke-linecap="round"/>' for d in ("M40 22h-10v12", "M120 22h10v12", "M40 102h-10v-12", "M120 102h10v-12"))
    + head(80, 50, 15, dashed=True) + badge(126, 30, "warn"))

ART["not-linking"] = ("Not linking",
    shadow(80, 104, 62) + screen(14, 40, 56, 38) + stand(42, 78) + screen(90, 40, 56, 38) + stand(118, 78)
    + f'<path d="M66 59h6" stroke="{N1}" stroke-width="3" stroke-linecap="round"/><path d="M88 59h6" stroke="{N1}" stroke-width="3" stroke-linecap="round"/>'
    + f'<path d="M76 52l-2-6M84 52l2-6M80 50v-7" stroke="{N1}" stroke-width="2" stroke-linecap="round"/>')

ART["waiting-for-host"] = ("Waiting for the host",
    shadow(80, 106, 64) + laptop(92, 44, 52) + screen(12, 34, 52, 36, faded=True) + stand(38, 70, faded=True)
    + "".join(f'<circle cx="{x}" cy="54" r="3.2" fill="{ACC}" opacity="{op}"/>' for x, op in ((70, ".35"), (78, ".6"), (86, "1"))))

ART["connection-lost"] = ("The connection dropped",
    shadow(80, 106, 66) + laptop(14, 46, 44) + screen(108, 40, 42, 32) + stand(129, 72)
    + arc("M52 58C60 52 66 50 72 50", color=N1, width=2.5) + arc("M88 50C96 50 102 52 110 56", color=N1, width=2.5)
    + f'<path d="M76 44l4 6l-4 6" fill="none" stroke="{WARN}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>' + badge(80, 30, "warn"))

ART["update"] = ("A new version is ready",
    shadow(80, 106, 34) + f'<rect x="50" y="26" width="60" height="60" rx="15" fill="url(#tile)" stroke="{ACC}" stroke-opacity=".5" stroke-width="1.5"/>'
    + f'<path d="M80 42v26M70 58l10 10l10-10" fill="none" stroke="{SKY}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>'
    + sparkle(122, 26, 5) + sparkle(40, 80, 3.5, SKY))

ART["diagnostics"] = ("A crash report was saved",
    shadow(80, 108, 36) + f'<path d="M54 14h38l16 16v70a4 4 0 0 1-4 4H54a4 4 0 0 1-4-4V18a4 4 0 0 1 4-4z" fill="{N2}" fill-opacity=".14" stroke="{N1}" stroke-width="2" stroke-linejoin="round"/>'
    + f'<path d="M92 14v16h16" fill="none" stroke="{N1}" stroke-width="2" stroke-linejoin="round"/>'
    + f'<path d="M58 62h10l5-12l8 24l6-14l4 2h11" fill="none" stroke="{ACC}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>'
    + "".join(f'<path d="M60 {y}h{w}" stroke="{N1}" stroke-width="2" stroke-linecap="round" opacity=".6"/>' for y, w in ((84, 36), (92, 26))))

ART["background"] = ("Curslink keeps running in the tray or menu bar",
    f'<rect x="8" y="16" width="144" height="18" rx="5" fill="{N2}" fill-opacity=".2" stroke="{N1}" stroke-width="1.5"/>'
    + f'<rect x="104" y="19" width="16" height="12" rx="3" fill="{ACC}" fill-opacity=".3"/><path d="M108 28l0-6l5 3z" fill="{ACC}"/>'
    + f'<circle cx="132" cy="25" r="2" fill="{N1}"/><circle cx="140" cy="25" r="2" fill="{N1}"/>'
    + f'<rect x="64" y="40" width="72" height="62" rx="8" fill="{N2}" fill-opacity=".14" stroke="{N1}" stroke-width="2"/>'
    + f'<rect x="70" y="47" width="60" height="10" rx="3" fill="{ACC}"/>'
    + "".join(f'<path d="M72 {y}h{w}" stroke="{N1}" stroke-width="2.5" stroke-linecap="round" opacity=".7"/>' for y, w in ((68, 40), (78, 48), (88, 32)))
    + f'<circle cx="77" cy="52" r="2" fill="#fff"/>')

for name, (label, body) in ART.items():
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="160" height="120" viewBox="0 0 160 120" role="img" aria-label="{label}">'
           f'{DEFS}{body}</svg>\n')
    open(os.path.join(OUT, f"{name}.svg"), "w").write(svg)
print(len(ART), "illustrations")
