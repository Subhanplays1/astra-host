"""Branding manager for Astra Host (white-label)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import discord

from config import BRAND_DISCORD_COLORS, BRAND_NAME

if TYPE_CHECKING:
    from bot import VexBot

_AYTRO_RE = re.compile(r"aytro(?:cloud)?", re.IGNORECASE)


def scrub_aytro(text: Any) -> str:
    """Remove any Aytro/AytroCloud branding from a string."""
    s = str(text or "")
    if not _AYTRO_RE.search(s):
        return s
    s = _AYTRO_RE.sub(BRAND_NAME, s)
    s = re.sub(r"\s{2,}", " ", s).strip()
    return s


def brand_color(brand: dict[str, Any], *, fallback: int = 0x00FFFF) -> discord.Color:
    key = str(brand.get("primary_color") or "cyan").lower()
    return discord.Color.from_rgb(
        (BRAND_DISCORD_COLORS.get(key, fallback) >> 16) & 0xFF,
        (BRAND_DISCORD_COLORS.get(key, fallback) >> 8) & 0xFF,
        BRAND_DISCORD_COLORS.get(key, fallback) & 0xFF,
    )


def brand_embed(
    brand: dict[str, Any],
    *,
    title: str,
    description: str = "",
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description or None,
        color=brand_color(brand),
    )
    website = scrub_aytro(brand.get("website") or "")
    if website and website not in {"-", "—"}:
        embed.set_author(name=str(brand.get("brand_name") or BRAND_NAME), url=website)
    else:
        embed.set_author(name=str(brand.get("brand_name") or BRAND_NAME))
    embed.set_footer(
        text=scrub_aytro(brand.get("footer") or brand.get("brand_name") or BRAND_NAME)
    )
    return embed


class BrandingManager:
    def __init__(self, bot: "VexBot") -> None:
        self.bot = bot

    def active(self) -> dict[str, Any]:
        brand = self.bot.db.get_active_branding()
        if not brand:
            from config import DEFAULT_BRAND

            brand = {"profile_name": "default", "version": 1, **DEFAULT_BRAND}
        # scrub foreign panel branding (e.g. AytroCloud) at read time
        for key in (
            "brand_name",
            "brand_tagline",
            "footer",
            "website",
            "support_email",
            "motd_template",
            "logo",
        ):
            if key in brand and isinstance(brand[key], str):
                brand[key] = scrub_aytro(brand[key])
        return brand

    def color(self) -> discord.Color:
        return brand_color(self.active())

    def embed(self, *, title: str, description: str = "") -> discord.Embed:
        return brand_embed(self.active(), title=title, description=description)

    def set_field(self, field: str, value: Any) -> int:
        brand = self.active()
        if isinstance(value, str):
            value = scrub_aytro(value)
        return self.bot.db.update_branding_field(
            str(brand["profile_name"]), field, value
        )

    def brand_label(self) -> str:
        return scrub_aytro(self.bot.db.active_brand_label())


def render_brand_embed(
    brand: dict[str, Any],
    *,
    title: str | None = None,
    version: int | None = None,
) -> discord.Embed:
    ver = version if version is not None else int(brand.get("version", 1))
    embed = brand_embed(
        brand,
        title=title or f"{scrub_aytro(brand.get('brand_name', BRAND_NAME))} — branding",
    )
    embed.add_field(
        name="Profile", value=str(brand.get("profile_name", "default")), inline=True
    )
    embed.add_field(
        name="Name", value=scrub_aytro(brand.get("brand_name", "")), inline=True
    )
    embed.add_field(name="Version", value=str(ver), inline=True)
    embed.add_field(
        name="Tagline",
        value=scrub_aytro(brand.get("brand_tagline") or "—"),
        inline=False,
    )
    embed.add_field(
        name="Website", value=scrub_aytro(brand.get("website") or "—"), inline=True
    )
    embed.add_field(
        name="Discord", value=scrub_aytro(brand.get("discord") or "—"), inline=True
    )
    embed.add_field(
        name="Support",
        value=scrub_aytro(brand.get("support_email") or "—"),
        inline=True,
    )
    embed.add_field(
        name="MOTD",
        value="on" if brand.get("motd_enabled") in (1, True, "1") else "off",
        inline=True,
    )
    embed.add_field(
        name="Colors",
        value=(
            f"primary=`{brand.get('primary_color')}` "
            f"secondary=`{brand.get('secondary_color')}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="Footer", value=scrub_aytro(brand.get("footer") or "—"), inline=True
    )
    embed.set_footer(
        text="Changes bump version; new VPS get the latest brand automatically."
    )
    return embed
