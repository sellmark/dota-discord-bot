import json
import os

TRANSLATIONS = {}
#Language in which commands will be shown
LANG = "PL"

#import translations
with open(os.path.join(os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__))), "EN.json"), encoding='utf-8') as english_file:
    english = json.load(english_file)
    TRANSLATIONS["EN"] = english

with open(os.path.join(os.path.realpath(os.path.join(os.getcwd(), os.path.dirname(__file__))), "PL.json"), encoding='utf-8') as polish_file:
    polish = json.load(polish_file)
    TRANSLATIONS["PL"] = polish
