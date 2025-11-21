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
STAFF_ROLES_FOR_PING = ["GetAnnoyedByTars"]
IMMUNITY_ROLES = ["Senior Admin, Head Admin, Co-Owner, Owner"]
recent_messages = {}
recent_message_timestamps = {}
recent_joins = []
recent_message_history = {}
