import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from tars_bot import bot, DISCORD_TOKEN
bot.run(DISCORD_TOKEN)