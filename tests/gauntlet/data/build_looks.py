"""Build looks.json: the universe of photographic looks with measurable targets.
Deterministic; re-run to regenerate. Research date 2026-09-26."""

ASOF = "2026-09-26"
LOOKS = []
KEYS = [
    "tone",
    "saturation_delta",
    "hue_shifts",
    "split_toning",
    "grey_cast",
    "black_lift",
    "white_point",
    "grain",
    "vignette",
    "clarity",
]


def S(url, as_of=ASOF, note=None):
    d = {"url": url, "as_of": as_of}
    if note:
        d["note"] = note
    return d


def look(
    id,
    family,
    name,
    origin,
    desc,
    *,
    tone=(0, 0, 0, 0),
    sat=0.0,
    hue=(0, 0, 0),
    split=(None, 0, None, 0),
    cast=(0, 0),
    black=0,
    white=100,
    grain=0,
    vig=0,
    clar=0,
    conf=None,
    default_conf="inferred",
    overall=None,
    dt=(),
    src=(),
    mono=False,
    bw=None,
    extra=None,
):
    tc = {k: default_conf for k in KEYS}
    if conf:
        tc.update(conf)
    t = {
        "tone": {
            "shadows_ev": tone[0],
            "mids_ev": tone[1],
            "highlights_ev": tone[2],
            "contrast": tone[3],
        },
        "saturation_delta": -1.0 if mono else sat,
        "hue_shifts": (
            None if mono else {"skin_deg": hue[0], "sky_deg": hue[1], "foliage_deg": hue[2]}
        ),
        "split_toning": {
            "shadow_hue": split[0],
            "shadow_sat": split[1],
            "highlight_hue": split[2],
            "highlight_sat": split[3],
        },
        "grey_cast": {"a": cast[0], "b": cast[1]},
        "black_lift": black,
        "white_point": white,
        "grain": grain,
        "vignette": vig,
        "clarity": clar,
    }
    if mono:
        t["monochrome"] = True
        t["bw_response"] = bw or {"sky_ev": 0, "foliage_ev": 0, "skin_ev": 0}
        tc["hue_shifts"] = "n/a"
    order = {"documented": 0, "community-consensus": 1, "inferred": 2, "n/a": 9}
    if overall is None:
        vals = [v for v in tc.values() if v != "n/a"]
        overall = min(vals, key=lambda v: order[v])  # best-supported signature
    d = {
        "id": id,
        "family": family,
        "name": name,
        "origin": origin,
        "description": desc,
        "targets": t,
        "confidence": overall,
        "target_confidence": tc,
        "dt_modules": list(dt),
        "sources": list(src),
    }
    if extra:
        d.update(extra)
    assert id not in {l["id"] for l in LOOKS}, id
    LOOKS.append(d)


# ---------------------------------------------------------------- sources
FUJI_SIMS = S(
    "https://www.fujifilm-x.com/global/products/film-simulation/",
    note="official one-line descriptions",
)
FUJI_CC = S(
    "https://www.fujifilm-x.com/global/stories/film-simulation-classic-chrome/", "2014-10-06"
)
IR_GUIDE = S(
    "https://www.imaging-resource.com/news/fujifilm-film-simulations-definitive-guide/",
    note="403 on fetch; claims taken from search snippet 2026-09-26",
)
JSSFR = S(
    "https://discuss.pixls.us/t/i-made-styles-which-mimic-fujifilm-film-simulations-without-luts/55181",
    "2026-01-02",
    "darktable 5.4 AgX styles for all Fuji sims except Sepia/Nostalgic Neg",
)
BASTIBE = S(
    "https://github.com/bastibe/Fujifilm-Auto-Settings-for-Darktable",
    note="lut 3d from Fujifilm RAW FILE CONVERTER; use .3dl not PNG (posterises)",
)
FXW_TOP = S(
    "https://fujixweekly.com/2026/08/03/top-26-most-popular-fujifilm-recipes-of-2026-so-far-summer-edition/",
    "2026-08-03",
)
FXW_BEST = S(
    "https://fujixweekly.com/2026/04/16/the-best-fujifilm-recipe-for-each-film-simulation/",
    "2026-04-16",
)
DT_CBRGB = S(
    "https://docs.darktable.org/usermanual/development/en/module-reference/processing-modules/color-balance-rgb/"
)
DT_GRAIN = S(
    "https://docs.darktable.org/usermanual/development/en/module-reference/processing-modules/grain/"
)

# ---------------------------------------------------------------- 1. Fujifilm film simulations
FUJI_DT = ["lut 3d (Fuji-derived .3dl)", "agx or sigmoid", "color balance rgb", "rgb curve"]
MONO_DT = ["color calibration (gray tab) or monochrome", "rgb curve", "grain"]
# base targets per sim, reused by recipes: tone(s,m,h,c), sat, hue(skin,sky,fol), split
SIM = {
    "provia": dict(tone=(0, 0, 0, 0), sat=0.0, hue=(0, 0, 0)),
    "velvia": dict(tone=(-0.25, 0, 0.1, 0.25), sat=0.35, hue=(-2, 6, -3)),
    "astia": dict(tone=(0.1, 0, -0.1, -0.1), sat=-0.05, hue=(2, 0, 0)),
    "classic_chrome": dict(tone=(-0.2, -0.05, -0.1, 0.1), sat=-0.25, hue=(3, -8, -6)),
    "pro_neg_hi": dict(tone=(-0.1, 0, 0.05, 0.1), sat=-0.1, hue=(2, 0, 0)),
    "pro_neg_std": dict(tone=(0.1, 0, -0.1, -0.15), sat=-0.15, hue=(2, 0, 0)),
    "classic_neg": dict(
        tone=(-0.25, -0.05, 0.1, 0.25), sat=0.05, hue=(-4, -6, 12), split=(200, 0.08, 60, 0.05)
    ),
    "nostalgic_neg": dict(
        tone=(-0.2, 0, 0, 0.1), sat=0.1, hue=(3, 0, -3), split=(None, 0, 70, 0.15), cast=(1, 4)
    ),
    "eterna": dict(
        tone=(0.15, 0, -0.2, -0.3), sat=-0.35, hue=(0, -4, 4), split=(190, 0.05, None, 0)
    ),
    "eterna_bb": dict(tone=(-0.3, -0.05, 0.15, 0.4), sat=-0.55, hue=(0, -4, 0)),
    "reala_ace": dict(tone=(-0.15, 0, 0.1, 0.2), sat=0.0, hue=(0, 0, 0)),
}
sim_meta = [
    ("provia", "Provia / Standard", "Ideal for a wide range of subjects.", {}),
    (
        "velvia",
        "Velvia / Vivid",
        "Vibrant reproduction for landscape/nature; higher contrast and saturation than Provia, blues nudged to magenta, extreme highlight curve.",
        {
            "saturation_delta": "documented",
            "tone": "community-consensus",
            "hue_shifts": "community-consensus",
        },
    ),
    (
        "astia",
        "Astia / Soft",
        "Softer colour and contrast for a more subdued look.",
        {"tone": "documented", "saturation_delta": "documented"},
    ),
    (
        "classic_chrome",
        "Classic Chrome",
        "Soft colour, enhanced shadow contrast; magenta removed from skies; reds/greens saturation and hue controlled; hue shift toward cyan.",
        {
            "tone": "documented",
            "saturation_delta": "documented",
            "hue_shifts": "community-consensus",
        },
    ),
    (
        "pro_neg_hi",
        "PRO Neg. Hi",
        "Portrait sim with slightly enhanced contrast.",
        {"tone": "documented"},
    ),
    (
        "pro_neg_std",
        "PRO Neg. Std",
        "Portrait sim with soft gradation and skin tones.",
        {"tone": "documented"},
    ),
    (
        "classic_neg",
        "Classic Negative",
        "Enhanced colour with hard tonality (Superia-like); cyan-leaning greens, magenta-leaning reds.",
        {
            "tone": "documented",
            "hue_shifts": "community-consensus",
            "split_toning": "community-consensus",
        },
    ),
    (
        "nostalgic_neg",
        "Nostalgic Neg.",
        "Amber-tinted highlights and rich shadow tone for a printed-photo look.",
        {"split_toning": "documented", "tone": "documented", "grey_cast": "community-consensus"},
    ),
    (
        "eterna",
        "Eterna / Cinema",
        "Soft colour and rich shadow tone for a film-look movie; very low saturation and contrast.",
        {"tone": "documented", "saturation_delta": "documented"},
    ),
    (
        "eterna_bb",
        "Eterna Bleach Bypass",
        "Low saturation and high contrast.",
        {"tone": "documented", "saturation_delta": "documented"},
    ),
    (
        "reala_ace",
        "Reala Ace",
        "Faithful colour reproduction with hard tonality.",
        {"tone": "documented", "hue_shifts": "documented"},
    ),
]
for key, name, desc, conf in sim_meta:
    b = SIM[key]
    look(
        f"fuji-sim-{key.replace('_','-')}",
        "fujifilm_film_simulation",
        name,
        "Fujifilm in-camera film simulation",
        desc,
        tone=b["tone"],
        sat=b["sat"],
        hue=b["hue"],
        split=b.get("split", (None, 0, None, 0)),
        cast=b.get("cast", (0, 0)),
        conf=conf,
        dt=FUJI_DT,
        src=[FUJI_SIMS, IR_GUIDE, JSSFR, BASTIBE] + ([FUJI_CC] if key == "classic_chrome" else []),
    )

BW_FILTERS = {  # sky/foliage/skin luminance response in EV vs unfiltered panchromatic
    "none": {"sky_ev": 0, "foliage_ev": 0, "skin_ev": 0},
    "ye": {"sky_ev": -0.5, "foliage_ev": 0.1, "skin_ev": 0.1},
    "or": {"sky_ev": -1.0, "foliage_ev": -0.1, "skin_ev": 0.25},
    "r": {"sky_ev": -1.7, "foliage_ev": -0.5, "skin_ev": 0.4},
    "g": {"sky_ev": -0.4, "foliage_ev": 0.5, "skin_ev": -0.3},
    "b": {"sky_ev": 0.5, "foliage_ev": -0.3, "skin_ev": -0.4},
}
BWF_SRC = [
    S("https://www.ilfordphoto.com/colour-filters/"),
    S("https://www.freestylephoto.com/black-and-white-filters-tutorial"),
]
for base, bname, tone, grain in [
    ("acros", "Acros", (-0.15, 0, 0.1, 0.15), 8),
    ("mono", "Monochrome", (0, 0, 0, 0), 0),
]:
    for f, fname, fdesc in [
        ("none", "", ""),
        ("ye", " + Ye filter", "slightly enhances contrast, darkens skies"),
        ("r", " + R filter", "enhances contrast, darkens skies considerably"),
        ("g", " + G filter", "pleasing skin tones in portraits"),
    ]:
        look(
            f"fuji-sim-{base}{'-'+f if f!='none' else ''}",
            "fujifilm_film_simulation",
            bname + fname,
            "Fujifilm in-camera film simulation",
            (
                "Acros: rich detail and sharpness, fine intrinsic grain that grows with ISO. "
                if base == "acros"
                else "Plain monochrome. "
            )
            + fdesc,
            tone=tone,
            grain=grain,
            mono=True,
            bw=BW_FILTERS[f],
            conf={
                "tone": "community-consensus" if base == "acros" else "inferred",
                "grain": "community-consensus" if base == "acros" else "inferred",
            },
            overall="documented" if f != "none" else None,
            dt=MONO_DT,
            src=[FUJI_SIMS, JSSFR] + (BWF_SRC if f != "none" else []),
        )
look(
    "fuji-sim-sepia",
    "fujifilm_film_simulation",
    "Sepia",
    "Fujifilm in-camera film simulation",
    "Shoots in sepia tone.",
    mono=True,
    split=(55, 0.2, 65, 0.2),
    cast=(3, 12),
    conf={"split_toning": "documented", "grey_cast": "inferred"},
    dt=MONO_DT + ["split-toning or color balance rgb 4-ways"],
    src=[FUJI_SIMS, FXW_BEST],
)

# ---------------------------------------------------------------- 2. Fuji X Weekly recipes
GRAIN = {
    "off": (0, None),
    "weak": (10, 800),
    "strong": (20, 800),
    "weak-small": (10, 800),
    "strong-small": (20, 800),
    "weak-large": (16, 1600),
    "strong-large": (28, 1600),
}
WB_PRESET_K = {
    "daylight": 5500,
    "auto": None,
    "aap": None,
    "awp": None,
    "fl1": 6500,
    "fl3": 4000,
}  # rough CCT of preset


def recipe(
    id,
    name,
    url,
    date,
    sim,
    *,
    H=0,
    Sh=0,
    color=0,
    grain="off",
    cce="off",
    ccb="off",
    wb=("auto", 0, 0),
    clarity=0,
    sharp=0,
    dr="DR100",
    ec="0",
    note="",
    src_extra=(),
    mono_filter=None,
    popular_rank=None,
):
    b = SIM.get(sim, SIM["provia"])
    kelvin, r, bl = wb
    dr_h = {"DR100": 0, "DR200": -0.08, "DR400": -0.15, "DR-Auto": -0.08, "DR-P Strong": -0.2}.get(
        dr, 0
    )
    s = round(b["tone"][0] - 0.08 * Sh, 2)
    h = round(b["tone"][2] + 0.08 * H + dr_h, 2)
    c = round(b["tone"][3] + 0.03 * (H + Sh), 2)
    sat = round(
        b["sat"] + 0.06 * color + (0.03 if cce == "strong" else 0.015 if cce == "weak" else 0), 2
    )
    # grey cast: Fuji WB shift steps (inferred ~0.6 a*/step red, ~0.7 b*/step for -blue) + kelvin vs 5500K
    k = WB_PRESET_K.get(kelvin, kelvin) if isinstance(kelvin, str) else kelvin
    kb = 0 if k is None else round((k - 5500) / 100 * 0.35, 1)
    ca, cb = round(0.6 * r, 1), round(-0.7 * bl + kb, 1)
    g, iso = GRAIN[grain]
    mono = sim in ("acros", "mono")
    sky = b["hue"][1] + (0 if ccb == "off" else 0)  # ccb deepens blue luminance, not hue
    look(
        id,
        "fuji_x_weekly_recipe",
        name,
        f"Fuji X Weekly recipe on {sim.replace('_',' ').title()}",
        note,
        tone=(s, 0 if mono else b["tone"][1], h, c),
        sat=sat,
        hue=(b["hue"][0], sky, b["hue"][2]),
        split=b.get("split", (None, 0, None, 0)),
        cast=(ca, cb),
        grain=g,
        clar=round(clarity * 0.1, 2),
        mono=mono,
        bw=BW_FILTERS.get(mono_filter or "none"),
        conf={"grey_cast": "inferred", "grain": "inferred"},
        overall="documented",
        dt=(
            MONO_DT
            if mono
            else [
                "lut 3d or base-sim style",
                "color calibration (WB shift)",
                "color balance rgb",
                "rgb curve",
                "grain",
                "local contrast",
            ]
        ),
        src=[S(url, date), FXW_TOP] + list(src_extra),
        extra={
            "recipe": {
                "film_simulation": sim,
                "highlight": H,
                "shadow": Sh,
                "color": color,
                "grain_effect": grain,
                "color_chrome": cce,
                "color_chrome_fx_blue": ccb,
                "white_balance": kelvin,
                "wb_shift_red": r,
                "wb_shift_blue": bl,
                "clarity": clarity,
                "sharpness": sharp,
                "dynamic_range": dr,
                "exposure_comp": ec,
                "recipe_confidence": "documented",
            },
            "grain_size_iso": iso,
            **({"popularity_rank_2026_summer": popular_rank} if popular_rank else {}),
        },
    )


R = recipe
R(
    "fxw-reggies-portra",
    "Reggie's Portra",
    "https://fujixweekly.com/2022/06/11/fujifilm-x-trans-iv-film-simulation-recipe-reggies-portra/",
    "2022-06-11",
    "classic_chrome",
    H=-1,
    Sh=-1,
    color=2,
    grain="weak-small",
    cce="strong",
    ccb="weak",
    wb=("auto", 2, -4),
    sharp=-2,
    dr="DR-Auto",
    note="Most popular FXW recipe; versatile warm Portra-like portrait look.",
    popular_rank=1,
)
R(
    "fxw-kodachrome-64-xt4",
    "Kodachrome 64 (X-Trans IV)",
    "https://fujixweekly.com/2020/05/27/my-fujifilm-x100v-kodachrome-64-film-simulation-recipe/",
    "2020-05-27",
    "classic_chrome",
    color=2,
    grain="weak-small",
    cce="strong",
    ccb="weak",
    wb=("daylight", 2, -5),
    clarity=3,
    sharp=1,
    dr="DR200",
    ec="0 to +2/3",
    note="Punchy warm slide look with deep shadows and high clarity.",
    popular_rank=2,
)
R(
    "fxw-kodachrome-64-xt5",
    "Kodachrome 64 (X-Trans V)",
    "https://fujixweekly.com/2022/11/28/kodachrome-64-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2022-11-28",
    "classic_chrome",
    Sh=0.5,
    color=2,
    grain="weak-small",
    cce="strong",
    wb=("daylight", 2, -5),
    clarity=3,
    sharp=1,
    dr="DR200",
    ec="0 to +2/3",
    note="FXW 'best Classic Chrome recipe'.",
    src_extra=[FXW_BEST],
    popular_rank=5,
)
R(
    "fxw-kodak-gold-200",
    "Kodak Gold 200 (X-Trans V)",
    "https://fujixweekly.com/2023/10/24/kodak-gold-200-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2023-10-24",
    "classic_chrome",
    H=-1.5,
    Sh=0.5,
    color=3,
    grain="strong-small",
    cce="weak",
    wb=("daylight", 4, -5),
    clarity=-2,
    sharp=-2,
    dr="DR400",
    ec="+2/3 to +1",
    note="Warm, yellow-leaning consumer-negative look.",
    popular_rank=3,
)
R(
    "fxw-vibrant-arizona",
    "Vibrant Arizona",
    "https://fujixweekly.com/2023/04/25/getting-a-wes-anderson-look-from-your-fujifilm-camera-4-new-film-simulation-recipes/",
    "2023-04-25",
    "classic_chrome",
    color=4,
    grain="weak-small",
    ccb="weak",
    wb=(4350, 6, -8),
    clarity=-3,
    sharp=-2,
    dr="DR-P Strong",
    ec="+2/3 to +1 1/3",
    note="Wes Anderson / Asteroid City: bright, colourful, warm, overexposed-Kodak-negative feel.",
    popular_rank=4,
)
R(
    "fxw-indoor-angouleme",
    "Indoor Angouleme",
    "https://fujixweekly.com/2023/04/25/getting-a-wes-anderson-look-from-your-fujifilm-camera-4-new-film-simulation-recipes/",
    "2023-04-25",
    "nostalgic_neg",
    H=-2,
    Sh=-2,
    color=-1,
    grain="weak-small",
    cce="strong",
    ccb="weak",
    wb=("aap", -2, -6),
    clarity=-3,
    sharp=-2,
    dr="DR400",
    note="Wes Anderson / French Dispatch interiors: warm, muted, low contrast.",
)
R(
    "fxw-portra-400-v2-xt5",
    "Kodak Portra 400 v2 (X-Trans V)",
    "https://fujixweekly.com/2022/12/16/kodak-portra-400-v2-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2022-12-16",
    "classic_chrome",
    Sh=-2,
    color=2,
    grain="strong-small",
    cce="strong",
    wb=(5200, 1, -6),
    clarity=-2,
    sharp=-2,
    dr="DR400",
    ec="+1/3 to +1",
    popular_rank=6,
)
R(
    "fxw-portra-400-xt4",
    "Kodak Portra 400 (X100V)",
    "https://fujixweekly.com/2020/06/10/fujifilm-x100v-film-simulation-kodak-portra-400/",
    "2020-06-10",
    "classic_chrome",
    H=-1,
    Sh=-2,
    color=2,
    grain="strong-small",
    cce="strong",
    ccb="weak",
    wb=("daylight", 3, -5),
    clarity=2,
    sharp=-2,
    dr="DR-Auto",
    popular_rank=18,
)
R(
    "fxw-tri-x-400",
    "Kodak Tri-X 400",
    "https://fujixweekly.com/2020/06/18/fujifilm-x100v-film-simulation-recipe-kodak-tri-x-400/",
    "2020-06-18",
    "acros",
    Sh=3,
    grain="strong-large",
    cce="strong",
    wb=("daylight", 9, -9),
    clarity=4,
    sharp=1,
    dr="DR200",
    ec="+1/3 to +1",
    mono_filter="ye",
    note="Most popular FXW B&W recipe (Anders Lindborg); gritty high-contrast; WB shift acts as a warm filter on Acros.",
    popular_rank=7,
)
R(
    "fxw-cinestill-800t",
    "CineStill 800T",
    "https://fujixweekly.com/2024/04/16/cinestill-800t-fujifilm-x-trans-v-film-simulation-recipe/",
    "2024-04-16",
    "eterna",
    Sh=2,
    color=4,
    grain="strong-large",
    cce="strong",
    ccb="weak",
    wb=("fl3", -6, -4),
    clarity=-5,
    sharp=-3,
    dr="DR400",
    note="Tungsten film under mixed light; camera cannot do halation (add bloom in post).",
    popular_rank=9,
)
R(
    "fxw-pacific-blues",
    "Pacific Blues (X-Trans V)",
    "https://fujixweekly.com/2022/12/06/pacific-blues-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2022-12-06",
    "classic_neg",
    H=-2,
    Sh=3,
    color=4,
    grain="strong-large",
    cce="strong",
    ccb="weak",
    wb=(5800, 1, -3),
    clarity=-3,
    sharp=-2,
    dr="DR400",
    note="FXW 'best Classic Negative recipe'; deep cyan-blue skies and seas.",
    src_extra=[FXW_BEST],
    popular_rank=10,
)
R(
    "fxw-fujifilm-negative",
    "Fujifilm Negative",
    "https://fujixweekly.com/2024/10/31/fujifilm-negative-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2024-10-31",
    "reala_ace",
    H=-1,
    Sh=-0.5,
    color=2,
    grain="weak-small",
    cce="strong",
    wb=(5000, 0, -2),
    clarity=-2,
    sharp=-1,
    dr="DR400",
    popular_rank=11,
)
R(
    "fxw-portra-800-v3",
    "Kodak Portra 800 v3",
    "https://fujixweekly.com/2024/02/14/kodak-portra-800-v3-fujifilm-x-t5-x-trans-v-x-e4-x-trans-iv-film-simulation-recipe/",
    "2024-02-14",
    "classic_chrome",
    H=-2,
    Sh=-0.5,
    color=3,
    grain="strong-large",
    cce="strong",
    wb=(6600, -1, -3),
    clarity=-3,
    sharp=-2,
    dr="DR400",
    popular_rank=12,
)
R(
    "fxw-reggies-superia",
    "Reggie's Superia",
    "https://fujixweekly.com/2026/03/12/reggies-superia-a-fujifilm-recipe-for-x-trans-iv-v-cameras/",
    "2026-03-12",
    "classic_neg",
    H=-2,
    Sh=-1,
    color=1,
    grain="strong-large",
    cce="strong",
    ccb="strong",
    wb=("auto", 1, -3),
    sharp=-2,
    dr="DR-Auto",
    popular_rank=14,
)
R(
    "fxw-california-summer",
    "California Summer",
    "https://fujixweekly.com/2024/06/04/california-summer-fujifilm-x100vi-x-trans-v-film-simulation-recipe/",
    "2024-06-04",
    "nostalgic_neg",
    H=-2,
    Sh=-1,
    color=4,
    grain="weak-small",
    cce="strong",
    ccb="weak",
    wb=(6700, -1, -6),
    clarity=-4,
    sharp=-2,
    dr="DR400",
    popular_rank=16,
)
R(
    "fxw-reala-ace-recipe",
    "Reala Ace (recipe)",
    "https://fujixweekly.com/2023/09/15/reala-ace-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2023-09-15",
    "classic_neg",
    H=-1.5,
    Sh=-2,
    color=2,
    grain="weak-small",
    cce="strong",
    ccb="strong",
    wb=("auto", -1, 1),
    clarity=-2,
    dr="DR400",
    popular_rank=19,
)
R(
    "fxw-pro-negative-160c",
    "PRO Negative 160C",
    "https://fujixweekly.com/2024/03/27/pro-negative-160c-fujifilm-x100vi-film-simulation-recipe/",
    "2024-03-27",
    "reala_ace",
    H=-1,
    Sh=-1,
    color=4,
    grain="weak-small",
    cce="strong",
    wb=("auto", 1, -2),
    clarity=-2,
    sharp=-1,
    dr="DR200",
    popular_rank=22,
)
R(
    "fxw-1976-kodak",
    "1976 Kodak",
    "https://fujixweekly.com/2023/08/03/1976-kodak-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2023-08-03",
    "nostalgic_neg",
    H=1.5,
    Sh=3,
    color=4,
    grain="strong-small",
    cce="strong",
    wb=("auto", -2, -4),
    clarity=-3,
    sharp=-2,
    dr="DR200",
    popular_rank=23,
)
R(
    "fxw-vintage-kodachrome",
    "Vintage Kodachrome",
    "https://fujixweekly.com/2017/10/21/my-fujifilm-x100f-vintage-kodachrome-film-simulation-recipe/",
    "2017-10-21",
    "classic_chrome",
    H=4,
    Sh=-2,
    color=4,
    grain="strong",
    wb=("auto", 2, -4),
    sharp=1,
    dr="DR200",
    ec="-1/3 to -1",
    popular_rank=24,
)
R(
    "fxw-super-hg-v2",
    "Fujicolor Super HG v2",
    "https://fujixweekly.com/2023/07/20/fujicolor-super-hg-v2-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2023-07-20",
    "classic_neg",
    H=-1,
    Sh=1,
    color=2,
    grain="weak-large",
    cce="weak",
    ccb="weak",
    wb=("awp", -3, -1),
    clarity=2,
    dr="DR400",
    popular_rank=25,
)
R(
    "fxw-mccurry-kodachrome",
    "McCurry Kodachrome",
    "https://fujixweekly.com/2024/01/25/mccurry-kodachrome-a-fujifilm-x-trans-iv-film-simulation-recipe/",
    "2024-01-25",
    "classic_chrome",
    color=2,
    grain="weak-small",
    cce="strong",
    wb=(5900, -1, 4),
    sharp=-2,
    dr="DR100",
    popular_rank=26,
)
R(
    "fxw-vision3-250d",
    "Kodak Vision3 250D",
    "https://fujixweekly.com/2023/11/23/kodak-vision3-250d-fujifilm-x-e4-x-trans-iv-film-simulation-recipe/",
    "2023-11-23",
    "eterna",
    H=3,
    Sh=4,
    color=3,
    grain="strong-small",
    cce="strong",
    wb=("fl1", -3, -1),
    clarity=-2,
    sharp=-2,
    dr="DR100",
    note="FXW 'best Eterna recipe'.",
    src_extra=[FXW_BEST],
)
R(
    "fxw-ferrania-solaris-fg-400",
    "Ferrania Solaris FG 400",
    "https://fujixweekly.com/2021/11/05/fujifilm-x-e4-x-trans-iv-film-simulation-recipe-ferrania-solaris-fg-400/",
    "2021-11-05",
    "eterna_bb",
    Sh=-2,
    color=2,
    grain="strong-small",
    cce="strong",
    ccb="weak",
    wb=(5200, 1, -6),
    clarity=-2,
    sharp=-2,
    dr="DR400",
    note="FXW 'best Eterna Bleach Bypass recipe'.",
    src_extra=[FXW_BEST],
)
R(
    "fxw-hp5-plus-400",
    "Ilford HP5 Plus 400",
    "https://fujixweekly.com/2022/03/23/fujifilm-x-trans-iv-film-simulation-recipe-ilford-hp5-plus-400/",
    "2022-03-23",
    "mono",
    H=-1,
    Sh=1,
    grain="strong-large",
    wb=("daylight", 1, -8),
    sharp=-2,
    dr="DR400",
    mono_filter="ye",
    note="FXW 'best Monochrome recipe'; WB shift acts as a yellow filter.",
    src_extra=[FXW_BEST],
)
R(
    "fxw-provia-slide",
    "Provia Slide",
    "https://fujixweekly.com/2024/04/15/provia-slide-fujifilm-x-pro2-x-trans-iii-film-simulation-recipe/",
    "2024-04-15",
    "provia",
    H=-1,
    Sh=1,
    color=3,
    grain="weak",
    wb=("fl3", -1, -8),
    sharp=-1,
    dr="DR200",
    note="FXW 'best Provia recipe'.",
    src_extra=[FXW_BEST],
)
R(
    "fxw-1970s-summer",
    "1970's Summer",
    "https://fujixweekly.com/2022/11/27/1970s-summer-fujifilm-x-t5-x-trans-v-film-simulation-recipe/",
    "2022-11-27",
    "nostalgic_neg",
    H=-2,
    Sh=-0.5,
    color=-2,
    grain="strong-large",
    cce="strong",
    ccb="strong",
    wb=(6500, -1, -4),
    clarity=-3,
    sharp=-2,
    dr="DR400",
    note="FXW 'best Nostalgic Neg recipe'.",
    src_extra=[FXW_BEST],
)
R(
    "fxw-cinestill-400d-v2",
    "CineStill 400D v2",
    "https://fujixweekly.com/2023/01/17/cinestill-400d-v2-a-fujifilm-x-trans-iv-v-film-simulation-recipe/",
    "2023-01-17",
    "astia",
    H=-2,
    color=2,
    grain="strong-small",
    cce="strong",
    ccb="weak",
    wb=("fl1", -2, 4),
    clarity=-4,
    sharp=-2,
    dr="DR200",
    note="FXW 'best Astia recipe'.",
    src_extra=[FXW_BEST],
)
R(
    "fxw-vivid-velvia",
    "Vivid Velvia (X-Trans V)",
    "https://fujixweekly.com/2025/10/28/vivid-velvia-fujifilm-x-e5-x-trans-v-film-simulation-recipe/",
    "2025-10-28",
    "velvia",
    H=-1,
    Sh=-1,
    color=4,
    grain="weak-small",
    cce="strong",
    ccb="weak",
    wb=("aap", 1, -3),
    clarity=3,
    sharp=1,
    dr="DR400",
)
R(
    "fxw-ektar-100",
    "Kodak Ektar 100 (X100V)",
    "https://fujixweekly.com/2020/08/10/fujifilm-x100v-film-simulation-recipe-kodak-ektar-100/",
    "2020-08-10",
    "classic_chrome",
    H=1,
    Sh=-2,
    color=4,
    grain="off",
    cce="strong",
    ccb="weak",
    wb=(6050, 3, 0),
    clarity=3,
    dr="DR-Auto",
    note="Developed with Thomas Schwab from real Ektar frames.",
)
R(
    "fxw-superia-800",
    "Fujicolor Superia 800",
    "https://fujixweekly.com/2020/07/27/fujifilm-x100v-film-simulation-recipe-fujicolor-superia-800/",
    "2020-07-27",
    "classic_neg",
    H=-1,
    Sh=1,
    color=-1,
    grain="strong-large",
    cce="strong",
    ccb="weak",
    wb=("daylight", -1, -3),
    clarity=-4,
    sharp=-1,
    dr="DR400",
)

exec(open(__file__.replace("build_looks.py", "build_looks_part2.py")).read())
