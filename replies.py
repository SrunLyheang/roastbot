"""
Hardcoded replies + canned lines for RoastBot.

Keep this file simple to edit — it's meant to be the place you drop in new
jokes without touching main.py.

Three things live here:
  1. KEYWORD_REPLIES — keyword -> list of replies. When someone's message
     contains one of these words (whole-word match), the bot fires back a
     random reply from the list *instead* of calling the AI. This is the
     "hardcoded message handling" layer.
  2. COOLDOWN_LINES — snarky "slow down" messages sent when a chat is roasting
     too fast (protects the free AI quotas). No AI call is made.
  3. FALLBACK_ROASTS — generic burns used only when every AI provider fails
     (no keys set, all rate-limited, no internet, etc).

HOW MATCHING WORKS (see main.py -> match_keyword):
  We split the message into whole words with re.findall(r"[a-z']+", text.lower())
  and check if any KEYWORD_REPLIES key is in that list of words. This is a
  WHOLE-WORD match, not a substring match — so "hi" matches the message "hi there"
  but NOT the message "this". Add new keywords in lowercase, one word each.
"""

# keyword (lowercase, single word) -> list of possible replies (one picked at random)
KEYWORD_REPLIES = {
    "hello": [
        "Oh great, you're here. The group's average IQ just dropped 💀",
        "Hi. That's the most effort you've put into anything all week 🔥",
    ],
    "hi": [
        "Oh great, you're here. The group's average IQ just dropped 💀",
        "Hi. That's the most effort you've put into anything all week 🔥",
    ],
    "bro": [
        "Don't 'bro' me — we both know I'm the only one who replies to you 💀",
        "Bro? The last person who called you bro blocked you right after 😭",
    ],
    "lol": [
        "You laugh at everything because your life is the real joke 😭",
        "lol indeed — that's the sound of your dreams leaving 💀",
    ],
    "hungry": [
        "You're always hungry but never cooking. Grab a spoon and some self-awareness 🍜💀",
        "Hungry again? Your fridge fears you more than your future does 🔥",
    ],
    "food": [
        "You talk about food more than you talk to actual humans 🍜💀",
        "The food's not the problem — it's who's eating it 🔥",
    ],
    "game": [
        "Still losing in ranked AND in real life? Impressive consistency 🎮🔥",
        "Your K/D ratio is the only relationship you've ever maintained 💀",
    ],
    "gaming": [
        "Still losing in ranked AND in real life? Impressive consistency 🎮🔥",
        "Gaming all night so you don't have to face the daylight, huh 💀",
    ],
    "sorry": [
        "Apology rejected. Your existence is the real mistake 💀",
        "Sorry doesn't fix your personality, but it's a start 🔥",
    ],
    "sleep": [
        "Tired from what? You haven't done anything since 2024 😴🔥",
        "Go to sleep — it's the only thing you're actually good at 💀",
    ],
    "tired": [
        "Tired from what? You haven't done anything since 2024 😴🔥",
        "Tired? Carrying a whole personality this boring must be exhausting 💀",
    ],
    "rich": [
        "Your wallet is emptier than your search history is clean 💸💀",
        "Rich? The only thing you're loaded with is excuses 🔥",
    ],
    "money": [
        "Your wallet is emptier than your search history is clean 💸💀",
        "You chase money the way it chases away from you 💀",
    ],
    "handsome": [
        "The front camera disagrees. So does the back one 📸🔥",
        "Handsome? Even your reflection asked to be left on read 💀",
    ],
    "beautiful": [
        "The front camera disagrees. So does the back one 📸🔥",
        "Beauty is in the eye of the beholder — good thing everyone's looking away 💀",
    ],
    "love": [
        "The only thing that loves you back is your phone's low-battery warning 🔋💀",
        "Love? Your situationship left you on 'delivered' and so did life 🔥",
    ],
}

# Sent when a chat is roasting faster than the cooldown allows.
COOLDOWN_LINES = [
    "Slow down 💀 I need a second to think of how sad you all are.",
    "Give me a break — even I can't roast this fast without burning out 🔥",
    "One at a time, vultures. The disappointment isn't going anywhere ⏳💀",
    "Cooldown active. Use the time to reflect on your life choices 😌",
]

# Used only when EVERY AI provider fails (no keys, all rate-limited, no internet).
FALLBACK_ROASTS = [
    "My AI brain is offline, but even a calculator could tell you're a joke 💀",
    "I can't reach the AI right now, so here's a free one: look in the mirror 🔥",
    "Servers are down and you're still the biggest error in this chat 💀",
    "No connection — but honestly your personality already timed out 😭",
]
