"""Invite tracking for Astra Host."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

import discord

from config import BRAND_NAME

if TYPE_CHECKING:
    from bot import VexBot

logger = logging.getLogger("vexdeploy.invites")


class InviteTracker:
    """Tracks which invite a member used and awards inviter credits."""

    def __init__(self, bot: "VexBot") -> None:
        self.bot = bot
        self._cache: dict[int, dict[str, int]] = {}

    async def prime_guild(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
            self._cache[guild.id] = {i.code: i.uses or 0 for i in invites}
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            logger.warning(
                "Could not prime invites for guild %s: %s", guild.id, exc
            )
            self._cache.setdefault(guild.id, {})

    async def prime_all(self) -> None:
        for guild in self.bot.guilds:
            await self.prime_guild(guild)

    async def handle_member_join(self, member: discord.Member) -> bool:
        """Attribute join and update inviter counts. Returns True if credited."""
        guild = member.guild
        cached = self._cache.get(guild.id)
        if cached is None:
            await self.prime_guild(guild)
            cached = self._cache.get(guild.id, {})

        try:
            current_invites = await guild.invites()
            current = {i.code: i.uses or 0 for i in current_invites}
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            logger.warning("Cannot read invites in guild %s: %s", guild.id, exc)
            return False

        inviter_id: Optional[str] = None
        invite_code: Optional[str] = None
        ambiguous = False

        for code, uses in current.items():
            old = cached.get(code, -1)
            if old >= 0 and uses > old:
                if inviter_id is not None:
                    ambiguous = True
                invite = next((i for i in current_invites if i.code == code), None)
                if invite and invite.inviter:
                    inviter_id = str(invite.inviter.id)
                    invite_code = code

        vanished = [c for c in cached if c not in current]
        if vanished and not inviter_id:
            ambiguous = True

        self._cache[guild.id] = current

        if ambiguous or inviter_id is None:
            self.bot.db.record_join_event(
                member_id=str(member.id),
                guild_id=str(guild.id),
                inviter_id=None,
                invite_code=invite_code,
                is_fake=True,
            )
            logger.info(
                "Join by %s ambiguous or unknown inviter — not credited", member.id
            )
            return False

        if inviter_id == str(member.id):
            return False

        new_credit = self.bot.db.record_join_event(
            member_id=str(member.id),
            guild_id=str(guild.id),
            inviter_id=inviter_id,
            invite_code=invite_code,
            is_fake=False,
        )
        if new_credit:
            self.bot.db.recompute_eligibility(inviter_id)
            await self._maybe_notify_completion(member.guild, inviter_id)
            logger.info(
                "Invite detected: %s invited by %s", member.id, inviter_id
            )
            return True
        return False

    async def handle_member_remove(self, member: discord.Member) -> None:
        try:
            inviter_id = self.bot.db.record_leave(str(member.id))
            if inviter_id:
                self.bot.db.recompute_eligibility(inviter_id)
                logger.info(
                    "Member %s left — revoked invite from %s", member.id, inviter_id
                )
        except Exception:
            logger.exception("leave bookkeeping failed for %s", member.id)
        try:
            await self.prime_guild(member.guild)
        except Exception:
            logger.debug("prime after leave failed for guild %s", member.guild.id)

    async def _maybe_notify_completion(
        self, guild: discord.Guild, inviter_id: str
    ) -> None:
        if not self.bot.db.recompute_eligibility(inviter_id):
            return
        row = self.bot.db.get_invite_row(inviter_id)
        if not row or row["completion_notified"]:
            return
        self.bot.db.set_completion_notified(inviter_id, True)
        required = int(self.bot.db.get_setting("required_invites", 5))
        brand = self.bot.db.get_active_branding() or {}
        brand_name = brand.get("brand_name", BRAND_NAME)

        user: Optional[discord.abc.User] = guild.get_member(int(inviter_id))
        if user is None:
            try:
                user = await self.bot.fetch_user(int(inviter_id))
            except discord.HTTPException:
                user = None

        embed = discord.Embed(
            title=f"Invite goal reached — {brand_name}",
            description=(
                f"You have **{row['valid_invites']}** valid invites "
                f"(required **{required}**).\n\n"
                f"You can now create a VPS with `/createvps`."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=brand.get("footer") or brand_name)

        if user is not None:
            try:
                await user.send(embed=embed)
            except discord.HTTPException:
                logger.info("Could not DM completion to %s", inviter_id)

        channel_id = int(self.bot.db.get_setting("completion_channel_id", 0) or 0)
        if channel_id:
            channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    e = embed.copy()
                    e.description = f"<@{inviter_id}> " + (embed.description or "")
                    await channel.send(embed=e)
                except discord.HTTPException:
                    pass

    def eligibility_status(self, user_id: str) -> dict:
        required = int(self.bot.db.get_setting("required_invites", 5))
        row = self.bot.db.get_invite_row(user_id)
        valid = int(row["valid_invites"]) if row else 0
        return {
            "valid": valid,
            "required": required,
            "eligible": valid >= required,
            "remaining": max(0, required - valid),
            "completion_notified": bool(row["completion_notified"]) if row else False,
        }
