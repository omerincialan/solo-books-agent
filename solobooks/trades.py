"""Per-trade wording and expense categories. Pick one with TRADE in .env."""

TRADES = {
    "tree_service": {
        "label": "tree service owner",
        "categories": ["Fuel", "Equipment & parts", "Equipment rental", "Dump & disposal fees",
                       "Safety gear", "Vehicle", "Crew wages", "Subcontractors", "Insurance", "Phone & software",
                       "Marketing", "Meals", "Other"],
    },
    "mechanic": {
        "label": "mobile mechanic",
        "categories": ["Parts", "Tools", "Fuel", "Shop supplies", "Vehicle", "Crew wages", "Insurance",
                       "Phone & software", "Marketing", "Meals", "Other"],
    },
    "tennis_coach": {
        "label": "tennis coach",
        "categories": ["Equipment", "Court fees", "Travel", "Certification & training", "Insurance",
                       "Phone & software", "Marketing", "Meals", "Other"],
    },
}


def get_trade(name: str) -> dict:
    return TRADES.get(name, TRADES["tree_service"])
