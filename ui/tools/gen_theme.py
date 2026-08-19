# -*- coding: utf-8 -*-
"""
Generate ui/Themes/Charcoal.xaml and CharcoalLight.xaml from the live WPF-UI
theme, retinted to the web panel's palette.

Why this is generated and not hand-written
------------------------------------------
WPF-UI's Dark.xaml defines 88 Colors and 361 SolidColorBrushes, and the brushes
bind their colour with {StaticResource}, which resolves at PARSE time. So
redefining the Colors in a dictionary merged afterwards changes nothing at all:
the brushes still hold the colour they captured. Only brush overrides win, and
there are 361 of them. Overriding the 17 tokens in webui/style.css by hand
gets you a stock Fluent-dark application with a charcoal window - every
ControlFillColor*, SubtleFillColor* and the secondary/tertiary text tiers stay
Microsoft's #202020 family.

Reading the dictionary at runtime rather than parsing Dark.xaml off disk also
keeps every path relative: the source lives inside Wpf.Ui.dll, which is in
ui/runtime next to this file. Nothing points at a wpfui checkout.

The transform
-------------
Most of WPF-UI's neutrals are not opaque greys - they are white or black at low
alpha, designed to tint whatever is under them (#0FFFFFFF, #C5FFFFFF). Those
are left exactly alone: they already do the right thing once the surfaces
beneath them are charcoal, and "correcting" them would double-apply the tint.

What does get remapped is the opaque neutrals, by luma, onto the panel's own
blue-grey ramp. Chromatic colours are left alone - the accent is applied at
runtime by ApplicationAccentColorManager, and the ok/warn/danger hues are
already the ones the web panel uses.

Finally a short list of semantic keys is pinned outright, because interpolation
gets a plausible answer where the palette has an exact one.

Usage (from the repo root, after build_ui.cmd):
    .venv\\Scripts\\python.exe ui\\tools\\gen_theme.py
"""
from __future__ import print_function

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.dirname(HERE)
REPO = os.path.dirname(UI_DIR)
RUNTIME = os.path.join(UI_DIR, "runtime")
THEMES = os.path.join(UI_DIR, "Themes")

# ---------------------------------------------------------------- palettes
# Lifted from webui/style.css so the two front ends cannot drift apart.

DARK = {
    "ramp": [
        "#05090B",   # below --bg: the deepest wells, scrollbar troughs
        "#0B1013",   # --bg
        "#0F161A",   # --bg-2
        "#141E24",   # --card
        "#152026",   # --panel
        "#1C2B33",   # --panel-2 / --line-soft
        "#283B45",   # --line
        "#5F7885",   # --muted
        "#8FA6B2",   # --text-2
        "#ECF4F7",   # --text
    ],
    "pin": {
        "ApplicationBackgroundBrush": "#FF0B1013",
        "SolidBackgroundFillColorBaseBrush": "#FF0B1013",
        "SolidBackgroundFillColorSecondaryBrush": "#FF0F161A",
        "SolidBackgroundFillColorTertiaryBrush": "#FF152026",
        "SolidBackgroundFillColorQuarternaryBrush": "#FF1C2B33",
        "LayerFillColorDefaultBrush": "#FF141E24",
        "LayerFillColorAltBrush": "#FF152026",
        "CardBackgroundFillColorDefaultBrush": "#FF141E24",
        "CardBackgroundFillColorSecondaryBrush": "#FF152026",
        "CardStrokeColorDefaultBrush": "#FF283B45",
        "CardStrokeColorDefaultSolidBrush": "#FF283B45",
        "ControlStrokeColorDefaultBrush": "#FF283B45",
        "ControlStrokeColorSecondaryBrush": "#FF1C2B33",
        "DividerStrokeColorDefaultBrush": "#FF1C2B33",
        "TextFillColorPrimaryBrush": "#FFECF4F7",
        "TextFillColorSecondaryBrush": "#FF8FA6B2",
        "TextFillColorTertiaryBrush": "#FF5F7885",
        "TextFillColorDisabledBrush": "#FF3C5461",
        "TextControlBackgroundBrush": "#FF0F161A",
        "TextControlBackgroundFocusedBrush": "#FF0B1013",
        "SystemFillColorSuccessBrush": "#FF4CAF50",
        "SystemFillColorCautionBrush": "#FFFFB300",
        "SystemFillColorCriticalBrush": "#FFFF5252",
        "SystemFillColorAttentionBrush": "#FF00BCD4",
        "SystemFillColorNeutralBrush": "#FF8FA6B2",
        # InfoBar grounds: the hue at low alpha over the card, which is how
        # the web panel draws its banner. WPF-UI ships opaque olive/maroon
        # here, and on charcoal that reads as mud rather than as a warning.
        "SystemFillColorSuccessBackgroundBrush": "#1C4CAF50",
        "SystemFillColorCautionBackgroundBrush": "#1CFFB300",
        "SystemFillColorCriticalBackgroundBrush": "#1CFF5252",
        "SystemFillColorAttentionBackgroundBrush": "#1F00BCD4",
        "SystemFillColorNeutralBackgroundBrush": "#1A8FA6B2",
        # The InfoBar template keys off its OWN brushes, not the SystemFillColor
        # family, and it reaches them with DynamicResource - so overriding them
        # here wins at runtime. Left alone they stay Microsoft's opaque olive
        # and maroon, which on charcoal reads as mud rather than as a warning.
        # Alphas taken from webui/style.css verbatim - .runBanner is the
        # warn hue at 11% with a 38% border, .bad is danger at 11%/40%, .busy
        # is --accent-soft (12%) with a 35% border. Amber at 11% over #0B1013
        # genuinely computes to a dark olive; the warning reads as a warning
        # because the BORDER and ICON carry the hue, not the fill.
        "InfoBarWarningSeverityBackgroundBrush": "#1CFFB300",
        "InfoBarWarningSeverityBorderBrush": "#61FFB300",
        "InfoBarWarningSeverityIconBackground": "#FFFFB300",
        "InfoBarErrorSeverityBackgroundBrush": "#1CFF5252",
        "InfoBarErrorSeverityBorderBrush": "#66FF5252",
        "InfoBarErrorSeverityIconBackground": "#FFFF5252",
        "InfoBarSuccessSeverityBackgroundBrush": "#1C4CAF50",
        "InfoBarSuccessSeverityBorderBrush": "#614CAF50",
        "InfoBarSuccessSeverityIconBackground": "#FF4CAF50",
        "InfoBarInformationalSeverityBackgroundBrush": "#1F00BCD4",
        "InfoBarInformationalSeverityBorderBrush": "#5900BCD4",
        "InfoBarInformationalSeverityIconBackground": "#FF00BCD4",
        "InfoBarBorderBrush": "#FF283B45",
        "InfoBarTitleForeground": "#FFECF4F7",
        "InfoBarMessageForeground": "#FF8FA6B2",
    },
}

LIGHT = {
    "ramp": [
        "#FFFFFF",   # --bg-2 / --panel
        "#F4F7F8",
        "#ECEFF1",   # --bg
        "#E7EDF0",   # --panel-2
        "#CFD8DC",   # --line
        "#AFBFC6",
        "#78909C",   # --muted
        "#546E7A",   # --text-2
        "#3A4E58",
        "#263238",   # --text
    ],
    "pin": {
        "ApplicationBackgroundBrush": "#FFECEFF1",
        "SolidBackgroundFillColorBaseBrush": "#FFECEFF1",
        "SolidBackgroundFillColorSecondaryBrush": "#FFE7EDF0",
        "LayerFillColorDefaultBrush": "#FFFFFFFF",
        "CardBackgroundFillColorDefaultBrush": "#FFFFFFFF",
        "CardStrokeColorDefaultBrush": "#FFCFD8DC",
        "ControlStrokeColorDefaultBrush": "#FFCFD8DC",
        "DividerStrokeColorDefaultBrush": "#FFE3EAEE",
        "TextFillColorPrimaryBrush": "#FF263238",
        "TextFillColorSecondaryBrush": "#FF546E7A",
        "TextFillColorTertiaryBrush": "#FF78909C",
        "TextControlBackgroundBrush": "#FFFFFFFF",
        "SystemFillColorSuccessBrush": "#FF388E3C",
        "SystemFillColorCautionBrush": "#FFF57C00",
        "SystemFillColorCriticalBrush": "#FFD32F2F",
        "SystemFillColorAttentionBrush": "#FF0097A7",
        "SystemFillColorNeutralBrush": "#FF546E7A",
        "SystemFillColorSuccessBackgroundBrush": "#1F388E3C",
        "SystemFillColorCautionBackgroundBrush": "#1FF57C00",
        "SystemFillColorCriticalBackgroundBrush": "#1FD32F2F",
        "SystemFillColorAttentionBackgroundBrush": "#1F0097A7",
        "SystemFillColorNeutralBackgroundBrush": "#14546E7A",
        "InfoBarWarningSeverityBackgroundBrush": "#1FF57C00",
        "InfoBarWarningSeverityBorderBrush": "#4DF57C00",
        "InfoBarWarningSeverityIconBackground": "#FFF57C00",
        "InfoBarErrorSeverityBackgroundBrush": "#1FD32F2F",
        "InfoBarErrorSeverityBorderBrush": "#4DD32F2F",
        "InfoBarErrorSeverityIconBackground": "#FFD32F2F",
        "InfoBarSuccessSeverityBackgroundBrush": "#1F388E3C",
        "InfoBarSuccessSeverityBorderBrush": "#4D388E3C",
        "InfoBarSuccessSeverityIconBackground": "#FF388E3C",
        "InfoBarInformationalSeverityBackgroundBrush": "#1F0097A7",
        "InfoBarInformationalSeverityBorderBrush": "#4D0097A7",
        "InfoBarInformationalSeverityIconBackground": "#FF0097A7",
        "InfoBarBorderBrush": "#FFCFD8DC",
        "InfoBarTitleForeground": "#FF263238",
        "InfoBarMessageForeground": "#FF546E7A",
    },
}

def _is_accent_key(name):
    """
    Anything accent-related is left out of the generated file entirely.

    ApplicationAccentColorManager recomputes these at runtime from whatever
    accent the panel started with, and several of them (AccentButtonBackground
    and friends) are defined with {DynamicResource} onto keys that only exist
    once that manager has run. Walking the dictionaries standalone resolves
    them to nothing: AccentButtonBackground came out as #00FFFFFF - fully
    transparent - and writing that here froze it, so the primary button
    rendered as an empty outline that looked disabled.

    Substring, not prefix: the families are AccentButton*, AccentFillColor*,
    AccentTextFillColor*, SystemAccentColor*, ControlStrokeColorOnAccent*,
    LayerOnAccentAcrylic*, TextOnAccentFillColor* - the word turns up in the
    middle as often as at the start.
    """
    return "Accent" in name


def _hex_to_argb(text):
    text = text.strip().lstrip("#")
    if len(text) == 6:
        text = "FF" + text
    return (int(text[0:2], 16), int(text[2:4], 16),
            int(text[4:6], 16), int(text[6:8], 16))


def _argb_to_hex(a, r, g, b):
    return "#{0:02X}{1:02X}{2:02X}{3:02X}".format(
        int(round(a)), int(round(r)), int(round(g)), int(round(b)))


def _luma(r, g, b):
    """Rec.709 luma on gamma-encoded values: cheap, and monotonic, which is
    all the ramp lookup needs."""
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def _is_neutral(r, g, b):
    """Grey enough that retinting it is an improvement rather than damage."""
    return max(r, g, b) - min(r, g, b) <= 12


def _ramp_lookup(ramp, x):
    """Interpolate the palette ramp at luma x, in luma space."""
    points = []
    for h in ramp:
        _, r, g, b = _hex_to_argb(h)
        points.append((_luma(r, g, b), r, g, b))
    points.sort(key=lambda p: p[0])

    if x <= points[0][0]:
        return points[0][1:]
    if x >= points[-1][0]:
        return points[-1][1:]
    for i in range(len(points) - 1):
        lo, hi = points[i], points[i + 1]
        if lo[0] <= x <= hi[0]:
            span = hi[0] - lo[0]
            t = 0.0 if span <= 0 else (x - lo[0]) / span
            return (lo[1] + (hi[1] - lo[1]) * t,
                    lo[2] + (hi[2] - lo[2]) * t,
                    lo[3] + (hi[3] - lo[3]) * t)
    return points[-1][1:]


def retint(argb, palette):
    """One colour, transformed. Alpha is never touched."""
    a, r, g, b = argb

    # Translucent white/black overlays are the mechanism WPF-UI uses for
    # nearly every fill. They tint whatever is beneath, so once the surfaces
    # are charcoal they are already correct - and retinting them would apply
    # the palette twice.
    if a < 250 and (r == g == b) and (r >= 250 or r <= 5):
        return argb

    if not _is_neutral(r, g, b):
        return argb          # chromatic: accent and status hues stay put

    nr, ng, nb = _ramp_lookup(palette["ramp"], _luma(r, g, b))
    return (a, nr, ng, nb)


def collect(dark):
    """Every brush key in the live WPF-UI theme, with its colour."""
    from System.Windows import Application, ResourceDictionary  # noqa: F401
    from System.Windows.Media import (LinearGradientBrush,  # noqa: F401
                                      SolidColorBrush)
    from Wpf.Ui.Appearance import ApplicationTheme
    from Wpf.Ui.Markup import ControlsDictionary, ThemesDictionary

    td = ThemesDictionary()
    td.Theme = ApplicationTheme.Dark if dark else ApplicationTheme.Light
    merged = ResourceDictionary()
    merged.MergedDictionaries.Add(td)
    merged.MergedDictionaries.Add(ControlsDictionary())

    solids, grads = {}, {}

    def walk(rd):
        for sub in rd.MergedDictionaries:
            walk(sub)
        for key in list(rd.Keys):
            name = str(key)
            try:
                value = rd[key]
            except Exception:  # noqa: BLE001
                continue
            if isinstance(value, SolidColorBrush):
                c = value.Color
                solids[name] = (c.A, c.R, c.G, c.B)
            elif isinstance(value, LinearGradientBrush):
                stops = []
                for s in value.GradientStops:
                    c = s.Color
                    stops.append((float(s.Offset), (c.A, c.R, c.G, c.B)))
                grads[name] = {
                    "stops": stops,
                    "start": (value.StartPoint.X, value.StartPoint.Y),
                    "end": (value.EndPoint.X, value.EndPoint.Y),
                    "mapping": str(value.MappingMode),
                }

    walk(merged)
    return solids, grads


def emit(dark):
    palette = DARK if dark else LIGHT
    solids, grads = collect(dark)

    lines = [
        "<!--",
        "    GENERATED by ui/tools/gen_theme.py - do not edit by hand.",
        "",
        "    WPF-UI's own brushes bind their colour with {StaticResource},",
        "    which resolves at parse time, so overriding the Color keys in a",
        "    dictionary merged afterwards changes nothing. Every brush has to",
        "    be restated. That is why this file is long and why it is not",
        "    written by a person.",
        "",
        "    Colours come from webui/style.css, so the desktop panel and the",
        "    browser panel cannot drift apart. Regenerate after upgrading the",
        "    WPF-UI package.",
        "-->",
        "<ResourceDictionary "
        "xmlns=\"http://schemas.microsoft.com/winfx/2006/xaml/presentation\"",
        "                    "
        "xmlns:x=\"http://schemas.microsoft.com/winfx/2006/xaml\">",
        "",
    ]

    pinned = palette["pin"]
    n_pin = n_moved = n_kept = 0

    for name in sorted(solids):
        if _is_accent_key(name):
            continue
        if name in pinned:
            value = pinned[name]
            n_pin += 1
        else:
            before = solids[name]
            after = retint(before, palette)
            value = _argb_to_hex(*after)
            if tuple(int(round(v)) for v in after) == before:
                n_kept += 1
            else:
                n_moved += 1
        lines.append(
            "    <SolidColorBrush x:Key=\"{0}\" Color=\"{1}\" />".format(name, value))

    # Pins that name a brush WPF-UI does not define still belong in the
    # dictionary - they are ours, and something may DynamicResource them.
    for name in sorted(pinned):
        if name not in solids:
            lines.append(
                "    <SolidColorBrush x:Key=\"{0}\" Color=\"{1}\" />"
                .format(name, pinned[name]))
            n_pin += 1

    lines.append("")
    for name in sorted(grads):
        g = grads[name]
        if _is_accent_key(name):
            continue
        lines.append(
            "    <LinearGradientBrush x:Key=\"{0}\" x:Shared=\"false\" "
            "MappingMode=\"{1}\" StartPoint=\"{2},{3}\" EndPoint=\"{4},{5}\">"
            .format(name, g["mapping"], g["start"][0], g["start"][1],
                    g["end"][0], g["end"][1]))
        lines.append("        <LinearGradientBrush.GradientStops>")
        for offset, argb in g["stops"]:
            lines.append(
                "            <GradientStop Offset=\"{0}\" Color=\"{1}\" />"
                .format(offset, _argb_to_hex(*retint(argb, palette))))
        lines.append("        </LinearGradientBrush.GradientStops>")
        lines.append("    </LinearGradientBrush>")

    lines.append("</ResourceDictionary>")

    out = os.path.join(THEMES, "Charcoal.xaml" if dark else "CharcoalLight.xaml")
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    print("  {0}".format(os.path.relpath(out, REPO)))
    print("     {0} brushes  ({1} pinned, {2} retinted, {3} left alone), "
          "{4} gradients".format(len(solids), n_pin, n_moved, n_kept, len(grads)))


def main():
    dll = os.path.join(RUNTIME, "LiveTranscription.Ui.dll")
    cfg = os.path.join(RUNTIME, "LiveTranscription.Ui.runtimeconfig.json")
    if not os.path.isfile(dll):
        print("Run build_ui.cmd first - ui/runtime does not exist yet.")
        return 1

    from clr_loader import get_coreclr
    from pythonnet import set_runtime

    set_runtime(get_coreclr(runtime_config=cfg))
    import clr

    clr.AddReference(os.path.join(RUNTIME, "Wpf.Ui.dll"))
    for asm in ("PresentationFramework", "PresentationCore", "WindowsBase"):
        clr.AddReference(asm)

    # Resource dictionaries want an Application to resolve against.
    from System.Windows import Application

    if Application.Current is None:
        Application()

    print("generating themes from the live WPF-UI dictionary")
    emit(dark=True)
    emit(dark=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
