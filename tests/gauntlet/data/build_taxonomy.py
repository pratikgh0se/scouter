import json

SEEN = "2026-09-26"
M = "https://docs.darktable.org/usermanual/development/en/module-reference/processing-modules/"


def P(name, lo, hi, unit, phrases=()):
    return {
        "name": name,
        "typical_min": lo,
        "typical_max": hi,
        "unit": unit,
        "phrases": list(phrases),
    }


mods = []


def mod(id, ui, group, plain, params, phrases, slug=None, host="yes", notes=None):
    d = {
        "id": id,
        "ui_name": ui,
        "purpose_group": group,
        "plain_words": plain,
        "params": params,
        "phrases": list(phrases),
        "on_host_5_4_1": host,
        "source": M + (slug or ui.replace(" ", "-")) + "/",
        "seen": SEEN,
    }
    if notes:
        d["notes"] = notes
    mods.append(d)


# ---------- raw / technical ----------
mod(
    "rawprepare",
    "raw black/white point",
    "raw_technical",
    "Sets the sensor's black and white levels so the raw data starts from true zero. Almost never touched unless a camera is mis-profiled.",
    [
        P(
            "black level (per channel)",
            0,
            4096,
            "raw ADU",
            ["black level is off", "lifted blacks from raw"],
        ),
        P("white point", 4000, 16383, "raw ADU", ["highlights clip too early"]),
    ],
    ["fix the raw black level", "blacks look lifted and grey"],
    slug="raw-black-white-point",
)
mod(
    "demosaic",
    "demosaic",
    "raw_technical",
    "Turns the single-colour sensor mosaic into full RGB pixels; 5.4 adds capture sharpening here to undo sensor/AA blur.",
    [
        P(
            "algorithm",
            None,
            None,
            "enum RCD|AMaZE|LMMSE|PPG|VNG4|dual (Bayer); Markesteijn 1/3-pass|FDC (X-Trans)",
            ["use AMaZE for fine detail", "LMMSE for noisy high ISO"],
        ),
        P(
            "dual demosaic threshold",
            0.0,
            0.3,
            "ratio",
            ["smooth flat areas, keep detail on edges"],
        ),
        P(
            "capture sharpening radius",
            0.3,
            1.5,
            "px",
            ["recover sensor softness", "capture sharpen"],
        ),
        P("capture sharpening iterations", 4, 20, "count", ["more capture sharpening"]),
    ],
    [
        "recover fine detail",
        "it looks mushy at 100%",
        "capture sharpening",
        "zipper or maze artifacts",
    ],
)
mod(
    "hotpixels",
    "hot pixels",
    "raw_technical",
    "Finds and removes stuck or hot sensor pixels, common in long exposures.",
    [
        P("threshold", 0.01, 0.1, "ratio", ["catch more hot pixels"]),
        P("strength", 0.1, 1.0, "ratio", []),
        P("detect by 3 neighbors", None, None, "bool", ["aggressive hot pixel removal"]),
    ],
    ["remove hot pixels", "get rid of the red/green dots in the long exposure", "stuck pixels"],
    slug="hot-pixels",
)
mod(
    "highlights",
    "highlight reconstruction",
    "raw_technical",
    "Rebuilds colour and detail in areas where one or more sensor channels clipped; default inpaint opposed.",
    [
        P(
            "method",
            None,
            None,
            "enum inpaint opposed|segmentation based|guided laplacians|clip highlights|reconstruct in LCh",
            ["rebuild the blown sky", "fix magenta highlights"],
        ),
        P("clipping threshold", 0.9, 1.1, "ratio", ["mask doesn't match the overexposure warning"]),
        P("combine (segmentation)", 0, 8, "px", []),
        P("diameter of reconstruction (guided laplacians)", 64, 1024, "px", []),
        P("iterations (guided laplacians)", 1, 256, "count", ["still magenta in the highlights"]),
    ],
    [
        "recover blown highlights",
        "the sun is magenta",
        "fix the clipped sky",
        "bring back the highlights",
    ],
    slug="highlight-reconstruction",
)
mod(
    "cacorrect",
    "raw chromatic aberrations",
    "raw_technical",
    "Removes lateral colour fringes at raw level (Bayer only), before demosaic.",
    [P("iterations", 1, 3, "count", []), P("avoid colorshift", None, None, "bool", [])],
    ["remove colour fringing at raw level"],
    slug="raw-chromatic-aberrations",
)
mod(
    "cacorrectrgb",
    "chromatic aberrations",
    "detail",
    "Removes purple/green fringes along high-contrast edges after demosaic; works on any sensor.",
    [
        P("guide channel", None, None, "enum red|green|blue", []),
        P("radius", 1, 10, "px", ["wider fringes"]),
        P("strength", 0.5, 1.5, "ratio", ["stronger defringe"]),
        P("correction mode", None, None, "enum standard|darken only|brighten only", []),
    ],
    [
        "remove purple fringing",
        "fix the chromatic aberration",
        "get rid of the colour fringes on the branches",
    ],
    slug="chromatic-aberrations",
)
mod(
    "rawdenoise",
    "raw denoise",
    "detail",
    "Wavelet noise reduction on raw data before demosaic; rarely used vs denoise (profiled).",
    [P("noise threshold", 0.001, 0.1, "ratio", [])],
    ["denoise at raw level"],
    slug="raw-denoise",
)
mod(
    "flip",
    "orientation",
    "geometry",
    "Rotates by 90-degree steps or flips; applied from EXIF automatically.",
    [
        P(
            "orientation",
            None,
            None,
            "enum no rotation|flip H|flip V|rotate 90 CW|rotate 90 CCW|rotate 180",
            ["rotate it", "flip it horizontally", "mirror it"],
        )
    ],
    ["rotate 90", "flip horizontally", "it's sideways"],
    slug="orientation",
)
mod(
    "temperature",
    "white balance",
    "colour",
    "Raw-level channel multipliers. In the modern scene-referred workflow it stays at 'camera reference' and colour calibration does the real white balance.",
    [
        P(
            "setting",
            None,
            None,
            "enum as shot|camera reference|user modified|as shot to reference|preset",
            [],
        ),
        P("temperature", 2500, 10000, "K", ["warm it up", "cool it down"]),
        P("tint", 0.8, 1.2, "ratio", ["too green", "too magenta"]),
    ],
    ["warm it up", "cool it down", "fix the white balance", "too blue", "too orange"],
    slug="white-balance",
)

# ---------- exposure / tone ----------
mod(
    "exposure",
    "exposure",
    "tone",
    "Sets overall brightness (the mid-grey point) in stops; also the dodge/burn workhorse when masked.",
    [
        P(
            "exposure",
            -2.0,
            3.0,
            "EV",
            [
                "brighten it",
                "darken it",
                "it's underexposed",
                "a stop brighter",
                "lift the whole image",
            ],
        ),
        P(
            "black level correction",
            -0.01,
            0.01,
            "ratio",
            ["don't use to crush blacks (manual says use the tone mapper)"],
        ),
        P("compensate camera exposure", None, None, "bool", []),
        P(
            "area exposure mapping target",
            18,
            50,
            "% lightness",
            ["match the brightness across the set"],
        ),
    ],
    [
        "brighten",
        "darken",
        "too dark",
        "too bright",
        "lift it a stop",
        "dodge this",
        "burn that down",
        "match exposure to the hero",
    ],
)
mod(
    "sigmoid",
    "sigmoid",
    "tone",
    "Scene-to-display tone mapper with an S-curve; fewer knobs than filmic; default tone mapper for new installs since 5.2.",
    [
        P("contrast", 1.0, 2.5, "ratio", ["more contrast", "punchier", "flatter"]),
        P(
            "skew",
            -0.5,
            0.5,
            "ratio",
            ["deeper shadows", "open the shadows", "compress highlights"],
        ),
        P("color processing", None, None, "enum per channel|rgb ratio", []),
        P("preserve hue", 0, 100, "%", ["let sunsets go yellow", "keep hues true"]),
        P("target black", 0.0, 0.5, "% display", ["faded blacks", "matte look"]),
        P("target white", 90, 100, "% display", []),
    ],
    [
        "add contrast",
        "make it punchier",
        "flatten it",
        "compress the highlights",
        "faded film look",
    ],
)
mod(
    "agx",
    "AgX",
    "tone",
    "Blender-derived tone mapper added in 5.4: exposure range, pivot, contrast, toe/shoulder power, plus look slope/lift/brightness/saturation and primaries for film-like highlight desaturation.",
    [
        P("white relative exposure", 2.0, 8.0, "EV", ["protect the highlights", "set white point"]),
        P("black relative exposure", -12.0, -5.0, "EV", ["deeper blacks", "set black point"]),
        P("contrast", 1.5, 3.5, "ratio", ["more contrast", "softer contrast"]),
        P("toe power", 1.0, 2.0, "ratio", ["crush the blacks", "keep shadow contrast"]),
        P("shoulder power", 1.0, 2.0, "ratio", ["softer highlight rolloff"]),
        P("look saturation", 0.7, 1.4, "ratio", ["more colour", "desaturate"]),
        P("look lift", -0.05, 0.05, "ratio", ["fade the blacks"]),
    ],
    [
        "film-like highlights",
        "auto tune levels",
        "filmic-style rolloff",
        "punchy preset",
        "smooth preset",
    ],
    slug="agx",
    notes="Presets: blender-like|base, scene-referred default, smooth, punchy, unmodified.",
)
mod(
    "filmicrgb",
    "filmic rgb",
    "tone",
    "Original scene-referred tone mapper (Aurelien Pierre): white/black relative exposure, contrast, latitude, highlight reconstruction; still default in older configs.",
    [
        P("white relative exposure", 2.0, 8.0, "EV", ["save the highlights"]),
        P("black relative exposure", -12.0, -6.0, "EV", ["crush the blacks", "lift the blacks"]),
        P("contrast", 1.0, 2.0, "ratio", ["more contrast"]),
        P("latitude", 0, 50, "%", []),
        P("shadows/highlights balance", -50, 50, "%", ["open the shadows"]),
        P("reconstruct threshold", -3.0, 0.0, "EV", ["fix clipped highlights"]),
    ],
    ["set white and black points", "filmic look", "too much contrast", "compress dynamic range"],
    slug="filmic-rgb",
)
mod(
    "basecurve",
    "base curve",
    "tone",
    "Display-referred camera-JPEG-like curve; legacy / display-referred workflow and camera styles; exposure fusion for pseudo-HDR.",
    [
        P("preset", None, None, "enum camera-like presets", ["look like the camera JPEG"]),
        P("fusion", 0, 3, "frames", ["HDR look from one frame"]),
    ],
    ["look like the camera JPEG", "out of camera look"],
    slug="base-curve",
)
mod(
    "toneequal",
    "tone equalizer",
    "tone",
    "Dodges and burns by brightness zone using a smoothed luminance mask, like a graphic equalizer for light: pull the sky down, lift faces in shadow, without halos.",
    [
        P(
            "zone gain -8..0 EV bands",
            -2.0,
            2.0,
            "EV per band",
            ["open the shadows", "lift the shadows", "pull down the highlights", "tame the sky"],
        ),
        P(
            "preserve details",
            None,
            None,
            "enum no|guided filter|averaged guided filter|eigf|averaged eigf",
            ["avoid halos"],
        ),
        P("smoothing diameter", 1, 20, "% long side", []),
        P("edges refinement/feathering", 1, 100, "ratio", ["halo around the horizon"]),
        P("mask exposure compensation", -4, 4, "EV", []),
        P("mask contrast compensation", -2, 2, "EV", []),
    ],
    [
        "open the shadows",
        "bring down the highlights",
        "recover the sky",
        "even out the light",
        "compress the dynamic range",
        "dodge and burn",
    ],
    slug="tone-equalizer",
    notes="Presets: compress shadows/highlights (gf/eigf) soft/medium/strong, relight. Set mask with the wand buttons first.",
)
mod(
    "rgbcurve",
    "rgb curve",
    "tone",
    "Curve on RGB (linked or per channel) for creative contrast shaping and colour casts; works in scene-referred space.",
    [
        P(
            "curve node (shadows)",
            -0.05,
            0.05,
            "output offset",
            ["crush the blacks", "matte blacks"],
        ),
        P("curve node (highlights)", -0.05, 0.05, "output offset", ["roll off highlights"]),
        P(
            "preserve colors",
            None,
            None,
            "enum none|luminance|max RGB|average RGB|sum RGB|norm|basic power",
            [],
        ),
        P(
            "mode",
            None,
            None,
            "enum RGB linked|independent channels",
            ["cross-process look", "teal shadows via blue curve"],
        ),
    ],
    [
        "S-curve",
        "add an S-curve",
        "crush the blacks",
        "fade the blacks",
        "matte look",
        "cross-processed",
    ],
    slug="rgb-curve",
)
mod(
    "rgblevels",
    "rgb levels",
    "tone",
    "Black/grey/white level pickers on RGB, linked or per channel.",
    [
        P("black", 0, 5, "%", ["set the black point", "crush blacks"]),
        P("grey", 40, 60, "%", []),
        P("white", 90, 100, "%", ["set the white point"]),
    ],
    ["set levels", "auto levels", "stretch the histogram"],
    slug="rgb-levels",
)
mod(
    "tonecurve",
    "tone curve",
    "tone",
    "Legacy Lab/RGB curve module; display-referred habit, still used for per-channel Lab curves.",
    [P("curve", None, None, "nodes", ["S-curve"]), P("preserve colors", None, None, "enum", [])],
    ["classic tone curve"],
    slug="tone-curve",
)
mod(
    "shadhi",
    "shadows and highlights",
    "tone",
    "Legacy display-referred local lift/compress; prone to halos, superseded by tone equalizer.",
    [
        P("shadows", 0, 60, "", ["lift shadows"]),
        P("highlights", -60, 0, "", ["recover highlights"]),
        P("radius", 50, 200, "px", []),
    ],
    ["quick shadow lift"],
    slug="shadows-and-highlights",
)
mod(
    "graduatednd",
    "graduated density",
    "tone",
    "Simulated graduated ND filter: darkens part of the frame with a linear gradient, optionally tinted.",
    [
        P("density", 0.5, 3.0, "EV", ["darken the sky"]),
        P("hardness", 0, 100, "%", []),
        P("rotation", -180, 180, "deg", []),
        P("hue", 0, 360, "deg", []),
        P("saturation", 0, 50, "%", ["blue the sky"]),
    ],
    ["darken the sky", "grad filter", "ND grad"],
    slug="graduated-density",
)

# ---------- colour ----------
mod(
    "channelmixerrgb",
    "color calibration",
    "colour",
    "The modern white balance (chromatic adaptation) plus channel mixer, colourfulness/brightness per channel, and grey tab for B&W; can calibrate from a colour checker.",
    [
        P("adaptation", None, None, "enum CAT16|linear Bradford|non-linear Bradford|XYZ|none", []),
        P(
            "illuminant",
            None,
            None,
            "enum as shot in camera|daylight|blackbody|custom|A|D50|D65|F...|detect from surfaces|detect from edges",
            ["neutralise the cast", "it's too yellow indoors"],
        ),
        P("temperature (CCT)", 2500, 8000, "K", ["warmer", "cooler"]),
        P("hue/chroma of illuminant", 0, 360, "deg / %", ["remove the green cast"]),
        P("gray tab R/G/B mix", -1.0, 2.0, "ratio", ["red filter B&W", "darker skies in B&W"]),
        P("colorfulness R/G/B", -50, 50, "%", ["mute the greens"]),
    ],
    [
        "fix the white balance",
        "neutralise the colour cast",
        "it's too orange indoors",
        "make it black and white",
        "red filter",
        "match the colour checker",
        "mixed light",
    ],
    slug="color-calibration",
    notes="Mixed light: one instance per illuminant with drawn/parametric masks; second instance reuses the first mask inverted via raster mask.",
)
mod(
    "colorbalancergb",
    "color balance rgb",
    "colour",
    "The main colour grading tool: global vibrance/chroma/saturation/brilliance, per-zone (shadows/mid/highlights) saturation and brilliance, 4-ways lift/power/gain/offset hue+chroma wheels.",
    [
        P("global vibrance", 0, 30, "%", ["add some pop", "more vibrant"]),
        P("global chroma", 0, 50, "%", ["more colour", "richer colours"]),
        P(
            "saturation shadows/mid/highlights",
            -30,
            50,
            "%",
            ["saturate the shadows", "desaturate the highlights"],
        ),
        P(
            "brilliance shadows/mid/highlights",
            -20,
            20,
            "%",
            ["brighter colours", "deeper colours"],
        ),
        P("contrast", -30, 30, "%", ["more midtone contrast"]),
        P(
            "4-ways shadows lift hue/chroma",
            0,
            360,
            "deg / 0-15 %",
            ["teal shadows", "cool shadows"],
        ),
        P(
            "4-ways highlights gain hue/chroma",
            0,
            360,
            "deg / 0-15 %",
            ["warm highlights", "orange highlights"],
        ),
        P("global offset luminance", -2, 2, "%", ["lift the blacks", "faded"]),
    ],
    [
        "add some pop",
        "make the colours richer",
        "teal and orange",
        "warm highlights, cool shadows",
        "muted colours",
        "desaturate",
        "colour grade it",
        "make it moody",
        "cinematic",
    ],
    slug="color-balance-rgb",
    notes="Presets: basic colorfulness (natural/vibrant/standard), add basic colorfulness. Pierre's rule of thumb: chroma globally, saturation in shadows/mids, brilliance in highlights.",
)
mod(
    "colorequal",
    "color equalizer",
    "colour",
    "Hue-selective hue/saturation/brightness curves (HSL-style) in darktable UCS; the modern replacement for color zones.",
    [
        P(
            "hue shift per hue node",
            -30,
            30,
            "deg",
            ["make the greens more yellow", "shift the sky toward cyan"],
        ),
        P(
            "saturation per hue node",
            -50,
            50,
            "%",
            ["mute the greens", "make the blues pop", "desaturate the oranges"],
        ),
        P("brightness per hue node", -30, 30, "%", ["darken the blue sky", "brighten the skin"]),
        P("hue analysis radius / contrast", None, None, "options", []),
    ],
    [
        "HSL adjustment",
        "darken the blue sky",
        "mute the greens",
        "fix the skin tones",
        "make the foliage less neon",
    ],
    slug="color-equalizer",
)
mod(
    "colorzones",
    "color zones",
    "colour",
    "Older hue/chroma/lightness curves in LCh; still widely used for selective colour and selective desaturation.",
    [
        P("lightness by hue", -30, 30, "%", ["darken the sky"]),
        P("chroma by hue", -100, 50, "%", ["desaturate everything but red"]),
        P("hue by hue", -30, 30, "deg", ["shift the greens"]),
    ],
    ["selective colour", "keep only the red", "desaturate one colour"],
    slug="color-zones",
)
mod(
    "primaries",
    "rgb primaries",
    "colour",
    "Rotates hue and purity of the R, G, B primaries (a friendly channel mixer); before tone mapping for colorimetry, after it for tints.",
    [
        P("red/green/blue hue", -20, 20, "deg", ["warmer reds", "cyan-er blues"]),
        P("red/green/blue purity", -30, 30, "%", []),
        P("tint hue/purity", 0, 360, "deg / 0-20 %", ["tint the greys"]),
    ],
    ["shift the primaries", "make it look like film colours"],
    slug="rgb-primaries",
)
mod(
    "vibrance",
    "vibrance (deprecated) / color balance rgb vibrance",
    "colour",
    "Boost muted colours more than already-saturated ones. Standalone vibrance module is deprecated; use color balance rgb global vibrance or velvia.",
    [P("vibrance", 0, 30, "%", ["add vibrance"])],
    ["add vibrance", "more vibrant without oversaturating skin"],
    slug="vibrance",
    host="deprecated",
)
mod(
    "velvia",
    "velvia",
    "colour",
    "Saturation boost that protects already saturated pixels; display-referred.",
    [
        P("strength", 10, 40, "%", ["punchy landscape colour"]),
        P("mid-tones bias", 0, 1, "ratio", []),
    ],
    ["velvia look", "punch up the colours"],
    slug="velvia",
)
mod(
    "colisa_replacement",
    "saturation (via color balance rgb)",
    "colour",
    "'Saturation' requests map to color balance rgb saturation/chroma; old contrast-brightness-saturation module was removed in 5.2.",
    [
        P(
            "saturation",
            -100,
            50,
            "%",
            ["more saturated", "less saturated", "desaturate", "black and white-ish"],
        )
    ],
    ["more saturation", "less saturation"],
    slug="color-balance-rgb",
    host="alias",
)
mod(
    "lut3d",
    "LUT 3D",
    "look",
    "Applies a 3D LUT (.cube, .3dl, .png haldclut, .gmz) for film looks or client grades; clips to [0,1]; LUT file lives in the LUT root folder, not the XMP.",
    [
        P("LUT file", None, None, "path", ["apply my LUT", "use the Kodak Portra LUT"]),
        P(
            "application color space",
            None,
            None,
            "enum sRGB|REC.709|lin sRGB/REC.709|lin prophoto RGB",
            [],
        ),
        P("interpolation", None, None, "enum tetrahedral|trilinear|pyramid", []),
        P("opacity (blend)", 20, 100, "%", ["half-strength LUT"]),
    ],
    ["apply a LUT", "film simulation", "use this .cube", "make it look like Portra"],
    slug="lut-3d",
)
mod(
    "colorchecker",
    "color look up table",
    "look",
    "Maps colour-checker patches to targets; used for camera-matching and styles (5.0 added an expanded checker preset).",
    [P("patch source/target", None, None, "Lab per patch", [])],
    ["match another camera", "camera profile look"],
    slug="color-look-up-table",
)
mod(
    "splittoning",
    "split-toning",
    "look",
    "Tints shadows and highlights with two hues; display-referred; color balance rgb 4-ways is the scene-referred equivalent.",
    [
        P("shadows hue", 180, 240, "deg", ["blue shadows", "teal shadows"]),
        P("highlights hue", 20, 60, "deg", ["warm highlights", "gold highlights"]),
        P("balance", 0, 100, "%", []),
        P("compress", 0, 50, "%", []),
        P("saturation", 10, 60, "%", []),
    ],
    ["split tone", "teal and orange", "sepia highlights, blue shadows"],
    slug="split-toning",
)
mod(
    "colorize",
    "colorize",
    "look",
    "Overlays a single solid colour tint; for toned monochrome.",
    [
        P("hue", 0, 360, "deg", ["sepia"]),
        P("saturation", 0, 60, "%", []),
        P("lightness", 40, 60, "%", []),
        P("source mix", 0, 100, "%", []),
    ],
    ["sepia tone", "tint it blue", "cyanotype"],
    slug="colorize",
)
mod(
    "colorharmonizer",
    "color harmonizer",
    "colour",
    "(5.6+) Pulls off-palette hues toward a harmony rule (complementary, triad, etc.) in UCS space; NOT on Scouter's 5.4.1 host.",
    [
        P(
            "harmony rule",
            None,
            None,
            "enum monochromatic|analogous|complementary|split complementary|triad|tetrad|square|custom",
            ["make the colours harmonise"],
        ),
        P("anchor hue", 0, 360, "deg", []),
        P("strength", 0, 100, "%", []),
    ],
    ["harmonise the colours", "complementary palette"],
    slug="color-harmonizer",
    host="no (5.6+)",
)
mod(
    "satcurve",
    "saturation curve",
    "colour",
    "(dev manual, post-5.6) Saturation and brilliance remapped as curves of input saturation; NOT on 5.4.1 host.",
    [P("saturation curve", None, None, "nodes", ["protect vivid colours, boost dull ones"])],
    ["saturate only the dull colours"],
    slug="satcurve-rgb",
    host="no (dev)",
)

# ---------- detail ----------
mod(
    "denoiseprofile",
    "denoise (profiled)",
    "detail",
    "Noise reduction tuned to each camera/ISO profile; non-local means or wavelets; defaults usually enough.",
    [
        P(
            "mode",
            None,
            None,
            "enum wavelets auto|non-local means auto|wavelets|non-local means",
            [],
        ),
        P("strength", 0.5, 2.0, "ratio", ["clean up the noise", "denoise harder", "too smeary"]),
        P("preserve shadows", 0.8, 1.5, "ratio", ["shadows too blotchy"]),
        P("bias correction", -0.5, 0.5, "ratio", ["green/magenta blotches"]),
        P("patch size (NLM)", 1, 4, "px", []),
        P("blend: chroma only", None, None, "blend mode", ["just remove colour noise"]),
    ],
    [
        "clean up the noise",
        "reduce the grain",
        "remove colour noise",
        "too noisy",
        "it's plasticky",
    ],
    slug="denoise-profiled",
)
mod(
    "nlmeans",
    "astrophoto denoise",
    "detail",
    "Non-local means denoise with separate luma/chroma sliders; used for night skies and heavy noise.",
    [
        P("patch size", 1, 4, "px", []),
        P("strength", 20, 100, "%", ["smooth the sky noise"]),
        P("luma", 0, 100, "%", []),
        P("chroma", 50, 100, "%", ["remove colour speckle"]),
    ],
    ["denoise the night sky", "clean up the astro noise"],
    slug="astrophoto-denoise",
)
mod(
    "sharpen",
    "sharpen",
    "detail",
    "Classic unsharp mask; legacy, meant to undo AA-filter softness; many turn it off in favour of diffuse or sharpen.",
    [
        P("radius", 0.5, 3.0, "px", []),
        P("amount", 0.3, 1.5, "ratio", ["sharpen it", "crisper"]),
        P("threshold", 0.3, 1.0, "ratio", ["don't sharpen the noise"]),
    ],
    ["sharpen it", "make it crisper", "it's soft"],
    slug="sharpen",
)
mod(
    "diffuse",
    "diffuse or sharpen",
    "detail",
    "Physics-based diffusion engine; its presets do lens deblur, sharpening, dehaze, local contrast, bloom, denoise, inpaint highlights, watercolour.",
    [
        P(
            "preset",
            None,
            None,
            "enum sharpen demosaicing (AA/no AA)|lens deblur soft/medium/hard|dehaze|local contrast|fast sharpness|fast local contrast|bloom|denoise fine/medium/coarse|inpaint highlights|surface blur|simulate watercolor|simulate line drawing",
            ["lens deblur", "dehaze", "add clarity"],
        ),
        P("iterations", 1, 32, "count", ["stronger effect"]),
        P("central radius", 0, 8, "px", []),
        P("radius span", 1, 64, "px", []),
        P("1st-4th order speed", -100, 100, "%", ["sharpen (negative) vs blur (positive)"]),
        P("edge sensitivity", 0, 5, "ratio", ["avoid halos"]),
        P("luminance masking threshold", 0, 10, "%", []),
    ],
    [
        "sharpen",
        "deblur",
        "clarity",
        "dehaze",
        "add bite",
        "Orton glow",
        "halation (with RGB blend)",
    ],
    slug="diffuse",
)
mod(
    "bilat",
    "local contrast",
    "detail",
    "Quick detail/clarity boost (local laplacian); just enabling it or picking a preset is often enough.",
    [
        P("detail", 100, 250, "%", ["add some pop", "more clarity", "more texture"]),
        P("highlights", 30, 100, "%", []),
        P("shadows", 30, 100, "%", []),
        P("mid-tone range", 0.3, 0.7, "ratio", []),
    ],
    ["add clarity", "add some pop", "more texture", "make the clouds pop", "more detail"],
    slug="local-contrast",
)
mod(
    "atrous",
    "contrast equalizer",
    "detail",
    "Wavelet contrast/denoise by feature size: fine texture vs large structure; 'clarity' preset is a popular clarity/structure move.",
    [
        P(
            "preset",
            None,
            None,
            "enum clarity|deblur|denoise|bloom|coarse/fine structure",
            ["clarity"],
        ),
        P("mix", 10, 40, "%", ["gentle clarity"]),
        P("contrast per scale", -50, 100, "%", []),
    ],
    ["clarity", "fine texture", "soften skin texture (negative)"],
    slug="contrast-equalizer",
)
mod(
    "hazeremoval",
    "haze removal",
    "detail",
    "One-slider dehaze using a dark-channel prior; negative strength adds haze.",
    [
        P("strength", -0.3, 0.6, "ratio", ["cut through the haze", "remove the fog", "add haze"]),
        P("distance", 0.1, 1.0, "ratio", ["dehaze only the far hills"]),
    ],
    ["dehaze", "remove the haze", "cut through the fog", "add atmosphere"],
    slug="haze-removal",
)
mod(
    "contrasttexture",
    "contrast & texture",
    "detail",
    "(dev manual, post-5.6) Scene-referred local contrast via eigf; NOT on 5.4.1 host.",
    [
        P("local contrast", -50, 50, "%", ["more texture", "smooth skin (negative)"]),
        P("detail level", None, None, "scale", []),
    ],
    ["texture slider"],
    slug="contrast-texture",
    host="no (dev)",
)
mod(
    "highpass",
    "highpass",
    "detail",
    "High-pass layer, blended (overlay/soft light) for sharpening.",
    [P("sharpness", 20, 60, "%", []), P("contrast boost", 0, 50, "%", [])],
    ["high-pass sharpen"],
    slug="highpass",
)
mod(
    "lowpass",
    "lowpass",
    "detail",
    "Low-pass blur; with blend modes used for soft glow, local contrast inversion, or shadow recovery tricks.",
    [
        P("radius", 5, 50, "px", []),
        P("contrast", -1, 1, "ratio", []),
        P("saturation", 0, 1, "ratio", []),
    ],
    ["soft glow"],
    slug="lowpass",
)
mod(
    "bilateral",
    "surface blur",
    "detail",
    "Edge-preserving bilateral blur; chroma blend reduces moire or skin redness.",
    [P("radius", 1, 50, "px", []), P("red/green/blue", 1, 50, "ratio", [])],
    ["remove moire", "even out skin redness"],
    slug="surface-blur",
)

# ---------- geometry / corrections ----------
mod(
    "lens",
    "lens correction",
    "geometry",
    "Auto-detects lens (Lensfun or embedded metadata) and fixes distortion, vignetting and TCA; enable before exposure since it changes brightness.",
    [
        P("correction method", None, None, "enum embedded metadata|Lensfun database", []),
        P(
            "corrections",
            None,
            None,
            "enum all|distortion|vignetting|TCA",
            ["fix the vignetting", "fix the distortion", "fix barrel distortion"],
        ),
        P("distortion fine-tune", -0.5, 0.5, "ratio", []),
        P("vignetting fine-tune", 0, 2, "ratio", ["corners still dark"]),
    ],
    ["fix the lens distortion", "fix the vignetting", "remove barrel distortion", "lens profile"],
    slug="lens-correction",
)
mod(
    "ashift",
    "rotate and perspective",
    "geometry",
    "Straightens horizons and fixes converging verticals (keystone), with auto structure detection; right-drag along the horizon to level.",
    [
        P("rotation", -10, 10, "deg", ["straighten the horizon", "it's tilted", "level it"]),
        P(
            "lens shift vertical",
            -1,
            1,
            "ratio",
            ["fix converging verticals", "buildings leaning back"],
        ),
        P("lens shift horizontal", -1, 1, "ratio", []),
        P("shear", -0.5, 0.5, "ratio", []),
        P("automatic cropping", None, None, "enum off|largest area|original format", []),
        P("structure fit", None, None, "enum vertical|horizontal|both", ["auto straighten"]),
    ],
    [
        "straighten the horizon",
        "level it",
        "fix the leaning buildings",
        "keystone",
        "perspective correction",
    ],
    slug="rotate-perspective",
)
mod(
    "crop",
    "crop",
    "geometry",
    "Crops to a region with aspect ratio presets and composition guides.",
    [
        P(
            "aspect",
            None,
            None,
            "enum freehand|original|square|3:2|4:3|5:4|16:9|4:5|45:35 etc.",
            ["crop to square", "make it 4:5 for Instagram"],
        ),
        P("margins left/right/top/bottom", 0, 40, "%", ["tighter crop", "crop in"]),
    ],
    [
        "crop it",
        "tighter crop",
        "crop to square",
        "4x5 for Instagram",
        "cut out the distraction at the edge",
    ],
)
mod(
    "liquify",
    "liquify",
    "geometry",
    "Push/pull/warp pixels with points, lines, curves; bulge/pinch.",
    [
        P("strength", 10, 100, "%", []),
        P("radius", 20, 300, "px", []),
        P("warp type", None, None, "enum linear|grow|shrink", ["slim", "enlarge the eyes"]),
    ],
    ["push this in", "slim the waist", "straighten a warped line locally"],
    slug="liquify",
)
mod(
    "retouch",
    "retouch",
    "geometry",
    "Clone, heal, fill, blur with drawn shapes; wavelet decomposition gives frequency-separation skin retouching. Source is manual (no content-aware).",
    [
        P(
            "algorithm",
            None,
            None,
            "enum clone|heal|fill|blur",
            ["remove the dust spot", "heal the blemish"],
        ),
        P("wavelet scales", 0, 8, "scale count", ["frequency separation"]),
        P("blur radius", 1, 20, "px", ["smooth the skin"]),
        P("fill color/brightness", None, None, "color", []),
        P("shape opacity", 50, 100, "%", []),
    ],
    [
        "remove the dust spots",
        "clean up the blemishes",
        "remove the pimple",
        "smooth the skin",
        "frequency separation",
        "remove the power line (small)",
    ],
    slug="retouch",
)
mod(
    "spots",
    "spot removal",
    "geometry",
    "Legacy clone-only spot tool; deprecated in favour of retouch.",
    [P("shape size", 5, 100, "px", [])],
    ["remove a spot"],
    slug="spot-removal",
    host="deprecated",
)
mod(
    "vignette",
    "vignetting",
    "finish",
    "Creative vignette (darken/brighten edges, desaturate) with shape and centre controls; add dither to avoid banding.",
    [
        P(
            "brightness",
            -1.0,
            0.2,
            "ratio",
            ["add a vignette", "darken the corners", "subtle vignette"],
        ),
        P("saturation", -0.5, 0.0, "ratio", []),
        P("scale", 40, 100, "%", []),
        P("fall-off strength", 30, 100, "%", []),
        P("shape", 0.8, 2.0, "ratio", []),
    ],
    [
        "add a vignette",
        "darken the edges",
        "draw the eye in",
        "remove the vignette (use lens correction)",
    ],
    slug="vignetting",
)
mod(
    "grain",
    "grain",
    "finish",
    "Adds film-like grain (luminance only) scaled by simulated ISO coarseness.",
    [
        P("coarseness", 400, 3200, "ISO", ["finer grain", "chunkier grain"]),
        P("strength", 5, 40, "%", ["add some grain", "film grain"]),
        P("midtones bias", 0, 100, "%", []),
    ],
    ["add grain", "make it look like film", "Tri-X grain"],
)
mod(
    "borders",
    "framing",
    "finish",
    "Adds a coloured border/frame with aspect ratio and position; a frame line too.",
    [
        P("border size", 1, 15, "% of image", ["add a white border"]),
        P("aspect", None, None, "enum", ["square frame for Instagram"]),
        P("border color", None, None, "rgb", []),
        P("frame line size", 0, 2, "%", []),
    ],
    ["add a border", "white frame", "Instagram square with white bars"],
    slug="framing",
)
mod(
    "watermark",
    "watermark",
    "finish",
    "Overlays an SVG/PNG watermark with variable text (e.g. $(EXIF.DATE), copyright).",
    [
        P("opacity", 20, 80, "%", ["subtle watermark"]),
        P("scale", 5, 30, "%", []),
        P("alignment", None, None, "3x3 grid", ["bottom right"]),
        P("text", None, None, "string with variables", ["add my copyright"]),
    ],
    ["add my watermark", "copyright in the corner", "sign it"],
)
mod(
    "blurs",
    "blurs",
    "finish",
    "Lens (bokeh), motion or gaussian blur; used masked to fake shallow depth or motion.",
    [
        P("blur type", None, None, "enum lens|motion|gaussian", ["fake bokeh", "motion blur"]),
        P("radius", 4, 64, "px", ["blur the background"]),
        P("blades / concavity / rotation", None, None, "lens shape", []),
    ],
    ["blur the background", "fake bokeh", "add motion blur"],
)
mod(
    "bloom",
    "bloom",
    "finish",
    "Glow around highlights (Lab, display-referred); diffuse-or-sharpen bloom preset is the scene-referred alternative.",
    [
        P("size", 10, 40, "%", []),
        P("threshold", 80, 95, "%", []),
        P("strength", 10, 50, "%", ["dreamy glow"]),
    ],
    ["dreamy glow", "Orton effect", "halation-ish"],
)
mod(
    "soften",
    "soften",
    "finish",
    "Orton-style soft glow by blending a blurred, brightened copy.",
    [
        P("size", 20, 80, "%", []),
        P("saturation", 50, 100, "%", []),
        P("brightness", 0, 1, "EV", []),
        P("mix", 20, 50, "%", ["soft dreamy look"]),
    ],
    ["soft focus", "Orton glow"],
)
mod(
    "monochrome",
    "monochrome",
    "monochrome",
    "Quick Lab B&W with a movable colour filter; the color calibration gray tab is the preferred modern method.",
    [
        P("filter position", None, None, "a,b point", ["red filter", "green filter"]),
        P("filter size", 20, 100, "%", []),
        P("highlights", 0, 100, "%", []),
    ],
    ["make it black and white", "B&W", "monochrome"],
)
mod(
    "negadoctor",
    "negadoctor",
    "look",
    "Inverts and corrects scanned colour/B&W film negatives (Aurelien Pierre).",
    [
        P("film stock", None, None, "enum color|black and white", []),
        P("D min", None, None, "color picker", []),
        P("paper black / grade", None, None, "ratio", []),
    ],
    ["invert my film scan", "convert the negative"],
)
mod(
    "colorreconstruct",
    "color reconstruction",
    "raw_technical",
    "Refills colour into clipped white areas from neighbours; mostly superseded by highlight reconstruction/filmic.",
    [
        P("lightness threshold", 90, 100, "%", []),
        P("spatial extent", 100, 800, "px", []),
        P("range extent", 10, 40, "%", []),
        P("precedence", None, None, "enum none|saturated colors|hue", []),
    ],
    ["colour back into blown areas"],
    slug="color-reconstruction",
)
mod(
    "dither",
    "dither or posterize",
    "finish",
    "Adds tiny noise to prevent banding in smooth gradients (skies, vignettes) on 8-bit export.",
    [
        P(
            "method",
            None,
            None,
            "enum Floyd-Steinberg 8-bit RGB|random|posterize",
            ["fix banding in the sky"],
        )
    ],
    ["fix banding", "stop the posterisation"],
    slug="dither-or-posterize",
)
mod(
    "censorize",
    "censorize",
    "finish",
    "Pixelates/blurs a masked area (faces, plates).",
    [
        P("radius", 5, 50, "px", []),
        P("pixelate", 5, 50, "px", ["pixelate the licence plate"]),
        P("noise", 0, 1, "ratio", []),
    ],
    ["blur the face", "pixelate the plate"],
)
mod(
    "enlargecanvas",
    "enlarge canvas",
    "geometry",
    "Adds canvas around the image (e.g. for a frame or text).",
    [
        P("percent left/right/top/bottom", 0, 50, "%", ["more canvas"]),
        P("color", None, None, "enum", []),
    ],
    ["extend the canvas"],
    slug="enlarge-canvas",
)

# ---------- masks (blend/masking system, not iops) ----------
for mid, ui, plain, params, phr in [
    (
        "mask_drawn",
        "drawn masks",
        "Hand-drawn circles, ellipses, paths, brushes, gradients attached to any module; combine with set operations.",
        [
            P(
                "shape",
                None,
                None,
                "enum circle|ellipse|path|brush|gradient",
                ["only the face", "just the sky"],
            ),
            P("feather/border", 5, 50, "% of size", ["softer edge"]),
            P("opacity", 20, 100, "%", []),
        ],
        ["just on the face", "only this area", "paint in", "gradient on the sky"],
    ),
    (
        "mask_parametric",
        "parametric masks",
        "Select pixels by channel ranges (L, chroma, hue, g, R/G/B, Jz etc.) on input and output; the luminosity-mask equivalent.",
        [
            P(
                "channel range (e.g. hue, L)",
                0,
                100,
                "%",
                ["only the blues", "only the highlights"],
            ),
            P("feathering radius", 1, 20, "px", []),
            P("mask blur", 1, 30, "px", []),
            P("details threshold", -50, 50, "%", ["edges only"]),
        ],
        ["only the highlights", "only the blues", "luminosity mask", "exclude skin"],
    ),
    (
        "mask_raster",
        "raster masks",
        "Reuse the final mask of an earlier module in a later one (and 5.2 adds external PFM raster masks with refine controls).",
        [
            P("source mask", None, None, "module instance", ["same mask as before"]),
            P("invert", None, None, "bool", ["the opposite area"]),
        ],
        ["reuse that mask", "apply to everything except the subject"],
    ),
    (
        "mask_ai_object",
        "AI object mask (5.6+)",
        "Click-to-segment subject mask (SAM2.1/SegNext), vectorised into editable paths; optional, disabled by default, needs AI build and models. NOT on 5.4.1 host.",
        [P("prompt points", 1, 10, "clicks", ["select the subject", "mask the person"])],
        ["select the subject", "mask the sky", "select the person"],
    ),
]:
    mods.append(
        {
            "id": mid,
            "ui_name": ui,
            "purpose_group": "masks",
            "plain_words": plain,
            "params": params,
            "phrases": phr,
            "on_host_5_4_1": "no (5.6+)" if mid == "mask_ai_object" else "yes",
            "source": "https://docs.darktable.org/usermanual/development/en/darkroom/masking-and-blending/masks/"
            + {
                "mask_drawn": "drawn/",
                "mask_parametric": "parametric/",
                "mask_raster": "raster/",
                "mask_ai_object": "ai-masking/",
            }[mid],
            "seen": SEEN,
        }
    )
mods.append(
    {
        "id": "neural_restore",
        "ui_name": "neural restore (5.6+)",
        "purpose_group": "detail",
        "plain_words": "AI raw denoise, denoise and upscale (NIND UNet, RawNIND, RealPLKSR) with strength slider; optional AI build only. NOT on 5.4.1 host.",
        "params": [
            P("task", None, None, "enum raw denoise|denoise|upscale", ["AI denoise", "upscale 2x"]),
            P("strength", 0, 100, "%", []),
        ],
        "phrases": ["AI denoise", "upscale it", "super resolution"],
        "on_host_5_4_1": "no (5.6+)",
        "source": "https://github.com/darktable-org/darktable/releases/tag/release-5.6.0",
        "seen": SEEN,
    }
)

# ---------- workflows ----------
W = lambda op, module, cond=None: (
    {"op": op, "module": module, "condition": cond} if cond else {"op": op, "module": module}
)
base_open = [
    W(
        "check raw overexposure (shift+O)",
        "highlights",
        "if clipped raw channels then enable highlight reconstruction (inpaint opposed; guided laplacians for speculars)",
    ),
    W("enable lens profile before exposure", "lens"),
]
workflows = [
    {
        "genre": "portrait",
        "steps": base_open
        + [
            W(
                "set exposure on the face (skin roughly +0.5..+1 EV over mid-grey for Caucasian skin; judge by eye)",
                "exposure",
            ),
            W(
                "white balance on a neutral or skin via picker",
                "channelmixerrgb",
                "if mixed window+tungsten then second masked instance",
            ),
            W("tone map, moderate contrast", "sigmoid"),
            W("lift faces in shadow, tame bright background", "toneequal"),
            W("crop and straighten", "crop"),
            W("remove blemishes, stray hairs; wavelet scale for skin", "retouch"),
            W(
                "soften skin texture gently (negative contrast on fine scales, masked to skin)",
                "atrous",
                "if close-up beauty",
            ),
            W("skin hue/sat tidy (oranges/reds)", "colorequal", "if skin too red/ruddy"),
            W("grade: warm highlights, low global saturation boost", "colorbalancergb"),
            W("sharpen eyes only (masked)", "diffuse"),
            W("subtle vignette", "vignette"),
        ],
    },
    {
        "genre": "landscape",
        "steps": base_open
        + [
            W("set mid-grey exposure", "exposure"),
            W("set white/black relative exposure via pickers", "agx"),
            W("pull the sky down, open foreground shadows", "toneequal"),
            W(
                "darken sky further with gradient",
                "graduatednd",
                "if sky still bright vs foreground",
            ),
            W("dehaze distant hills", "hazeremoval", "if atmospheric haze"),
            W("straighten horizon", "ashift"),
            W("local contrast / clarity", "bilat"),
            W("HSL: mute neon greens, deepen blue sky", "colorequal"),
            W("chroma + saturation per zone", "colorbalancergb"),
            W("sharpen (lens deblur soft)", "diffuse"),
            W("crop", "crop"),
        ],
    },
    {
        "genre": "night_astro",
        "steps": [
            W("remove hot pixels", "hotpixels", "if long exposure"),
            W("lens correction incl. vignetting", "lens"),
            W("raise exposure (often +1..+3 EV)", "exposure"),
            W("white balance sky to neutral/blue-ish (4000-4500K)", "channelmixerrgb"),
            W(
                "denoise sky (NLM / astrophoto denoise), heavier chroma",
                "denoiseprofile",
                "if ISO>=3200 use astrophoto denoise or stronger strength",
            ),
            W("tone map, low contrast to keep stars", "sigmoid"),
            W(
                "dark sky background down, Milky Way core up (parametric mask on luminance)",
                "toneequal",
            ),
            W("local contrast on the core only (masked)", "bilat"),
            W(
                "reduce light-pollution cast near horizon (gradient-masked calibration)",
                "channelmixerrgb",
                "if orange skyglow",
            ),
            W(
                "foreground separately masked exposure",
                "exposure",
                "if foreground is a separate blended frame then do stacking/blending outside darktable",
            ),
        ],
    },
    {
        "genre": "backlit",
        "steps": base_open
        + [
            W("expose for the subject, accept sky going bright", "exposure"),
            W("raise white relative exposure to hold sky", "sigmoid"),
            W("lift the subject, compress the bright background", "toneequal"),
            W("reduce veiling glare / flare haze", "hazeremoval", "if flare wash"),
            W("warm rim light / golden highlights", "colorbalancergb"),
            W("dodge the face with masked exposure instance", "exposure"),
            W("if sun blown to magenta, reconstruct", "highlights", "if magenta/cyan sun disk"),
        ],
    },
    {
        "genre": "indoor_mixed_light",
        "steps": base_open
        + [
            W("white balance main light (tungsten) with picker on a neutral", "channelmixerrgb"),
            W(
                "second instance for window/daylight area, masked (drawn + parametric), or reuse inverted raster mask",
                "channelmixerrgb",
                "if two illuminants clash",
            ),
            W("exposure and tone map", "sigmoid"),
            W("balance window vs interior brightness", "toneequal"),
            W("denoise (indoor ISO)", "denoiseprofile"),
            W("tidy colour casts on walls (desat yellow/orange)", "colorequal"),
        ],
    },
    {
        "genre": "product",
        "steps": base_open
        + [
            W("neutralise white background with picker (or colour checker)", "channelmixerrgb"),
            W("set exposure so background reaches just-white", "exposure"),
            W("tone map with high white relative exposure", "sigmoid"),
            W("correct perspective and square up", "ashift"),
            W("clean dust / scratches", "retouch"),
            W("crisp detail", "diffuse"),
            W("brand colour accuracy check / fix hue", "colorequal", "if brand colour off"),
            W("export sRGB + TIFF master", "export"),
        ],
    },
    {
        "genre": "street_bw",
        "steps": base_open
        + [
            W("exposure", "exposure"),
            W("convert with gray tab (film presets, or red/orange filter mix)", "channelmixerrgb"),
            W("contrast: tone map + S curve", "sigmoid"),
            W("dodge & burn faces / backgrounds", "toneequal"),
            W("local contrast for grit", "bilat"),
            W("grain (ISO 800-1600 coarseness)", "grain"),
            W("vignette", "vignette"),
            W("optional tone (sepia/selenium via split toning or colorize)", "splittoning"),
        ],
    },
    {
        "genre": "sports_wildlife",
        "steps": base_open
        + [
            W("exposure on subject", "exposure"),
            W("denoise high ISO (profiled NLM, raise strength)", "denoiseprofile", "if ISO>=3200"),
            W("tone map", "sigmoid"),
            W("crop tight (subject large in frame)", "crop"),
            W("sharpen subject only, not the bokeh (masked)", "diffuse"),
            W("clarity on feathers/fur masked", "bilat"),
            W("darken distracting bright background", "toneequal"),
            W("copy to burst, adjust exposure outliers", "exposure"),
        ],
    },
    {
        "genre": "architecture",
        "steps": base_open
        + [
            W("fix converging verticals (auto structure then fit vertical)", "ashift"),
            W("exposure", "exposure"),
            W("tone map, hold windows/sky", "agx"),
            W("balance interior/exterior", "toneequal"),
            W("chromatic aberration clean", "cacorrectrgb", "if fringes on window frames"),
            W("neutral whites (mixed light handling)", "channelmixerrgb"),
            W("crisp sharpening", "diffuse"),
            W("crop to remove perspective-fix edges", "crop"),
        ],
    },
    {
        "genre": "food",
        "steps": base_open
        + [
            W("white balance so plates are neutral / slightly warm", "channelmixerrgb"),
            W("exposure bright and airy", "exposure"),
            W("tone map softer contrast", "sigmoid"),
            W("warm and saturate reds/oranges, calm blues", "colorequal"),
            W("texture on the food (masked local contrast)", "bilat"),
            W("clean crumbs/spots", "retouch"),
            W("subtle vignette", "vignette"),
        ],
    },
    {
        "genre": "real_estate",
        "steps": base_open
        + [
            W("verticals straight (mandatory)", "ashift"),
            W("exposure bright", "exposure"),
            W("tone map, protect windows (window pull)", "sigmoid"),
            W(
                "bring windows down / open rooms",
                "toneequal",
                "if single exposure; if bracketed, HDR merge in lighttable 'create HDR' (DNG) first",
            ),
            W("neutralise mixed light, walls white", "channelmixerrgb"),
            W("lens distortion off lines", "lens"),
            W("mild local contrast", "bilat"),
            W("sync to the whole shoot via selective paste; per-room exposure tweaks", "history"),
        ],
    },
    {
        "genre": "events",
        "steps": base_open
        + [
            W("exposure (flash / venue light)", "exposure"),
            W("white balance per lighting setup (one per venue zone)", "channelmixerrgb"),
            W("tone map", "sigmoid"),
            W("denoise high ISO", "denoiseprofile"),
            W("lift faces", "toneequal"),
            W("crop / straighten", "crop"),
            W("hero edit then paste to set, match exposure via area exposure mapping", "exposure"),
            W("export client proofs with watermark", "export"),
        ],
    },
]

bulk = [
    {
        "name": "import_and_cull",
        "steps": [
            "import (add to library / copy & import)",
            "culling mode X (fixed) or ctrl+X (dynamic) side-by-side with synced zoom",
            "rate 0-5 via number keys, R to reject, F1-F5 colour labels",
            "filter collection by rating/label",
            "group bursts, pick best of group",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/lighttable/digital-asset-management/star-color/",
        "seen": SEEN,
    },
    {
        "name": "hero_then_propagate",
        "steps": [
            "primary grade on each image first (exposure, color calibration / checker profile) so all are neutral",
            "edit a reference (hero) image",
            "selective copy the secondary grade (tone mapper, color balance rgb, looks)",
            "selective paste in APPEND mode so per-image primary grade survives",
            "fine-tune per image: area exposure mapping to a control sample, tone-mapper white picker, color calibration area mapping",
            "if background changed, add a later-instance secondary tweak rather than altering the shared grade",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/guides-tutorials/batch-editing/",
        "seen": SEEN,
    },
    {
        "name": "copy_paste_history",
        "steps": [
            "ctrl+C / ctrl+V copies full stack in append mode",
            "ctrl+shift+C / ctrl+shift+V selective copy/paste dialog",
            "full copy excludes orientation, lens correction, raw black/white point, rotate pixels, scale pixels, white balance and deprecated modules unless selectively pasted",
            "overwrite mode irrevocably replaces the target history",
            "compress history / discard history are not undoable",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/module-reference/utility-modules/lighttable/history-stack/",
        "seen": SEEN,
    },
    {
        "name": "styles",
        "steps": [
            "create style from history stack (choose modules, optionally 'reset' a module)",
            "name with '|' for hierarchy e.g. 'client|warm portrait'",
            "apply by double-click or shortcut, append or overwrite, optionally onto a duplicate",
            "chain several styles by applying in append mode in order",
            "export/import .dtstyle XML to share",
            "camera styles group (5.0+) approximates camera JPEG looks for 500+ bodies; auto-apply via Lua script official/camera style",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/module-reference/utility-modules/shared/styles/",
        "seen": SEEN,
    },
    {
        "name": "module_presets",
        "steps": [
            "save module settings as preset from the hamburger menu",
            "auto-apply presets by camera/lens/ISO/format filters (e.g. lens correction, denoise per ISO)",
            "presets hierarchical via '|' since 5.2",
            "auto presets only apply on first darkroom open or after history discard",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/darkroom/processing-modules/presets/",
        "seen": SEEN,
    },
    {
        "name": "batch_export_profiles",
        "steps": [
            "export module presets: web (JPEG, long edge 2048, sRGB, quality 85-92), print (TIFF 16-bit, AdobeRGB/ProPhoto, full size), client proof (JPEG 1600px + watermark style)",
            "filename templates with variables like $(FILE_NAME)_$(SEQUENCE)",
            "style applied at export time (append) e.g. watermark or sharpen-for-web",
            "5.2+: multi-preset export in one go",
            "darktable-cli for headless export with an XMP/style",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/module-reference/utility-modules/shared/export/",
        "seen": SEEN,
    },
    {
        "name": "sidecar_handling",
        "steps": [
            "XMP sidecar per image (name.ext.xmp) written on every history change by default",
            "duplicates get _01, _02 sidecars",
            "look for updated XMP on startup (preference) to pick up external edits",
            "load sidecar file / write sidecar files in history stack module",
            "exported files embed history; can be loaded back as a sidecar",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/overview/sidecar-files/sidecar/",
        "seen": SEEN,
    },
    {
        "name": "outlier_handling",
        "steps": [
            "sort edited set by exposure/time, view in culling or filmstrip",
            "outliers (different light) get area exposure mapping + color calibration area mapping to the memorised control sample",
            "if still off, extra later instance of the grading module rather than editing the shared one",
            "flag with colour label for review",
        ],
        "source": "https://docs.darktable.org/usermanual/development/en/guides-tutorials/batch-editing/",
        "seen": SEEN,
    },
    {
        "name": "time_lapse_deflicker",
        "steps": [
            "exposure module automatic mode (percentile + target level) pasted to all frames to deflicker"
        ],
        "source": M + "exposure/",
        "seen": SEEN,
    },
]

look_components = [
    {
        "name": "tone curve shape (contrast, toe, shoulder)",
        "module": "sigmoid|agx|rgbcurve",
        "params": {
            "contrast": "1.3-2.2",
            "skew/toe power": "shadow density",
            "rgbcurve": "S-curve with lifted black node for matte",
        },
    },
    {
        "name": "faded / matte blacks",
        "module": "agx|sigmoid|colorbalancergb",
        "params": {
            "agx look lift": "+0.01..+0.05",
            "sigmoid target black": "0.2-0.5%",
            "colorbalancergb global offset luminance": "+0.5..+2%",
        },
    },
    {
        "name": "primary palette shift (film primaries)",
        "module": "primaries|agx primaries",
        "params": {
            "red hue": "+/-5..15 deg",
            "blue hue toward cyan": "-5..-15 deg",
            "purity": "-10..+20%",
        },
    },
    {
        "name": "split toning / colour grade",
        "module": "colorbalancergb (4 ways)",
        "params": {
            "shadows lift hue": "190-220 deg (teal) chroma 3-10%",
            "highlights gain hue": "40-70 deg (warm) chroma 3-10%",
            "power (midtones)": "small hue/chroma",
        },
    },
    {
        "name": "saturation per zone",
        "module": "colorbalancergb",
        "params": {
            "saturation shadows": "+10..+30%",
            "saturation highlights": "-10..-30%",
            "global chroma": "+10..+30%",
            "brilliance highlights": "+5..+15%",
        },
    },
    {
        "name": "hue-specific palette (greens to olive, skin to peach)",
        "module": "colorequal",
        "params": {
            "green hue": "toward yellow +10..+20 deg",
            "green sat": "-20..-40%",
            "orange brightness": "+5..+15%",
        },
    },
    {
        "name": "colour harmony enforcement (5.6+)",
        "module": "colorharmonizer",
        "params": {"rule": "complementary/analogous", "strength": "20-60%"},
    },
    {
        "name": "grain",
        "module": "grain",
        "params": {"coarseness": "800-3200 ISO", "strength": "10-35%"},
    },
    {
        "name": "vignette",
        "module": "vignette",
        "params": {"brightness": "-0.2..-0.6", "saturation": "-0.1..-0.3", "scale": "60-90%"},
    },
    {
        "name": "halation",
        "module": "diffuse (masked to highlights, red channel blend) | bloom | spektrafilm (dev)",
        "params": {
            "diffuse 1st/4th order speed": "positive (blur)",
            "blend": "RGB red channel, parametric mask on highlights",
            "radius span": "8-32 px",
        },
    },
    {"name": "glow / Orton", "module": "soften|diffuse bloom preset", "params": {"mix": "20-40%"}},
    {
        "name": "LUT-based look",
        "module": "lut3d",
        "params": {
            "file": ".cube/.3dl/.png haldclut/.gmz in LUT root folder",
            "application color space": "match LUT (usually sRGB/REC.709)",
            "opacity": "30-100% via blend",
            "placement": "after tone mapper on a neutral image",
        },
    },
    {
        "name": "camera colour science / film sim without LUT",
        "module": "style (agx + channelmixerrgb + input profile) | darktable camera styles | colorchecker",
        "params": {
            "jssfr dtsolve Fujifilm styles": "need 5.4 (AgX)",
            "camera styles": "5.0+, 500+ bodies",
        },
    },
    {
        "name": "monochrome film emulation",
        "module": "channelmixerrgb gray tab + grain + rgbcurve",
        "params": {
            "gray preset": "film presets (e.g. Ilford/Kodak b&w film)",
            "red filter": "R>1, B<0 for dark skies",
        },
    },
    {
        "name": "physical film emulation (dev, post-5.6)",
        "module": "spektrafilm",
        "params": {
            "film stock": "choose stock",
            "print exposure": "auto",
            "replaces tone mapper": "yes, do not stack with sigmoid/agx/filmic",
        },
    },
    {
        "name": "toning (sepia/selenium/cyanotype)",
        "module": "splittoning|colorize|colorbalancergb",
        "params": {"highlights hue": "30-50 deg", "chroma": "5-20%"},
    },
]

cannot_do = [
    {
        "intent": "generative fill / add or invent content",
        "why": "darktable has no generative model; retouch only clones/heals from a user-chosen source",
        "right_response": "Refuse generative content; offer retouch clone/heal for small removals, or recommend a raster editor (GIMP, Photoshop) for the rest.",
    },
    {
        "intent": "content-aware removal of large objects (people, cars, poles across the frame)",
        "why": "retouch has no content-aware/resynthesizer mode (open feature request #15006); needs a manual source region, poor on large areas",
        "right_response": "Say it only works for small, simple backgrounds; offer clone/heal on small spots; suggest an external editor for big removals.",
    },
    {
        "intent": "sky replacement",
        "why": "no layer compositing of another image into masked areas (composite module overlays a whole image, no sky-aware masking on 5.4.1)",
        "right_response": "Refuse replacement; offer to improve the existing sky (tone equalizer, graduated density, colour equalizer blue, haze removal).",
    },
    {
        "intent": "AI subject / sky / person masks on 5.4.1",
        "why": "AI object mask exists only in 5.6+ with an optional AI build, disabled by default; not on the Scouter host",
        "right_response": "Use drawn + parametric masks; explain AI masking needs darktable 5.6 with AI enabled.",
    },
    {
        "intent": "AI denoise / upscaling / super-resolution on 5.4.1",
        "why": "neural restore arrived in 5.6 optional AI build",
        "right_response": "Use denoise (profiled)/astrophoto denoise; say upscaling isn't available on this version.",
    },
    {
        "intent": "face/body reshaping automatically, face-aware portrait sliders",
        "why": "no face detection; liquify is manual point/line warps",
        "right_response": "Offer manual liquify with explicit points or decline.",
    },
    {
        "intent": "automatic HDR merge of brackets / panorama stitching / focus stacking",
        "why": "darktable only has a basic 'create HDR' DNG merge in lighttable (same-framing raws); no panorama or focus stacking",
        "right_response": "Offer create-HDR for aligned brackets; recommend Hugin (pano) or external stacking tools; then edit the result.",
    },
    {
        "intent": "text/graphic design, adding shapes or typography beyond a watermark",
        "why": "only watermark (SVG/PNG with variables) and framing",
        "right_response": "Offer watermark/frame; otherwise a design tool.",
    },
    {
        "intent": "change the time of day, relight a scene, move the sun",
        "why": "no generative relighting; only tonal/colour adjustments",
        "right_response": "Offer warm grade + tone equalizer to suggest golden hour; say true relighting is impossible.",
    },
    {
        "intent": "remove motion blur / fix missed focus substantially",
        "why": "diffuse or sharpen lens deblur recovers mild softness only",
        "right_response": "Try lens deblur preset, set expectations that missed focus can't be recovered.",
    },
    {
        "intent": "recover fully clipped highlights with real detail",
        "why": "reconstruction disguises clipped areas plausibly, cannot recover absent data",
        "right_response": "Apply highlight reconstruction and explain the limit.",
    },
    {
        "intent": "remove reflections / glare from glasses automatically",
        "why": "no semantic tool",
        "right_response": "Manual retouch clone/heal only; decline if large.",
    },
    {"intent": "video editing", "why": "stills-only", "right_response": "Decline."},
    {
        "intent": "swap faces / change expressions / open closed eyes",
        "why": "no generative or face tools; also sensitive manipulation",
        "right_response": "Decline; suggest choosing another frame from the burst.",
    },
]

out = {
    "meta": {
        "title": "darktable editing universe taxonomy",
        "generated": SEEN,
        "darktable_host": "5.4.1 (Scouter host, /Applications/darktable.app)",
        "latest_release": "5.6.1 (2026-08-27)",
        "id_convention": "id = darktable iop op name (as in XMP darktable:operation) where one exists; mask_* and neural_restore are non-iop",
        "typical ranges": "practitioner working ranges, not hard slider limits; derived from manual + tutorials, medium confidence",
        "vault_note": "Knowledge/Research - darktable editing universe 2026-09-26.md",
    },
    "modules": mods,
    "workflows": workflows,
    "bulk": bulk,
    "look_components": look_components,
    "cannot_do": cannot_do,
}
p = "/private/tmp/claude-501/-Users-pratik/a8352cbe-ecd5-40d4-b210-f2080d32af41/scratchpad/gauntlet/taxonomy-modules.json"
json.dump(out, open(p, "w"), indent=1, ensure_ascii=False)
phr = sum(len(m["phrases"]) + sum(len(x["phrases"]) for x in m["params"]) for m in mods)
print(len(mods), phr, len(workflows), len(look_components), len(cannot_do), len(bulk))
