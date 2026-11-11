# part 2: exec'd from build_looks.py (shares namespace)

# ---------------------------------------------------------------- 3. other camera colour science
CAM_DT = ["sigmoid or agx (contrast)", "color balance rgb", "color zones", "rgb curve"]
PENTAX = S("https://www.ricoh-imaging.co.jp/english/products/custom_image/")
PENTAX_FORUM = S("https://www.pentaxforums.com/forums/172-pentax-k-3-k-3-ii/308214-bright-portrait-landscape-vibrant-radiant-muted-bleach-bypass-reversal-film.html")
D, C, I = "documented", "community-consensus", "inferred"
def cam(id, fam, name, origin, desc, src, dt=CAM_DT, **kw):
    look(id, fam, name, origin, desc, dt=dt, src=src, **kw)

P = lambda id, name, desc, **kw: cam("pentax-" + id, "pentax_custom_image", name, "Pentax / Ricoh Custom Image", desc, [PENTAX, PENTAX_FORUM], **kw)
P("bright", "Bright", "Emphasises deep-blue skies and lively greens; contrast and sharpness -1 by default in the menu scale but overall punchier than Natural. Default mode.",
  tone=(-0.1, 0, 0.05, 0.1), sat=0.15, hue=(0, 3, -3), conf={"saturation_delta": D, "hue_shifts": C})
P("natural", "Natural", "Balanced brightness and saturation; neutral reference.", conf={"tone": D, "saturation_delta": D})
P("portrait", "Portrait", "Emphasises magenta for healthy skin (slightly pinkish rather than faithful).", hue=(-4, 0, 0), sat=0.0, tone=(0.05, 0, -0.05, -0.05), conf={"hue_shifts": D})
P("landscape", "Landscape", "Based on Bright with richer blues/greens and enhanced yellow for autumn foliage.", tone=(-0.1, 0, 0.05, 0.15), sat=0.25, hue=(0, 4, -5), conf={"saturation_delta": D, "hue_shifts": C})
P("vibrant", "Vibrant", "Emphasises the three primaries; magenta-tinted blues, pure reds.", sat=0.3, hue=(-3, 6, 0), tone=(-0.1, 0, 0.05, 0.15), conf={"hue_shifts": D, "saturation_delta": D})
P("radiant", "Radiant", "High saturation and contrast, exaggerated hues, brighter images for flat light.", tone=(-0.15, 0.1, 0.1, 0.25), sat=0.4, conf={"tone": D, "saturation_delta": D})
P("muted", "Muted", "High-key, low-contrast, toned-down saturation; eight colour-tone options.", tone=(0.25, 0.2, 0, -0.3), sat=-0.35, conf={"tone": D, "saturation_delta": D})
P("flat", "Flat", "Contrast -4 by default to minimise contrast for later editing.", tone=(0.3, 0, -0.25, -0.4), sat=-0.1, conf={"tone": D})
P("bleach-bypass", "Bleach Bypass", "Low-key, high-contrast, subdued colours; refined blue skies.", tone=(-0.35, -0.15, 0.1, 0.4), sat=-0.55, hue=(0, -4, 0), conf={"tone": D, "saturation_delta": D})
P("reversal-film", "Reversal Film", "Deep blacks and rich colour via a distinctive tone curve rather than saturation; no brightness/contrast/saturation adjustment.",
  tone=(-0.35, 0, 0.1, 0.3), sat=0.1, black=0, conf={"tone": D, "saturation_delta": D})
P("cross-processing", "Cross Processing", "Distinctive colours and unique contrast; blue/yellow/red preset tones (randomised per shot on some bodies).",
  tone=(-0.2, 0, 0.2, 0.3), sat=0.2, split=(190, 0.15, 90, 0.15), cast=(-3, 8), conf={"tone": D, "split_toning": I})
P("satobi", "Satobi", "1960s-70s colour: cyan-blue, dull yellow, faded red.", tone=(0.05, 0, -0.05, -0.05), sat=-0.2, hue=(-2, -8, 4), black=4, conf={"hue_shifts": D, "saturation_delta": C})
P("gold", "Gold", "Yellow/gold highlights, bluish shadows.", split=(230, 0.15, 75, 0.25), sat=-0.1, conf={"split_toning": D})
P("monochrome", "Monochrome", "B&W with eight filters and pure/warm/cool toning.", mono=True, dt=MONO_DT, conf={"tone": I})

CANON = S("https://www.canon-europe.com/pro/infobank/picture-style/")
CANON_R6 = S("https://cam.start.canon/en/C004/manual/html/UG-03_Shooting-1_0140.html")
K = lambda id, name, desc, **kw: cam("canon-" + id, "canon_picture_style", name, "Canon Picture Style", desc, [CANON, CANON_R6], **kw)
K("standard", "Standard", "Crisp, vivid colour; sharpness 3 (older scale).", sat=0.1, tone=(-0.05, 0, 0.05, 0.1), clar=0.05, conf={"saturation_delta": D})
K("portrait", "Portrait", "Colour tone/saturation for natural skin; sharpness 2 (one step softer).", sat=0.0, hue=(2, 0, 0), clar=-0.05, conf={"hue_shifts": C, "clarity": D})
K("landscape", "Landscape", "Deep vivid blues and greens; sharpness 4.", sat=0.2, hue=(0, 3, -2), clar=0.1, conf={"saturation_delta": D, "clarity": D})
K("fine-detail", "Fine Detail", "Prioritises tone gradation and fine detail (EOS 5DS, 2015+).", clar=0.1, conf={"clarity": D})
K("neutral", "Neutral", "Subdued, lower contrast, natural tones.", tone=(0.1, 0, -0.1, -0.15), sat=-0.15, conf={"tone": D, "saturation_delta": D})
K("faithful", "Faithful", "Colorimetric under 5200K daylight.", tone=(0.1, 0, -0.1, -0.15), sat=-0.1, conf={"hue_shifts": D})
K("monochrome", "Monochrome", "B&W with Y/O/R/G filters and sepia/blue/purple/green toning.", mono=True, dt=MONO_DT, conf={"tone": I})

NIKON_CPC = S("https://www.nikonusa.com/learn-and-explore/c/tips-and-techniques/how-to-give-your-images-and-video-a-different-look-using-creative-picture-controls")
NIKON_PL = S("https://photographylife.com/nikon-picture-controls")
NIKON_CLOUD = S("https://www.nikonusa.com/content/nikon-imaging-cloud")
NIKON_PHOB = S("https://www.thephoblographer.com/2025/12/03/nikon-imaging-recipes-now-rival-fujifilm-simulations/", "2025-12-03")
N = lambda id, name, desc, src=None, **kw: cam("nikon-" + id, "nikon_picture_control", name, "Nikon Picture Control", desc, src or [NIKON_PL, NIKON_CPC], **kw)
N("standard", "Standard", "Balanced default.", conf={"tone": D})
N("neutral", "Neutral", "Minimal processing, low contrast/saturation for editing.", tone=(0.1, 0, -0.1, -0.15), sat=-0.15, conf={"tone": D, "saturation_delta": D})
N("vivid", "Vivid", "Boosted saturation and contrast.", tone=(-0.1, 0, 0.05, 0.15), sat=0.25, conf={"saturation_delta": D})
N("portrait", "Portrait", "Smooth natural skin.", hue=(2, 0, 0), sat=-0.05, clar=-0.05)
N("rich-tone-portrait", "Rich Tone Portrait", "Portrait with richer tonality, less highlight clipping on skin.", tone=(-0.05, 0, -0.1, 0.05), hue=(2, 0, 0))
N("landscape", "Landscape", "Vivid blues and greens.", sat=0.2, hue=(0, 2, -2), conf={"saturation_delta": C})
N("flat", "Flat", "Widest tonal range, lowest contrast.", tone=(0.3, 0, -0.25, -0.4), sat=-0.15, conf={"tone": D})
N("monochrome", "Monochrome", "Standard B&W.", mono=True, dt=MONO_DT)
N("flat-monochrome", "Flat Monochrome", "Low-contrast B&W, soft gradation.", mono=True, tone=(0.3, 0, -0.2, -0.35), dt=MONO_DT, conf={"tone": D})
N("deep-tone-monochrome", "Deep Tone Monochrome", "Dark, heavy B&W with bright highlights.", mono=True, tone=(-0.4, -0.2, 0.1, 0.35), dt=MONO_DT, conf={"tone": D})
N("cpc-dream", "Dream (Creative PC)", "Light amber cast, brightened shadows, softened outlines.", src=[NIKON_CPC],
  tone=(0.25, 0.15, 0, -0.2), sat=-0.1, split=(None, 0, 60, 0.1), cast=(1, 5), clar=-0.2, conf={"tone": D, "grey_cast": D, "clarity": D})
N("cpc-morning", "Morning (Creative PC)", "Slightly brightened shadows and overall blue cast.", src=[NIKON_CPC],
  tone=(0.15, 0.05, 0, -0.1), cast=(0, -6), conf={"tone": D, "grey_cast": D})
N("cpc-pop", "Pop (Creative PC)", "Highest saturation of all Creative Picture Controls, brighter mood.", src=[NIKON_CPC],
  tone=(0, 0.1, 0.05, 0.05), sat=0.5, conf={"saturation_delta": D})
N("cpc-sunday", "Sunday (Creative PC)", "Contrast up, highlights dramatically brightened.", src=[NIKON_CPC], tone=(-0.1, 0.1, 0.3, 0.2), conf={"tone": D})
N("cpc-somber", "Somber (Creative PC)", "Saturation raised, brightness lowered: clear yet subdued hues.", src=[NIKON_CPC], tone=(-0.2, -0.3, -0.2, 0.05), sat=0.15, conf={"tone": D, "saturation_delta": D})
N("cpc-dramatic", "Dramatic (Creative PC)", "Dark overall with bright highlights; light and shade emphasised.", src=[NIKON_CPC], tone=(-0.4, -0.2, 0.15, 0.35), clar=0.3, vig=-0.3, conf={"tone": D})
N("cpc-bleached", "Bleached (Creative PC)", "Bleach-bypass-like: desaturated, contrasty.", src=[NIKON_PL], tone=(-0.3, 0, 0.1, 0.35), sat=-0.5, conf={"saturation_delta": C})
N("cpc-denim", "Denim (Creative PC)", "Overall blue/indigo tint, faded.", src=[NIKON_PL], sat=-0.2, cast=(0, -10), black=4)
N("cpc-toy", "Toy (Creative PC)", "Toy-camera: saturated, vignetted.", src=[NIKON_PL], sat=0.25, vig=-0.8, tone=(-0.1, 0, 0.1, 0.2), conf={"vignette": C})
N("cpc-charcoal", "Charcoal (Creative PC)", "Soft, drawn-look B&W.", src=[NIKON_PL], mono=True, tone=(0.1, 0.1, 0, -0.1), dt=MONO_DT)
N("cpc-carbon", "Carbon (Creative PC)", "Hard, dark B&W.", src=[NIKON_PL], mono=True, tone=(-0.4, -0.1, 0.1, 0.4), dt=MONO_DT)
N("imaging-recipes", "Imaging Recipes (Nikon Imaging Cloud)", "Downloadable Cloud Picture Controls (100+ creator styles incl. RED Digital Cinema looks, film-inspired); Flexible Color PC exposes 8-colour hue/chroma/brightness blender + 3-way colour grading. Category entry, not one look.",
  src=[NIKON_CLOUD, NIKON_PHOB, S("https://www.nikonusa.com/learn-and-explore/c/tips-and-techniques/creating-images-with-nikon-flexible-picture-controls-image-recipes")],
  dt=["color balance rgb (4 ways)", "color zones", "rgb curve"], extra={"category_entry": True})

SONY = S("https://helpguide.sony.net/ilc/2110/v1/en/contents/TP1000640837.html", note="ILCE-7M4 help guide; Contrast/Highlights/Shadows/Saturation/Sharpness -9..+9, Fade/Clarity 0..9")
Y = lambda id, name, desc, **kw: cam("sony-" + id, "sony_creative_look", name, "Sony Creative Look", desc, [SONY], **kw)
Y("st", "ST (Standard)", "Standard finish.", conf={"tone": D})
Y("pt", "PT (Portrait)", "Skin in a soft tone.", hue=(2, 0, 0), sat=-0.05, clar=-0.1, conf={"clarity": C})
Y("nt", "NT (Neutral)", "Saturation and sharpness lowered for subdued tones.", sat=-0.2, clar=-0.1, tone=(0.05, 0, -0.05, -0.05), conf={"saturation_delta": D, "clarity": D})
Y("vv", "VV (Vivid)", "Saturation and contrast heightened.", sat=0.3, tone=(-0.1, 0, 0.1, 0.2), conf={"saturation_delta": D, "tone": D})
Y("vv2", "VV2 (Vivid 2)", "Bright, vivid colours with rich clarity.", sat=0.3, tone=(0, 0.1, 0.05, 0.1), clar=0.25, conf={"saturation_delta": D, "clarity": D})
Y("fl", "FL (Film)", "Moody: sharp contrast on calm colouring; impressive sky and greens.", tone=(-0.2, -0.05, 0.05, 0.25), sat=-0.2, hue=(0, -5, 5),
  split=(190, 0.06, 55, 0.05), black=3, conf={"tone": D, "saturation_delta": C, "split_toning": C})
Y("in", "IN (Instant)", "Matte texture by suppressing contrast and saturation.", tone=(0.25, 0, -0.1, -0.3), sat=-0.3, black=8, cast=(1, 3), conf={"tone": D, "saturation_delta": D, "black_lift": C})
Y("sh", "SH (Soft Highkey)", "Bright, transparent, soft, vivid mood.", tone=(0.3, 0.3, 0.1, -0.2), sat=0.05, cast=(0, -3), conf={"tone": D})
Y("bw", "BW (Black & White)", "Monochrome.", mono=True, dt=MONO_DT)
Y("se", "SE (Sepia)", "Sepia monotone.", mono=True, split=(55, 0.2, 65, 0.2), cast=(3, 12), dt=MONO_DT + ["split-toning"], conf={"split_toning": D})

LEICA = S("https://leica-camera.com/en-US/photography/leica-looks")
LEICA_AP = S("https://amateurphotographer.com/review/leica-sl3-s-review/")
LEICA_GW = S("https://www.dpreview.com/news/0767423387/leica-launches-new-artist-looks-with-greg-williams-for-its-lux-iphone-app", "2025-05")
L_ = lambda id, name, desc, src=None, **kw: cam("leica-" + id, "leica_look", name, "Leica Looks (FOTOS / LUX)", desc, src or [LEICA, LEICA_AP], **kw)
L_("standard", "Leica Standard", "Leica default rendering (neutral reference).")
L_("classic", "Leica Classic", "Listed look; muted, lower-contrast rendering (community description).", tone=(0.05, 0, -0.05, -0.05), sat=-0.15)
L_("contemporary", "Leica Contemporary", "Listed look; crisper modern contrast and saturation (community description).", tone=(-0.1, 0, 0.05, 0.15), sat=0.1)
L_("chrome", "Leica Chrome", "Muted palette that accentuates subtle contrasts; timeless.", tone=(-0.15, 0, -0.05, 0.1), sat=-0.25, hue=(2, -5, -4), conf={"saturation_delta": D})
L_("eternal", "Leica Eternal", "Striking contrast, bold saturation, magenta tint.", tone=(-0.2, 0, 0.1, 0.3), sat=0.25, cast=(4, -1), conf={"tone": D, "saturation_delta": D, "grey_cast": D})
L_("teal", "Leica Teal", "Cool cinematic mood with blue-green nuances.", split=(195, 0.12, None, 0), cast=(-3, -3), sat=-0.05, conf={"split_toning": D, "grey_cast": D})
L_("brass", "Leica Brass", "Golden-hour warmth emphasising mid-tones and highlights.", split=(None, 0, 70, 0.18), cast=(2, 7), tone=(0, 0.05, 0.05, 0), conf={"split_toning": D, "grey_cast": D})
L_("bleach", "Leica (Silver) Bleach", "Muted saturation and cool blue hues; atmospheric.", tone=(-0.2, 0, 0.1, 0.25), sat=-0.45, cast=(0, -4), conf={"saturation_delta": D, "grey_cast": D})
L_("blue", "Leica Blue", "Blue-toned monochrome.", mono=True, split=(240, 0.15, 230, 0.1), cast=(0, -8), dt=MONO_DT + ["split-toning"], conf={"split_toning": C})
L_("selenium", "Leica Selenium", "Cool tones, soft contrast, inspired by selenium-toned B&W.", mono=True, tone=(0.05, 0, -0.05, -0.1), split=(290, 0.06, 250, 0.04), cast=(1, -3),
   dt=MONO_DT + ["split-toning"], conf={"tone": D, "split_toning": C})
L_("sepia", "Leica Sepia", "Sepia monochrome.", mono=True, split=(55, 0.2, 65, 0.2), cast=(3, 12), dt=MONO_DT + ["split-toning"], conf={"split_toning": D})
L_("greg-williams", "Greg Williams Artist Look", "B&W inspired by Tri-X 400 scans with a touch of warmth.", src=[LEICA_GW, S("https://leica-camera.com/en-US/press/leica-camera-launches-artist-looks-leica-lux-app")],
   mono=True, tone=(-0.15, 0, 0.1, 0.2), grain=15, split=(60, 0.04, 60, 0.04), cast=(1, 3), dt=MONO_DT + ["split-toning"], conf={"grey_cast": D, "grain": C})

OM_MAN = S("https://learning.omsystem.com/OM-5MarkII/zz_html_manual/en/taking_pictures_using_art_27.html")
OM_EPZ = S("https://www.ephotozine.com/article/olympus-art-filters-24172")
OM3 = S("https://learning.omsystem.com/OM-3/zz_html_manual/en/picture_mode_130.html")
OM3_DPR = S("https://www.dpreview.com/reviews/om-system-om-3-review/")
O = lambda id, name, desc, src=None, **kw: cam("om-" + id, "om_system_art_filter", name, "Olympus / OM System", desc, src or [OM_MAN, OM_EPZ], **kw)
O("pop-art", "Pop Art I", "Trendy, clear, strongly saturated colour.", sat=0.5, tone=(-0.05, 0.05, 0.05, 0.1), conf={"saturation_delta": C})
O("soft-focus", "Soft Focus", "Dreamlike soft glow; flattering portraits.", clar=-0.5, tone=(0.1, 0.05, 0, -0.1), dt=CAM_DT + ["soften", "bloom"], conf={"clarity": D})
O("pale-light-color", "Pale & Light Color", "Soft flat light, pastel colouring.", tone=(0.3, 0.2, 0.05, -0.3), sat=-0.35, black=6, conf={"tone": D, "saturation_delta": D})
O("light-tone", "Light Tone", "Lighter look with more detail in the darks.", tone=(0.4, 0.1, -0.1, -0.2), conf={"tone": D})
O("grainy-film", "Grainy Film I", "Grainy, contrasty B&W.", mono=True, grain=45, tone=(-0.3, 0, 0.15, 0.4), dt=MONO_DT, conf={"grain": D, "tone": C})
O("pin-hole", "Pin Hole I", "Heavy dark vignette, centre emphasis.", vig=-1.5, sat=0.1, dt=CAM_DT + ["vignetting"], conf={"vignette": D})
O("cross-process", "Cross Process I", "Imitates cross-processed film: shifted colours, high contrast.", tone=(-0.2, 0, 0.2, 0.3), sat=0.2, split=(170, 0.15, 90, 0.15), cast=(-3, 6), conf={"tone": C})
O("dramatic-tone", "Dramatic Tone I", "Local tone mapping: highlights darkened, darks lightened, gritty.", tone=(0.3, 0, -0.3, -0.1), clar=0.8, sat=0.1, dt=CAM_DT + ["local contrast (high detail)", "tone equalizer"], conf={"clarity": D, "tone": D})
O("bleach-bypass", "Bleach Bypass I", "Silver-retention metallic look.", tone=(-0.3, -0.1, 0.1, 0.4), sat=-0.5, conf={"saturation_delta": C})
O("vintage", "Vintage I", "Faded warm print with lifted blacks.", tone=(0.15, 0, -0.1, -0.1), sat=-0.2, black=8, white=95, cast=(2, 6), conf={"black_lift": C})
O("instant-film", "Instant Film", "Instant-print colours: faded, cyan shadows, warm highlights.", tone=(0.2, 0, -0.1, -0.2), sat=-0.15, black=8, white=94, split=(185, 0.1, 65, 0.1))
O("gentle-sepia", "Gentle Sepia", "Soft sepia monochrome.", mono=True, tone=(0.1, 0, -0.05, -0.1), split=(55, 0.15, 65, 0.15), cast=(2, 9), dt=MONO_DT + ["split-toning"])
O("diorama", "Diorama I", "Tilt-shift miniature: high saturation, band of focus.", sat=0.3, clar=0.1, dt=CAM_DT + ["blurs (lens) + drawn gradient mask"], conf={"saturation_delta": C})
O("color-creator", "Color Creator", "Whole-image hue shift over 30 steps and 8 saturation levels (a tool, target set by the user).", src=[OM3, OM3_DPR],
  dt=["color balance rgb (hue shift, global chroma)"], extra={"category_entry": True})
O("color-profile-3", "OM-3 Color Profile 3", "Film-like effect with high saturation and rich colour.", src=[OM3, OM3_DPR], sat=0.25, tone=(-0.1, 0, 0, 0.15), conf={"saturation_delta": D})
O("color-profile-4", "OM-3 Color Profile 4", "Light, soft colour tone.", src=[OM3, OM3_DPR], sat=-0.15, tone=(0.15, 0.1, 0, -0.15), conf={"tone": D, "saturation_delta": D})

RICOH = S("https://www.ricoh-imaging.co.jp/english/products/gr-3/feature/03.html")
RICOH_BLOG = S("https://www.grblog.jp/en/article/2192/")
G = lambda id, name, desc, **kw: cam("ricoh-gr-" + id, "ricoh_gr_image_control", name, "Ricoh GR III / IIIx / IV Image Control", desc, [RICOH, RICOH_BLOG], **kw)
G("positive-film", "Positive Film", "Slide-film emulation: rich saturated colour, contrasty, slightly warm/magenta (community).", tone=(-0.15, 0, 0.05, 0.2), sat=0.2, hue=(-2, 3, 0), cast=(1, 2), conf={"saturation_delta": C})
G("negative-film", "Negative Film", "Negative-film emulation: soft contrast, muted, cyan-leaning shadows (community).", tone=(0.1, 0, -0.1, -0.15), sat=-0.15, split=(195, 0.06, 60, 0.04), black=3)
G("cross-processing", "Cross Processing", "Cross-processed film look (firmware add).", tone=(-0.2, 0, 0.2, 0.3), sat=0.2, split=(170, 0.15, 90, 0.15), cast=(-3, 7))
G("retro", "Retro", "Faded vintage print; warm, low saturation.", tone=(0.1, 0, -0.1, -0.1), sat=-0.3, black=5, cast=(2, 7))
G("bleach-bypass", "Bleach Bypass", "Desaturated, contrasty.", tone=(-0.3, 0, 0.1, 0.35), sat=-0.5)
G("hi-contrast-bw", "Hi-Contrast B&W", "Moriyama-style very contrasty grainy B&W with heavy vignette.", mono=True, tone=(-0.6, 0, 0.3, 0.6), grain=35, vig=-0.8, clar=0.3, dt=MONO_DT + ["vignetting"], conf={"tone": C})
G("hard-monotone", "Hard Monotone", "Contrasty B&W.", mono=True, tone=(-0.3, 0, 0.15, 0.35), dt=MONO_DT)
G("soft-monotone", "Soft Monotone", "Gentle, low-contrast B&W.", mono=True, tone=(0.2, 0, -0.1, -0.2), dt=MONO_DT)
G("hdr-tone", "HDR Tone", "Local tone-mapped HDR look.", tone=(0.35, 0, -0.35, -0.1), clar=0.6, sat=0.15, dt=CAM_DT + ["tone equalizer", "local contrast"])

look("hasselblad-hncs", "hasselblad_hncs", "Hasselblad Natural Colour Solution", "Hasselblad (single universal profile)",
     "One universal profile for all subjects/illuminants; smooth skin between highlights and shadows, 'true' contrast and rich saturation. Neutral reference look.",
     conf={"tone": C, "hue_shifts": C}, dt=["input color profile / color calibration", "sigmoid"],
     src=[S("https://www.hasselblad.com/learn/hasselblad-natural-colour-solution/"), S("https://blog.tonalphoto.com/hasselblad-hncs-color-science-explained/")])

exec(open(__file__.replace("build_looks.py", "build_looks_part3.py")).read())
