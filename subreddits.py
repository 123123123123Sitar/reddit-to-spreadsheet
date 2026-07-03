"""Health-focused subreddit data for the picker.

The app is scoped to patient / health communities. Subreddits are organized
into condition *themes* (e.g. women's health, cancer, autoimmune). Themes serve
two jobs:

  1. Browse-by-theme chips in the UI (replacing the old category dropdowns).
  2. Relatedness: when a user selects a subreddit, the UI suggests other members
     of the same theme(s) — pick a women's-health community and it surfaces other
     women's-health communities, and so on.

A subreddit may belong to more than one theme (PCOS is both women's health and
endocrine), which is what makes the "you might also want" suggestions useful.

Public interface:
    THEMES  : dict[str, list[str]]  -- condition theme -> subreddits
    POOL    : list[str]             -- every health subreddit (autocomplete)
    POPULAR : list[str]             -- starter suggestions before any selection
"""

THEMES: dict[str, list[str]] = {
    "Women's health": [
        "endometriosis", "ENDO", "PCOS", "adenomyosis", "PMDD", "Fibroids",
        "Menopause", "Perimenopause", "TryingForABaby", "infertility",
        "birthcontrol", "WomensHealth", "Periods", "vulvodynia",
        "Interstitialcystitis",
    ],
    "Cancer": [
        "BreastCancer", "cancer", "lymphoma", "leukemia", "ovariancancer",
        "ColonCancer", "testicularcancer", "braincancer", "ProstateCancer",
        "thyroidcancer", "cancersurvivors",
    ],
    "Autoimmune & rheumatic": [
        "lupus", "rheumatoid", "MultipleSclerosis", "Hashimotos", "Sjogrens",
        "psoriasis", "PsoriaticArthritis", "ankylosingspondylitis",
        "Scleroderma", "MyastheniaGravis", "Crohns", "UlcerativeColitis",
        "Celiac",
    ],
    "Diabetes & endocrine": [
        "diabetes", "Type1Diabetes", "diabetes_t2", "Hypothyroidism",
        "hyperthyroidism", "thyroidhealth", "Hashimotos", "PCOS",
        "insulinresistance",
    ],
    "Digestive & GI": [
        "ibs", "Crohns", "UlcerativeColitis", "Celiac", "IBD", "GERD",
        "gastroparesis", "gallbladders", "Gastritis", "Constipation",
    ],
    "Chronic pain & neurological": [
        "ChronicPain", "Fibromyalgia", "migraine", "ChronicIllness",
        "ehlersdanlos", "POTS", "epilepsy", "ClusterHeadaches", "CRPS",
        "backpain", "Sciatica",
    ],
    "Respiratory": [
        "Asthma", "COPD", "CysticFibrosis", "sleepapnea",
    ],
    "Mental health": [
        "mentalhealth", "depression", "Anxiety", "bipolar", "BPD", "ptsd",
        "CPTSD", "OCD", "ADHD", "autism", "EatingDisorders", "SuicideWatch",
    ],
}

# Every distinct health subreddit (case-insensitive de-dupe), for autocomplete.
def _dedupe(names):
    seen, out = set(), []
    for n in names:
        if n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out

POOL: list[str] = sorted(
    _dedupe(n for names in THEMES.values() for n in names), key=str.lower
)

# A spread of common communities shown before the user has selected anything.
POPULAR: list[str] = [
    "endometriosis", "PCOS", "BreastCancer", "diabetes", "lupus",
    "ChronicPain", "Fibromyalgia", "ibs", "Crohns", "Asthma", "migraine",
    "Hypothyroidism",
]
