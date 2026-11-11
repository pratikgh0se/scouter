# part 3: film stocks, cinematic grades, creators/LUT families, B&W styles, analog artefacts, output

# ---------------------------------------------------------------- 4. film stocks
FILM_DT = ["lut 3d (film LUT) or agx/sigmoid + color balance rgb", "rgb curve", "grain"]
PORTRA_DS = S("https://kodakprofessional.com/sites/default/files/2025-07/e4050.pdf", "2025-07")
PEVA = S(
    "https://github.com/peva3/Lightroom-Presets/blob/main/research/Kodak-Portra-400/datasheet.md",
    note="gamma ~0.55-0.60, RMS ~8; Portra 160 RMS ~6, 800 RMS ~10, Pro 400H RMS ~7",
)
MAXK = S("https://www.maxkent.co.uk/blogg/kodak-ektar-100-vs-kodak-portra-400-whats-the-difference")
KUBUS = S("https://www.kubusphoto.com/blog/best-film-for-portraits")
CASUAL = S("https://casualphotophile.com/2018/10/29/kodak-film-guide/", "2018-10-29")
BH_SLIDE = S(
    "https://www.bhphotovideo.com/c/compare/FUJIFILM_Velvia_50_vs_Kodak_E100_vs_FUJIFILM_Provia_100F/BHitems/1131176-REG_274846-USA_181489-USA",
    note="RMS: Velvia 50 = 9, Provia 100F = 8, E100 = 8",
)
VELVIA_WIKI = S("https://en.wikipedia.org/wiki/Velvia")
KCHROME = S("https://photography-fyi.com/film/kodak-kodachrome-64")
KCHROME2 = S("https://x-alchemy.app/academy/film-look/kodachrome-look")
CINESTILL = S("https://en.wikipedia.org/wiki/Cinestill")
CS_LOOK = S("https://thebostonfilm.com/blog/the-cinestill-look/")
BW_CMP = S("https://shootitwithfilm.com/kodak-tri-x-ilford-hp5-kodak-tmax-comparisons/")
HP5_CMP = S("https://www.kubusphoto.com/blog/ilford-hp5-vs-tri-x")
DELTA = S("https://www.analog.cafe/r/ilford-delta-400-film-review-b316")
LOMO = S("https://www.lomography.com/school/what-are-lomochrome-films-fa-g3ebpber")
AERO = S("https://www.analog.cafe/r/shooting-kodak-aerochrome-vs-lomochrome-purple-7sid")
F = lambda id, name, desc, src, dt=FILM_DT, **kw: look(
    "film-" + id, "film_stock", name, "Film stock emulation", desc, dt=dt, src=src, **kw
)
F(
    "portra-160",
    "Kodak Portra 160",
    "Lowest-contrast Portra; very fine grain (RMS ~6); neutral-warm pastel skin.",
    [PEVA, CASUAL],
    tone=(0.2, 0.05, -0.15, -0.25),
    sat=-0.15,
    hue=(3, -3, -4),
    cast=(1, 3),
    black=2,
    grain=6,
    conf={"grain": D, "tone": C, "saturation_delta": C},
    extra={"grain_size_iso": 200},
)
F(
    "portra-400",
    "Kodak Portra 400",
    "Gamma ~0.55-0.60 (low contrast), RMS ~8, slightly warm bias favouring flesh tones, wide latitude; commonly shot +1 stop.",
    [PORTRA_DS, PEVA, MAXK, KUBUS],
    tone=(0.2, 0.1, -0.1, -0.2),
    sat=-0.1,
    hue=(3, -4, -5),
    cast=(1, 4),
    black=2,
    grain=10,
    conf={"tone": D, "grain": D, "grey_cast": D, "saturation_delta": C},
    extra={"grain_size_iso": 400},
)
F(
    "portra-800",
    "Kodak Portra 800",
    "More contrast/saturation than 400, warmer, RMS ~10.",
    [PEVA, CASUAL],
    tone=(0.1, 0.05, -0.1, -0.1),
    sat=0.0,
    hue=(3, -4, -4),
    cast=(2, 5),
    black=2,
    grain=16,
    conf={"grain": D},
    extra={"grain_size_iso": 800},
)
F(
    "ektar-100",
    "Kodak Ektar 100",
    "Most saturated C-41 colour, high contrast, finest grain; reds can be unflattering on skin.",
    [MAXK, KUBUS, S("https://www.daydreamfilm.app/blog/film-reviews/kodak-ektar-100-review")],
    tone=(-0.2, 0, 0.1, 0.25),
    sat=0.35,
    hue=(-4, 4, -2),
    grain=4,
    conf={"saturation_delta": C, "tone": C, "grain": C},
    extra={"grain_size_iso": 100},
)
F(
    "gold-200",
    "Kodak Gold 200",
    "Warm/yellow consumer film between Portra and Ektar in saturation and contrast; grainier than Portra 400.",
    [
        MAXK,
        S(
            "https://www.maxkent.co.uk/blogg/kodak-portra-400-vs-kodak-gold-200-whats-the-difference"
        ),
        KUBUS,
    ],
    tone=(-0.05, 0.05, 0, 0.1),
    sat=0.15,
    hue=(5, -3, -6),
    cast=(1, 8),
    grain=18,
    conf={"grey_cast": C, "saturation_delta": C, "grain": C},
)
F(
    "colorplus-200",
    "Kodak ColorPlus 200",
    "Budget warm stock; like grainier Ektar without the red skin problem.",
    [S("https://casualphotophile.com/2018/10/29/kodak-film-guide/")],
    tone=(-0.05, 0, 0, 0.1),
    sat=0.15,
    hue=(4, -2, -5),
    cast=(1, 7),
    grain=22,
)
F(
    "ultramax-400",
    "Kodak UltraMax 400",
    "Punchy warm consumer 400 speed.",
    [CASUAL],
    tone=(-0.1, 0, 0.05, 0.15),
    sat=0.15,
    hue=(3, 0, -4),
    cast=(1, 5),
    grain=22,
)
F(
    "ektachrome-e100",
    "Kodak Ektachrome E100",
    "Slide: natural colour, moderately enhanced saturation, cool-neutral, RMS 8; Euphoria S2 stock.",
    [BH_SLIDE, S("https://metergeist.com/film/kodak-ektachrome-e100")],
    tone=(-0.25, 0, 0.1, 0.3),
    sat=0.15,
    hue=(0, 2, 0),
    cast=(-1, -3),
    grain=5,
    conf={"saturation_delta": D, "grain": D},
)
F(
    "kodachrome-64",
    "Kodak Kodachrome 64",
    "Rich magenta-leaning reds, deep saturated blues with velvet shadow cast, amber skin, deep solid blacks, crisp contrast, extremely fine grain; greens slightly muted.",
    [KCHROME, KCHROME2],
    tone=(-0.3, 0, 0.05, 0.3),
    sat=0.15,
    hue=(3, 3, -3),
    split=(260, 0.05, None, 0),
    grain=4,
    conf={"tone": C, "hue_shifts": C, "split_toning": C},
)
F(
    "velvia-50",
    "Fujichrome Velvia 50",
    "Very high saturation, moderate-to-high contrast, narrow ±½ stop latitude, RMS 9; magenta-leaning blues, vivid greens.",
    [
        VELVIA_WIKI,
        BH_SLIDE,
        S("https://www.analog.cafe/r/fujifilm-fujichrome-velvia-50-film-review-iv04"),
    ],
    tone=(-0.35, 0, 0.15, 0.35),
    sat=0.45,
    hue=(-3, 6, -4),
    grain=6,
    conf={"saturation_delta": D, "tone": D, "grain": D, "hue_shifts": C},
)
F(
    "provia-100f",
    "Fujichrome Provia 100F",
    "Slide between Ektachrome and Velvia in saturation; RMS 8; neutral.",
    [S("https://en.wikipedia.org/wiki/Provia"), BH_SLIDE],
    tone=(-0.25, 0, 0.1, 0.25),
    sat=0.15,
    grain=5,
    conf={"saturation_delta": D, "grain": D},
)
F(
    "pro-400h",
    "Fujifilm Pro 400H",
    "Discontinued pastel pro film; cyan-green shifted greens, airy highlights, RMS ~7; wedding 'light & airy' staple.",
    [PEVA, S("https://mastinlabs.com/blogs/photoism/vsco-alternative")],
    tone=(0.25, 0.15, -0.05, -0.25),
    sat=-0.15,
    hue=(0, -4, 12),
    split=(185, 0.06, None, 0),
    black=3,
    grain=8,
    conf={"hue_shifts": C, "grain": D},
)
F(
    "superia-400",
    "Fujicolor Superia 400",
    "Consumer Fuji: green/cyan bias, punchy, visible grain; Classic Negative's model.",
    [
        S(
            "https://fujixweekly.com/2020/07/27/fujifilm-x100v-film-simulation-recipe-fujicolor-superia-800/"
        )
    ],
    tone=(-0.15, 0, 0.05, 0.2),
    sat=0.1,
    hue=(-3, -5, 10),
    split=(190, 0.08, None, 0),
    grain=20,
    conf={"hue_shifts": C},
)
F(
    "cinestill-800t",
    "CineStill 800T",
    "Vision3 500T with remjet removed: tungsten 3200K balance → cool/blue cast in daylight/LED, red-orange halation around highlights, moderate grain, slightly oversaturated reds.",
    [CINESTILL, CS_LOOK, S("https://www.kubusphoto.com/blog/cinestill-800t-guide")],
    tone=(0, 0, -0.05, 0.05),
    sat=0.05,
    hue=(-2, -5, 8),
    split=(200, 0.1, None, 0),
    cast=(-2, -10),
    grain=22,
    dt=FILM_DT + ["bloom/soften with parametric highlight mask + red tint (halation)"],
    conf={"grey_cast": D, "grain": C},
    extra={
        "halation": {
            "threshold_ev": 2.5,
            "radius_px_at_24mp": 12,
            "hue_deg": 30,
            "confidence": "documented (direction); inferred (numbers)",
        }
    },
)
F(
    "cinestill-50d",
    "CineStill 50D",
    "Vision3 50D, daylight, very fine grain, clean cinematic colour, halation on speculars.",
    [CINESTILL],
    tone=(0.1, 0, -0.1, -0.1),
    sat=0.0,
    grain=6,
    extra={
        "halation": {
            "threshold_ev": 3.0,
            "radius_px_at_24mp": 8,
            "hue_deg": 30,
            "confidence": "community-consensus",
        }
    },
)
F(
    "vision3-500t",
    "Kodak Vision3 500T (ECN-2)",
    "Motion-picture tungsten negative; soft contrast, cool daylight rendering; Euphoria S2 second stock.",
    [S("https://nofilmschool.com/kodak-ektachrome-season-2-euphoria")],
    tone=(0.15, 0, -0.15, -0.2),
    sat=-0.05,
    cast=(-1, -8),
    grain=18,
)
F(
    "tri-x-400",
    "Kodak Tri-X 400",
    "Iconic visible grain, gritty street look, crisp contrast, higher base fog.",
    [BW_CMP, HP5_CMP],
    mono=True,
    tone=(-0.2, 0, 0.1, 0.25),
    grain=30,
    black=2,
    bw=BW_FILTERS["none"],
    dt=MONO_DT,
    conf={"grain": C, "tone": C, "black_lift": C},
    extra={"grain_size_iso": 1600},
)
F(
    "tmax-100",
    "Kodak T-Max 100",
    "Tabular grain: very fine grain, clean, high sharpness.",
    [BW_CMP],
    mono=True,
    tone=(-0.1, 0, 0.1, 0.15),
    grain=5,
    dt=MONO_DT,
)
F(
    "tmax-400",
    "Kodak T-Max 400",
    "Finest-grained 400 B&W; technically clean, less character than Tri-X.",
    [BW_CMP, S("https://shootitwithfilm.com/kodak-tri-x-400-vs-kodak-t-max-400/")],
    mono=True,
    tone=(-0.1, 0, 0.1, 0.15),
    grain=10,
    dt=MONO_DT,
    conf={"grain": C},
)
F(
    "hp5-plus",
    "Ilford HP5 Plus",
    "Lower base fog, longer toe (more deep-shadow detail), sandpaper-like grain.",
    [HP5_CMP, BW_CMP],
    mono=True,
    tone=(0.05, 0, 0.05, 0.1),
    grain=25,
    dt=MONO_DT,
    conf={"tone": C, "grain": C},
    extra={"grain_size_iso": 1600},
)
F(
    "delta-100",
    "Ilford Delta 100",
    "Finest grain of the Delta range; blends contrast and tonality.",
    [
        S(
            "https://petapixel.com/2026/07/03/film-friday-ilford-delta-100-perfectly-blends-contrast-and-tonality/",
            "2026-07-03",
        )
    ],
    mono=True,
    tone=(-0.1, 0, 0.1, 0.15),
    grain=5,
    dt=MONO_DT,
)
F(
    "delta-400",
    "Ilford Delta 400",
    "Fine-grain mild-contrast T-grain 400; smoother highlights and better shadow detail than HP5.",
    [DELTA],
    mono=True,
    tone=(0.05, 0, 0, 0.05),
    grain=12,
    dt=MONO_DT,
    conf={"tone": C},
)
F(
    "delta-3200",
    "Ilford Delta 3200",
    "Very grainy, low-contrast high-speed B&W.",
    [S("https://mastinlabs.com/blogs/photoism/vsco-alternative")],
    mono=True,
    tone=(0.1, 0, -0.1, -0.1),
    grain=45,
    black=3,
    dt=MONO_DT,
)
F(
    "lomochrome-purple",
    "LomoChrome Purple",
    "Blue and green dye layers swapped: greens/yellows → pinks/purples; blue skies → greenish.",
    [
        LOMO,
        S(
            "https://www.lomography.com/magazine/353954-guide-to-lomography-color-shifting-film-stocks-lomochrome-purple"
        ),
    ],
    hue=(0, -80, 170),
    sat=0.2,
    grain=18,
    dt=["channel mixer rgb (swap G/B)", "color zones", "lut 3d"],
    conf={"hue_shifts": D},
)
F(
    "lomochrome-turquoise",
    "LomoChrome Turquoise",
    "Red and blue dye layers swapped: blue skies → orange/gold, warm tones → turquoise.",
    [LOMO],
    hue=(140, -170, 0),
    sat=0.1,
    grain=18,
    dt=["channel mixer rgb (swap R/B)", "lut 3d"],
    conf={"hue_shifts": D},
)
F(
    "lomochrome-metropolis",
    "LomoChrome Metropolis",
    "Desaturated colours with high contrast; muted, 'urban'.",
    [LOMO],
    tone=(-0.25, 0, 0.1, 0.3),
    sat=-0.5,
    hue=(0, 0, 0),
    grain=18,
    conf={"tone": D, "saturation_delta": D},
)
F(
    "aerochrome",
    "Kodak Aerochrome (IR)",
    "False-colour infrared: foliage red/pink, skies deep blue; discontinued 2011.",
    [
        AERO,
        LOMO,
        S(
            "https://fujixweekly.com/2024/08/29/aerochrome-v2-fujifilm-x-t4-es-full-spectrum-infrared-film-simulation-recipe/",
            "2024-08-29",
        ),
    ],
    hue=(0, 0, -110),
    sat=0.3,
    tone=(-0.1, 0, 0.1, 0.2),
    grain=12,
    dt=["channel mixer rgb", "color zones", "lut 3d"],
    conf={"hue_shifts": D},
)
F(
    "expired-colour-negative",
    "Expired colour negative",
    "Fog raises blacks, contrast and saturation drop, colour shifts (often magenta/yellow or cyan), more grain.",
    [
        S(
            "https://danfinnen.com/article/how-to-create-the-polaroid-look-a-glossary-and-guide-to-instant-film-defects-and-manipulation/"
        )
    ],
    tone=(0.2, 0, -0.1, -0.25),
    sat=-0.3,
    black=10,
    white=94,
    cast=(4, 6),
    grain=28,
)

# ---------------------------------------------------------------- 5. cinematic & creator grades
GRADE_DT = ["color balance rgb (4 ways)", "sigmoid/agx contrast", "rgb curve", "color zones"]
TEAL_ORANGE = S("https://pixflow.net/blog/teal-and-orange-color-grading/")
SKINLINE = S(
    "https://pixelvalleystudio.com/pmf-articles/the-skin-tone-line",
    note="vectorscope skin line ~123° (Rec.709 scope) between red and yellow",
)
BLEACH = S("https://en.wikipedia.org/wiki/Bleach_bypass")
BORIS = S("https://borisfx.com/blog/what-is-bleach-bypass-complete-guide-film-technique/")
STYLES = S("https://tovstudiophoto.com/photography-editing-styles-compared/")
AIRY = S("https://imagen-ai.com/valuable-tips/light-and-airy-lightroom-presets/")
MOODY = S("https://thelenslounge.com/dark-and-moody-photography-style/")
Cg = lambda id, name, desc, src, dt=GRADE_DT, **kw: look(
    "grade-" + id, "cinematic_grade", name, "Colour grading convention", desc, dt=dt, src=src, **kw
)
Cg(
    "teal-orange",
    "Teal and orange",
    "Complementary split: teal shadows/backgrounds, orange highlights and skin; skin kept on the vectorscope skin line.",
    [TEAL_ORANGE, SKINLINE],
    tone=(-0.1, 0, 0.05, 0.15),
    sat=0.05,
    hue=(2, -15, 25),
    split=(195, 0.2, 55, 0.15),
    conf={"split_toning": D, "hue_shifts": C},
)
Cg(
    "bleach-bypass",
    "Bleach bypass",
    "Retained silver: +contrast (mids/shadows densified), −saturation, +grain, darker overall (Saving Private Ryan).",
    [BLEACH, BORIS, S("https://theasc.com/magazine/nov98/soupdujour/pg2.htm", "1998-11")],
    tone=(-0.4, -0.2, 0.05, 0.45),
    sat=-0.55,
    grain=18,
    conf={"tone": D, "saturation_delta": D, "grain": D},
)
Cg(
    "cross-process",
    "Cross-process (E-6 in C-41)",
    "Slide film in negative chemistry: high contrast, yellow-green highlights, cyan/blue shadows, strong saturation.",
    [
        S(
            "https://www.lomography.com/school/which-color-filter-should-i-use-for-black-and-white-film-photography-fa-rlmak3lm"
        )
    ],
    tone=(-0.25, 0, 0.2, 0.35),
    sat=0.3,
    split=(210, 0.2, 100, 0.2),
    cast=(-4, 10),
    hue=(5, -10, -10),
    conf={"split_toning": C},
)
Cg(
    "matte-fade",
    "Matte fade / lifted blacks",
    "Tone curve toe lifted so pure black never reaches 0; lower contrast; often slight colour in the lifted black.",
    [S("https://imagen-ai.com/post/dark-lightroom-presets/")],
    tone=(0.15, 0, -0.05, -0.15),
    sat=-0.1,
    black=10,
    white=96,
    conf={"black_lift": C},
)
Cg(
    "dark-moody",
    "Dark and moody",
    "Histogram shifted left, deep shadows, restrained highlights, desaturated greens/yellows, earthy hue, often slight lifted black.",
    [STYLES, MOODY, S("https://imagen-ai.com/post/dark-lightroom-presets/")],
    tone=(-0.4, -0.5, -0.3, 0.2),
    sat=-0.25,
    hue=(3, -5, -12),
    black=3,
    vig=-0.5,
    conf={"tone": C, "saturation_delta": C},
)
Cg(
    "light-airy",
    "Light and airy",
    "Bright exposure, low contrast, pastel palette, desaturated greens, gentle warm highlights, soft shadows.",
    [STYLES, AIRY, S("https://presets.io/a/blog/lightroom-color-toning-bright-and-airy-explained")],
    tone=(0.4, 0.4, 0.1, -0.3),
    sat=-0.2,
    hue=(3, -3, -10),
    split=(None, 0, 65, 0.06),
    black=5,
    cast=(0, 2),
    conf={"tone": C, "saturation_delta": C},
)
Cg(
    "golden-hour-warm",
    "Golden hour warm",
    "Warm amber global cast, glowing highlights, soft contrast.",
    [S("https://leica-camera.com/en-US/photography/leica-looks")],
    tone=(0.05, 0.05, 0.05, 0),
    sat=0.1,
    split=(None, 0, 60, 0.2),
    cast=(3, 12),
    hue=(4, 0, -6),
)
Cg(
    "cool-clinical",
    "Cool clinical",
    "Cool/cyan cast, neutral-to-high contrast, low saturation, clean whites (tech/medical/thriller).",
    [TEAL_ORANGE],
    tone=(-0.1, 0, 0.1, 0.15),
    sat=-0.3,
    cast=(-3, -8),
    split=(210, 0.08, 200, 0.05),
)
Cg(
    "high-key",
    "High-key",
    "Mostly bright tones, minimal shadows, low contrast.",
    [STYLES],
    tone=(0.8, 0.7, 0.2, -0.3),
    sat=-0.1,
    black=6,
)
Cg(
    "low-key",
    "Low-key",
    "Mostly dark tones, single light source, deep blacks, preserved highlights.",
    [MOODY],
    tone=(-0.8, -0.8, 0, 0.3),
    vig=-0.6,
)
Cg(
    "pastel",
    "Pastel",
    "High luminance, low chroma, lifted blacks, soft pinks/mints/lavenders.",
    [AIRY],
    tone=(0.3, 0.3, 0, -0.35),
    sat=-0.4,
    black=8,
    white=97,
)
Cg(
    "desaturated-earth",
    "Desaturated earth tones",
    "Muted palette pulled toward olive/tan/brown; greens → olive, blues muted.",
    [S("https://www.indiewire.com/features/general/dune-cinematography-color-imax-1234711042/")],
    tone=(0, 0, -0.05, 0.05),
    sat=-0.4,
    hue=(3, -10, -15),
    cast=(1, 4),
)
Cg(
    "wes-anderson",
    "Wes Anderson",
    "Controlled, often warm primary palettes (salmon pink, mustard, sage, dusty teal); 'pastel' is a meme — Moonrise Kingdom is warm/saturated. Look is mostly art direction + symmetry.",
    [
        S(
            "https://medium.com/@secretsocietyofnewpenzance/the-persistent-myth-of-wes-andersons-pastel-palette-a3b51643fa59"
        ),
        S("https://pixflow.net/blog/the-ultimate-guide-to-wes-andersons-color-palette/"),
        S(
            "https://fujixweekly.com/2023/04/25/getting-a-wes-anderson-look-from-your-fujifilm-camera-4-new-film-simulation-recipes/",
            "2023-04-25",
        ),
    ],
    tone=(0.1, 0.15, 0, -0.1),
    sat=0.1,
    split=(None, 0, 65, 0.1),
    cast=(2, 8),
    hue=(3, -6, -8),
)
Cg(
    "blade-runner-2049-orange",
    "Blade Runner 2049 (Las Vegas orange)",
    "Near-monochromatic orange haze: one hue dominates, very low contrast, lifted haze blacks.",
    [
        S("https://filmmakermagazine.com/103272-the-color-of-the-future/"),
        S("https://www.rkcolor.com/blog/look-builds-blade-runner-2049/"),
    ],
    tone=(0.3, 0, -0.2, -0.4),
    sat=-0.2,
    split=(50, 0.3, 65, 0.35),
    cast=(10, 25),
    black=12,
    conf={"split_toning": C},
)
Cg(
    "blade-runner-2049-night",
    "Blade Runner 2049 (LA night)",
    "Cool cyan/blue night with magenta/pink and amber practicals; contrasty, deep blacks.",
    [S("https://www.premiumbeat.com/blog/symmetry-color-cinematography-blade-runner/")],
    tone=(-0.3, -0.3, 0.1, 0.25),
    sat=0.0,
    split=(215, 0.2, 320, 0.08),
    cast=(-2, -8),
)
Cg(
    "dune",
    "Dune (Greig Fraser)",
    "Deliberately unsaturated: skies not blue, rocks not red, sand not golden; custom LUT removes sky blue; soft contrast.",
    [
        S("https://www.indiewire.com/features/general/dune-cinematography-color-imax-1234711042/"),
        S(
            "https://postperspective.com/cinematographer-greig-fraser-on-dunes-digital-film-process/"
        ),
    ],
    tone=(0.05, 0, -0.1, -0.05),
    sat=-0.45,
    hue=(0, -15, -5),
    cast=(1, 5),
    grain=10,
    conf={"saturation_delta": D, "hue_shifts": D},
)
Cg(
    "euphoria",
    "Euphoria",
    "Saturated purples/blues with green/yellow undertones; S2 on Ektachrome + Vision3 500T: rich colour, fine grain, glitter/neon practicals.",
    [
        S("https://nofilmschool.com/kodak-ektachrome-season-2-euphoria"),
        S("https://theasc.com/article/euphoria-rev/"),
        S("https://www.yellowbrick.co/blog/film/the-visual-dynamics-of-euphoria"),
    ],
    tone=(-0.2, -0.2, 0.05, 0.2),
    sat=0.25,
    split=(280, 0.18, 320, 0.1),
    cast=(3, -8),
    grain=12,
    conf={"split_toning": C, "saturation_delta": C},
)
Cg(
    "matrix-green",
    "Matrix green",
    "Green cast in the 'Matrix' world, desaturated, contrasty.",
    [TEAL_ORANGE],
    tone=(-0.2, 0, 0.05, 0.25),
    sat=-0.3,
    cast=(-10, 4),
    split=(150, 0.15, 110, 0.12),
)
Cg(
    "mad-max-fury-road",
    "Mad Max: Fury Road",
    "Hyper-saturated orange desert vs deep teal sky, crushed shadows.",
    [TEAL_ORANGE],
    tone=(-0.3, 0, 0.1, 0.35),
    sat=0.45,
    split=(200, 0.2, 50, 0.25),
    hue=(5, -10, 0),
)
Cg(
    "day-for-night",
    "Day for night",
    "Under 1.5-2.5 EV, blue cast, desaturated, highlights kept as moonlight.",
    [TEAL_ORANGE],
    tone=(-1.5, -2.0, -1.5, 0.1),
    sat=-0.5,
    cast=(-2, -15),
)
Cg(
    "dark-academia",
    "Dark academia",
    "Warm browns, deep greens, low key, soft grain, vignette.",
    [NIKON_PHOB],
    tone=(-0.3, -0.3, -0.2, 0.1),
    sat=-0.2,
    cast=(2, 6),
    hue=(3, -8, -10),
    grain=10,
    vig=-0.5,
)
Cg(
    "neon-noir",
    "Neon noir / cyberpunk",
    "Magenta and cyan practicals on crushed blacks; saturated.",
    [S("https://thebostonfilm.com/blog/the-cinestill-look/")],
    tone=(-0.4, -0.3, 0.1, 0.35),
    sat=0.35,
    split=(200, 0.15, 320, 0.15),
)

CR = lambda id, name, desc, src, dt=GRADE_DT, **kw: look(
    "creator-" + id,
    "creator_signature",
    name,
    "Named creator style (as publicly described)",
    desc,
    dt=dt,
    src=src,
    **kw,
)
CR(
    "peter-mckinnon",
    "Peter McKinnon",
    "Moody: very deep blacks, desaturated greens/yellows so warm skin and reds pop; orange/teal leaning.",
    [S("https://imagen-ai.com/valuable-tips/peter-mckinnon-lightroom/")],
    tone=(-0.35, -0.1, -0.1, 0.25),
    sat=-0.15,
    hue=(2, -8, -15),
    split=(200, 0.1, 50, 0.08),
    black=2,
    vig=-0.4,
    clar=0.2,
)
CR(
    "brandon-woelfel",
    "Brandon Woelfel",
    "Night fairy-light portraits: crushed blacks, cotton-candy pinks/purples, blue-tinted whites, turquoise/aqua.",
    [
        S(
            "https://petapixel.com/2017/08/01/recreating-brandon-woelfels-editing-style-lightroom-photoshop/",
            "2017-08-01",
        ),
        S(
            "https://pomegranatesandstories.com/blog/2017/06/13/deconstructing-photography-brandon-woelfel",
            "2017-06-13",
        ),
    ],
    tone=(-0.2, 0.05, 0, 0.1),
    sat=0.1,
    split=(270, 0.18, 200, 0.1),
    cast=(2, -8),
    hue=(-8, 0, 30),
    black=4,
    conf={"split_toning": C, "grey_cast": C},
)
CR(
    "sam-kolder",
    "Sam Kolder",
    "Desaturated teal-and-orange travel grade; high contrast, crisp.",
    [
        S(
            "https://fstoppers.com/education/how-edit-your-pictures-instagrammer-sam-kolder-using-lightroom-198606"
        )
    ],
    tone=(-0.2, 0, 0.1, 0.25),
    sat=-0.15,
    split=(195, 0.18, 50, 0.15),
    hue=(2, -12, 25),
    clar=0.2,
    conf={"split_toning": C},
)
CR(
    "nigel-danson",
    "Nigel Danson (landscape)",
    "Natural colour with warmth in highlights and coolness in shadows; restrained.",
    [
        S(
            "https://www.imaging-resource.com/news/2022/07/19/video-a-simple-guide-to-landscape-photography-color-editing",
            "2022-07-19",
        ),
        S("https://www.drlphotography.co.uk/articles-homepage/editing-nigel-dansons-images"),
    ],
    split=(225, 0.06, 60, 0.06),
    sat=0.05,
    clar=0.1,
    conf={"split_toning": D},
)
CR(
    "fan-ho",
    "Fan Ho (street B&W)",
    "High-contrast B&W: shafts of backlight, deep blacks, bright whites, smoke/haze, heavy dodging/burning.",
    [
        S("https://aboutphotography.blog/photographer/fan-ho"),
        S("https://www.mplus.org.hk/en/magazine/ho-fan-on-the-decisive-moment/"),
    ],
    mono=True,
    tone=(-0.6, -0.1, 0.2, 0.5),
    vig=-0.6,
    grain=12,
    dt=MONO_DT + ["tone equalizer", "drawn masks (dodge/burn)", "vignetting"],
    conf={"tone": C},
)
CR(
    "steve-mccurry",
    "Steve McCurry (Kodachrome era)",
    "Saturated warm Kodachrome colour, deep reds and blues, rich shadows.",
    [
        KCHROME,
        S(
            "https://fujixweekly.com/2024/01/25/mccurry-kodachrome-a-fujifilm-x-trans-iv-film-simulation-recipe/",
            "2024-01-25",
        ),
    ],
    tone=(-0.3, 0, 0.05, 0.3),
    sat=0.2,
    hue=(3, 3, -3),
    cast=(1, 3),
)
CR(
    "daido-moriyama",
    "Daido Moriyama (are-bure-boke)",
    "Grainy, blurry, out-of-focus, extreme-contrast B&W.",
    [S("https://www.grblog.jp/en/article/2192/")],
    mono=True,
    tone=(-0.7, 0, 0.4, 0.7),
    grain=50,
    vig=-0.5,
    dt=MONO_DT + ["vignetting"],
)
CR(
    "saul-leiter",
    "Saul Leiter (early colour)",
    "Muted, painterly early Kodachrome/expired colour: soft, low contrast, reds as accents.",
    [KCHROME],
    tone=(0.1, 0, -0.1, -0.15),
    sat=-0.2,
    black=5,
    grain=12,
)

LUTV = S(
    "https://www.popphoto.com/reviews/the-best-film-emulation-presets-for-your-digital-photos/"
)
MASTIN = S("https://mastinlabs.com/blogs/photoism/vsco-alternative")
RNI = S("https://www.35mmc.com/25/07/2016/really-nice-images-review/", "2016-07-25")
LP = lambda id, name, desc, src, **kw: look(
    "lutpack-" + id,
    "lut_pack_family",
    name,
    "Commercial preset / LUT family",
    desc,
    dt=["lut 3d", "color balance rgb", "grain"],
    src=src,
    extra={"category_entry": True},
    **kw,
)
LP(
    "mastin-labs",
    "Mastin Labs (Kodak/Fuji/Ilford packs)",
    "Encodes Portra 160/400, Fuji 400H/160, Ilford Pan F/HP5/Delta 3200 as scanned on a Frontier; scanner-style tone/colour controls.",
    [MASTIN, LUTV],
)
LP(
    "rni-all-films",
    "RNI All Films",
    "Breadth: colour negative, slide, instant, vintage, B&W profiles based on real emulsions.",
    [RNI, LUTV],
)
LP(
    "vsco",
    "VSCO (legacy Film packs / app filters)",
    "Film-inspired filters; VSCO Film desktop packs discontinued; app filters not tied to specific stocks.",
    [
        S("https://nuugraphics.com/blogs/pushed-pulled/vsco-film-is-gone-heres-the-alternatives"),
        S(
            "https://blog.thomasfitzgeraldphotography.com/blog/2019/11/vsco-film-alternatives",
            "2019-11",
        ),
    ],
)
LP(
    "dehancer",
    "Dehancer film profiles",
    "Per-stock film profiles plus separate halation, bloom, grain and print-stock stages (e.g. CineStill 800T profile).",
    [S("https://www.dehancer.com/profiles/film/cinestill-800t")],
)
LP(
    "kodak-2383-print",
    "Kodak 2383 print-film emulation LUT",
    "Cinema print-stock emulation: S-curve with rolled-off highlights, dense blacks, warm-yellow highlight bias, cyan-teal shadows. Common in video LUT packs.",
    [TEAL_ORANGE],
    tone=(-0.2, 0, -0.1, 0.25),
    sat=0.05,
    split=(195, 0.06, 70, 0.06),
)
LP(
    "fuji-raw-converter-luts",
    "Fuji RAW FILE CONVERTER-derived LUTs",
    "Per-sim LUTs sampled from Fujifilm's converter; close to in-camera JPEG.",
    [BASTIBE, S("https://blog.sowerby.me/fuji-film-simulation-profiles/")],
)
LP(
    "peter-mckinnon-luts",
    "Creator LUT packs (McKinnon etc.)",
    "Creator-branded LUTs: typically teal/orange split, crushed/lifted blacks, desaturated greens.",
    [S("https://filmora.wondershare.com/video-editing/peter-mckinnon-luts.html")],
    tone=(-0.2, 0, 0, 0.2),
    sat=-0.1,
    split=(200, 0.1, 50, 0.1),
)

# ---------------------------------------------------------------- 6. black-and-white styles
TONING = S("https://en.wikipedia.org/wiki/Photographic_print_toning")
ANSEL = S("https://ansel.photos/en/workflows/monochrome-toning/")
B = lambda id, name, desc, src, **kw: look(
    "bw-" + id,
    "black_and_white",
    name,
    "B&W convention",
    desc,
    mono=True,
    dt=kw.pop("dt", MONO_DT),
    src=src,
    **kw,
)
for f, fname, desc in [
    ("ye", "Yellow filter", "Darkens blue sky slightly, natural skin; filter factor ~2 (1 stop)."),
    ("or", "Orange filter", "Strong sky darkening, bolder than yellow; factor ~4 (+1-2 stops)."),
    ("r", "Red filter", "Near-black skies, darker foliage, dramatic 'storm' contrast; factor ~8."),
    ("g", "Green filter", "Lightens foliage, darkens reds/skin blemishes; separates greens."),
    ("b", "Blue filter", "Lightens sky, darkens skin/reds; haze-emphasising, 'old ortho' feel."),
]:
    B(
        f"{f}-filter",
        fname,
        desc,
        BWF_SRC + [S("https://hoyafilterusa.com/pages/how-black-white-filters-work")],
        bw=BW_FILTERS[f],
        conf={"tone": I},
        overall="documented",
        dt=["color calibration (gray tab: channel weights)", "rgb curve"],
    )
B(
    "high-contrast-street",
    "High-contrast street",
    "Crushed shadows, bright highlights, heavy grain and clarity.",
    [BW_CMP],
    tone=(-0.5, 0, 0.3, 0.5),
    grain=30,
    clar=0.3,
    vig=-0.3,
)
B(
    "film-noir",
    "Film noir",
    "Low-key hard light, deep blacks, bright specular highlights, vignette.",
    [MOODY],
    tone=(-0.7, -0.5, 0.2, 0.5),
    vig=-0.7,
    grain=10,
)
B(
    "infrared-bw",
    "Infrared B&W (Wood effect)",
    "Foliage glows white, skies near black, skin porcelain, halation bloom.",
    [AERO],
    bw={"sky_ev": -2.0, "foliage_ev": 2.0, "skin_ev": 0.7},
    tone=(-0.2, 0, 0.2, 0.3),
    grain=15,
    dt=MONO_DT + ["bloom"],
    conf={"tone": I},
)
B(
    "low-contrast-matte",
    "Low-contrast matte B&W",
    "Faded blacks, soft highlights, gentle tonality.",
    [S("https://imagen-ai.com/post/dark-lightroom-presets/")],
    tone=(0.3, 0, -0.2, -0.3),
    black=10,
    white=95,
)
B(
    "selenium",
    "Selenium toned",
    "Cool purple-brown (strong) or red-brown (dilute) shadows; deepens blacks.",
    [TONING, ANSEL, S("https://yana-sk-photo.uk/blog/selenium-toning-photography-guide")],
    split=(320, 0.08, None, 0),
    cast=(2, -1),
    tone=(-0.1, 0, 0, 0.05),
    dt=MONO_DT + ["split-toning / color balance rgb shadows lift"],
    conf={"split_toning": D},
)
B(
    "sepia",
    "Sepia toned",
    "Warm brown across tones.",
    [TONING, S("https://digitalsilverimaging.com/print-toning/")],
    split=(55, 0.2, 65, 0.2),
    cast=(3, 12),
    dt=MONO_DT + ["split-toning"],
    conf={"split_toning": D},
)
B(
    "cyanotype",
    "Cyanotype",
    "Prussian blue monochrome print; blue-white highlights.",
    [
        S("https://en.wikipedia.org/wiki/Cyanotype"),
        S("https://mymodernmet.com/cyanotype-photography/"),
    ],
    split=(250, 0.35, 240, 0.15),
    cast=(-3, -20),
    black=5,
    dt=MONO_DT + ["split-toning", "color balance rgb"],
    conf={"split_toning": D, "grey_cast": D},
)
B(
    "gold-toned",
    "Gold toned",
    "Blue-black tones (alone); orange-red with sepia.",
    [TONING],
    split=(240, 0.08, 240, 0.04),
    cast=(0, -4),
    dt=MONO_DT + ["split-toning"],
    conf={"split_toning": D},
)
B(
    "platinum-palladium",
    "Platinum / palladium print",
    "Rich soft tonality, long scale, warm neutral to brown-black.",
    [TONING, S("https://psap.library.illinois.edu/collection-id-guide/photoprint")],
    tone=(0.1, 0, -0.1, -0.15),
    split=(40, 0.06, 60, 0.08),
    cast=(1, 5),
    dt=MONO_DT + ["split-toning"],
)
B(
    "split-tone-bw",
    "Split-toned B&W (cool shadows / warm highlights)",
    "Darkroom split toning: selenium/blue shadows, sepia highlights.",
    [TONING, ANSEL],
    split=(230, 0.1, 60, 0.1),
    dt=MONO_DT + ["split-toning"],
    conf={"split_toning": C},
)

# ---------------------------------------------------------------- 7. vintage / analog artefacts
POLA = S(
    "https://danfinnen.com/article/how-to-create-the-polaroid-look-a-glossary-and-guide-to-instant-film-defects-and-manipulation/"
)
POLA2 = S("https://imagen-ai.com/valuable-tips/polaroid-lightroom-preset/")
A = lambda id, name, desc, src, **kw: look(
    "analog-" + id,
    "analog_artefact",
    name,
    "Analog artefact / instant film",
    desc,
    dt=kw.pop("dt"),
    src=src,
    **kw,
)
A(
    "halation",
    "Halation",
    "Red-orange glow around bright highlights and high-contrast edges (light reflected off base, hits red layer first).",
    [CINESTILL, CS_LOOK],
    dt=["bloom or soften + parametric (luminance) mask", "color balance rgb (mask tint red)"],
    conf={"grey_cast": I},
    extra={
        "halation": {
            "threshold_ev": 2.5,
            "radius_px_at_24mp": 12,
            "hue_deg": 30,
            "confidence": "documented (direction); inferred (numbers)",
        }
    },
)
for iso, g, coarse in [(100, 6, 100), (400, 14, 400), (1600, 28, 1600), (3200, 40, 3200)]:
    A(
        f"grain-iso{iso}",
        f"Film grain ISO {iso}",
        f"Luminance grain at ISO {iso} film scale; strength rises with speed (RMS granularity: 100-speed slide 8-9; Portra 160/400/800 ~6/8/10).",
        [DT_GRAIN, BH_SLIDE, PEVA],
        grain=g,
        dt=["grain (coarseness = ISO, strength %, midtone bias)"],
        conf={"grain": C},
        extra={"grain_size_iso": coarse},
    )
A(
    "light-leak",
    "Light leaks",
    "Warm orange/red flares entering from a frame edge; raises local black level.",
    [POLA],
    dt=["graduated density (colour) or color balance rgb with drawn/gradient mask"],
    split=(None, 0, 30, 0.3),
    black=2,
)
A(
    "heavy-vignette",
    "Heavy vignette (toy camera)",
    "Strong corner falloff (Holga/Diana/LC-A).",
    [OM_EPZ],
    vig=-1.5,
    sat=0.15,
    dt=["vignetting"],
    conf={"vignette": C},
)
A(
    "colour-fringing",
    "Colour fringing / chromatic aberration",
    "Red/cyan or purple/green edges from cheap lenses (deliberate).",
    [
        S(
            "https://www.dpreview.com/forums/threads/olympus-om-art-filters-many-example-images.4833845/"
        )
    ],
    dt=["lens correction (manual TCA override red/blue scale)"],
    extra={"tca": {"red_scale": 1.0015, "blue_scale": 0.9985, "confidence": "inferred"}},
)
A(
    "faded-print",
    "Faded print",
    "Aged C-print: lifted blacks, compressed whites, cyan dye fade → magenta/yellow cast, low saturation.",
    [POLA],
    tone=(0.2, 0, -0.1, -0.3),
    sat=-0.4,
    black=12,
    white=92,
    cast=(4, 8),
    dt=["rgb curve", "color balance rgb", "sigmoid"],
)
A(
    "polaroid-600",
    "Polaroid 600 / i-Type",
    "Lifted milky blacks, soft contrast, creamy warm-yellow highlights, cyan/teal blues, rosy skin, muted.",
    [POLA, POLA2, S("https://www.presetpro.com/polaroid-color-600-inspired-lightroom-preset/")],
    tone=(0.25, 0, -0.15, -0.3),
    sat=-0.2,
    hue=(-3, -10, -8),
    split=(185, 0.1, 70, 0.12),
    black=12,
    white=93,
    vig=-0.4,
    dt=["rgb curve", "color balance rgb", "vignetting", "framing"],
    conf={"black_lift": C, "split_toning": C},
)
A(
    "polaroid-sx70",
    "Polaroid SX-70",
    "Warmer, softer than 600; expressive warm tones controllable by temperature.",
    [S("https://www.analog.cafe/r/polaroid-sx-70-colour-film-review-420m")],
    tone=(0.3, 0, -0.2, -0.35),
    sat=-0.25,
    split=(40, 0.1, 65, 0.15),
    black=14,
    white=90,
    cast=(3, 10),
    vig=-0.4,
    dt=["rgb curve", "color balance rgb", "vignetting"],
)
A(
    "instax",
    "Instax Mini",
    "Cleaner than Polaroid: brighter, cooler-neutral, moderate contrast, slight highlight bloom.",
    [POLA2],
    tone=(0.1, 0.1, 0.05, -0.1),
    sat=0.0,
    black=6,
    white=96,
    cast=(-1, -2),
    dt=["rgb curve", "color balance rgb"],
)
A(
    "orton-glow",
    "Orton / soft glow",
    "Blurred overexposed layer blended over sharp: dreamy glow, higher saturation.",
    [OM_EPZ],
    clar=-0.3,
    sat=0.15,
    tone=(0, 0.1, 0.1, 0),
    dt=["soften", "bloom"],
)

# ---------------------------------------------------------------- output
CONVENTIONS = {
    "as_of": ASOF,
    "reference_pipeline": "darktable 5.4.x scene-referred default (exposure +0.7 EV, sigmoid or agx defaults, color calibration CAT). All deltas are relative to that baseline render of the SAME raw, not to the camera JPEG.",
    "tone": "shadows_ev/mids_ev/highlights_ev = output luminance change in EV at scene patches of 18%·2^-4, 18%, 18%·2^2 (ColorChecker/grey-ramp); contrast = fractional change of the mid-grey slope of the tone curve (sigmoid contrast / agx contrast / cbrgb contrast), e.g. +0.2 = 20% steeper.",
    "saturation_delta": "fractional change of mean CIE LCh(ab) chroma over the 18 chromatic ColorChecker patches (-1 = monochrome).",
    "hue_shifts": "CIE LCh(ab) hue-angle change in degrees on patches: skin = 'light skin' (#2, h≈50°), sky = 'blue sky' (#3, h≈255°), foliage = 'foliage' (#4, h≈125°). Positive = counter-clockwise (red→yellow→green→cyan→blue→magenta). Toward magenta for sky is +, toward cyan is −; for foliage toward yellow is −, toward cyan is +; for skin toward yellow is +, toward red/magenta is −.",
    "split_toning": "hues in LCh(ab) degrees (null = no tint on that end); sat = chroma added at that end as fraction of darktable color balance rgb chroma slider (0.1 = 10%).",
    "grey_cast": "a*, b* measured on the neutral grey ramp mid patch after the look (0,0 = neutral). The primary grey-chart assertion for tints/WB shifts.",
    "black_lift": "output L* of scene black (0 = no lift; 10 = matte).",
    "white_point": "output L* ceiling of a +3 EV patch (100 = none).",
    "grain": "darktable grain strength % (0 = none); grain_size_iso when given = coarseness.",
    "vignette": "EV darkening at image corners relative to centre (negative = darker).",
    "clarity": "local-contrast change on a -1..+1 scale; +0.1 ≈ local contrast 'detail' +10%; Fuji clarity step ≈ 0.1.",
    "monochrome_looks": "saturation_delta = -1, hue_shifts = null, bw_response {sky_ev, foliage_ev, skin_ev} = luminance change of those patches vs an equal-weight panchromatic conversion.",
    "confidence": "documented = the source states the direction or the parameter value (magnitudes in EV/deg are still our mapping); community-consensus = several community sources agree; inferred = our estimate from description/physics. 'confidence' at look level = best-supported signature trait; see target_confidence per target.",
    "suggested_tolerances": {
        "documented": {"ev": 0.15, "hue_deg": 5, "sat": 0.08, "ab": 2},
        "community-consensus": {"ev": 0.25, "hue_deg": 8, "sat": 0.12, "ab": 3},
        "inferred": {
            "ev": 0.4,
            "hue_deg": 12,
            "sat": 0.2,
            "ab": 5,
            "note": "assert direction (sign) only",
        },
    },
    "fuji_recipe_mapping": "Inferred: highlight step = +0.08 EV at +2 EV; shadow step = -0.08 EV at -4 EV (Fuji +shadow = darker); DR200/DR400 = -0.08/-0.15 EV highlights; colour step = +6% chroma; WB shift step ≈ +0.6 a* per red, -0.7 b* per blue step; kelvin vs 5500K ≈ 0.35 b* per 100K; grain Weak/Strong × Small/Large = 10/20/16/28% strength.",
}
DT_GROUPS = collections.OrderedDict()


def group_of(l):
    m = " ".join(l["dt_modules"]).lower()
    t = l["targets"]
    if t.get("monochrome"):
        return "monochrome (color calibration gray / monochrome + curve + toning)"
    if "lut 3d" in m and l["family"] in ("fujifilm_film_simulation", "lut_pack_family"):
        return "lut 3d first"
    if "channel mixer" in m:
        return "channel mixer rgb (dye-swap / false colour)"
    st = t["split_toning"]
    if (st["shadow_hue"] is not None and st["shadow_sat"] >= 0.1) or (
        st["highlight_hue"] is not None and st["highlight_sat"] >= 0.1
    ):
        return "color balance rgb 4-ways (split toning)"
    if any(k in m for k in ("bloom", "soften", "lens correction", "graduated", "blurs")):
        return "effect modules (bloom/soften/lens/graduated density)"
    if l["targets"]["vignette"] <= -1.0:
        return "vignetting-led"
    if (
        abs(t["tone"]["contrast"]) >= 0.2
        or abs(t["tone"]["shadows_ev"]) >= 0.3
        or t["black_lift"] >= 6
    ):
        return "tone-led (sigmoid/agx contrast + rgb curve)"
    if t["saturation_delta"] and abs(t["saturation_delta"]) >= 0.2:
        return "saturation-led (color balance rgb chroma/vibrance; velvia)"
    if l["family"] == "fuji_x_weekly_recipe":
        return "recipe = base sim + WB shift (color calibration) + grain"
    return "subtle (color balance rgb + color zones)"


for l in LOOKS:
    l["dt_group"] = group_of(l)

out = {
    "schema_version": 1,
    "generated": ASOF,
    "conventions": CONVENTIONS,
    "counts": {
        "total": len(LOOKS),
        "by_family": dict(collections.Counter(l["family"] for l in LOOKS)),
        "by_dt_group": dict(collections.Counter(l["dt_group"] for l in LOOKS)),
        "by_confidence": dict(collections.Counter(l["confidence"] for l in LOOKS)),
    },
    "looks": LOOKS,
}
import pathlib

p = pathlib.Path(__file__).with_name("looks.json")
p.write_text(json.dumps(out, indent=1, ensure_ascii=False))
print(json.dumps(out["counts"], indent=1))
