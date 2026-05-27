# ── Python 3.13 audioop compatibility shim ──────────────────────────────────
# discord.py 2.3.x hard-imports `audioop` which was removed in Python 3.13.
# This bot uses no voice features, so a stub is enough to let discord load.
import sys as _sys
if _sys.version_info >= (3, 13):
    try:
        import audioop  # already available (Python ≤ 3.12 or audioop-lts installed)
    except ModuleNotFoundError:
        try:
            # audioop-lts installs the real C extension as 'audioop'
            import importlib as _il
            _audioop = _il.import_module("audioop_lts")
            _sys.modules.setdefault("audioop", _audioop)
        except ModuleNotFoundError:
            # Last resort: minimal stub so discord.py can import cleanly.
            # All voice/audio calls become no-ops; text features work normally.
            from types import ModuleType as _MT
            _stub = _MT("audioop")
            for _fn in (
                "add", "adpcm2lin", "alaw2lin", "avg", "avgpp", "bias",
                "byteswap", "cross", "findfactor", "findfit", "findmax",
                "getsample", "lin2adpcm", "lin2alaw", "lin2lin", "lin2ulaw",
                "max", "maxpp", "minmax", "mul", "ratecv", "reverse", "rms",
                "tomono", "tostereo", "ulaw2lin",
            ):
                setattr(_stub, _fn, lambda *a, **k: b"")
            _sys.modules["audioop"] = _stub
# ─────────────────────────────────────────────────────────────────────────────

import discord
from discord.ext import commands, tasks
from discord.ui import View, Button, Modal, TextInput, Select
import os, json, asyncio, time
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
TOKEN               = os.getenv("DISCORD_TOKEN")
DATA_FILE           = "market_data.json"
ARCHIVE_CHANNEL     = "market-archives"
LOG_CHANNEL         = "market-logs"
BOUNTY_AUDIT_CH     = "bounty-staff-audit"
SCAM_AUDIT_CH       = "scam-reports-audit"
ROLE_STAFF          = "Staff"
ROLE_HITMAN         = "Hitman"
ROLE_VERIFIED_BROKER= "Verified Broker"
SPAM_COOLDOWN_SECS  = 5
MAX_PRICE           = 999_999_999_999

BAD_WORDS = ["nigga","nigger","fuck","shit","bitch","asshole","retard","faggot","cunt","whore","slut","bastard"]

VEHICLE_TYPES      = ["Car","Bike","Boat","Helicopter"]
HOUSE_LOCATIONS    = ["Arzamas","Yuzhny","Batyrevo","Buzaevo","Koryakino","Elite Village","Near Church"]
APARTMENT_LOCATIONS= ["Arzamas","Yuzhny","Edodo","Lytkarino"]
HOUSE_GRADES       = ["Economy","Standard","Luxury"]
APARTMENT_GRADES   = ["Standard","Luxury"]
BUSINESS_TYPES     = {
    "1":"Convenience Store 24*7","2":"Clothing Store","3":"Restaurant","4":"Gas Station",
    "5":"Parking Lot","6":"Transportation Company","7":"Weapon Shop","8":"Pickaxe Shop",
    "9":"Parcel Locker Station","10":"Notary Agency",
}
CATEGORY_LABELS = {
    "vehicle":"🚗 Vehicles","realestate":"🏡 Real Estate",
    "skin":"🎒 Skins & Accessories","business":"🏢 Businesses",
}

# ─────────────────────────────────────────────
#  DATA
# ─────────────────────────────────────────────
def _default_data():
    return {
        "listings": {},
        "auction": {"active":False,"message_id":None,"channel_id":None,"owner_id":None,
                    "owner_name":None,"product_name":None,"current_bid":0,"top_bidder_id":None,
                    "top_bidder_name":None,"ends_at":None,"image_url":None},
        "stats": {"total_ads":0,"vehicle_value":0,"realestate_neighborhood_counts":{},"business_max_deal":0},
        "subscriptions": {"vehicle":[],"realestate":[],"skin":[],"business":[]},
        "lf_requests": {},
        "price_history": {},
        "vouches": {},
        "bounties": {"active":{},"cooldowns":{}},
        "scammer_registry": {"pending":{},"confirmed":{}},
        "cash_escrow_map": {},
        "cash_rate_history": [],
        "giveaways": {},
    }

def load_data():
    if not os.path.exists(DATA_FILE):
        return _default_data()
    try:
        with open(DATA_FILE) as f:
            data = json.load(f)
        for k, v in _default_data().items():
            data.setdefault(k, v)
        return data
    except Exception:
        return _default_data()

def save_data(data):
    with open(DATA_FILE,"w") as f:
        json.dump(data, f, indent=2)

def fmt(v): return f"${v:,}"
def listing_key(ch, msg): return f"{ch}_{msg}"
def normalize(s): return s.lower().strip()

def validate_price(raw):
    c = raw.replace(",","").replace("$","").strip()
    if not c.isdigit(): return False,0,"Price must be a whole number."
    v = int(c)
    if v < 1: return False,0,"Price must be at least $1."
    if v > MAX_PRICE: return False,0,"Price exceeds the maximum allowed value."
    return True,v,""

def has_bad_words(t): return any(w in t.lower() for w in BAD_WORDS)
def has_role(member, role_name): return any(r.name==role_name for r in member.roles)

# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

pending: dict[int, dict] = {}
spam_cooldowns: dict[int, float] = {}   # user_id → last interaction timestamp

# ─────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────
def check_spam(user_id: int) -> bool:
    """Returns True if user is on cooldown."""
    last = spam_cooldowns.get(user_id, 0)
    if time.time() - last < SPAM_COOLDOWN_SECS:
        return True
    spam_cooldowns[user_id] = time.time()
    return False

def is_blacklisted(user: discord.Member) -> bool:
    data = load_data()
    confirmed = data["scammer_registry"].get("confirmed", {})
    uid_str = str(user.id)
    if uid_str in confirmed:
        return True
    name_low = normalize(user.display_name)
    for entry in confirmed.values():
        if normalize(entry.get("target_name","")) == name_low:
            return True
    return False

async def audit_log(guild: discord.Guild, message: str):
    ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL)
    if ch:
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            await ch.send(f"`{ts}` {message}")
        except Exception:
            pass

async def notify_subscribers(category: str, title: str, description: str, jump_url: str):
    data = load_data()
    subs = data.get("subscriptions",{}).get(category,[])
    if not subs: return
    embed = discord.Embed(
        title=f"🔔 Trade Alert — {CATEGORY_LABELS.get(category,category)}",
        description=f"{description}\n\n[👉 View Listing]({jump_url})",
        color=discord.Color.yellow(),
    )
    embed.set_footer(text="Manage alerts with !subscribe")
    for uid in subs:
        try:
            u = await bot.fetch_user(uid)
            await u.send(embed=embed)
        except Exception:
            pass

def log_price(asset_name: str, price: int, listing_type: str):
    data = load_data()
    key = normalize(asset_name)
    history = data["price_history"].setdefault(key, [])
    history.append({"price": price, "date": datetime.now(timezone.utc).isoformat()[:10], "type": listing_type})
    if len(history) > 50:
        data["price_history"][key] = history[-50:]
    save_data(data)

# ─────────────────────────────────────────────
#  PERSISTENT VIEW: LISTING FOOTER
# ─────────────────────────────────────────────
class ListingFooterView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Mark as Sold", style=discord.ButtonStyle.danger, custom_id="listing_mark_sold")
    async def mark_sold(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down! Wait a few seconds.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        lst  = data["listings"].get(key)
        if lst is None:
            await interaction.response.send_message("❌ Listing not found.", ephemeral=True); return
        is_owner = interaction.user.id == lst["owner_id"]
        is_admin = interaction.user.guild_permissions.manage_messages
        if not (is_owner or is_admin):
            await interaction.response.send_message("❌ Only the seller or an admin can mark this sold.", ephemeral=True); return
        if lst.get("sold"):
            await interaction.response.send_message("ℹ️ Already marked as sold.", ephemeral=True); return
        lst["sold"] = True
        save_data(data)
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.dark_grey()
        title_clean = embed.title or "Item"
        embed.title = "✅ [SOLD] " + title_clean
        dv = View(timeout=None)
        dv.add_item(Button(label="🔒 SOLD", style=discord.ButtonStyle.secondary, disabled=True, custom_id="listing_sold_disabled"))
        await interaction.response.send_message("✅ Listing marked as Sold.", ephemeral=True)
        await interaction.message.edit(embed=embed, view=dv)
        # Log price history
        price_field = next((f.value for f in embed.fields if "Asking Price" in f.name or "Buyout Price" in f.name), None)
        if price_field:
            raw = price_field.replace("$","").replace(",","")
            if raw.isdigit():
                log_price(title_clean, int(raw), lst.get("type","unknown"))
        await audit_log(interaction.guild, f"[SOLD] <@{interaction.user.id}> marked **{title_clean}** as sold.")

    @discord.ui.button(label="💬 Contact Seller", style=discord.ButtonStyle.primary, custom_id="listing_contact_seller")
    async def contact_seller(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down! Wait a few seconds.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        lst  = data["listings"].get(key)
        if lst is None:
            await interaction.response.send_message("❌ Listing not found.", ephemeral=True); return
        if interaction.user.id == lst["owner_id"]:
            await interaction.response.send_message("⚠️ You cannot contact yourself.", ephemeral=True); return
        try:
            owner = await bot.fetch_user(lst["owner_id"])
            e = discord.Embed(title="💬 Someone is interested in your listing!",
                description=f"**{interaction.user.display_name}** is interested in your listing on **{interaction.guild.name}**.\n\n[Jump to listing]({interaction.message.jump_url})",
                color=discord.Color.green())
            e.set_thumbnail(url=interaction.user.display_avatar.url)
            await owner.send(embed=e)
            await interaction.response.send_message("✅ Seller notified via DM!", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("⚠️ Seller has DMs closed.", ephemeral=True)
        except Exception as ex:
            await interaction.response.send_message(f"❌ {ex}", ephemeral=True)

    @discord.ui.button(label="🤝 Request Middleman", style=discord.ButtonStyle.success, custom_id="listing_request_middleman")
    async def request_middleman(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down! Wait a few seconds.", ephemeral=True); return
        data  = load_data()
        key   = listing_key(interaction.channel.id, interaction.message.id)
        lst   = data["listings"].get(key)
        guild = interaction.guild
        buyer = interaction.user

        owner_id = lst["owner_id"] if lst else None
        if owner_id and buyer.id == owner_id:
            await interaction.response.send_message("⚠️ You cannot request a middleman for your own listing.", ephemeral=True); return

        owner      = guild.get_member(owner_id) if owner_id else None
        staff_role = discord.utils.get(guild.roles, name=ROLE_STAFF)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            buyer:              discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        if owner:        overwrites[owner]      = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if staff_role:   overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            room = await guild.create_text_channel(
                name=f"middleman-{buyer.name[:10]}", category=interaction.channel.category,
                overwrites=overwrites, reason="Marketplace middleman request")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to create channels.", ephemeral=True); return

        mention_staff  = staff_role.mention if staff_role else "@Staff"
        owner_mention  = owner.mention if owner else (f"<@{owner_id}>" if owner_id else "*(seller unknown)*")
        embed = discord.Embed(title="🤝 Official Middleman Room",
            description=(f"**Buyer:** {buyer.mention}\n**Seller:** {owner_mention}\n\n"
                         f"**Listing:** [Jump to message]({interaction.message.jump_url})\n\n"
                         f"{mention_staff} — please supervise this transaction."),
            color=discord.Color.gold())
        await room.send(embed=embed, view=MiddlemanRoomView())
        await interaction.response.send_message(f"✅ Middleman room created: {room.mention}", ephemeral=True)
        await audit_log(guild, f"[MIDDLEMAN] Room created by {buyer.mention} for listing {interaction.message.jump_url}")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[ListingFooterView Error] {error}")
        try:
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except Exception: pass

    @discord.ui.button(label="📉 Drop Price", style=discord.ButtonStyle.secondary, custom_id="listing_drop_price", row=1)
    async def drop_price(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        lst  = data["listings"].get(key)
        if lst is None:
            await interaction.response.send_message("❌ Listing not found.", ephemeral=True); return
        if interaction.user.id != lst["owner_id"]:
            await interaction.response.send_message("❌ Only the listing owner can drop the price.", ephemeral=True); return
        if lst.get("sold"):
            await interaction.response.send_message("❌ Already sold.", ephemeral=True); return
        embed = interaction.message.embeds[0]
        old_price = new_price = None
        field_name = "Asking Price"
        for i, f in enumerate(embed.fields):
            if "Asking Price" in f.name or "Buyout Price" in f.name:
                raw = f.value.replace("$","").replace(",","")
                if raw.isdigit():
                    old_price = int(raw)
                    new_price = max(1, int(old_price * 0.90))
                    field_name = f.name
                    embed.set_field_at(i, name=field_name, value=fmt(new_price), inline=True)
                break
        if old_price is None:
            await interaction.response.send_message("❌ Could not find price field.", ephemeral=True); return
        if "[PRICE DROP" not in (embed.title or ""):
            embed.title = "📉 [PRICE DROP] " + (embed.title or "")
        await interaction.response.send_message(f"✅ Price dropped: {fmt(old_price)} → **{fmt(new_price)}**", ephemeral=True)
        await interaction.message.edit(embed=embed)
        await audit_log(interaction.guild, f"[PRICE DROP] {interaction.user.mention} dropped price on **{embed.title}** to {fmt(new_price)}")


# ─────────────────────────────────────────────
#  PERSISTENT VIEW: MIDDLEMAN ROOM
# ─────────────────────────────────────────────
class MiddlemanRoomView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="✅ Mark as Resolved & Rate", style=discord.ButtonStyle.success, custom_id="mm_resolve")
    async def resolve(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_channels:
            await interaction.response.send_message("❌ Only staff can resolve trades.", ephemeral=True); return
        middleman = interaction.user
        await interaction.response.send_message(
            f"✅ Trade resolved by {middleman.mention}!\n"
            f"**Buyer & Seller** — click below to rate the middleman service. Staff can now archive this channel.",
            ephemeral=False)
        await interaction.channel.send(
            f"⭐ Rate **{middleman.display_name}**'s middleman service:",
            view=MMRatingView(middleman.id, middleman.display_name))
        await audit_log(interaction.guild, f"[MIDDLEMAN] Trade resolved by {middleman.mention} in {interaction.channel.mention}")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[MiddlemanRoomView Error] {error}")
        try:
            await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
        except Exception: pass


class MMRatingView(View):
    """Non-persistent — holds middleman_id in instance. 10-min window for parties to rate."""
    def __init__(self, middleman_id: int, middleman_name: str):
        super().__init__(timeout=600)
        self.middleman_id   = middleman_id
        self.middleman_name = middleman_name

    @discord.ui.button(label="⭐ Rate This Middleman", style=discord.ButtonStyle.primary)
    async def rate(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id == self.middleman_id:
            await interaction.response.send_message("⚠️ You can't rate yourself!", ephemeral=True); return
        await interaction.response.send_modal(MMRatingModal(self.middleman_id, self.middleman_name))

    async def on_timeout(self):
        pass  # just let the buttons grey out naturally

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[MMRatingView Error] {error}")
        try:
            await interaction.response.send_message("❌ Rating failed. Please try again.", ephemeral=True)
        except Exception: pass


class MMRatingModal(Modal, title="Rate Your Middleman"):
    stars   = TextInput(label="Stars (1–5)", placeholder="Enter a number: 1, 2, 3, 4, or 5", max_length=1)
    comment = TextInput(label="Comment (optional)", placeholder="How was the service?",
                        max_length=200, required=False, style=discord.TextStyle.paragraph)

    def __init__(self, middleman_id: int, middleman_name: str):
        super().__init__()
        self.middleman_id   = middleman_id
        self.middleman_name = middleman_name

    async def on_submit(self, interaction: discord.Interaction):
        if not self.stars.value.isdigit() or not (1 <= int(self.stars.value) <= 5):
            await interaction.response.send_message("❌ Stars must be 1, 2, 3, 4, or 5.", ephemeral=True); return
        star_count = int(self.stars.value)
        comment    = self.comment.value.strip() or "No comment"
        data  = load_data()
        entry = data["vouches"].setdefault(str(self.middleman_id), {"ratings": [], "highest_deal": 0})
        entry.setdefault("ratings", [])
        # Prevent duplicate rating from same user for this session (same-day check)
        today = datetime.now(timezone.utc).isoformat()[:10]
        if any(r.get("rater_id") == interaction.user.id and r.get("date") == today for r in entry["ratings"]):
            await interaction.response.send_message("⚠️ You've already rated this middleman today.", ephemeral=True); return
        entry["ratings"].append({
            "stars": star_count, "comment": comment,
            "date": today, "rater_id": interaction.user.id,
        })
        save_data(data)
        stars_str = "⭐" * star_count
        await interaction.response.send_message(
            f"{stars_str} Rating submitted for **{self.middleman_name}**! Thank you.", ephemeral=True)
        await audit_log(interaction.guild,
            f'[VOUCH] {interaction.user.mention} rated **{self.middleman_name}** {stars_str} — "{comment[:80]}"')

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[MMRatingModal Error] {error}")
        try:
            await interaction.response.send_message("❌ Failed to submit rating. Please try again.", ephemeral=True)
        except Exception: pass


# ─────────────────────────────────────────────
#  PERSISTENT VIEW: AUCTION BIDS
# ─────────────────────────────────────────────
class AuctionBidView(View):
    def __init__(self): super().__init__(timeout=None)

    async def _apply_bid(self, interaction: discord.Interaction, amount: int):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        if is_blacklisted(interaction.user):
            await interaction.response.send_message("❌ Access Denied: Your account is on the Scammer Registry.", ephemeral=True); return
        data    = load_data()
        auction = data["auction"]
        if not auction["active"]:
            await interaction.response.send_message("❌ No active auction.", ephemeral=True); return
        ends_at = datetime.fromisoformat(auction["ends_at"]).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= ends_at:
            await interaction.response.send_message("⏰ Auction has ended.", ephemeral=True); return
        if interaction.user.id == auction["owner_id"]:
            await interaction.response.send_message("⚠️ You cannot bid on your own auction.", ephemeral=True); return
        new_bid = auction["current_bid"] + amount
        auction["current_bid"]    = new_bid
        auction["top_bidder_id"]  = interaction.user.id
        auction["top_bidder_name"]= str(interaction.user)
        save_data(data)
        ch = bot.get_channel(auction["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(auction["message_id"])
                e   = msg.embeds[0]
                for i, f in enumerate(e.fields):
                    if f.name == "💰 Current Bid": e.set_field_at(i, name="💰 Current Bid", value=fmt(new_bid), inline=True)
                    if f.name == "👑 Top Bidder":  e.set_field_at(i, name="👑 Top Bidder",  value=interaction.user.display_name, inline=True)
                await msg.edit(embed=e)
            except Exception: pass
        await interaction.response.send_message(f"✅ Bid raised to **{fmt(new_bid)}**!", ephemeral=True)

    @discord.ui.button(label="+ $50,000",  style=discord.ButtonStyle.primary,   custom_id="auction_bid_50k")
    async def bid_50k(self, i, b): await self._apply_bid(i, 50_000)
    @discord.ui.button(label="+ $100,000", style=discord.ButtonStyle.primary,   custom_id="auction_bid_100k")
    async def bid_100k(self, i, b): await self._apply_bid(i, 100_000)
    @discord.ui.button(label="+ $500,000", style=discord.ButtonStyle.success,   custom_id="auction_bid_500k")
    async def bid_500k(self, i, b): await self._apply_bid(i, 500_000)
    @discord.ui.button(label="✏️ Custom Bid", style=discord.ButtonStyle.secondary, custom_id="auction_bid_custom")
    async def bid_custom(self, interaction: discord.Interaction, b):
        await interaction.response.send_modal(CustomBidModal())


class CustomBidModal(Modal, title="Custom Bid Amount"):
    amount = TextInput(label="Your total bid amount ($)", placeholder="e.g. 750000", max_length=15)

    async def on_submit(self, interaction: discord.Interaction):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        if is_blacklisted(interaction.user):
            await interaction.response.send_message("❌ Access Denied: Your account is on the Scammer Registry.", ephemeral=True); return
        data    = load_data()
        auction = data["auction"]
        if not auction["active"]:
            await interaction.response.send_message("❌ No active auction.", ephemeral=True); return
        ok, val, err = validate_price(self.amount.value)
        if not ok:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        if val <= auction["current_bid"]:
            await interaction.response.send_message(f"❌ Bid must exceed current {fmt(auction['current_bid'])}.", ephemeral=True); return
        if interaction.user.id == auction["owner_id"]:
            await interaction.response.send_message("⚠️ You cannot bid on your own auction.", ephemeral=True); return
        auction["current_bid"]    = val
        auction["top_bidder_id"]  = interaction.user.id
        auction["top_bidder_name"]= str(interaction.user)
        save_data(data)
        ch = bot.get_channel(auction["channel_id"])
        if ch:
            try:
                msg = await ch.fetch_message(auction["message_id"])
                e   = msg.embeds[0]
                for i, f in enumerate(e.fields):
                    if f.name == "💰 Current Bid": e.set_field_at(i, name="💰 Current Bid", value=fmt(val), inline=True)
                    if f.name == "👑 Top Bidder":  e.set_field_at(i, name="👑 Top Bidder",  value=interaction.user.display_name, inline=True)
                await msg.edit(embed=e)
            except Exception: pass
        await interaction.response.send_message(f"✅ Bid of **{fmt(val)}** placed!", ephemeral=True)


# ─────────────────────────────────────────────
#  PERSISTENT VIEW: LF REQUEST EMBED
# ─────────────────────────────────────────────
class LFRequestEmbedView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🤝 Sell This to Me", style=discord.ButtonStyle.success, custom_id="lf_sell_this")
    async def sell_this(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        if is_blacklisted(interaction.user):
            await interaction.response.send_message("❌ Access Denied: Your account is on the Scammer Registry.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        req  = data["lf_requests"].get(key)
        if req is None:
            await interaction.response.send_message("❌ Request data not found.", ephemeral=True); return
        if req.get("fulfilled"):
            await interaction.response.send_message("❌ This request has already been fulfilled.", ephemeral=True); return
        if interaction.user.id == req["owner_id"]:
            await interaction.response.send_message("⚠️ You cannot fulfill your own request.", ephemeral=True); return
        await interaction.response.send_modal(LFFulfillModal(key, req["max_budget"]))


# ─────────────────────────────────────────────
#  PERSISTENT VIEW: BOUNTY CONTRACT
# ─────────────────────────────────────────────
class BountyContractView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🕶️ Accept Contract", style=discord.ButtonStyle.danger, custom_id="bounty_accept")
    async def accept_contract(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        if not has_role(interaction.user, ROLE_HITMAN):
            await interaction.response.send_message(f"❌ Only **@{ROLE_HITMAN}** members can accept contracts.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        bty  = data["bounties"]["active"].get(key)
        if bty is None:
            await interaction.response.send_message("❌ Bounty not found.", ephemeral=True); return
        if bty.get("hitman_id"):
            await interaction.response.send_message("❌ This contract has already been claimed.", ephemeral=True); return
        if bty.get("completed"):
            await interaction.response.send_message("❌ This contract is already completed.", ephemeral=True); return
        bty["hitman_id"]   = interaction.user.id
        bty["accepted_at"] = datetime.now(timezone.utc).isoformat()
        save_data(data)
        embed = interaction.message.embeds[0]
        embed.add_field(name="🕶️ Assigned Hitman", value=interaction.user.display_name, inline=True)
        embed.add_field(name="⏰ Deadline", value=f"<t:{int((datetime.now(timezone.utc)+timedelta(hours=12)).timestamp())}:R>", inline=True)
        await interaction.response.send_message(f"✅ Contract accepted! You have **12 hours** to complete this hit. Use **📸 Submit Proof** when done.", ephemeral=True)
        await interaction.message.edit(embed=embed)
        await audit_log(interaction.guild, f"[BOUNTY] {interaction.user.mention} accepted contract on **{bty.get('target_name')}**")

    @discord.ui.button(label="📸 Submit Proof", style=discord.ButtonStyle.primary, custom_id="bounty_submit_proof")
    async def submit_proof(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        bty  = data["bounties"]["active"].get(key)
        if bty is None:
            await interaction.response.send_message("❌ Bounty not found.", ephemeral=True); return
        if bty.get("hitman_id") != interaction.user.id:
            await interaction.response.send_message("❌ Only the assigned hitman can submit proof.", ephemeral=True); return
        await interaction.response.send_message(
            "📸 Upload a screenshot of the **knocked-out target / hospital screen** in this channel. *(2 minutes)*", ephemeral=True)
        await _collect_bounty_proof(interaction, key)


# ─────────────────────────────────────────────
#  PERSISTENT VIEW: SCAM AUDIT
# ─────────────────────────────────────────────
class ScamAuditView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="✅ Approve & Blacklist", style=discord.ButtonStyle.danger, custom_id="scam_approve")
    async def approve(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Staff only.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        rpt  = data["scammer_registry"]["pending"].get(key)
        if rpt is None:
            await interaction.response.send_message("❌ Report not found.", ephemeral=True); return
        # Confirm and publish
        target_low = normalize(rpt["target_name"])
        confirmed  = data["scammer_registry"]["confirmed"]
        confirmed[target_low] = {
            "target_name": rpt["target_name"], "scam_type": rpt["scam_type"],
            "value": rpt["value"], "description": rpt["description"],
            "proof_img": rpt.get("proof_img"), "proof_url": rpt.get("proof_url","N/A"),
            "added_at": datetime.now(timezone.utc).isoformat()[:10],
            "reporter_id": rpt["reporter_id"],
        }
        del data["scammer_registry"]["pending"][key]
        save_data(data)
        # Post to public channel (same channel where the panel was)
        public_ch = bot.get_channel(rpt.get("public_channel_id", interaction.channel.id))
        wall_embed = discord.Embed(
            title="🚨 VERIFIED SCAMMER — Wall of Shame",
            color=discord.Color.red(),
        )
        wall_embed.add_field(name="👤 Offender", value=rpt["target_name"], inline=True)
        wall_embed.add_field(name="📁 Scam Type", value=rpt["scam_type"], inline=True)
        wall_embed.add_field(name="💰 Value Stolen", value=fmt(rpt["value"]), inline=True)
        wall_embed.add_field(name="📝 Description", value=rpt["description"][:300], inline=False)
        wall_embed.add_field(name="🔍 Evidence", value=f"[Screenshot]({rpt.get('proof_img','N/A')}) | [Video/URL]({rpt.get('proof_url','N/A')})", inline=False)
        wall_embed.set_footer(text="⚠️ VERIFIED SCAMMER — DO NOT TRADE")
        if public_ch:
            await public_ch.send(embed=wall_embed)
        await interaction.response.send_message("✅ Scammer blacklisted and published.", ephemeral=True)
        await interaction.message.edit(content="✅ Report approved and published.", view=None)
        await audit_log(interaction.guild, f"[SCAMMER APPROVED] {interaction.user.mention} blacklisted **{rpt['target_name']}**")

    @discord.ui.button(label="❌ Reject Report", style=discord.ButtonStyle.secondary, custom_id="scam_reject")
    async def reject(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Staff only.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        if key in data["scammer_registry"]["pending"]:
            del data["scammer_registry"]["pending"][key]
            save_data(data)
        await interaction.response.send_message("Report rejected and discarded.", ephemeral=True)
        await interaction.message.edit(content="❌ Report rejected.", view=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[ScamAuditView Error] {error}")
        try:
            await interaction.response.send_message("❌ An error occurred processing this report.", ephemeral=True)
        except Exception: pass


# ─────────────────────────────────────────────
#  MAIN MARKETPLACE PANEL
# ─────────────────────────────────────────────
class MainMarketView(View):
    def __init__(self): super().__init__(timeout=None)

    async def _check_entry(self, interaction: discord.Interaction) -> bool:
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down! Wait a few seconds.", ephemeral=True); return False
        if is_blacklisted(interaction.user):
            await interaction.response.send_message("❌ Access Denied: Your account is on the Scammer Registry.", ephemeral=True); return False
        return True

    @discord.ui.select(
        custom_id="market_category_select",
        placeholder="📋 Choose what you want to sell...",
        options=[
            discord.SelectOption(
                label="🚗 Sell a Vehicle",
                value="vehicle",
                description="Cars, bikes, boats, aircraft & more",
            ),
            discord.SelectOption(
                label="🏡 Sell Real Estate",
                value="realestate",
                description="Houses, apartments, garages  •  Verified Broker only",
            ),
            discord.SelectOption(
                label="🎒 Skins & Accessories",
                value="skins",
                description="Clothing, weapon skins, collectibles",
            ),
            discord.SelectOption(
                label="🏢 Sell a Business",
                value="business",
                description="MC clubs, nightclubs, warehouses  •  Verified Broker only",
            ),
            discord.SelectOption(
                label="🪙 Sell Game Cash",
                value="gamecash",
                description="Liquidate in-game cash (RMT)  •  Verified Broker only",
            ),
        ],
    )
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        if not await self._check_entry(interaction): return
        choice = select.values[0]

        # ── Broker-gated categories ──────────────────────────────────────────
        broker_categories = {"realestate", "business", "gamecash"}
        if choice in broker_categories and not has_role(interaction.user, ROLE_VERIFIED_BROKER):
            labels = {"realestate": "real estate", "business": "businesses", "gamecash": "in-game cash"}
            await interaction.response.send_message(
                f"❌ Only members with the **@{ROLE_VERIFIED_BROKER}** role can list {labels[choice]}.",
                ephemeral=True,
            )
            return

        # ── Route to the correct flow ────────────────────────────────────────
        if choice == "vehicle":
            await interaction.response.send_message("**Step 1/5** — Select your vehicle type:", view=VehicleTypeView(), ephemeral=True)
        elif choice == "realestate":
            await interaction.response.send_message("**Step 1/6** — Select property type:", view=RealEstateTypeView(), ephemeral=True)
        elif choice == "skins":
            await interaction.response.send_message("**Step 1/4** — Select item type:", view=SkinTypeView(), ephemeral=True)
        elif choice == "business":
            await interaction.response.send_message("**Step 1/5** — Select your business type:", view=BusinessTypeView(), ephemeral=True)
        elif choice == "gamecash":
            await interaction.response.send_modal(SellGameCashModal())


# ─────────────────────────────────────────────
#  GAME CASH FLOW
# ─────────────────────────────────────────────
class SellGameCashModal(Modal, title="🪙 Sell In-Game Cash"):
    amount  = TextInput(label="Total In-Game Cash to Sell (Millions)", placeholder="e.g. 10  →  means $10,000,000 in-game", max_length=10)
    rate    = TextInput(label="Your Rate per 1 Million (Real Money)", placeholder="e.g. 20  →  $20 real per 1M", max_length=10)
    payment = TextInput(label="Payment Method / In-Game ID Details",
                        placeholder="e.g. PayPal: john@email.com | IGN: BigSeller_99",
                        max_length=200, style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        amt_raw = self.amount.value.replace(",", "").strip()
        if not amt_raw.replace(".", "", 1).isdigit():
            await interaction.response.send_message("❌ Amount must be a number (e.g. 10 for 10 million).", ephemeral=True); return
        amt = float(amt_raw)
        if amt <= 0 or amt > 10_000:
            await interaction.response.send_message("❌ Amount must be between 1 and 10,000 million.", ephemeral=True); return
        rate_raw = self.rate.value.replace(",", "").replace("$", "").strip()
        if not rate_raw.replace(".", "", 1).isdigit():
            await interaction.response.send_message("❌ Rate must be a number (e.g. 20).", ephemeral=True); return
        rate = float(rate_raw)
        if rate <= 0 or rate > 100_000:
            await interaction.response.send_message("❌ Rate out of range.", ephemeral=True); return
        if has_bad_words(self.payment.value):
            await interaction.response.send_message("🚨 Inappropriate content blocked.", ephemeral=True); return
        total = amt * rate
        user  = interaction.user
        in_game_fmt = f"{amt:g}M  (${int(amt * 1_000_000):,} in-game)"
        embed = discord.Embed(title="🪙 In-Game Cash For Sale", color=discord.Color.gold())
        embed.add_field(name="💵 Amount",           value=in_game_fmt,             inline=True)
        embed.add_field(name="📈 Rate",             value=f"${rate:g} per 1M",     inline=True)
        embed.add_field(name="💰 Total Real Cost",  value=f"${total:,.2f}",        inline=True)
        embed.add_field(name="💳 Payment / IGN",    value=self.payment.value,      inline=False)
        embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
        embed.set_footer(text=f"Listed {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} | Verified Broker listing")
        msg = await interaction.channel.send(embed=embed, view=CashListingView())
        data = load_data()
        key = listing_key(interaction.channel.id, msg.id)
        data["listings"][key] = {
            "type": "gamecash", "owner_id": user.id, "owner_name": str(user),
            "created_at": datetime.now(timezone.utc).isoformat(), "sold": False,
            "channel_id": interaction.channel.id, "message_id": msg.id,
            "escrow_channel_id": None,
            "amount_millions": amt, "rate_per_million": rate,
        }
        data["stats"]["total_ads"] += 1
        save_data(data)
        await interaction.response.send_message("✅ Your game cash listing is live!", ephemeral=True)
        await audit_log(interaction.guild, f"[CASH LISTING] {user.mention} listed **{amt:g}M** in-game cash — total ${total:,.2f} real")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[SellGameCashModal Error] {error}")
        try:
            await interaction.response.send_message("❌ Something went wrong. Please try again.", ephemeral=True)
        except Exception: pass


class CashListingView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🤝 Request Cash Middleman", style=discord.ButtonStyle.success, custom_id="cash_request_middleman")
    async def request_middleman(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down! Wait a few seconds.", ephemeral=True); return
        if is_blacklisted(interaction.user):
            await interaction.response.send_message("❌ Access Denied: Your account is on the Scammer Registry.", ephemeral=True); return
        data  = load_data()
        key   = listing_key(interaction.channel.id, interaction.message.id)
        lst   = data["listings"].get(key)
        guild = interaction.guild
        buyer = interaction.user
        if lst is None:
            await interaction.response.send_message("❌ Listing data not found.", ephemeral=True); return
        if buyer.id == lst["owner_id"]:
            await interaction.response.send_message("⚠️ You cannot request a middleman for your own listing.", ephemeral=True); return
        if lst.get("sold"):
            await interaction.response.send_message("❌ This listing is already sold.", ephemeral=True); return
        existing_id = lst.get("escrow_channel_id")
        if existing_id:
            existing = guild.get_channel(existing_id)
            if existing:
                await interaction.response.send_message(f"⚠️ An escrow room is already open: {existing.mention}", ephemeral=True); return
        owner      = guild.get_member(lst["owner_id"])
        staff_role = discord.utils.get(guild.roles, name=ROLE_STAFF)
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            buyer:              discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me:           discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        if owner:      overwrites[owner]      = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        if staff_role: overwrites[staff_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        try:
            escrow_ch = await guild.create_text_channel(
                name=f"cash-escrow-{buyer.name[:12]}",
                category=interaction.channel.category,
                overwrites=overwrites,
                reason="Cash trade escrow room")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I don't have permission to create channels.", ephemeral=True); return
        lst["escrow_channel_id"] = escrow_ch.id
        data.setdefault("cash_escrow_map", {})[str(escrow_ch.id)] = key
        save_data(data)
        mention_staff = staff_role.mention if staff_role else "@Staff"
        owner_mention = owner.mention if owner else (f"<@{lst['owner_id']}>" if lst.get("owner_id") else "*(unknown)*")
        escrow_embed = discord.Embed(
            title="🪙 Cash Trade Escrow Room",
            description=(
                f"**Buyer:** {buyer.mention}\n"
                f"**Seller:** {owner_mention}\n\n"
                f"**Original Listing:** [Jump to message]({interaction.message.jump_url})\n\n"
                f"{mention_staff} — please supervise this real-money trade.\n\n"
                "⚠️ **Do not send any real funds until staff confirms both parties are present.**\n"
                "Staff: click the button below once the trade is completed to upload proof and close this room."
            ),
            color=discord.Color.gold(),
        )
        escrow_embed.set_footer(text="SimpleMarketHub | Cash Escrow | Staff-supervised trade")
        await escrow_ch.send(embed=escrow_embed, view=CashEscrowView())
        await interaction.response.send_message(f"✅ Escrow room created: {escrow_ch.mention}", ephemeral=True)
        await audit_log(guild, f"[CASH ESCROW] Room opened by {buyer.mention} for {owner_mention}'s listing — {interaction.message.jump_url}")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[CashListingView Error] {error}")
        try:
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except Exception: pass


class CashEscrowView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="📸 Complete Trade & Upload Proof", style=discord.ButtonStyle.success, custom_id="cash_escrow_complete")
    async def complete_trade(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Staff only — only a staff member can complete this trade.", ephemeral=True); return
        staff   = interaction.user
        channel = interaction.channel
        guild   = interaction.guild
        await interaction.response.send_message(
            f"📸 {staff.mention} — Upload a **screenshot confirming the completed trade** in this channel. *(2 minutes)*",
            ephemeral=False)
        def chk(m): return m.author.id == staff.id and m.channel.id == channel.id and len(m.attachments) > 0
        try:
            proof_msg = await bot.wait_for("message", check=chk, timeout=120)
            proof_url = proof_msg.attachments[0].url
        except asyncio.TimeoutError:
            await channel.send(f"{staff.mention} ⏰ Timed out — no proof uploaded. Trade not closed.", delete_after=20); return
        data            = load_data()
        escrow_map      = data.get("cash_escrow_map", {})
        listing_key_str = escrow_map.get(str(channel.id))
        lst             = data["listings"].get(listing_key_str) if listing_key_str else None
        if lst:
            orig_ch = guild.get_channel(lst["channel_id"])
            if orig_ch:
                try:
                    orig_msg   = await orig_ch.fetch_message(lst["message_id"])
                    orig_embed = orig_msg.embeds[0]
                    orig_embed.color = discord.Color.dark_grey()
                    orig_embed.title = "✅ [SOLD OUT / VERIFIED] " + (orig_embed.title or "In-Game Cash")
                    sold_view = View(timeout=None)
                    sold_view.add_item(Button(label="✅ SOLD OUT / VERIFIED", style=discord.ButtonStyle.secondary,
                                              disabled=True, custom_id="cash_sold_disabled"))
                    await orig_msg.edit(embed=orig_embed, view=sold_view)
                except Exception as ex:
                    print(f"[CashEscrow] Could not edit original listing: {ex}")
            lst["sold"] = True
            # Log rate to history for !rates command
            rate_entry = {
                "rate":           lst.get("rate_per_million"),
                "amount_millions": lst.get("amount_millions"),
                "date":           datetime.now(timezone.utc).isoformat(),
            }
            if rate_entry["rate"] and rate_entry["amount_millions"]:
                history = data.setdefault("cash_rate_history", [])
                history.append(rate_entry)
                if len(history) > 200:
                    data["cash_rate_history"] = history[-200:]
            save_data(data)
        log_ch = discord.utils.get(guild.text_channels, name=LOG_CHANNEL)
        if log_ch:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            receipt_embed = discord.Embed(title="🧾 Cash Trade Receipt — Verified", color=discord.Color.green())
            receipt_embed.add_field(name="🕵️ Verified By", value=staff.mention,                       inline=True)
            receipt_embed.add_field(name="📅 Timestamp",   value=ts,                                   inline=True)
            receipt_embed.add_field(name="📸 Proof",       value=f"[View Screenshot]({proof_url})",    inline=False)
            if lst:
                receipt_embed.add_field(name="💼 Seller",  value=f"<@{lst['owner_id']}>",              inline=True)
            receipt_embed.set_footer(text=f"Escrow channel: #{channel.name}")
            await log_ch.send(embed=receipt_embed)
        done_embed = discord.Embed(
            title="✅ Trade Completed & Verified",
            description=f"**Proof uploaded by {staff.mention}**\n[View Screenshot]({proof_url})\n\nThis channel can now be deleted by staff.",
            color=discord.Color.green(),
        )
        done_embed.set_image(url=proof_url)
        done_view = View(timeout=None)
        done_view.add_item(Button(label="✅ Trade Completed", style=discord.ButtonStyle.secondary,
                                  disabled=True, custom_id="cash_done_disabled"))
        await channel.send(embed=done_embed, view=done_view)
        await audit_log(guild, f"[CASH TRADE COMPLETE] {staff.mention} verified cash trade in {channel.mention} — [Proof]({proof_url})")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[CashEscrowView Error] {error}")
        try:
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except Exception: pass


# ─────────────────────────────────────────────
#  VEHICLE FLOW
# ─────────────────────────────────────────────
class VehicleTypeView(View):
    def __init__(self): super().__init__(timeout=120)
    @discord.ui.select(placeholder="Choose vehicle type...",
                       options=[discord.SelectOption(label=v, value=v) for v in VEHICLE_TYPES],
                       custom_id="vehicle_type_select")
    async def select_type(self, interaction: discord.Interaction, select: Select):
        pending[interaction.user.id] = {"vehicle_type": select.values[0]}
        await interaction.response.send_modal(VehicleModal(select.values[0]))

class VehicleModal(Modal):
    model_name  = TextInput(label="Model Name",                  placeholder="e.g. Sultan RS",  max_length=80)
    state_price = TextInput(label="State Base Price ($)",         placeholder="e.g. 850000",     max_length=15)
    asking_price= TextInput(label="Your Asking Price ($)",        placeholder="e.g. 700000",     max_length=15)
    num_owners  = TextInput(label="Number of Previous Owners",    placeholder="e.g. 2",          max_length=3)
    def __init__(self, vt): super().__init__(title=f"Sell a {vt}"); self.vt = vt
    async def on_submit(self, interaction: discord.Interaction):
        ok1,sp,e1 = validate_price(self.state_price.value)
        ok2,ap,e2 = validate_price(self.asking_price.value)
        nr = self.num_owners.value.strip()
        if not ok1: await interaction.response.send_message(f"❌ State Price — {e1}", ephemeral=True); return
        if not ok2: await interaction.response.send_message(f"❌ Asking Price — {e2}", ephemeral=True); return
        if not nr.isdigit(): await interaction.response.send_message("❌ Owners must be a number.", ephemeral=True); return
        if has_bad_words(self.model_name.value): await interaction.response.send_message("🚨 Inappropriate content blocked.", ephemeral=True); return
        pending[interaction.user.id].update({"model_name":self.model_name.value,"state_price":sp,"asking_price":ap,"num_owners":int(nr)})
        await interaction.response.send_message("📸 **Step 4/5** — Upload your **License Plate Screenshot** in this channel. *(2 min)*", ephemeral=True)
        await _collect_vehicle_images(interaction)

async def _collect_vehicle_images(interaction: discord.Interaction):
    user, channel = interaction.user, interaction.channel
    snap = pending.get(user.id, {})
    def chk(m): return m.author.id==user.id and m.channel.id==channel.id and len(m.attachments)>0
    try:
        m1 = await bot.wait_for("message", check=chk, timeout=120)
        plate_url = m1.attachments[0].url
    except asyncio.TimeoutError:
        await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    p = await channel.send(f"{user.mention} ✅ Plate received! Now upload your **Main Showcase Image**. *(2 min)*")
    try:
        m2 = await bot.wait_for("message", check=chk, timeout=120)
        showcase_url = m2.attachments[0].url
        await p.delete()
    except asyncio.TimeoutError:
        await p.delete(); await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    vt = snap.get("vehicle_type","Vehicle"); model = snap.get("model_name","Unknown")
    sp = snap.get("state_price",0);         ap    = snap.get("asking_price",0)
    embed = discord.Embed(title=f"🚗 {vt} For Sale — {model}", color=discord.Color.blue())
    embed.add_field(name="Vehicle Type",     value=vt,          inline=True)
    embed.add_field(name="State Base Price", value=fmt(sp),     inline=True)
    embed.add_field(name="Asking Price",     value=fmt(ap),     inline=True)
    embed.add_field(name="Previous Owners",  value=str(snap.get("num_owners",0)), inline=True)
    embed.add_field(name="🔍 License Plate", value=f"[View]({plate_url})", inline=False)
    embed.set_image(url=showcase_url)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_footer(text=f"Listed {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    msg = await channel.send(embed=embed, view=ListingFooterView())
    data = load_data()
    data["listings"][listing_key(channel.id,msg.id)] = {"type":"vehicle","owner_id":user.id,"owner_name":str(user),"created_at":datetime.now(timezone.utc).isoformat(),"sold":False,"channel_id":channel.id,"message_id":msg.id}
    data["stats"]["total_ads"] += 1
    data["stats"]["vehicle_value"] = data["stats"].get("vehicle_value",0) + ap
    save_data(data)
    pending.pop(user.id,None)
    await audit_log(channel.guild, f"[LISTING] {user.mention} posted a **{vt}** — {model} for {fmt(ap)}")
    asyncio.create_task(notify_subscribers("vehicle", f"New {vt} — {model}", f"Asking: {fmt(ap)}", msg.jump_url))


# ─────────────────────────────────────────────
#  REAL ESTATE FLOW
# ─────────────────────────────────────────────
class RealEstateTypeView(View):
    def __init__(self): super().__init__(timeout=120)
    @discord.ui.select(placeholder="House or Apartment?",
                       options=[discord.SelectOption(label="🏠 House",value="House"),discord.SelectOption(label="🏢 Apartment",value="Apartment")],
                       custom_id="re_type_select")
    async def select_type(self, interaction: discord.Interaction, select: Select):
        pt = select.values[0]; pending[interaction.user.id] = {"prop_type": pt}
        locs = HOUSE_LOCATIONS if pt=="House" else APARTMENT_LOCATIONS
        await interaction.response.edit_message(content=f"**Step 2/6** — Select location:", view=RealEstateLocationView(locs, pt))

class RealEstateLocationView(View):
    def __init__(self, locs, pt):
        super().__init__(timeout=120); self.pt = pt
        s = Select(placeholder="Select location...", options=[discord.SelectOption(label=l,value=l) for l in locs], custom_id="re_loc_select")
        s.callback = self.sel; self.add_item(s)
    async def sel(self, interaction: discord.Interaction):
        pending[interaction.user.id]["location"] = interaction.data["values"][0]
        grades = HOUSE_GRADES if self.pt=="House" else APARTMENT_GRADES
        await interaction.response.edit_message(content="**Step 3/6** — Select interior grade:", view=RealEstateGradeView(grades))

class RealEstateGradeView(View):
    def __init__(self, grades):
        super().__init__(timeout=120)
        s = Select(placeholder="Select grade...", options=[discord.SelectOption(label=g,value=g) for g in grades], custom_id="re_grade_select")
        s.callback = self.sel; self.add_item(s)
    async def sel(self, interaction: discord.Interaction):
        pending[interaction.user.id]["grade"] = interaction.data["values"][0]
        await interaction.response.send_modal(RealEstatePriceModal())

class RealEstatePriceModal(Modal, title="Property Asking Price"):
    price = TextInput(label="Your Asking Price ($)", placeholder="e.g. 1500000", max_length=15)
    async def on_submit(self, interaction: discord.Interaction):
        ok,val,err = validate_price(self.price.value)
        if not ok: await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        pending[interaction.user.id]["asking_price"] = val
        await interaction.response.send_message("📸 **Step 5/6** — Upload your **Deed/Registration Screenshot** in this channel. *(2 min)*", ephemeral=True)
        await _collect_realestate_images(interaction)

async def _collect_realestate_images(interaction: discord.Interaction):
    user, channel = interaction.user, interaction.channel
    snap = pending.get(user.id, {})
    def chk(m): return m.author.id==user.id and m.channel.id==channel.id and len(m.attachments)>0
    try:
        m1 = await bot.wait_for("message", check=chk, timeout=120)
        deed_url = m1.attachments[0].url
    except asyncio.TimeoutError:
        await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    p = await channel.send(f"{user.mention} ✅ Deed received! Now upload your **Exterior Property Picture**. *(2 min)*")
    try:
        m2 = await bot.wait_for("message", check=chk, timeout=120)
        ext_url = m2.attachments[0].url
        await p.delete()
    except asyncio.TimeoutError:
        await p.delete(); await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    pt = snap.get("prop_type","Property"); loc = snap.get("location","?"); grade = snap.get("grade","?"); ap = snap.get("asking_price",0)
    icon = "🏠" if pt=="House" else "🏢"
    embed = discord.Embed(title=f"{icon} {pt} For Sale — {loc}", color=discord.Color.green())
    embed.add_field(name="Location",       value=loc,   inline=True)
    embed.add_field(name="Interior Grade", value=grade, inline=True)
    embed.add_field(name="Asking Price",   value=fmt(ap),inline=True)
    embed.add_field(name="📄 Deed",        value=f"[View]({deed_url})", inline=False)
    embed.set_image(url=ext_url)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_footer(text=f"Listed {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    msg = await channel.send(embed=embed, view=ListingFooterView())
    data = load_data()
    data["listings"][listing_key(channel.id,msg.id)] = {"type":"realestate","owner_id":user.id,"owner_name":str(user),"created_at":datetime.now(timezone.utc).isoformat(),"sold":False,"channel_id":channel.id,"message_id":msg.id,"location":loc}
    data["stats"]["total_ads"] += 1
    cnts = data["stats"].setdefault("realestate_neighborhood_counts",{})
    cnts[loc] = cnts.get(loc,0) + 1
    save_data(data); pending.pop(user.id,None)
    await audit_log(channel.guild, f"[LISTING] {user.mention} posted **{pt}** in {loc} for {fmt(ap)}")
    asyncio.create_task(notify_subscribers("realestate", f"New {pt} — {loc}", f"Grade: {grade} | Asking: {fmt(ap)}", msg.jump_url))


# ─────────────────────────────────────────────
#  SKINS FLOW
# ─────────────────────────────────────────────
class SkinTypeView(View):
    def __init__(self): super().__init__(timeout=120)
    @discord.ui.select(placeholder="Skin or Accessory?",
                       options=[discord.SelectOption(label="👤 Character Skin",value="Skin"),discord.SelectOption(label="🎩 Accessory",value="Accessory")],
                       custom_id="skin_type_select")
    async def select_type(self, interaction: discord.Interaction, select: Select):
        pending[interaction.user.id] = {"item_type": select.values[0]}
        await interaction.response.send_modal(SkinModal(select.values[0]))

class SkinModal(Modal):
    asking_price = TextInput(label="Asking Price ($)", placeholder="e.g. 250000", max_length=15)
    def __init__(self, it):
        super().__init__(title=f"List a {it}"); self.it = it
        self.identifier = TextInput(label="Skin Model ID" if it=="Skin" else "Item Catalog Name", placeholder="e.g. SKIN_0042", max_length=80)
        self.add_item(self.identifier)
    async def on_submit(self, interaction: discord.Interaction):
        ok,val,err = validate_price(self.asking_price.value)
        if not ok: await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        if has_bad_words(self.identifier.value): await interaction.response.send_message("🚨 Inappropriate content blocked.", ephemeral=True); return
        pending[interaction.user.id].update({"asking_price":val,"identifier":self.identifier.value})
        await interaction.response.send_message("📸 **Step 3/4** — Upload your **Inventory Grid Screenshot**. *(2 min)*", ephemeral=True)
        await _collect_skin_images(interaction)

async def _collect_skin_images(interaction: discord.Interaction):
    user, channel = interaction.user, interaction.channel
    snap = pending.get(user.id, {})
    def chk(m): return m.author.id==user.id and m.channel.id==channel.id and len(m.attachments)>0
    try:
        m1 = await bot.wait_for("message", check=chk, timeout=120)
        inv_url = m1.attachments[0].url
    except asyncio.TimeoutError:
        await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    p = await channel.send(f"{user.mention} ✅ Inventory shot received! Now upload your **Avatar Wearing** screenshot. *(2 min)*")
    try:
        m2 = await bot.wait_for("message", check=chk, timeout=120)
        wear_url = m2.attachments[0].url
        await p.delete()
    except asyncio.TimeoutError:
        await p.delete(); await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    it = snap.get("item_type","Item"); ident = snap.get("identifier","?"); ap = snap.get("asking_price",0)
    icon = "👤" if it=="Skin" else "🎩"
    embed = discord.Embed(title=f"{icon} {it} For Sale — {ident}", color=discord.Color.purple())
    embed.add_field(name="ID/Name",       value=ident,  inline=True)
    embed.add_field(name="Asking Price",  value=fmt(ap),inline=True)
    embed.add_field(name="📦 Inventory", value=f"[View]({inv_url})", inline=False)
    embed.set_image(url=wear_url)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_footer(text=f"Listed {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    msg = await channel.send(embed=embed, view=ListingFooterView())
    data = load_data()
    data["listings"][listing_key(channel.id,msg.id)] = {"type":"skin","owner_id":user.id,"owner_name":str(user),"created_at":datetime.now(timezone.utc).isoformat(),"sold":False,"channel_id":channel.id,"message_id":msg.id}
    data["stats"]["total_ads"] += 1
    save_data(data); pending.pop(user.id,None)
    await audit_log(channel.guild, f"[LISTING] {user.mention} posted **{it}** — {ident} for {fmt(ap)}")
    asyncio.create_task(notify_subscribers("skin", f"New {it} — {ident}", f"Asking: {fmt(ap)}", msg.jump_url))


# ─────────────────────────────────────────────
#  BUSINESS FLOW
# ─────────────────────────────────────────────
class BusinessTypeView(View):
    def __init__(self):
        super().__init__(timeout=120)
        s = Select(placeholder="Select business type (1–10)...",
                   options=[discord.SelectOption(label=f"{k}. {v}", value=k) for k,v in BUSINESS_TYPES.items()],
                   custom_id="biz_type_select")
        s.callback = self.sel; self.add_item(s)
    async def sel(self, interaction: discord.Interaction):
        pending[interaction.user.id] = {"biz_type": BUSINESS_TYPES[interaction.data["values"][0]]}
        await interaction.response.edit_message(content="**Step 2/5** — How many days owned?", view=BusinessDaysView())

class BusinessDaysView(View):
    def __init__(self):
        super().__init__(timeout=120)
        opts = [discord.SelectOption(label=f"{i} Day{'s'if i>1 else ''}",value=str(i)) for i in range(1,11)]
        opts.append(discord.SelectOption(label="10+ Days",value="10+"))
        s = Select(placeholder="Days owned...", options=opts, custom_id="biz_days_select")
        s.callback = self.sel; self.add_item(s)
    async def sel(self, interaction: discord.Interaction):
        pending[interaction.user.id]["days_owned"] = interaction.data["values"][0]
        await interaction.response.send_modal(BusinessModal())

class BusinessModal(Modal, title="Business Listing Details"):
    address      = TextInput(label="Business Address / Location",  placeholder="e.g. 42 Main St, Arzamas", max_length=100)
    daily_profit = TextInput(label="Average Daily Profit ($)",      placeholder="e.g. 15000",               max_length=15)
    buyout_price = TextInput(label="Total Corporate Buyout Price ($)", placeholder="e.g. 2500000",           max_length=15)
    async def on_submit(self, interaction: discord.Interaction):
        ok1,dp,e1 = validate_price(self.daily_profit.value)
        ok2,bp,e2 = validate_price(self.buyout_price.value)
        if not ok1: await interaction.response.send_message(f"❌ Daily Profit — {e1}", ephemeral=True); return
        if not ok2: await interaction.response.send_message(f"❌ Buyout Price — {e2}", ephemeral=True); return
        if has_bad_words(self.address.value): await interaction.response.send_message("🚨 Inappropriate content blocked.", ephemeral=True); return
        pending[interaction.user.id].update({"address":self.address.value,"daily_profit":dp,"buyout_price":bp})
        await interaction.response.send_message("📸 **Step 4/5** — Upload your **Business Management Dashboard** screenshot. *(2 min)*", ephemeral=True)
        await _collect_business_images(interaction)

async def _collect_business_images(interaction: discord.Interaction):
    user, channel = interaction.user, interaction.channel
    snap = pending.get(user.id, {})
    def chk(m): return m.author.id==user.id and m.channel.id==channel.id and len(m.attachments)>0
    try:
        m1 = await bot.wait_for("message", check=chk, timeout=120)
        dash_url = m1.attachments[0].url
    except asyncio.TimeoutError:
        await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    p = await channel.send(f"{user.mention} ✅ Dashboard received! Now upload your **10-Day Profit Ledger** screenshot. *(2 min)*")
    try:
        m2 = await bot.wait_for("message", check=chk, timeout=120)
        ledger_url = m2.attachments[0].url
        await p.delete()
    except asyncio.TimeoutError:
        await p.delete(); await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    bt = snap.get("biz_type","Business"); addr = snap.get("address","?"); days = snap.get("days_owned","?")
    dp = snap.get("daily_profit",0);     bp   = snap.get("buyout_price",0)
    embed = discord.Embed(title=f"🏢 Business For Sale — {bt}", color=discord.Color.orange())
    embed.add_field(name="Business Type",    value=bt,       inline=True)
    embed.add_field(name="Address",          value=addr,     inline=True)
    embed.add_field(name="Days Owned",       value=f"{days}d",inline=True)
    embed.add_field(name="Avg Daily Profit", value=fmt(dp),  inline=True)
    embed.add_field(name="💼 Buyout Price",  value=fmt(bp),  inline=True)
    embed.add_field(name="📊 Dashboard",     value=f"[View]({dash_url})", inline=False)
    embed.set_image(url=ledger_url)
    embed.set_author(name=user.display_name, icon_url=user.display_avatar.url)
    embed.set_footer(text=f"Listed {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    msg = await channel.send(embed=embed, view=ListingFooterView())
    data = load_data()
    data["listings"][listing_key(channel.id,msg.id)] = {"type":"business","owner_id":user.id,"owner_name":str(user),"created_at":datetime.now(timezone.utc).isoformat(),"sold":False,"channel_id":channel.id,"message_id":msg.id}
    data["stats"]["total_ads"] += 1
    if bp > data["stats"].get("business_max_deal",0): data["stats"]["business_max_deal"] = bp
    save_data(data); pending.pop(user.id,None)
    await audit_log(channel.guild, f"[LISTING] {user.mention} posted business **{bt}** for {fmt(bp)}")
    asyncio.create_task(notify_subscribers("business", f"New Business — {bt}", f"Buyout: {fmt(bp)} | Profit: {fmt(dp)}/day", msg.jump_url))


# ─────────────────────────────────────────────
#  LF REQUEST FLOW
# ─────────────────────────────────────────────
class LFBoardView(View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="🔍 Create Buyer Request", style=discord.ButtonStyle.primary, custom_id="lf_create_request")
    async def create_request(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        if is_blacklisted(interaction.user):
            await interaction.response.send_message("❌ Access Denied: You are on the Scammer Registry.", ephemeral=True); return
        await interaction.response.send_modal(LFRequestModal())

class LFRequestModal(Modal, title="Looking For — Buyer Request"):
    asset_name = TextInput(label="Target Asset Name",            placeholder="e.g. Luxury Sultan RS, Elite Village House", max_length=100)
    max_budget = TextInput(label="Your Maximum Buying Budget ($)",placeholder="e.g. 900000",                               max_length=15)
    notes      = TextInput(label="Preferred Specs / Notes",       placeholder="e.g. Less than 2 owners, any colour",       max_length=300, required=False)
    async def on_submit(self, interaction: discord.Interaction):
        ok,budget,err = validate_price(self.max_budget.value)
        if not ok: await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        if has_bad_words(self.asset_name.value) or has_bad_words(self.notes.value or ""):
            await interaction.response.send_message("🚨 Inappropriate content blocked.", ephemeral=True); return
        expires = datetime.now(timezone.utc) + timedelta(days=3)
        embed = discord.Embed(title=f"🔍 Looking For — {self.asset_name.value}", color=discord.Color.blurple())
        embed.add_field(name="Max Budget",  value=fmt(budget),               inline=True)
        embed.add_field(name="Notes",       value=self.notes.value or "N/A", inline=False)
        embed.add_field(name="⏰ Expires",  value=f"<t:{int(expires.timestamp())}:R>", inline=True)
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text="Click 🤝 Sell This to Me if you have this item!")
        await interaction.response.send_message("✅ Request posted!", ephemeral=True)
        msg = await interaction.channel.send(embed=embed, view=LFRequestEmbedView())
        data = load_data()
        data["lf_requests"][listing_key(interaction.channel.id,msg.id)] = {
            "owner_id": interaction.user.id, "asset_name": self.asset_name.value,
            "max_budget": budget, "notes": self.notes.value or "",
            "channel_id": interaction.channel.id, "message_id": msg.id,
            "created_at": datetime.now(timezone.utc).isoformat(), "fulfilled": False,
        }
        save_data(data)
        await audit_log(interaction.guild, f"[LF REQUEST] {interaction.user.mention} looking for **{self.asset_name.value}** (budget: {fmt(budget)})")

class LFFulfillModal(Modal, title="Fulfill This Request"):
    asking_price = TextInput(label="Your Asking Price ($)",  placeholder="e.g. 850000", max_length=15)
    game_handle  = TextInput(label="Your In-Game Contact / Handle", placeholder="e.g. PlayerTag#1234", max_length=80)
    def __init__(self, req_key, max_budget):
        super().__init__(); self.req_key = req_key; self.max_budget = max_budget
    async def on_submit(self, interaction: discord.Interaction):
        ok,price,err = validate_price(self.asking_price.value)
        if not ok: await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        if price > self.max_budget:
            await interaction.response.send_message(
                f"❌ **Transaction Blocked:** Your price ({fmt(price)}) exceeds the buyer's declared budget of {fmt(self.max_budget)}!", ephemeral=True); return
        data = load_data()
        req  = data["lf_requests"].get(self.req_key)
        if req is None: await interaction.response.send_message("❌ Request not found.", ephemeral=True); return
        req["fulfilled"] = True; save_data(data)
        try:
            buyer = await bot.fetch_user(req["owner_id"])
            dm = discord.Embed(title="🎉 A seller has stepped up for your request!",
                description=(f"**{interaction.user.display_name}** wants to sell you **{req['asset_name']}**!\n\n"
                             f"💰 **Their Price:** {fmt(price)}\n"
                             f"🎮 **Game Handle:** {self.game_handle.value}\n\n"
                             "Contact them directly to complete the trade!"),
                color=discord.Color.green())
            await buyer.send(embed=dm)
        except Exception: pass
        await interaction.response.send_message("✅ Offer sent to the buyer via DM!", ephemeral=True)
        await audit_log(interaction.guild, f"[LF FULFILLED] {interaction.user.mention} offered {fmt(price)} for **{req['asset_name']}**")


# ─────────────────────────────────────────────
#  BOUNTY FLOW
# ─────────────────────────────────────────────
class BountyBoardView(View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="💀 Anonymous Hit Contract", style=discord.ButtonStyle.danger, custom_id="bounty_create")
    async def create_bounty(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        await interaction.response.send_modal(BountyContractModal())

class BountyContractModal(Modal, title="Anonymous Hit Contract"):
    target_name = TextInput(label="Target Player Handle / Game Name", placeholder="e.g. BigBadGuy_99",   max_length=80)
    reward      = TextInput(label="Bounty Reward Pool ($)",           placeholder="e.g. 500000",          max_length=15)
    terms       = TextInput(label="Reason / Contract Terms",          placeholder="e.g. Gang betrayal",   max_length=300)
    async def on_submit(self, interaction: discord.Interaction):
        ok,val,err = validate_price(self.reward.value)
        if not ok: await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        if has_bad_words(self.target_name.value) or has_bad_words(self.terms.value):
            await interaction.response.send_message("🚨 Inappropriate content blocked.", ephemeral=True); return
        data = load_data()
        # Cooldown check
        tgt_low = normalize(self.target_name.value)
        cooldown = data["bounties"]["cooldowns"].get(tgt_low)
        if cooldown:
            try:
                exp = datetime.fromisoformat(cooldown).replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) < exp:
                    ts = int(exp.timestamp())
                    await interaction.response.send_message(f"❌ **Target Protected:** This citizen was recently targeted. Protection expires <t:{ts}:R>.", ephemeral=True); return
            except Exception: pass
        await interaction.response.send_message("✅ Contract posted!", ephemeral=True)
        embed = discord.Embed(title=f"💀 HIT CONTRACT — {self.target_name.value}", color=discord.Color.dark_red())
        embed.add_field(name="🎯 Target",        value=self.target_name.value, inline=True)
        embed.add_field(name="💰 Reward Pool",   value=fmt(val),               inline=True)
        embed.add_field(name="📜 Terms",         value=self.terms.value,       inline=False)
        embed.add_field(name="🕶️ Status",        value="⏳ Open — Awaiting Hitman", inline=True)
        embed.set_footer(text=f"Restricted to @{ROLE_HITMAN} members only")
        msg = await interaction.channel.send(embed=embed, view=BountyContractView())
        data["bounties"]["active"][listing_key(interaction.channel.id,msg.id)] = {
            "poster_id": interaction.user.id, "target_name": self.target_name.value,
            "reward": val, "terms": self.terms.value, "hitman_id": None,
            "accepted_at": None, "channel_id": interaction.channel.id, "message_id": msg.id,
            "created_at": datetime.now(timezone.utc).isoformat(), "completed": False,
        }
        save_data(data)
        await audit_log(interaction.guild, f"[BOUNTY POSTED] Contract on **{self.target_name.value}** for {fmt(val)} by {interaction.user.mention}")

async def _collect_bounty_proof(interaction: discord.Interaction, bounty_key: str):
    user, channel = interaction.user, interaction.channel
    def chk(m): return m.author.id==user.id and m.channel.id==channel.id and len(m.attachments)>0
    try:
        m1 = await bot.wait_for("message", check=chk, timeout=120)
        proof1_url = m1.attachments[0].url
    except asyncio.TimeoutError:
        await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); return
    p = await channel.send(f"{user.mention} ✅ Got it! Now upload a screenshot of your **Kill Log / Faction Notification**. *(2 min)*")
    try:
        m2 = await bot.wait_for("message", check=chk, timeout=120)
        proof2_url = m2.attachments[0].url
        await p.delete()
    except asyncio.TimeoutError:
        await p.delete(); await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); return
    # Route to staff audit channel
    audit_ch = discord.utils.get(channel.guild.text_channels, name=BOUNTY_AUDIT_CH)
    data = load_data()
    bty  = data["bounties"]["active"].get(bounty_key,{})
    if audit_ch:
        audit_embed = discord.Embed(title=f"📑 Bounty Proof Submission — {bty.get('target_name','?')}",
            description=f"Submitted by {user.mention}\n\n[Screenshot 1]({proof1_url})\n[Kill Log]({proof2_url})",
            color=discord.Color.dark_red())
        audit_embed.add_field(name="Reward",  value=fmt(bty.get("reward",0)), inline=True)
        audit_embed.set_footer(text="Staff: verify and award reward manually if confirmed.")
        await audit_ch.send(embed=audit_embed, view=BountyProofApprovalView(bounty_key, user.id))
    await channel.send(f"{user.mention} ✅ Proof submitted! Staff are reviewing your claim.", delete_after=20)
    await audit_log(channel.guild, f"[BOUNTY PROOF] {user.mention} submitted proof for contract on **{bty.get('target_name','?')}**")

class BountyProofApprovalView(View):
    """Non-persistent — stores bounty_key/hitman_id in instance. 24-hr staff review window."""
    def __init__(self, bounty_key, hitman_id):
        super().__init__(timeout=86400); self.bounty_key=bounty_key; self.hitman_id=hitman_id

    @discord.ui.button(label="✅ Confirm Kill & Pay", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Staff only.", ephemeral=True); return
        data = load_data()
        bty  = data["bounties"]["active"].get(self.bounty_key)
        if bty:
            bty["completed"] = True
            exp = datetime.now(timezone.utc) + timedelta(hours=48)
            data["bounties"]["cooldowns"][normalize(bty.get("target_name",""))] = exp.isoformat()
            save_data(data)
        reward = bty.get("reward",0) if bty else 0
        try:
            hitman = await bot.fetch_user(self.hitman_id)
            await hitman.send(f"✅ Your bounty claim on **{bty.get('target_name','?')}** has been **verified**! Reward: {fmt(reward)} — collect from an admin.")
            hitman_mention = hitman.mention
        except Exception:
            hitman_mention = f"<@{self.hitman_id}>"
        await interaction.message.edit(content="✅ Kill confirmed — reward authorised.", view=None)
        await interaction.response.send_message("Contract closed.", ephemeral=True)
        await audit_log(interaction.guild, f"[BOUNTY CONFIRMED] Staff approved kill on **{bty.get('target_name','?') if bty else '?'}** — {fmt(reward)} to {hitman_mention}")

    @discord.ui.button(label="❌ Reject Claim", style=discord.ButtonStyle.secondary)
    async def reject(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message("❌ Staff only.", ephemeral=True); return
        await interaction.message.edit(content="❌ Proof rejected.", view=None)
        await interaction.response.send_message("Claim rejected.", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[BountyApprovalView Error] {error}")
        try:
            await interaction.response.send_message("❌ An error occurred processing this action.", ephemeral=True)
        except Exception: pass


# ─────────────────────────────────────────────
#  SCAM REPORT FLOW
# ─────────────────────────────────────────────
class ScamReportBoardView(View):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label="🚨 Submit Scam Report", style=discord.ButtonStyle.danger, custom_id="scam_report_create")
    async def create_report(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down!", ephemeral=True); return
        await interaction.response.send_modal(ScamReportModal())

class ScamReportModal(Modal, title="Submit a Scam Report"):
    target_name = TextInput(label="Scammer's Game Name & ID",        placeholder="e.g. Badboy_Flipping #884192", max_length=80)
    scam_type   = TextInput(label="Scam Type / Category",            placeholder="e.g. Vehicle Trade Fraud",    max_length=80)
    stolen_value= TextInput(label="Stolen Asset Value ($)",           placeholder="e.g. 2500000",                max_length=15)
    description = TextInput(label="Detailed Description",             placeholder="How did the scam happen?",    max_length=500, style=discord.TextStyle.paragraph)
    async def on_submit(self, interaction: discord.Interaction):
        ok,val,err = validate_price(self.stolen_value.value)
        if not ok: await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        pending[interaction.user.id] = {
            "target_name": self.target_name.value, "scam_type": self.scam_type.value,
            "value": val, "description": self.description.value, "reporter_id": interaction.user.id,
            "public_channel_id": interaction.channel_id,
        }
        await interaction.response.send_message("📸 Upload a **screenshot** proving the scam (trade log, chat, etc.) in this channel. *(2 min)*", ephemeral=True)
        await _collect_scam_proof(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[ScamReportModal Error] {error}")
        try:
            await interaction.response.send_message("❌ Something went wrong submitting your report. Please try again.", ephemeral=True)
        except Exception: pass

async def _collect_scam_proof(interaction: discord.Interaction):
    user  = interaction.user
    guild = interaction.guild
    # Resolve channel safely — partial channels lack .guild / .send; fall back to guild cache
    channel = guild.get_channel(interaction.channel_id) if guild else interaction.channel
    if channel is None:
        print(f"[ScamProof] Could not resolve channel {interaction.channel_id}"); return
    snap = pending.get(user.id, {})
    def chk(m): return m.author.id==user.id and m.channel.id==channel.id and len(m.attachments)>0
    try:
        m1 = await bot.wait_for("message", check=chk, timeout=120)
        img_url = m1.attachments[0].url
    except asyncio.TimeoutError:
        await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    def chk_txt(m): return m.author.id==user.id and m.channel.id==channel.id and m.content.startswith("http")
    p = await channel.send(f"{user.mention} ✅ Screenshot received! Now **paste a video/URL link** (YouTube, Imgur, Medal.tv) as proof. *(2 min)*")
    try:
        m2 = await bot.wait_for("message", check=chk_txt, timeout=120)
        video_url = m2.content.strip()
        await p.delete()
    except asyncio.TimeoutError:
        await p.delete(); video_url = "N/A"
    snap["proof_img"] = img_url; snap["proof_url"] = video_url
    # Route to staff audit — use guild from interaction, not channel.guild
    audit_ch = discord.utils.get(guild.text_channels, name=SCAM_AUDIT_CH) if guild else None
    if not audit_ch:
        await channel.send(f"{user.mention} ⚠️ Staff audit channel `#{SCAM_AUDIT_CH}` not found. Ask an admin to create it.", delete_after=20)
        pending.pop(user.id,None); return
    audit_embed = discord.Embed(title=f"🚨 SCAM REPORT — {snap['target_name']}", color=discord.Color.red())
    audit_embed.add_field(name="Reporter",    value=user.mention,         inline=True)
    audit_embed.add_field(name="Scam Type",   value=snap["scam_type"],    inline=True)
    audit_embed.add_field(name="Value Stolen",value=fmt(snap["value"]),   inline=True)
    audit_embed.add_field(name="Description", value=snap["description"][:400], inline=False)
    audit_embed.add_field(name="📸 Screenshot",value=f"[View]({img_url})", inline=True)
    audit_embed.add_field(name="🔗 Video/URL", value=video_url,            inline=True)
    audit_msg = await audit_ch.send(embed=audit_embed, view=ScamAuditView())
    data = load_data()
    data["scammer_registry"]["pending"][listing_key(audit_ch.id, audit_msg.id)] = snap
    save_data(data); pending.pop(user.id,None)
    await channel.send(f"{user.mention} ✅ Report submitted for staff review. You'll be notified when a decision is made.", delete_after=20)
    await audit_log(channel.guild, f"[SCAM REPORT] {user.mention} filed report against **{snap['target_name']}**")


# ─────────────────────────────────────────────
#  AUCTION HOUSE SETUP
# ─────────────────────────────────────────────
class AuctionSetupModal(Modal, title="Create Auction Listing"):
    product_name = TextInput(label="Product Name",    placeholder="e.g. Sultan RS — Unique Plates", max_length=100)
    starting_bid = TextInput(label="Starting Bid ($)", placeholder="e.g. 100000",                   max_length=15)
    async def on_submit(self, interaction: discord.Interaction):
        ok,val,err = validate_price(self.starting_bid.value)
        if not ok: await interaction.response.send_message(f"❌ {err}", ephemeral=True); return
        if has_bad_words(self.product_name.value): await interaction.response.send_message("🚨 Inappropriate content blocked.", ephemeral=True); return
        pending[interaction.user.id] = {"product_name":self.product_name.value,"starting_bid":val}
        await interaction.response.send_message("📸 Upload the **Item Showcase Image** for your auction now. *(2 min)*", ephemeral=True)
        await _collect_auction_image(interaction)

async def _collect_auction_image(interaction: discord.Interaction):
    user, channel = interaction.user, interaction.channel
    snap = pending.get(user.id, {})
    def chk(m): return m.author.id==user.id and m.channel.id==channel.id and len(m.attachments)>0
    try:
        m = await bot.wait_for("message", check=chk, timeout=120)
        img_url = m.attachments[0].url
    except asyncio.TimeoutError:
        await channel.send(f"{user.mention} ⏰ Timed out.", delete_after=15); pending.pop(user.id,None); return
    pname  = snap.get("product_name","Item"); sbid = snap.get("starting_bid",0)
    ends_at = datetime.now(timezone.utc) + timedelta(hours=24)
    embed = discord.Embed(title=f"🔨 LIVE AUCTION — {pname}",
        description=f"⏰ **Closes:** <t:{int(ends_at.timestamp())}:R> (<t:{int(ends_at.timestamp())}:F>)",
        color=discord.Color.gold())
    embed.add_field(name="💰 Current Bid", value=fmt(sbid),         inline=True)
    embed.add_field(name="👑 Top Bidder",  value="No bids yet",     inline=True)
    embed.add_field(name="📦 Seller",      value=user.display_name, inline=True)
    embed.set_image(url=img_url)
    embed.set_footer(text="SimpleMarketHub Auction House")
    msg = await channel.send(embed=embed, view=AuctionBidView())
    data = load_data()
    data["auction"] = {"active":True,"message_id":msg.id,"channel_id":channel.id,"owner_id":user.id,
                       "owner_name":str(user),"product_name":pname,"current_bid":sbid,"top_bidder_id":None,
                       "top_bidder_name":None,"ends_at":ends_at.isoformat(),"image_url":img_url}
    save_data(data); pending.pop(user.id,None)
    await audit_log(channel.guild, f"[AUCTION] {user.mention} started auction for **{pname}** — starting at {fmt(sbid)}")
    delay = (ends_at - datetime.now(timezone.utc)).total_seconds()
    asyncio.create_task(_delayed_close(max(delay, 0)))

async def _close_auction():
    data = load_data()
    auction = data["auction"]
    if not auction["active"]: return
    auction["active"] = False; save_data(data)
    ch = bot.get_channel(auction["channel_id"])
    if not ch: return
    try:
        msg   = await ch.fetch_message(auction["message_id"])
        embed = msg.embeds[0]
        embed.color = discord.Color.dark_gold()
        embed.title = "🏆 AUCTION ENDED — " + (auction.get("product_name") or "Item")
        winner_id = auction.get("top_bidder_id")
        if winner_id:
            embed.description = f"⏰ **Auction concluded!**\n\n🥇 **Winner:** <@{winner_id}>\n💰 **Winning Bid:** {fmt(auction['current_bid'])}"
        else:
            embed.description = "⏰ **Auction ended with no bids.**"
        dv = View(timeout=None)
        for lbl in ["+ $50,000","+ $100,000","+ $500,000","✏️ Custom Bid"]:
            dv.add_item(Button(label=lbl, style=discord.ButtonStyle.secondary, disabled=True, custom_id=f"dis_{lbl}"))
        await msg.edit(embed=embed, view=dv)
        if winner_id:
            await ch.send(f"🎉 Congratulations <@{winner_id}>! You won **{auction.get('product_name')}** with **{fmt(auction['current_bid'])}**! Contact <@{auction['owner_id']}> to complete the transaction.")
            # Log to price history
            log_price(auction.get("product_name","Auction Item"), auction["current_bid"], "auction")
    except Exception as ex:
        print(f"[Auction close error] {ex}")

async def _delayed_close(delay):
    await asyncio.sleep(delay)
    await _close_auction()


# ─────────────────────────────────────────────
#  SUBSCRIBE VIEW & MY LISTINGS VIEW
# ─────────────────────────────────────────────
class SubscribeView(View):
    def __init__(self, uid): super().__init__(timeout=60); self.uid = uid
    @discord.ui.select(placeholder="Pick a category to toggle...",
        options=[
            discord.SelectOption(label="🚗 Vehicles",           value="vehicle",    description="New vehicle alert"),
            discord.SelectOption(label="🏡 Real Estate",         value="realestate", description="New property alert"),
            discord.SelectOption(label="🎒 Skins & Accessories", value="skin",       description="New skin/accessory alert"),
            discord.SelectOption(label="🏢 Businesses",          value="business",   description="New business alert"),
        ], custom_id="subscribe_select")
    async def toggle(self, interaction: discord.Interaction, select: Select):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Not your menu.", ephemeral=True); return
        cat  = select.values[0]
        data = load_data()
        subs = data["subscriptions"].setdefault(cat, [])
        if interaction.user.id in subs:
            subs.remove(interaction.user.id); action = "unsubscribed from"
        else:
            subs.append(interaction.user.id); action = "subscribed to"
        save_data(data)
        await interaction.response.send_message(f"✅ You are now **{action}** **{CATEGORY_LABELS.get(cat,cat)}** alerts!", ephemeral=True)

class MyListingsDeleteView(View):
    def __init__(self, uid, listings):
        super().__init__(timeout=120); self.uid = uid
        if listings:
            opts = [discord.SelectOption(label=f"{v.get('type','?').title()} — {v.get('created_at','')[:10]}"[:100], value=k)
                    for k,v in list(listings.items())[:25]]
            s = Select(placeholder="Select a listing to delete...", options=opts, custom_id="my_listings_del")
            s.callback = self._delete; self.add_item(s)
    async def _delete(self, interaction: discord.Interaction):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Not your listings.", ephemeral=True); return
        key  = interaction.data["values"][0]
        data = load_data()
        lst  = data["listings"].get(key)
        if lst:
            ch = bot.get_channel(lst.get("channel_id"))
            if ch:
                try:
                    m = await ch.fetch_message(lst["message_id"])
                    await m.delete()
                except Exception: pass
            del data["listings"][key]; save_data(data)
        await interaction.response.send_message("✅ Listing deleted.", ephemeral=True); self.stop()


# ─────────────────────────────────────────────
#  BACKGROUND TASKS
# ─────────────────────────────────────────────
@tasks.loop(hours=6)
async def cleanup_old_listings():
    data = load_data()
    now  = datetime.now(timezone.utc)
    # 7-day listing cleanup
    cutoff7 = now - timedelta(days=7)
    to_rm = [k for k,v in data["listings"].items()
             if not v.get("sold") and datetime.fromisoformat(v.get("created_at",now.isoformat())).replace(tzinfo=timezone.utc) < cutoff7]
    for k in to_rm:
        lst = data["listings"][k]
        ch  = bot.get_channel(lst.get("channel_id"))
        if ch:
            arc = discord.utils.get(ch.guild.text_channels, name=ARCHIVE_CHANNEL)
            if arc:
                e = discord.Embed(title="📦 Archived Listing",
                    description=f"Listing by <@{lst['owner_id']}> archived after 7 days.",
                    color=discord.Color.greyple())
                e.add_field(name="Type", value=lst.get("type","?").title(), inline=True)
                e.add_field(name="Listed", value=lst.get("created_at","")[:10], inline=True)
                await arc.send(embed=e)
            try:
                m = await ch.fetch_message(lst["message_id"]); await m.delete()
            except Exception: pass
        del data["listings"][k]
    # 3-day LF request cleanup
    cutoff3 = now - timedelta(days=3)
    to_rm_lf = [k for k,v in data["lf_requests"].items()
                if not v.get("fulfilled") and datetime.fromisoformat(v.get("created_at",now.isoformat())).replace(tzinfo=timezone.utc) < cutoff3]
    for k in to_rm_lf:
        req = data["lf_requests"][k]
        try:
            ch  = bot.get_channel(req.get("channel_id"))
            if ch:
                m = await ch.fetch_message(req["message_id"]); await m.delete()
        except Exception: pass
        try:
            buyer = await bot.fetch_user(req["owner_id"])
            await buyer.send(f"⏰ Your bounty request for **{req['asset_name']}** has expired (3-day limit). Post a fresh one anytime with `!setup_requests`.")
        except Exception: pass
        del data["lf_requests"][k]
    if to_rm or to_rm_lf:
        save_data(data)
        print(f"[Cleanup] Removed {len(to_rm)} listings, {len(to_rm_lf)} LF requests.")

@tasks.loop(minutes=5)
async def check_auction_expiry():
    data    = load_data()
    auction = data["auction"]
    if not auction.get("active"): return
    try:
        end = datetime.fromisoformat(auction["ends_at"]).replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= end:
            await _close_auction()
    except Exception: pass


# ─────────────────────────────────────────────
#  GIVEAWAY SYSTEM
# ─────────────────────────────────────────────
class GiveawayView(View):
    def __init__(self): super().__init__(timeout=None)

    @discord.ui.button(label="🎉 Join Giveaway", style=discord.ButtonStyle.success, custom_id="giveaway_join")
    async def join(self, interaction: discord.Interaction, button: Button):
        if check_spam(interaction.user.id):
            await interaction.response.send_message("⚠️ Slow down! Wait a few seconds.", ephemeral=True); return
        if is_blacklisted(interaction.user):
            await interaction.response.send_message("❌ Access Denied: Your account is on the Scammer Registry.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        gw   = data["giveaways"].get(key)
        if gw is None:
            await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True); return
        if gw.get("ended"):
            await interaction.response.send_message("⏰ This giveaway has already ended.", ephemeral=True); return
        uid = interaction.user.id
        if uid in gw["entries"]:
            await interaction.response.send_message("ℹ️ You are already entered in this giveaway!", ephemeral=True); return
        gw["entries"].append(uid)
        save_data(data)
        await interaction.response.send_message(
            f"✅ You're in! Good luck in the **{gw['prize']}** giveaway. 🎉\n"
            f"Current entries: **{len(gw['entries'])}**", ephemeral=True)
        try:
            dm_embed = discord.Embed(
                title="🎉 Giveaway Entry Confirmed!",
                description=f"You've been entered into the **{gw['prize']}** giveaway on **{interaction.guild.name}**.\n\n"
                            f"[Jump to giveaway]({interaction.message.jump_url})",
                color=discord.Color.green())
            dm_embed.set_footer(text="Good luck! Winners are drawn by staff.")
            await interaction.user.send(embed=dm_embed)
        except discord.Forbidden:
            pass

    @discord.ui.button(label="🌀 Spin the Wheel", style=discord.ButtonStyle.danger, custom_id="giveaway_spin")
    async def spin(self, interaction: discord.Interaction, button: Button):
        is_owner = interaction.user.id == interaction.guild.owner_id
        is_staff = has_role(interaction.user, ROLE_STAFF)
        if not (is_owner or is_staff):
            await interaction.response.send_message(f"❌ Only **@{ROLE_STAFF}** or the server owner can spin the wheel.", ephemeral=True); return
        data = load_data()
        key  = listing_key(interaction.channel.id, interaction.message.id)
        gw   = data["giveaways"].get(key)
        if gw is None:
            await interaction.response.send_message("❌ Giveaway not found.", ephemeral=True); return
        if gw.get("ended"):
            await interaction.response.send_message("⏰ This giveaway has already ended.", ephemeral=True); return
        entries = gw["entries"]
        if not entries:
            await interaction.response.send_message("⚠️ No one has entered yet! The wheel needs players to spin.", ephemeral=True); return

        # Disable entry button immediately
        gw["ended"] = True
        save_data(data)
        locked_view = View(timeout=None)
        locked_view.add_item(Button(label="🔒 Entries Closed", style=discord.ButtonStyle.secondary,
                                    disabled=True, custom_id="giveaway_closed"))
        locked_view.add_item(Button(label="🌀 Spinning...", style=discord.ButtonStyle.danger,
                                    disabled=True, custom_id="giveaway_spinning"))

        # Fetch member display names for the animation
        import random
        pool_names = []
        for uid in entries:
            member = interaction.guild.get_member(uid)
            pool_names.append(member.display_name if member else f"Player#{uid}")

        prize = gw["prize"]
        spin_embed = interaction.message.embeds[0]
        spin_embed.color = discord.Color.orange()
        spin_embed.title = "🎰 SPINNING THE WHEEL..."

        await interaction.response.edit_message(embed=spin_embed, view=locked_view)

        # Animated spin loop — 5 frames at 0.6s each
        for _ in range(5):
            candidate = random.choice(pool_names)
            spin_embed.description = (
                "✨ **SPINNING THE ROLLING WHEEL...** ✨\n\n"
                f"⏩  `🎰  {candidate}  `  ⏪\n\n"
                f"*{len(entries)} entrants competing...*"
            )
            await interaction.message.edit(embed=spin_embed, view=locked_view)
            await asyncio.sleep(0.6)

        # Pick winner
        winner_id   = random.choice(entries)
        winner      = interaction.guild.get_member(winner_id)
        winner_name = winner.display_name if winner else f"<@{winner_id}>"
        winner_mention = winner.mention if winner else f"<@{winner_id}>"

        # Final winner embed
        win_embed = discord.Embed(
            title="🏆 GIVEAWAY COMPLETED",
            description=(
                f"🎉 Congratulations to our winner!\n\n"
                f"👑  **{winner_mention}**\n\n"
                f"🎁  **Prize:** {prize}\n"
                f"👥  **Total Entrants:** {len(entries)}"
            ),
            color=discord.Color.gold(),
        )
        win_embed.set_footer(text=f"Drawn by {interaction.user.display_name} | SimpleMarketHub Giveaway System")
        done_view = View(timeout=None)
        done_view.add_item(Button(label="🏆 Winner Drawn!", style=discord.ButtonStyle.secondary,
                                  disabled=True, custom_id="giveaway_done"))
        await interaction.message.edit(embed=win_embed, view=done_view)

        # Announce winner publicly in channel
        await interaction.channel.send(
            f"🎉 **GIVEAWAY WINNER** 🎉\n"
            f"Congratulations {winner_mention}! You won **{prize}**! 🏆\n"
            f"Please contact a staff member to claim your prize.")

        # DM the winner
        if winner:
            try:
                win_dm = discord.Embed(
                    title="🏆 You Won a Giveaway!",
                    description=f"Congratulations! You won **{prize}** in the giveaway on **{interaction.guild.name}**!\n\nPlease contact a staff member to claim your prize.",
                    color=discord.Color.gold())
                await winner.send(embed=win_dm)
            except discord.Forbidden:
                pass

        # Log to market-logs
        await audit_log(interaction.guild,
            f"[GIVEAWAY] 🏆 **{winner_name}** won **{prize}** ({len(entries)} entrants) — drawn by {interaction.user.mention}")

    async def on_error(self, interaction: discord.Interaction, error: Exception, item):
        print(f"[GiveawayView Error] {error}")
        try:
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
        except Exception: pass


# ─────────────────────────────────────────────
#  COMMANDS
# ─────────────────────────────────────────────
@bot.command(name="setup_market")
@commands.has_permissions(manage_channels=True)
async def setup_market(ctx):
    embed = discord.Embed(title="🏪 SimpleMarketHub — Marketplace",
        description=("Choose a category below to list your item.\n\n"
                     "🚗 **Vehicles** — Cars, Bikes, Boats, Helicopters\n"
                     "🏡 **Real Estate** — Houses & Apartments\n"
                     "🎒 **Skins & Accessories** — Character skins and gear\n"
                     "🏢 **Businesses** — Commercial enterprises\n"
                     f"🪙 **Sell Game Cash** — @{ROLE_VERIFIED_BROKER} role required\n\n"
                     "*All listings auto-expire after 7 days.*"),
        color=discord.Color.blurple())
    embed.set_footer(text="SimpleMarketHub | Anti-troll & auto-cleanup enabled")
    await ctx.message.delete()
    await ctx.send(embed=embed, view=MainMarketView())

@bot.command(name="setup_auction")
async def setup_auction(ctx):
    data    = load_data()
    auction = data["auction"]
    if auction.get("active"):
        try:
            ts = int(datetime.fromisoformat(auction["ends_at"]).replace(tzinfo=timezone.utc).timestamp())
            await ctx.reply(f"❌ An auction is already live — closes <t:{ts}:R>.", delete_after=15)
        except Exception:
            await ctx.reply("❌ An auction is already active.", delete_after=15)
        return
    try: await ctx.message.delete()
    except Exception: pass
    class _StartView(View):
        def __init__(self): super().__init__(timeout=60)
        @discord.ui.button(label="🔨 Launch Auction Setup", style=discord.ButtonStyle.danger)
        async def launch(self, interaction: discord.Interaction, button: Button):
            if interaction.user.id != ctx.author.id and not interaction.user.guild_permissions.manage_channels:
                await interaction.response.send_message("❌ Not authorised.", ephemeral=True); return
            await interaction.response.send_modal(AuctionSetupModal()); self.stop()
    await ctx.send("Click below to open the auction setup form:", view=_StartView())

@bot.command(name="setup_requests")
async def setup_requests(ctx):
    embed = discord.Embed(title="🔍 SimpleMarketHub — Looking For Board",
        description=("Can't find what you need? Post a buyer request and let sellers come to you!\n\n"
                     "Click **🔍 Create Buyer Request** to post your search.\n"
                     "Sellers click **🤝 Sell This to Me** to make you an offer.\n\n"
                     "*Requests auto-expire after 3 days.*"),
        color=discord.Color.blurple())
    embed.set_footer(text="Anti-overpricing filter active — sellers cannot exceed your budget.")
    try: await ctx.message.delete()
    except Exception: pass
    await ctx.send(embed=embed, view=LFBoardView())

@bot.command(name="gstart")
@commands.has_permissions(manage_channels=True)
async def gstart(ctx, *, prize: str = ""):
    if not prize:
        await ctx.reply("❌ Usage: `!gstart <prize name>`  e.g. `!gstart Sultan RS + $500,000`", delete_after=15); return
    if has_bad_words(prize):
        await ctx.reply("🚨 Inappropriate content in prize name.", delete_after=10); return
    try:
        await ctx.message.delete()
    except Exception:
        pass
    embed = discord.Embed(
        title="🎉 GIVEAWAY — Enter Now!",
        description=(
            f"🎁  **Prize:** {prize}\n\n"
            "Click **🎉 Join Giveaway** below to enter.\n"
            "Staff will spin the wheel when ready to draw a winner.\n\n"
            "*Scammer Registry members are automatically blocked from entering.*"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name="👥 Entries", value="0", inline=True)
    embed.add_field(name="📋 Status",  value="🟢 Open",  inline=True)
    embed.set_footer(text=f"Started by {ctx.author.display_name} | SimpleMarketHub Giveaway System")
    msg = await ctx.send(embed=embed, view=GiveawayView())
    data = load_data()
    key  = listing_key(ctx.channel.id, msg.id)
    data["giveaways"][key] = {
        "prize":      prize,
        "host_id":    ctx.author.id,
        "channel_id": ctx.channel.id,
        "message_id": msg.id,
        "entries":    [],
        "ended":      False,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_data(data)
    await audit_log(ctx.guild, f"[GIVEAWAY] {ctx.author.mention} started giveaway for **{prize}**")


@bot.command(name="setup_bounties")
async def setup_bounties(ctx):
    embed = discord.Embed(title="💀 Dark Web — Anonymous Bounty Board",
        description=("Place an anonymous contract on a player in the underworld.\n\n"
                     "💀 **Anonymous Hit Contract** — Post your target & reward\n"
                     "🕶️ **Accept Contract** — @Hitman role only, 12-hour window\n"
                     "📸 **Submit Proof** — Routes to staff for verification\n\n"
                     "*48-hour target protection after a confirmed kill.*"),
        color=discord.Color.dark_red())
    embed.set_footer(text=f"Restricted to @{ROLE_HITMAN} members for acceptance")
    try: await ctx.message.delete()
    except Exception: pass
    await ctx.send(embed=embed, view=BountyBoardView())

@bot.command(name="setup_reports")
async def setup_reports(ctx):
    embed = discord.Embed(title="🛡️ Scammer Registry — Report Centre",
        description=("Protect the community by reporting scammers. All reports require proof and go through staff review.\n\n"
                     "🚨 **Submit Scam Report** — Fill the report form\n"
                     "📸 Upload screenshot evidence\n"
                     "🔗 Provide a video/URL link\n\n"
                     "*Confirmed scammers are blocked from the entire marketplace.*"),
        color=discord.Color.red())
    embed.set_footer(text="False reports are a bannable offence.")
    try: await ctx.message.delete()
    except Exception: pass
    await ctx.send(embed=embed, view=ScamReportBoardView())

@bot.command(name="market_stats")
async def market_stats(ctx):
    data    = load_data()
    stats   = data.get("stats",{})
    listings= data.get("listings",{})
    auction = data.get("auction",{})
    open_ads= sum(1 for l in listings.values() if not l.get("sold"))
    nc      = stats.get("realestate_neighborhood_counts",{})
    hottest = max(nc,key=nc.get) if nc else "N/A"
    embed   = discord.Embed(title="📈 SimpleMarketHub — Economy Dashboard", color=discord.Color.gold())
    embed.add_field(name="📋 Open Ads",           value=str(open_ads),                         inline=True)
    embed.add_field(name="🚗 Vehicle Value Total", value=fmt(stats.get("vehicle_value",0)),     inline=True)
    embed.add_field(name="🏡 Hottest Neighbourhood",value=hottest,                              inline=True)
    embed.add_field(name="🏢 Biggest Business Deal",value=fmt(stats.get("business_max_deal",0)),inline=True)
    embed.add_field(name="📊 All-Time Ads Posted",  value=str(stats.get("total_ads",0)),        inline=True)
    if auction.get("active"):
        try:
            ts = int(datetime.fromisoformat(auction["ends_at"]).replace(tzinfo=timezone.utc).timestamp())
            astat = f"🔴 **LIVE** — *{auction.get('product_name','Item')}* closes <t:{ts}:R>"
        except Exception: astat = "🔴 **LIVE**"
    else: astat = "⚪ No active auction"
    embed.add_field(name="🔨 Auction House", value=astat, inline=False)
    embed.set_footer(text="SimpleMarketHub | Real-time stats")
    await ctx.reply(embed=embed)

@bot.command(name="subscribe")
async def subscribe(ctx):
    data  = load_data()
    subs  = data.get("subscriptions",{})
    uid   = ctx.author.id
    active= [CATEGORY_LABELS[c] for c,ids in subs.items() if uid in ids]
    embed = discord.Embed(title="🔔 Trade Alert Subscriptions",
        description=f"Toggle DM alerts per category.\n\n**Currently subscribed to:** {', '.join(active) or 'None'}",
        color=discord.Color.blurple())
    await ctx.reply(embed=embed, view=SubscribeView(uid), mention_author=False)

@bot.command(name="my_listings")
async def my_listings(ctx):
    data   = load_data()
    mine   = {k:v for k,v in data["listings"].items() if v.get("owner_id")==ctx.author.id and not v.get("sold")}
    if not mine:
        await ctx.reply("📭 You have no active listings.", mention_author=False); return
    icons  = {"vehicle":"🚗","realestate":"🏡","skin":"🎒","business":"🏢"}
    embed  = discord.Embed(title=f"📋 Your Active Listings ({len(mine)})", color=discord.Color.blurple(),
                           description="Use the dropdown below to delete any listing.")
    for k,v in list(mine.items())[:25]:
        ltype = v.get("type","?"); icon = icons.get(ltype,"📦")
        date  = v.get("created_at","")[:10]
        jump  = f"https://discord.com/channels/{ctx.guild.id}/{v.get('channel_id')}/{v.get('message_id')}"
        embed.add_field(name=f"{icon} {ltype.title()} — {date}", value=f"[Jump]({jump})", inline=False)
    await ctx.reply(embed=embed, view=MyListingsDeleteView(ctx.author.id, mine), mention_author=False)

@bot.command(name="rates")
async def rates(ctx):
    data    = load_data()
    history = data.get("cash_rate_history", [])
    # Filter to last 10 completed trades for the rate stats
    recent10 = [e for e in history if e.get("rate") and e.get("amount_millions")][-10:]
    # Filter to last 7 days for weekly volume
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    weekly = [
        e for e in history
        if e.get("amount_millions") and
        datetime.fromisoformat(e["date"]).replace(tzinfo=timezone.utc) >= cutoff
    ]
    weekly_volume = sum(e["amount_millions"] for e in weekly)
    if not recent10:
        embed = discord.Embed(
            title="🪙 GRAND MOBILE ECONOMY RATE HUB",
            description=(
                "📊 No completed cash trades yet.\n\n"
                "Rates are recorded automatically each time a deal is verified through the escrow system.\n"
                "Complete a trade via **🤝 Request Cash Middleman** to see live rates here."
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="SimpleMarketHub | Rates update after every verified escrow deal")
        await ctx.reply(embed=embed, mention_author=False); return
    rates_list = [e["rate"] for e in recent10]
    avg_rate   = sum(rates_list) / len(rates_list)
    low_rate   = min(rates_list)
    high_rate  = max(rates_list)
    last_rate  = recent10[-1]["rate"]
    last_date  = recent10[-1]["date"][:10]
    trade_count = len(history)
    # Trend arrow vs previous average (compare last 5 vs prior 5 if enough data)
    if len(recent10) >= 6:
        old_avg = sum(rates_list[:5]) / 5
        new_avg = sum(rates_list[5:]) / len(rates_list[5:])
        trend = "📈 Rising" if new_avg > old_avg else ("📉 Falling" if new_avg < old_avg else "➡️ Stable")
    else:
        trend = "➡️ Stable"
    embed = discord.Embed(
        title="🪙 GRAND MOBILE ECONOMY RATE HUB",
        description=(
            f"Live market data from the last **{len(recent10)} verified escrow deals**.\n"
            f"All rates are in **Real Money per 1 Million in-game cash**."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(name="📊 Average Market Rate",  value=f"**${avg_rate:.2f}** / 1M",  inline=True)
    embed.add_field(name="📉 Lowest Recent Rate",   value=f"**${low_rate:.2f}** / 1M",  inline=True)
    embed.add_field(name="📈 Highest Recent Rate",  value=f"**${high_rate:.2f}** / 1M", inline=True)
    embed.add_field(name="🔄 Market Trend",         value=trend,                         inline=True)
    embed.add_field(name="🕒 Last Verified Deal",   value=f"${last_rate:.2f}/1M  ·  {last_date}", inline=True)
    embed.add_field(name="📦 Total Deals Logged",   value=str(trade_count),              inline=True)
    embed.add_field(
        name="🌐 Ecosystem Volume (7 days)",
        value=f"**{weekly_volume:g}M** game cash traded safely this week!",
        inline=False,
    )
    embed.set_footer(text="SimpleMarketHub | Rates update after every verified escrow deal · Use !price_check for item history")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="price_check")
async def price_check(ctx, *, item: str = ""):
    if not item:
        await ctx.reply("Usage: `!price_check <item name>` e.g. `!price_check Sultan RS`", mention_author=False); return
    data    = load_data()
    history = data.get("price_history", {})
    key_low = normalize(item)
    # Partial match
    matches: list = []
    matched_key = ""
    for k, records in history.items():
        if key_low in k or k in key_low:
            matches = records; matched_key = k; break
    if not matches:
        if not history:
            await ctx.reply(
                "📊 **No price history yet.** Prices are recorded automatically when:\n"
                "• A seller clicks **🔒 Mark as Sold** on a listing\n"
                "• An auction closes with a winning bid\n\n"
                "Run `!price_check <item>` again once some sales have been completed.",
                mention_author=False)
        else:
            known = ", ".join(f"`{k}`" for k in list(history.keys())[:10])
            await ctx.reply(
                f"📊 No sales data for **{item}**.\n\n**Items with recorded sales:** {known}",
                mention_author=False)
        return
    last10  = matches[-10:]
    prices  = [r["price"] for r in last10]
    recent  = last10[-1]
    embed   = discord.Embed(title=f"📊 Price Guide — {matched_key.title()}", color=discord.Color.blue())
    embed.add_field(name="🔺 Highest Sale",      value=fmt(max(prices)),                      inline=True)
    embed.add_field(name="🔻 Lowest Sale",        value=fmt(min(prices)),                      inline=True)
    embed.add_field(name="📈 Avg (last 10 sales)",value=fmt(int(sum(prices)/len(prices))),     inline=True)
    embed.add_field(name="📦 Total Records",      value=str(len(matches)),                     inline=True)
    embed.add_field(name="🕒 Last Sale",          value=f"{fmt(recent['price'])} on {recent.get('date','?')}", inline=True)
    embed.add_field(name="🏷️ Category",           value=recent.get("type","unknown").replace("_"," ").title(), inline=True)
    embed.set_footer(text="Prices logged from completed sales (Mark as Sold / Auction wins)")
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="vouches")
async def vouches(ctx, member: discord.Member = None):
    target = member or ctx.author
    data   = load_data()
    entry  = data.get("vouches", {}).get(str(target.id))
    if not entry or not entry.get("ratings"):
        tip = " Ratings are submitted by trade parties when a middleman closes a room." if target == ctx.author else ""
        await ctx.reply(f"📋 **{target.display_name}** has no vouch ratings yet.{tip}", mention_author=False); return
    ratings    = entry.get("ratings", [])
    total_stars= sum(r["stars"] for r in ratings)
    avg        = total_stars / len(ratings)
    stars_str  = "⭐" * round(avg)
    embed      = discord.Embed(title=f"🤝 Certified Escrow Officer — {target.display_name}", color=discord.Color.gold())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Total Rated Trades",    value=str(len(ratings)),                inline=True)
    embed.add_field(name="Reputation Rating",     value=f"{stars_str} ({avg:.1f}/5)",     inline=True)
    embed.add_field(name="Highest Handled Deal",  value=fmt(entry.get("highest_deal",0)), inline=True)
    last3 = ratings[-3:]
    for r in reversed(last3):
        embed.add_field(name=f"{'⭐'*r['stars']} — {r.get('date','?')}",
                        value=r.get("comment","No comment") or "No comment", inline=False)
    embed.set_footer(text="Rate a middleman by asking staff after a trade closes.")
    await ctx.reply(embed=embed, mention_author=False)

@bot.command(name="clear_market")
@commands.has_permissions(manage_channels=True)
async def clear_market(ctx):
    data  = load_data()
    ch_id = ctx.channel.id
    keys  = [k for k,v in data["listings"].items() if v.get("channel_id")==ch_id and not v.get("sold")]
    if not keys:
        await ctx.reply("📭 No active listings found in this channel.", mention_author=False); return
    confirm_view = _ConfirmClearView(keys, ctx.author.id)
    await ctx.reply(f"⚠️ This will delete **{len(keys)} active listing(s)** in this channel. Confirm?", view=confirm_view, mention_author=False)

class _ConfirmClearView(View):
    def __init__(self, keys, uid):
        super().__init__(timeout=30); self.keys=keys; self.uid=uid
    @discord.ui.button(label="✅ Yes, Clear All", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.uid:
            await interaction.response.send_message("❌ Not authorised.", ephemeral=True); return
        data = load_data(); count = 0
        for k in self.keys:
            lst = data["listings"].get(k)
            if lst:
                try:
                    ch = bot.get_channel(lst["channel_id"])
                    if ch:
                        m = await ch.fetch_message(lst["message_id"]); await m.delete()
                except Exception: pass
                del data["listings"][k]; count += 1
        save_data(data)
        await interaction.response.send_message(f"✅ Cleared **{count}** listing(s).", ephemeral=True)
        await interaction.message.edit(content=f"✅ Cleared **{count}** listing(s).", view=None)
        await audit_log(interaction.guild, f"[CLEAR] {interaction.user.mention} force-cleared {count} listings from {interaction.channel.mention}")
        self.stop()
    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("Cancelled.", ephemeral=True)
        await interaction.message.edit(content="Cancelled.", view=None)
        self.stop()


# ─────────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ SimpleMarketHub Bot online — {bot.user} ({bot.user.id})")
    bot.add_view(ListingFooterView())
    bot.add_view(AuctionBidView())
    bot.add_view(LFRequestEmbedView())
    bot.add_view(LFBoardView())
    bot.add_view(BountyContractView())
    bot.add_view(BountyBoardView())
    bot.add_view(ScamAuditView())
    bot.add_view(ScamReportBoardView())
    bot.add_view(MiddlemanRoomView())
    bot.add_view(MainMarketView())
    bot.add_view(CashListingView())
    bot.add_view(CashEscrowView())
    bot.add_view(GiveawayView())
    cleanup_old_listings.start()
    check_auction_expiry.start()
    # Resume any active auction
    data = load_data()
    auction = data.get("auction",{})
    if auction.get("active") and auction.get("ends_at"):
        try:
            end = datetime.fromisoformat(auction["ends_at"]).replace(tzinfo=timezone.utc)
            delay = (end - datetime.now(timezone.utc)).total_seconds()
            if delay > 0:
                print(f"[Auction] Resuming — closes in {int(delay)}s")
                asyncio.create_task(_delayed_close(delay))
            else:
                asyncio.create_task(_close_auction())
        except Exception as ex:
            print(f"[Auction resume error] {ex}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound): return
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("❌ You don't have permission to use that command.", delete_after=10); return
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"❌ Missing argument: `{error.param.name}`. Check `!help {ctx.invoked_with}`.", delete_after=15); return
    if isinstance(error, commands.BadArgument):
        await ctx.reply(f"❌ Invalid argument. Check `!help {ctx.invoked_with}`.", delete_after=15); return
    # Unwrap CommandInvokeError to get the real exception
    inner = getattr(error, "original", error)
    print(f"[Error in !{ctx.invoked_with}] {type(inner).__name__}: {inner}")
    await ctx.reply(f"❌ Something went wrong running that command. The error has been logged.", delete_after=20)


# ─────────────────────────────────────────────
#  ENTRYPOINT
# ─────────────────────────────────────────────
async def main():
    async with bot:
        await bot.start(TOKEN)

asyncio.run(main())
