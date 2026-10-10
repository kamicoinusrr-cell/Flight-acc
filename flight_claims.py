"""
Flight staff signup bot (discord.py 2.x)

Commands
  /flight post  - post a signup (flight number, event link, notes, slot counts)
  /flight edit  - find a flight by number, pick a member, pick their position,
                  and the original post is edited

Setup
  pip install -U discord.py
  set DISCORD_TOKEN in your environment, then: python flight_claims.py
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")

# On Railway, attach a Volume to the service: Railway sets
# RAILWAY_VOLUME_MOUNT_PATH automatically and flights.json is stored there so
# it survives redeploys. Override with DATA_PATH if you want a custom location.
_base = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", ".")
DATA_FILE = Path(os.getenv("DATA_PATH", str(Path(_base) / "flights.json")))

# Role pinged on every new post
PING_ROLE_ID = 1532328027613036546
# Also include @everyone like the original posts (set False to only ping the role)
PING_EVERYONE = True

# key, display label (Flight Engineer removed)
ROLES = [
    ("captain", "CAPTAIN"),
    ("first_officer", "FIRST OFFICER"),
    ("cabin_lead", "CABIN CREW LEAD"),
    ("cabin", "CABIN CREW"),
    ("ground_lead", "GROUND CREW LEAD"),
    ("ground", "GROUND CREW"),
    ("background", "BACKGROUND TRAFFIC"),
    ("backups", "BACKUPS"),
]
REMOVE = "__remove__"


# ---------------------------------------------------------------- storage
def load() -> dict:
    if not DATA_FILE.exists():
        return {}
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def save(data: dict) -> None:
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, DATA_FILE)  # atomic, avoids a corrupt file on crash


def norm(flight_number: str) -> str:
    """'aa 07' -> 'AA07'"""
    return re.sub(r"\s+", "", flight_number).upper()


# ---------------------------------------------------------------- rendering
def render(flight: dict) -> str:
    slots, caps = flight["slots"], flight["caps"]

    def count(k: str) -> str:
        return f"{len(slots[k])}/{caps[k]}"

    def tail(ids: list) -> str:
        return " - " + ", ".join(f"<@{i}>" for i in ids) if ids else " -"

    lines = [
        f"```\n{flight['display']}\n```",
        "",
        "\u25b8 Ask for your roles in the thread below.",
    ]
    if flight.get("notes"):
        lines.append(f"-# Notes: {flight['notes']}")
    lines.append("")

    lines.append(f"\u25aa CAPTAIN - [{count('captain')}]{tail(slots['captain'])}")
    lines.append(
        f"\u25aa FIRST OFFICER - [{count('first_officer')}]{tail(slots['first_officer'])}"
    )
    lines.append(
        f"\u25aa CABIN CREW - [LEAD: {count('cabin_lead')}] [{count('cabin')}]"
        f"{tail(slots['cabin_lead'] + slots['cabin'])}"
    )
    lines.append(
        f"\u25aa GROUND CREW - [LEAD: {count('ground_lead')}] [{count('ground')}]"
        f"{tail(slots['ground_lead'] + slots['ground'])}"
    )
    lines.append(
        f"\u25aa BACKGROUND TRAFFIC - [{count('background')}]{tail(slots['background'])}"
    )
    lines.append("")
    lines.append(f"\u25aa BACKUPS{tail(slots['backups'])}")
    lines.append("")

    pings = []
    if PING_EVERYONE:
        pings.append("@everyone")
    pings.append(f"<@&{PING_ROLE_ID}>")
    lines.append(" ".join(pings))
    lines.append("")
    lines.append(flight["link"])
    return "\n".join(lines)


async def update_message(bot: commands.Bot, flight: dict) -> None:
    channel = bot.get_channel(flight["channel_id"]) or await bot.fetch_channel(
        flight["channel_id"]
    )
    msg = await channel.fetch_message(flight["message_id"])
    # Editing never re-pings, but be explicit anyway
    await msg.edit(
        content=render(flight), allowed_mentions=discord.AllowedMentions.none()
    )


# ---------------------------------------------------------------- bot
class FlightBot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=commands.when_mentioned, intents=discord.Intents.default()
        )

    async def setup_hook(self):
        self.tree.add_command(flight_group)
        await self.tree.sync()


bot = FlightBot()

flight_group = app_commands.Group(
    name="flight",
    description="Flight staff signups",
    guild_only=True,
    default_permissions=discord.Permissions(manage_messages=True),
)


async def flight_autocomplete(interaction: discord.Interaction, current: str):
    needle = norm(current)
    return [
        app_commands.Choice(name=f["display"], value=key)
        for key, f in load().items()
        if needle in key
    ][:25]


# ---------------------------------------------------------------- /flight post
@flight_group.command(name="post", description="Post a flight signup")
@app_commands.describe(
    flight_number="Flight number, e.g. AA07 or LX38",
    event_link="Discord event link for the flight",
    notes="Optional notes shown under the header",
    cabin_crew_slots="Number of cabin crew slots (default 5)",
    ground_crew_slots="Number of ground crew slots (default 5)",
    background_slots="Number of background traffic slots (default 2)",
)
async def flight_post(
    interaction: discord.Interaction,
    flight_number: str,
    event_link: str,
    notes: Optional[str] = None,
    cabin_crew_slots: app_commands.Range[int, 0, 25] = 5,
    ground_crew_slots: app_commands.Range[int, 0, 25] = 5,
    background_slots: app_commands.Range[int, 0, 25] = 2,
):
    await interaction.response.defer(ephemeral=True)

    key = norm(flight_number)
    flight = {
        "display": key,
        "notes": notes,
        "link": event_link,
        "channel_id": interaction.channel_id,
        "message_id": None,
        "caps": {
            "captain": 1,
            "first_officer": 1,
            "cabin_lead": 1,
            "cabin": cabin_crew_slots,
            "ground_lead": 1,
            "ground": ground_crew_slots,
            "background": background_slots,
            "backups": None,
        },
        "slots": {k: [] for k, _ in ROLES},
    }

    msg = await interaction.channel.send(
        render(flight),
        allowed_mentions=discord.AllowedMentions(
            everyone=PING_EVERYONE,
            roles=[discord.Object(id=PING_ROLE_ID)],
            users=False,
        ),
    )
    flight["message_id"] = msg.id

    try:
        await msg.create_thread(
            name=f"{key} Staff Signups", auto_archive_duration=10080
        )
    except discord.HTTPException:
        pass  # channel may not support threads

    # latest post with a given flight number wins
    data = load()
    data[key] = flight
    save(data)

    await interaction.followup.send(f"Posted {key}: {msg.jump_url}", ephemeral=True)


# ---------------------------------------------------------------- /flight edit
class PositionSelect(discord.ui.Select):
    def __init__(self, flight: dict, key: str, member: discord.Member):
        self.key = key
        self.member = member
        options = []
        for k, label in ROLES:
            cap = flight["caps"][k]
            used = len(flight["slots"][k])
            desc = f"{used}/{cap}" if cap is not None else f"{used} signed up"
            options.append(
                discord.SelectOption(label=label.title(), value=k, description=desc)
            )
        options.append(
            discord.SelectOption(label="Remove from flight", value=REMOVE)
        )
        super().__init__(placeholder="Choose a position", options=options)

    async def callback(self, interaction: discord.Interaction):
        data = load()
        flight = data.get(self.key)
        if flight is None:
            await interaction.response.edit_message(
                content="That flight no longer exists.", view=None
            )
            return

        choice = self.values[0]
        slots, uid = flight["slots"], self.member.id

        if choice != REMOVE:
            cap = flight["caps"][choice]
            if uid in slots[choice]:
                await interaction.response.send_message(
                    f"{self.member.display_name} already has that position.",
                    ephemeral=True,
                )
                return
            if cap is not None and len(slots[choice]) >= cap:
                await interaction.response.send_message(
                    "That position is full. Pick another or use Backups.",
                    ephemeral=True,
                )
                return

        # a person holds one position at a time
        for ids in slots.values():
            if uid in ids:
                ids.remove(uid)
        if choice != REMOVE:
            slots[choice].append(uid)
        save(data)

        try:
            await update_message(interaction.client, flight)
        except discord.HTTPException:
            await interaction.response.edit_message(
                content="Saved, but I could not edit the original message.", view=None
            )
            return

        if choice == REMOVE:
            text = f"Removed {self.member.display_name} from {flight['display']}."
        else:
            label = dict(ROLES)[choice].title()
            text = f"Set {self.member.display_name} as {label} on {flight['display']}."
        await interaction.response.edit_message(content=text, view=None)


class PositionView(discord.ui.View):
    def __init__(self, owner_id: int, flight: dict, key: str, member: discord.Member):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.add_item(PositionSelect(flight, key, member))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.owner_id


@flight_group.command(name="edit", description="Assign a member to a position")
@app_commands.describe(
    flight_number="Flight number to edit",
    member="The person who claimed a role",
)
@app_commands.autocomplete(flight_number=flight_autocomplete)
async def flight_edit(
    interaction: discord.Interaction, flight_number: str, member: discord.Member
):
    key = norm(flight_number)
    flight = load().get(key)
    if flight is None:
        await interaction.response.send_message(
            f"No signup found for {key}.", ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"Choose a position for {member.display_name} on {flight['display']}:",
        view=PositionView(interaction.user.id, flight, key, member),
        ephemeral=True,
    )


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN environment variable is not set.")
    print(f"Using data file: {DATA_FILE.resolve()}")
    bot.run(TOKEN)
