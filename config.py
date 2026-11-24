"""ReportFix configuration: the ONE place to edit campus locations, rules and thresholds."""
import os

# ---------------------------------------------------------------- LLM
LLM_MODEL = os.environ.get("REPORTFIX_MODEL", "claude-haiku-5-5")
LLM_TIMEOUT_SECONDS = 15
DB_PATH = os.environ.get("REPORTFIX_DB", os.path.join(os.path.dirname(__file__), "reportfix.db"))

# ---------------------------------------------------------------- Departments
DEPARTMENTS = ["EE", "CSE", "IS", "Library", "Cafeteria", "Labs"]
ADMIN_QUEUE = "Admin"  # confidential (harassment) issues are routed here, never to a department

# ---------------------------------------------------------------- Campus locations
# canonical name -> default department + aliases. Aliases are matched on normalised text
# (lowercase, punctuation stripped, "lab3"/"lab no 3"/"lab #3" -> "lab 3", "labb" -> "lab").
# An alias starting with "=" is matched case-sensitively: needed for "IS", because in Roman Urdu
# "is lab mein" means "in THIS lab" and must not be routed to the IS Lab.
# Add explicit typo aliases here; we NEVER fuzzy-match to a "closest" place.
LOCATIONS = {
    "Lab 1": {"department": "Labs", "aliases": ["lab 1", "lab one"]},
    "Lab 2": {"department": "Labs", "aliases": ["lab 2", "lab two"]},
    "Lab 3": {"department": "Labs", "aliases": ["lab 3", "lab three"]},
    "Lab 4": {"department": "Labs", "aliases": ["lab 4", "lab four"]},
    "Lab 5": {"department": "Labs", "aliases": ["lab 5", "lab five"]},
    "CSE Lab 1": {"department": "CSE", "aliases": ["cse lab 1", "cs lab 1", "computer lab 1"]},
    "CSE Lab 2": {"department": "CSE", "aliases": ["cse lab 2", "cs lab 2", "computer lab 2"]},
    "EE Lab 1": {"department": "EE", "aliases": ["ee lab 1", "electrical lab 1"]},
    "EE Lab 2": {"department": "EE", "aliases": ["ee lab 2", "electrical lab 2"]},
    "IS Lab": {"department": "IS", "aliases": ["=IS lab", "=IS Lab", "=IS LAB", "information systems lab", "islab"]},
    "Library": {"department": "Library", "aliases": ["library", "libary", "librery", "libraray", "liberary",
                                                     "lib", "kitab ghar", "kitaab ghar"]},
    "Library Reading Hall": {"department": "Library", "aliases": ["library reading hall", "reading hall",
                                                                  "libary reading hall", "reading room"]},
    "Cafeteria": {"department": "Cafeteria", "aliases": ["cafeteria", "cafetaria", "caffeteria", "cafeteriya",
                                                         "canteen", "cantine", "cafe", "dhaba"]},
    "CSE Department": {"department": "CSE", "aliases": ["cse department", "cse dept", "cse office", "cs department",
                                                        "cs dept", "cse block", "cse building"]},
    "EE Department": {"department": "EE", "aliases": ["ee department", "ee dept", "ee office", "ee block",
                                                      "ee building", "electrical department"]},
    "IS Department": {"department": "IS", "aliases": ["=IS department", "=IS Department", "=IS dept",
                                                      "=IS office", "=IS block", "=IS building",
                                                      "information systems department"]},
}

# Words that name a place that is NOT on the campus list. If the rule engine sees one of these
# (or "lab/room/hall N" that is not listed) and no listed place, the report needs clarification.
UNLISTED_PLACE_WORDS = ["auditorium", "hostel", "parking", "mosque", "masjid", "gym", "ground", "admin block",
                        "main gate", "gate", "sports complex", "basement", "rooftop", "corridor", "hallway",
                        "washroom block", "clinic", "medical centre", "medical center", "bus"]

# ---------------------------------------------------------------- Categories
CATEGORIES = ["wifi", "ac", "projector", "power", "water", "cleanliness", "furniture", "equipment", "food",
              "books", "electrical_hazard", "fire", "gas", "medical", "harassment", "other"]
SAFETY_CATEGORIES = {"electrical_hazard", "fire", "gas", "medical"}

# Categories that are routed to a fixed department regardless of location.
CATEGORY_DEPARTMENT_OVERRIDE = {"food": "Cafeteria", "books": "Library", "harassment": ADMIN_QUEUE}

# Categories where "same location + same category" is by definition the same problem
# (a room has one WiFi, one AC system, ...). Code merges these deterministically even if the AI misses it.
SINGLETON_CATEGORIES = {"wifi", "ac", "projector", "power", "water", "fire", "gas", "electrical_hazard"}

# ---------------------------------------------------------------- Priority
TIERS = {1: "low", 2: "medium", 3: "high", 4: "critical"}
SEVERITY = {  # fixed severity table: category -> base tier. Text tone is never an input.
    "fire": 4, "gas": 4, "electrical_hazard": 4, "medical": 4,
    "harassment": 3, "power": 3,
    "wifi": 2, "ac": 2, "projector": 2, "water": 2, "equipment": 2,
    "cleanliness": 1, "furniture": 1, "food": 1, "books": 1, "other": 1,
}
MAX_CROWD_TIER = 3  # crowds can push a non-safety issue up to "high", never to "critical"
# Each rule = one tier bump. Requires BOTH distinct students AND credible weight.
CROWD_BUMPS = [
    {"min_students": 3, "min_weight": 3.0},
    {"min_students": 6, "min_weight": 6.0},
]
CROWD_FACTOR_WEIGHT_CAP = 10.0  # importance = tier * (1 + min(credible_weight, cap) / cap)

# ---------------------------------------------------------------- Trust
TRUST_START = 50
TRUST_MIN, TRUST_MAX = 0, 100
WEIGHT_MIN, WEIGHT_MAX = 0.2, 2.0  # weight = clamp(trust / 50, WEIGHT_MIN, WEIGHT_MAX)

TRUST_REPORT_CONFIRMED = +5        # your report's issue was confirmed fixed
TRUST_VOTE_CORRECT = +2            # your vote matched the final outcome
TRUST_VOTE_WRONG = -3              # you voted against the final outcome by a clear margin
TRUST_REPORT_FLAGGED_FAKE = -15    # staff/admin flagged your report as fake/spam
TRUST_INJECTION_ATTEMPT = -5       # your report tried to instruct the system
TRUST_DUPLICATE_SPAM = -3          # you reported the same issue again
CLEAR_MARGIN = 1.0                 # "clear margin" = winning side's weight exceeds losing side by this

# Anti-brigading: brand-new accounts (younger than this and with no positive trust history)
# vote/count at PROBATION_WEIGHT each, and ALL probation accounts together add at most PROBATION_TOTAL_CAP
# to one side of a vote or to an issue's crowd weight.
PROBATION_HOURS = 24
PROBATION_WEIGHT = 0.2
PROBATION_TOTAL_CAP = 0.6

# ---------------------------------------------------------------- Verification
REOPEN_MIN_DISPUTE_WEIGHT = 2.0
REOPEN_MIN_DISTINCT_DISPUTERS = 2   # established (non-probation) students
VERIFICATION_WINDOW_MINUTES = 60 * 48  # after this, a pending issue is finalized automatically
