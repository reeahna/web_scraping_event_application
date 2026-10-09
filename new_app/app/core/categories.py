INITIAL_EVENT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("Arts and Culture", "arts-and-culture"),
    ("Music", "music"),
    ("Sports", "sports"),
    ("Family", "family"),
    ("Education", "education"),
    ("Community", "community"),
    ("Food and Drink", "food-and-drink"),
    ("Business", "business"),
    ("Nightlife", "nightlife"),
    ("Outdoors", "outdoors"),
    ("Government", "government"),
    ("Religious", "religious"),
    ("Health and Wellness", "health-and-wellness"),
    ("Other", "other"),
)

# What each seeded category covers, written for the AI categorizer's prompt and
# shown to administrators. Without these the model sees only "music: Music" and
# has to guess where the edges are. The seeder fills them in on categories that
# have no description yet, so an administrator's own wording is never replaced.
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "arts-and-culture": (
        "Theater, film screenings, comedy, galleries, museums, readings, and hands-on "
        "creative classes such as pottery, painting, crafts, or dance lessons."
    ),
    "music": "Concerts, live bands, DJs, open mics, recitals, and music festivals.",
    "sports": (
        "Games to watch or play: college and pro games, pickup leagues, races, "
        "climbing, and other athletic events."
    ),
    "family": "Events aimed mainly at young children and parents, such as storytime.",
    "education": (
        "Public talks, guest lectures, author events, and learning sessions open to "
        "anyone. Not professional training, certifications, or business seminars."
    ),
    "community": (
        "Festivals, fairs, markets, parades, volunteering, fundraisers, club and "
        "student-organization gatherings, and other neighborhood events."
    ),
    "food-and-drink": (
        "Tastings, food trucks, brewery or winery events, dinners, and cooking classes."
    ),
    "business": (
        "Career fairs, student networking, and entrepreneurship or startup events "
        "open to students."
    ),
    "nightlife": "Parties, trivia nights, bar events, drag shows, and late-night events.",
    "outdoors": "Hikes, nature walks, gardening, paddling, camping, and park events.",
    "government": "Public civic events such as town halls and voter registration drives.",
    "religious": "Worship services, faith gatherings, and religious celebrations.",
    "health-and-wellness": "Yoga, fitness classes, meditation, and wellness events.",
    "other": "Use only when nothing else reasonably fits.",
}
