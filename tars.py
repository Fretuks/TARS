import random
def tars_text(text: str, category: str = "default") -> str:
    variants = {
        "success": [
            f"Mission accomplished: {text}",
            f"Objective complete — {text}",
            f"Task executed flawlessly: {text}",
            f"Done. {text}",
        ],
        "info": [
            f"Processing complete: {text}",
            f"Update: {text}",
            f"Noted. {text}",
            f"Acknowledged: {text}",
        ],
        "error": [
            f"Error detected: {text}",
            f"Minor malfunction: {text}",
            f"That didn’t go as planned — {text}",
            f"Negative. {text}",
        ],
        "default": [
            f"{text}",
            f"{text}.",
            f"Affirmative — {text}",
            f"Understood: {text}",
        ]
    }
    pool = variants.get(category, variants["default"])
    line = random.choice(pool)
    return f"{line}"