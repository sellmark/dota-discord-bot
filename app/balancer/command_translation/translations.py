import json
import os
from dota2_eu_ladder.settings import BASE_DIR

TRANSLATIONS = {}
#Language in which commands will be shown
LANG = "PL"

#import translations
with open(os.path.join(BASE_DIR, "./app/balancer/command_translation/EN.json"), encoding="utf-8") as english_file:
    english = json.load(english_file)
    TRANSLATIONS["EN"] = english

with open(os.path.join(BASE_DIR, "./app/balancer/command_translation/PL.json"), encoding="utf-8") as polish_file:
    polish = json.load(polish_file)
    TRANSLATIONS["PL"] = polish