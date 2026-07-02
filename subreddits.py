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
