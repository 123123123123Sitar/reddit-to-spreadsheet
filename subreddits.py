"""Curated starter subreddits for the picker UI.

These are just suggestions to seed the search bar / checkboxes. The UI also
lets the user type any custom subreddit name, so this list does not need to be
exhaustive.

Public interface:
    CATEGORIES : dict[str, list[str]]  -- subreddits grouped by theme
    ALL        : list[str]             -- sorted, de-duplicated set of every name
"""

CATEGORIES: dict[str, list[str]] = {
    "Health": [
        "endometriosis",
        "ENDO",
        "PCOS",
        "BreastCancer",
        "diabetes",
        "Fibromyalgia",
        "ChronicPain",
        "ChronicIllness",
        "migraine",
        "ibs",
        "Crohns",
        "Hypothyroidism",
        "cancer",
        "lupus",
        "Asthma",
    ],
    "Mental health": [
        "mentalhealth",
        "depression",
        "Anxiety",
        "bipolar",
        "ptsd",
        "OCD",
        "ADHD",
        "autism",
        "BPD",
        "socialanxiety",
    ],
    "Tech": [
        "programming",
        "technology",
        "learnprogramming",
        "Python",
        "javascript",
        "webdev",
        "sysadmin",
        "MachineLearning",
        "cybersecurity",
        "datascience",
        "linux",
        "gadgets",
    ],
    "Finance": [
        "personalfinance",
        "investing",
        "financialindependence",
        "stocks",
        "wallstreetbets",
        "CryptoCurrency",
        "Bogleheads",
        "financialplanning",
        "povertyfinance",
        "StockMarket",
    ],
    "General": [
        "AskReddit",
        "news",
        "worldnews",
        "todayilearned",
        "explainlikeimfive",
        "LifeProTips",
        "science",
        "books",
        "movies",
        "gaming",
    ],
}

# Sorted, de-duplicated set of every curated subreddit name.
ALL: list[str] = sorted({name for names in CATEGORIES.values() for name in names})

# A broader pool of popular subreddits used ONLY to power the search-box
# autocomplete (matched client-side). It is intentionally larger than the
# curated checkbox groups above; any name not here can still be added by
# typing it in full. Not exhaustive by design.
_SUGGEST_EXTRA: list[str] = [
    # general / default front page
    "AskReddit", "news", "worldnews", "todayilearned", "explainlikeimfive",
    "LifeProTips", "science", "askscience", "books", "movies", "television",
    "Music", "gaming", "funny", "pics", "aww", "food", "DIY", "history",
    "Futurology", "space", "dataisbeautiful", "Showerthoughts", "NoStupidQuestions",
    "OutOfTheLoop", "YouShouldKnow", "productivity", "getdisciplined",
    # relationships / advice
    "relationship_advice", "relationships", "AmItheAsshole", "tifu",
    "offmychest", "confession", "CasualConversation", "decidingtobebetter",
    # tech
    "programming", "learnprogramming", "Python", "javascript", "typescript",
    "webdev", "rust", "golang", "java", "cpp", "csharp", "reactjs", "django",
    "flask", "node", "linux", "sysadmin", "devops", "docker", "kubernetes",
    "aws", "MachineLearning", "artificial", "LocalLLaMA", "datascience",
    "dataengineering", "cybersecurity", "netsec", "selfhosted", "homelab",
    "buildapc", "pcmasterrace", "Android", "apple", "iphone", "technology",
    "gadgets", "webdesign", "ExperiencedDevs", "cscareerquestions",
    # health / medical
    "endometriosis", "ENDO", "PCOS", "BreastCancer", "cancer", "diabetes",
    "Type1Diabetes", "diabetes_t2", "Fibromyalgia", "ChronicPain",
    "ChronicIllness", "migraine", "ibs", "Crohns", "UlcerativeColitis",
    "Hypothyroidism", "thyroidhealth", "lupus", "Asthma", "Celiac",
    "ehlersdanlos", "POTS", "epilepsy", "MultipleSclerosis", "psoriasis",
    "eczema", "AskDocs", "medicine", "nursing", "Health", "WomensHealth",
    "TryingForABaby", "infertility", "Menopause", "birthcontrol",
    # mental health
    "mentalhealth", "depression", "Anxiety", "bipolar", "BPD", "ptsd",
    "CPTSD", "OCD", "ADHD", "autism", "aspergers", "socialanxiety",
    "SuicideWatch", "therapy", "getting_over_it", "mentalillness",
    # fitness / diet
    "Fitness", "loseit", "gainit", "bodyweightfitness", "running", "xxfitness",
    "nutrition", "keto", "intermittentfasting", "vegan", "EatCheapAndHealthy",
    "MealPrepSunday", "GYM", "flexibility",
    # finance
    "personalfinance", "financialindependence", "investing", "stocks",
    "wallstreetbets", "Bogleheads", "financialplanning", "povertyfinance",
    "StockMarket", "CryptoCurrency", "Bitcoin", "ethereum", "Frugal",
    "realestateinvesting",
]

# Sorted, de-duplicated autocomplete pool (curated groups + the extras above).
SUGGEST_POOL: list[str] = sorted(
    {name for names in CATEGORIES.values() for name in names} | set(_SUGGEST_EXTRA),
    key=str.lower,
)
