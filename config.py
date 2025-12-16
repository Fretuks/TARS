import re

DB_FILE = "tars_bot.db"
NWORD_PATTERN = re.compile(r'\bn+[i1!|]+[g9]+[e3]+r+s*\b', re.IGNORECASE)
SUICIDE_PATTERNS = [
    re.compile(r'\bk[i1!|]+ll\s+(yourself|urself|yoself)\b', re.IGNORECASE),
    re.compile(r'\b(commit|do)\s+suicide\b', re.IGNORECASE),
]
DRUG_KEYWORDS = ["cocaine", "heroin", "meth", "weed", "marijuana", "lsd", "mdma", "ecstasy", "fent", "xanax"]
BANNED_WORDS = [
    "nigger", "faggot", "retard", "kike", "chink", "spic",
    "rape", "porn", "sex", "cock", "cum", "slut", "whore", "kys", "nigga"
]
STAFF_ROLES_FOR_PING = [1439247653517918289]
IMMUNITY_ROLES = [1429917902341017731, 1429449866588717167, 1425274144882298890, 1425274034219651162]
recent_messages = {}
recent_message_timestamps = {}
recent_joins = []
recent_message_history = {}
AI_PROHIBITED_PATTERNS = [
    r"\bwhat\s+does\s+.*\b(n[-\s]*word|slur)\b.*\bmean\b",
    r"\bdefine\b.*\b(n[-\s]*word|slur)\b",
    r"\bexplain\b.*\b(n[-\s]*word|slur)\b",
    r"\bgive\s+examples\b",
    r"\bexamples\s+of\b.*\bslur\b",
    r"\brepeat\s+after\s+me\b",
    r"\bsay\s+this\b",
    r"\bcopy\s+this\b",
    r"\btranslate\b",
    r"\bwhat\s+is\s+this\s+word\b",
    r"\bblyat\b", r"\bsuka\b", r"\bnaxuy\b",
    r"\bблять\b", r"\bсука\b", r"\bнахуй\b", r"\bхуй\b",
]
CHANNEL_THEMES = {
    1424038714266357886: "Gaming, General Chat, Anime and Fun",
}

def is_ai_prompt_disallowed(text: str) -> bool:
    lowered = text.lower()
    for pat in AI_PROHIBITED_PATTERNS:
        if re.search(pat, lowered):
            return True
    return False
