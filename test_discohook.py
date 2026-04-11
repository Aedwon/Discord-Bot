import json
import base64
from urllib.parse import urlparse, parse_qs
import discord

link = "https://discohook.org/?data=eyJtZXNzYWdlcyI6W3siZGF0YSI6eyJjb250ZW50IjoiSGVsbG8iLCJlbWJlZHMiOlt7InRpdGxlIjoiVGVzdCIsImltYWdlIjp7InVybCI6Imh0dHBzOi8vaW1hZ2VzLnVuc3BsYXNoLmNvbS9waG90by0xNTc1OTM2MTIyNTMwLTRiMWYyYzA4ZDQ3YyJ9fV19LCJ0eXBlIjoibWVzc2FnZSJ9XX0="
parsed = urlparse(link)
qs = parse_qs(parsed.query)
encoded = qs.get("data", [None])[0]
missing = len(encoded) % 4
if missing:
    encoded += "=" * (4 - missing)
decoded = base64.urlsafe_b64decode(encoded).decode("utf-8")
data = json.loads(decoded)
msg_data = data["messages"][0]["data"]
embeds = [discord.Embed.from_dict(e) for e in msg_data.get("embeds", [])]
for e in embeds:
    print(e.to_dict())

