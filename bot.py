from __future__ import annotations

import logging
from datetime import timedelta
from typing import Awaitable, Callable
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands

from config import Settings
from storage import ModerationStore


LOGGER = logging.getLogger("discord_moderation_bot")


class ModerationError(Exception):
    """A user-facing moderation error."""


class ModerationBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.store = ModerationStore(settings.database_path)
        self._synced = False

    async def setup_hook(self) -> None:
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            try:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            except (discord.Forbidden, discord.NotFound):
                LOGGER.warning(
                    "Could not sync commands to configured guild %s; "
                    "falling back to global sync.",
                    self.settings.guild_id,
                )
                synced = await self.tree.sync()
        else:
            synced = await self.tree.sync()
        self._synced = True
        LOGGER.info("Synced %d slash commands.", len(synced))

    async def on_ready(self) -> None:
        if self.user:
            LOGGER.info("Logged in as %s (%s)", self.user, self.user.id)
            LOGGER.info("Connected to %d server(s).", len(self.guilds))

    async def close(self) -> None:
        self.store.close()
        await super().close()

    async def send_log(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        target: discord.Member | discord.User | None = None,
        reason: str | None = None,
        details: str | None = None,
    ) -> None:
        channel_id = self.settings.log_channel_id
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        embed = discord.Embed(
            title=f"Moderationsaktion: {action}",
            colour=discord.Colour.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        if target:
            embed.add_field(name="Betroffenes Mitglied", value=f"{target.mention} (`{target.id}`)", inline=False)
        embed.add_field(name="Moderator", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        if reason:
            embed.add_field(name="Grund", value=reason, inline=False)
        if details:
            embed.add_field(name="Details", value=details, inline=False)
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            LOGGER.warning("Cannot send moderation log to channel %s.", channel_id)


def _guild_only() -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    return app_commands.guild_only()


def _normalize_role_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def _has_permission(
    permission: str,
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    def decorator(
        command: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        wrapped = app_commands.checks.has_permissions(**{permission: True})(command)
        return wrapped

    return decorator


def _has_role_at_or_above(
    role_name: str,
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        required_role = discord.utils.find(
            lambda role: _normalize_role_name(role.name) == _normalize_role_name(role_name),
            interaction.guild.roles,
        )
        return bool(
            required_role
            and any(role.position >= required_role.position for role in interaction.user.roles)
        )

    def decorator(
        command: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        return app_commands.check(predicate)(command)

    return decorator


def _has_one_of_named_roles(
    role_names: tuple[str, ...],
) -> Callable[[Callable[..., Awaitable[None]]], Callable[..., Awaitable[None]]]:
    normalized_names = {_normalize_role_name(role_name) for role_name in role_names}

    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        return any(_normalize_role_name(role.name) in normalized_names for role in interaction.user.roles)

    def decorator(
        command: Callable[..., Awaitable[None]],
    ) -> Callable[..., Awaitable[None]]:
        return app_commands.check(predicate)(command)

    return decorator


TEST_SUPPORT_ROLE_NAME = "test-supoorter"


def _ensure_can_moderate(
    interaction: discord.Interaction,
    member: discord.Member,
) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        raise ModerationError("Dieser Befehl funktioniert nur auf einem Server.")
    if member == interaction.user:
        raise ModerationError("Du kannst dich nicht selbst moderieren.")
    if member == interaction.guild.owner:
        raise ModerationError("Der Serverbesitzer kann nicht moderiert werden.")
    if interaction.user != interaction.guild.owner and member.top_role >= interaction.user.top_role:
        raise ModerationError("Das Zielmitglied hat eine gleich hohe oder höhere Rolle.")
    if interaction.guild.me and member.top_role >= interaction.guild.me.top_role:
        raise ModerationError("Meine Bot-Rolle muss über der Rolle des Zielmitglieds stehen.")


def _ensure_can_manage_role(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role,
) -> None:
    _ensure_can_moderate(interaction, member)
    if not interaction.guild:
        raise ModerationError("Dieser Befehl funktioniert nur auf einem Server.")
    if role.is_default():
        raise ModerationError("Die @everyone-Rolle kann nicht vergeben oder entfernt werden.")
    if interaction.guild.me and role >= interaction.guild.me.top_role:
        raise ModerationError("Meine Bot-Rolle muss über der Zielrolle stehen.")
    if (
        interaction.user != interaction.guild.owner
        and isinstance(interaction.user, discord.Member)
        and role >= interaction.user.top_role
    ):
        raise ModerationError("Du kannst keine gleich hohe oder höhere Rolle verwalten.")


async def _reply(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True)
    else:
        await interaction.response.send_message(content, ephemeral=True)


class RankReasonModal(discord.ui.Modal, title="Grund festlegen"):
    reason_input = discord.ui.TextInput(
        label="Grund",
        placeholder="Warum wird die Rolle geändert?",
        required=False,
        max_length=500,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, menu: "RankMenuView") -> None:
        super().__init__()
        self.menu = menu

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.menu.reason = str(self.reason_input.value).strip() or "Kein Grund angegeben"
        await interaction.response.send_message("Der Grund wurde gespeichert.", ephemeral=True)


class RankMenuView(discord.ui.View):
    def __init__(self, moderator_id: int) -> None:
        super().__init__(timeout=300)
        self.moderator_id = moderator_id
        self.selected_member: discord.Member | None = None
        self.selected_role: discord.Role | None = None
        self.reason = "Kein Grund angegeben"

        self.member_select = discord.ui.UserSelect(
            placeholder="Mitglied auswählen",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.member_select.callback = self.on_member_select
        self.add_item(self.member_select)

        self.role_select = discord.ui.RoleSelect(
            placeholder="Rolle auswählen",
            min_values=1,
            max_values=1,
            row=1,
        )
        self.role_select.callback = self.on_role_select
        self.add_item(self.role_select)

        reason_button = discord.ui.Button(
            label="Grund setzen",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        reason_button.callback = self.on_reason_button
        self.add_item(reason_button)

        uprank_button = discord.ui.Button(
            label="Befördern",
            emoji="⬆️",
            style=discord.ButtonStyle.success,
            row=3,
        )
        uprank_button.callback = self.on_uprank_button
        self.add_item(uprank_button)

        derank_button = discord.ui.Button(
            label="Downrank",
            emoji="⬇️",
            style=discord.ButtonStyle.danger,
            row=3,
        )
        derank_button.callback = self.on_derank_button
        self.add_item(derank_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.moderator_id:
            await interaction.response.send_message(
                "Dieses Rollenmenü wurde von einer anderen Person geöffnet.",
                ephemeral=True,
            )
            return False
        return True

    async def on_member_select(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not self.member_select.values:
            await interaction.response.send_message("Kein gültiges Mitglied ausgewählt.", ephemeral=True)
            return
        selected = self.member_select.values[0]
        if isinstance(selected, discord.Member):
            self.selected_member = selected
        else:
            try:
                self.selected_member = await interaction.guild.fetch_member(selected.id)
            except discord.NotFound:
                self.selected_member = None
        await interaction.response.send_message("Mitglied ausgewählt.", ephemeral=True)

    async def on_role_select(self, interaction: discord.Interaction) -> None:
        self.selected_role = self.role_select.values[0] if self.role_select.values else None
        await interaction.response.send_message("Rolle ausgewählt.", ephemeral=True)

    async def on_reason_button(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RankReasonModal(self))

    async def on_uprank_button(self, interaction: discord.Interaction) -> None:
        await self._perform_rank(interaction, promote=True)

    async def on_derank_button(self, interaction: discord.Interaction) -> None:
        await self._perform_rank(interaction, promote=False)

    async def _perform_rank(self, interaction: discord.Interaction, *, promote: bool) -> None:
        if not interaction.guild or not self.selected_member or not self.selected_role:
            await interaction.response.send_message(
                "Bitte zuerst ein Mitglied und eine Rolle auswählen.",
                ephemeral=True,
            )
            return

        member = self.selected_member
        role = self.selected_role
        bot = interaction.client
        if not isinstance(bot, ModerationBot):
            await interaction.response.send_message("Interner Bot-Fehler.", ephemeral=True)
            return

        try:
            _ensure_can_manage_role(interaction, member, role)
            before_role = member.top_role
            action_name = "Uprank" if promote else "Downrank"
            if promote:
                if role in member.roles:
                    raise ModerationError(f"{member.mention} hat die Rolle {role.mention} bereits.")
                if role <= member.top_role:
                    raise ModerationError(
                        "Für eine Beförderung muss die Zielrolle über der höchsten aktuellen Rolle liegen."
                    )
                await member.add_roles(role, reason=f"{interaction.user}: {self.reason}")
                after_role = role
            else:
                if role not in member.roles:
                    raise ModerationError(f"{member.mention} hat die Rolle {role.mention} nicht.")
                remaining_roles = [candidate for candidate in member.roles if candidate != role]
                after_role = max(remaining_roles, key=lambda candidate: candidate.position)
                await member.remove_roles(role, reason=f"{interaction.user}: {self.reason}")
        except (ModerationError, discord.Forbidden) as error:
            message = (
                str(error)
                if isinstance(error, ModerationError)
                else "Discord hat die Rollenänderung abgelehnt. Prüfe die Bot-Rolle."
            )
            await interaction.response.send_message(message, ephemeral=True)
            return

        embed = discord.Embed(
            title=f"{'⬆️ Uprank' if promote else '⬇️ Downrank'}",
            colour=discord.Colour.green() if promote else discord.Colour.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="👤 User", value=member.mention, inline=False)
        embed.add_field(name="🧑‍✈️ Moderator", value=interaction.user.mention, inline=False)
        embed.add_field(name="📈 Von Rolle", value=before_role.mention, inline=False)
        embed.add_field(name="📉 Auf Rolle", value=after_role.mention, inline=False)
        embed.add_field(name="📝 Grund", value=self.reason, inline=False)
        embed.set_footer(
            text=f"{interaction.guild.name} • {action_name} | heute um {discord.utils.utcnow().strftime('%H:%M')} Uhr"
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        await interaction.response.defer(ephemeral=True)
        if interaction.channel:
            try:
                await interaction.channel.send(embed=embed)
            except discord.Forbidden:
                await interaction.followup.send(
                    "Die Rolle wurde geändert, aber ich darf die öffentliche Meldung in diesem Kanal nicht senden.",
                    ephemeral=True,
                )
                return
        await bot.send_log(
            interaction,
            action=action_name,
            target=member,
            reason=self.reason,
            details=f"Von: {before_role.name} → Auf: {after_role.name}",
        )
        await interaction.followup.send(f"{action_name} erfolgreich ausgeführt.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        if interaction.message:
            await interaction.message.edit(view=self)
        self.stop()


MODERATION_ACTIONS = {
    "kick": "Kick",
    "ban": "Ban",
    "timeout": "Timeout",
    "untimeout": "Untimeout",
    "clear": "Nachrichten löschen",
    "warn": "Warnung",
    "warnings": "Verwarnungen anzeigen",
    "clearwarnings": "Verwarnungen löschen",
    "uprank": "Uprank",
    "derank": "Downrank",
}


RP_START_ANNOUNCEMENT = (
    "🚨 **RP-START | IMPERIA RP VC** 🚨\n\n"
    "🎉 Imperia RP VC – Server 1 ist offiziell eröffnet! 🎉\n\n"
    "Ab sofort könnt ihr auf Server 1 euer eigenes Roleplay-Abenteuer starten! 🏙️🚔\n\n"
    "👮 Polizei & Rettungsdienst\n"
    "🚗 Autos & Fahrzeuge\n"
    "🏢 Berufe & Unternehmen\n"
    "🎭 Realistisches Roleplay\n"
    "🤝 Events & Community\n\n"
    "Egal, ob du als Bürger, Polizist, Sanitäter, Unternehmer oder in einer "
    "anderen Rolle durchstarten möchtest – bei Imperia RP VC ist für jeden etwas dabei!\n\n"
    "🔥 Der RP-Start ist jetzt!\n"
    "Kommt auf den Server, schnappt euch eure Rolle und schreibt eure eigene Geschichte!\n\n"
    "🔗 Discord: https://discord.gg/8xXHAh3Cr5\n\n"
    "👑 Imperia RP VC – Deine Stadt. Deine Rolle. Deine Geschichte."
)

RP_END_ANNOUNCEMENT = (
    "📢 **RP ENDE – IMPERIA RP VC**\n\n"
    "🔴 Das RP auf **Imperia RP VC – Server 1** ist hiermit **offiziell beendet**.\n\n"
    "Vielen Dank an alle, die heute dabei waren und für ein schönes RP gesorgt haben! ❤️\n\n"
    "Wir sehen uns beim nächsten RP wieder! 🚔🔥\n\n"
    "**Imperia RP VC | Server 1**\n"
    "🌟 Danke fürs Mitmachen! 🌟"
)

RP_CITIZEN_ROLE_NAME = "Imperia Rp Bürger"
TEAM_ROLE_NAMES = ("test supporter", "test-supoorter")
TEAMKICK_MINIMUM_ROLE_NAME = "Teamleitung"


async def _teamkick_access_check(interaction: discord.Interaction) -> tuple[bool, bool]:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False, False

    member = interaction.user
    roles = interaction.guild.roles
    try:
        roles = await interaction.guild.fetch_roles()
        member = await interaction.guild.fetch_member(interaction.user.id)
    except discord.HTTPException:
        LOGGER.warning("Could not refresh roles before checking teamkick access.")

    if interaction.guild.owner_id == member.id:
        return True, True

    minimum_role = discord.utils.find(
        lambda role: _normalize_role_name(role.name)
        == _normalize_role_name(TEAMKICK_MINIMUM_ROLE_NAME),
        roles,
    )
    if not minimum_role:
        LOGGER.warning(
            "Teamkick role %r was not found in guild %s.",
            TEAMKICK_MINIMUM_ROLE_NAME,
            interaction.guild.id,
        )
        return False, False

    member_role_ids = {role.id for role in member.roles}
    allowed = any(
        role.id in member_role_ids and role.position >= minimum_role.position
        for role in roles
    )
    if not allowed:
        LOGGER.warning(
            "Teamkick denied for member %s in guild %s; member roles: %s.",
            member.id,
            interaction.guild.id,
            ", ".join(role.name for role in member.roles),
        )
    return allowed, True


def _has_teamkick_access(
    command: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    async def predicate(interaction: discord.Interaction) -> bool:
        allowed, _ = await _teamkick_access_check(interaction)
        return allowed

    return app_commands.check(predicate)(command)


class UmfrageView(discord.ui.View):
    def __init__(self, title: str, options: list[str]) -> None:
        super().__init__(timeout=86400)
        self.title = title
        self.options = options
        self.votes: dict[int, int] = {}

        for index, option in enumerate(options):
            button = discord.ui.Button(
                label=f"{index + 1}️⃣ {option[:70]}",
                style=discord.ButtonStyle.primary,
                row=0,
            )
            button.callback = self._make_vote_callback(index)
            self.add_item(button)

    def build_embed(self) -> discord.Embed:
        counts = [0] * len(self.options)
        for option_index in self.votes.values():
            counts[option_index] += 1
        total_votes = len(self.votes)
        choices = [
            f"**{index + 1}. {option}** — `{counts[index]}` Stimme(n)"
            for index, option in enumerate(self.options)
        ]
        embed = discord.Embed(
            title=f"📊 {self.title}",
            description="\n".join(choices),
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text=f"{total_votes} Stimme(n) insgesamt • Klicke auf eine Auswahl zum Abstimmen"
        )
        return embed

    def _make_vote_callback(self, option_index: int):
        async def vote(interaction: discord.Interaction) -> None:
            self.votes[interaction.user.id] = option_index
            await interaction.response.edit_message(embed=self.build_embed(), view=self)

        return vote


class UmfrageModal(discord.ui.Modal, title="Umfrage erstellen"):
    title_input = discord.ui.TextInput(
        label="Titel der Umfrage",
        placeholder="Zum Beispiel: Wann soll das nächste RP starten?",
        required=True,
        max_length=200,
    )
    option_one_input = discord.ui.TextInput(
        label="Auswahl 1",
        placeholder="Erste Antwortmöglichkeit",
        required=True,
        max_length=100,
    )
    option_two_input = discord.ui.TextInput(
        label="Auswahl 2",
        placeholder="Zweite Antwortmöglichkeit",
        required=True,
        max_length=100,
    )
    option_three_input = discord.ui.TextInput(
        label="Auswahl 3 (optional)",
        placeholder="Kann leer bleiben",
        required=False,
        max_length=100,
    )
    option_four_input = discord.ui.TextInput(
        label="Auswahl 4 (optional)",
        placeholder="Kann leer bleiben",
        required=False,
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_input.value).strip()
        options = [
            str(input_field.value).strip()
            for input_field in (
                self.option_one_input,
                self.option_two_input,
                self.option_three_input,
                self.option_four_input,
            )
            if str(input_field.value).strip()
        ]
        if not interaction.channel:
            await interaction.response.send_message(
                "In diesem Kanal kann keine Umfrage gesendet werden.",
                ephemeral=True,
            )
            return

        view = UmfrageView(title, options)
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(
                embed=view.build_embed(),
                view=view,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Die Umfrage konnte nicht öffentlich gesendet werden. "
                "Prüfe die Bot-Berechtigungen „Nachrichten senden“ und „Embed Links“.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            "Die Umfrage wurde öffentlich gesendet.",
            ephemeral=True,
        )


class TeamkickMenuView(discord.ui.View):
    def __init__(self, creator_id: int) -> None:
        super().__init__(timeout=300)
        self.creator_id = creator_id
        self.selected_member: discord.Member | None = None

        self.member_select = discord.ui.UserSelect(
            placeholder="Mitglied für Teamkick auswählen",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.member_select.callback = self.on_member_select
        self.add_item(self.member_select)

        execute_button = discord.ui.Button(
            label="Teamkick ausführen",
            style=discord.ButtonStyle.danger,
            row=1,
        )
        execute_button.callback = self.on_execute
        self.add_item(execute_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Dieses Teamkick-Menü wurde von einer anderen Person geöffnet.",
                ephemeral=True,
            )
            return False
        return True

    async def on_member_select(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not self.member_select.values:
            await interaction.response.send_message(
                "Kein gültiges Mitglied ausgewählt.",
                ephemeral=True,
            )
            return
        selected = self.member_select.values[0]
        if isinstance(selected, discord.Member):
            self.selected_member = selected
        else:
            try:
                self.selected_member = await interaction.guild.fetch_member(selected.id)
            except discord.NotFound:
                self.selected_member = None
        await interaction.response.send_message("Mitglied ausgewählt.", ephemeral=True)

    async def on_execute(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not self.selected_member:
            await interaction.response.send_message(
                "Bitte zuerst ein Mitglied auswählen.",
                ephemeral=True,
            )
            return
        has_access, minimum_role_found = await _teamkick_access_check(interaction)
        if not minimum_role_found:
            await interaction.response.send_message(
                f"Die Mindestrolle **{TEAMKICK_MINIMUM_ROLE_NAME}** wurde auf diesem Server nicht gefunden.",
                ephemeral=True,
            )
            return
        if not has_access:
            await interaction.response.send_message(
                f"Nur der Serverinhaber sowie **{TEAMKICK_MINIMUM_ROLE_NAME}** "
                "oder höher eingestufte Rollen dürfen diesen Befehl verwenden.",
                ephemeral=True,
            )
            return

        team_role = discord.utils.find(
            lambda role: role.name.casefold() in {name.casefold() for name in TEAM_ROLE_NAMES},
            interaction.guild.roles,
        )
        citizen_role = discord.utils.find(
            lambda role: role.name.casefold() == RP_CITIZEN_ROLE_NAME.casefold(),
            interaction.guild.roles,
        )
        if not team_role:
            await interaction.response.send_message(
                "Die Teamrolle „test supporter“ wurde auf diesem Server nicht gefunden.",
                ephemeral=True,
            )
            return
        if not citizen_role:
            await interaction.response.send_message(
                f"Die Rolle **{RP_CITIZEN_ROLE_NAME}** wurde auf diesem Server nicht gefunden.",
                ephemeral=True,
            )
            return

        try:
            _ensure_can_moderate(interaction, self.selected_member)
            _ensure_can_manage_role(interaction, self.selected_member, citizen_role)
        except ModerationError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return

        removable_roles = [
            role
            for role in self.selected_member.roles
            if not role.is_default()
            and role != citizen_role
            and role.position >= team_role.position
        ]
        if not removable_roles:
            await interaction.response.send_message(
                f"{self.selected_member.mention} hat keine Rolle ab „test supporter“.",
                ephemeral=True,
            )
            return

        bot = interaction.client
        await interaction.response.defer()
        try:
            await self.selected_member.add_roles(
                citizen_role,
                reason=f"{interaction.user}: Teamkick",
            )
            await self.selected_member.remove_roles(
                *removable_roles,
                reason=f"{interaction.user}: Teamkick",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Der Teamkick wurde von Discord abgelehnt. Prüfe die Rollen-Hierarchie "
                "und die Bot-Berechtigung „Rollen verwalten“.",
                ephemeral=True,
            )
            return

        removed_names = ", ".join(role.name for role in removable_roles)
        embed = discord.Embed(
            title="🚪 Teamkick",
            colour=discord.Colour.red(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="👤 User", value=self.selected_member.mention, inline=False)
        embed.add_field(name="🧑‍✈️ Moderator", value=interaction.user.mention, inline=False)
        embed.add_field(name="🗑️ Entfernte Rollen", value=removed_names, inline=False)
        embed.add_field(name="✅ Neue Rolle", value=citizen_role.mention, inline=False)
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(
            text=f"{interaction.guild.name} • Teamkick | heute um "
            f"{discord.utils.utcnow().strftime('%H:%M')} Uhr"
        )
        await interaction.followup.send(embed=embed)
        if isinstance(bot, ModerationBot):
            await bot.send_log(
                interaction,
                action="Teamkick",
                target=self.selected_member,
                details=f"Entfernt: {removed_names} → Vergeben: {citizen_role.name}",
            )
        for item in self.children:
            item.disabled = True
        if interaction.message:
            await interaction.message.edit(view=self)
        self.stop()


class AnkuendigungModal(discord.ui.Modal, title="Ankündigung erstellen"):
    title_input = discord.ui.TextInput(
        label="Titel",
        placeholder="Titel der Ankündigung",
        required=True,
        max_length=200,
    )
    content_input = discord.ui.TextInput(
        label="Was soll geschrieben werden?",
        placeholder="Text der Ankündigung",
        required=True,
        max_length=4000,
        style=discord.TextStyle.paragraph,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        title = str(self.title_input.value).strip()
        content = str(self.content_input.value).strip()
        if not interaction.guild:
            await interaction.response.send_message(
                "Dieser Befehl kann nur auf einem Server verwendet werden.",
                ephemeral=True,
            )
            return

        citizen_role = discord.utils.find(
            lambda role: role.name.casefold() == RP_CITIZEN_ROLE_NAME.casefold(),
            interaction.guild.roles,
        )
        if not citizen_role:
            await interaction.response.send_message(
                f"Die Rolle **{RP_CITIZEN_ROLE_NAME}** wurde auf diesem Server nicht gefunden.",
                ephemeral=True,
            )
            return
        if not interaction.channel:
            await interaction.response.send_message(
                "In diesem Kanal kann keine Ankündigung gesendet werden.",
                ephemeral=True,
            )
            return

        announcement = f"📢 **{title}**\n\n{content}\n\n{citizen_role.mention}"
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(
                announcement,
                allowed_mentions=discord.AllowedMentions(roles=[citizen_role]),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Die Ankündigung konnte nicht öffentlich gesendet werden. "
                "Prüfe die Bot-Berechtigungen „Nachrichten senden“ und „Erwähnungen verwenden“.",
                ephemeral=True,
            )
            return

        bot = interaction.client
        if isinstance(bot, ModerationBot):
            await bot.send_log(
                interaction,
                action="Ankündigung",
                reason=title,
                details=f"Erwähnte Rolle: {citizen_role.name}",
            )
        await interaction.followup.send(
            "Die Ankündigung wurde öffentlich gesendet.",
            ephemeral=True,
        )


async def _send_rp_announcement(
    interaction: discord.Interaction,
    announcement: str,
) -> None:
    if not interaction.guild:
        await interaction.response.send_message(
            "Dieser Befehl kann nur auf einem Server verwendet werden.",
            ephemeral=True,
        )
        return

    citizen_role = discord.utils.find(
        lambda role: role.name.casefold() == RP_CITIZEN_ROLE_NAME.casefold(),
        interaction.guild.roles,
    )
    if not citizen_role:
        await interaction.response.send_message(
            f"Die Rolle **{RP_CITIZEN_ROLE_NAME}** wurde auf diesem Server nicht gefunden.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"{announcement}\n\n{citizen_role.mention}",
        allowed_mentions=discord.AllowedMentions(roles=True),
    )


def _build_public_action_embed(
    interaction: discord.Interaction,
    *,
    title: str,
    colour: discord.Colour,
    reason: str,
    member: discord.Member | None = None,
    details: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(title=title, colour=colour, timestamp=discord.utils.utcnow())
    if member:
        embed.add_field(name="👤 User", value=member.mention, inline=False)
    embed.add_field(name="🧑‍✈️ Moderator", value=interaction.user.mention, inline=False)
    if details:
        embed.add_field(name="ℹ️ Details", value=details, inline=False)
    embed.add_field(name="📝 Grund", value=reason, inline=False)
    if interaction.guild:
        embed.set_footer(
            text=f"{interaction.guild.name} • {title} | heute um "
            f"{discord.utils.utcnow().strftime('%H:%M')} Uhr"
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
    return embed


class AbmeldungSettingsModal(discord.ui.Modal, title="Abmeldung eintragen"):
    def __init__(self, menu: "AbmeldungMenuView") -> None:
        super().__init__()
        self.menu = menu
        self.from_input = discord.ui.TextInput(
            label="Ab wann?",
            placeholder="Zum Beispiel 30.08.2026, 08:00 Uhr",
            required=True,
            max_length=100,
        )
        self.until_input = discord.ui.TextInput(
            label="Bis wann?",
            placeholder="Zum Beispiel 02.09.2026, 18:00 Uhr",
            required=True,
            max_length=100,
        )
        self.reason_input = discord.ui.TextInput(
            label="Grund",
            placeholder="Grund der Abmeldung",
            required=True,
            max_length=500,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.from_input)
        self.add_item(self.until_input)
        self.add_item(self.reason_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.menu.starts_at = str(self.from_input.value).strip()
        self.menu.ends_at = str(self.until_input.value).strip()
        self.menu.reason = str(self.reason_input.value).strip()
        await interaction.response.send_message(
            "Die Angaben wurden gespeichert. Klicke jetzt auf **Abmeldung senden**.",
            ephemeral=True,
        )


class AbmeldungMenuView(discord.ui.View):
    def __init__(self, creator_id: int) -> None:
        super().__init__(timeout=300)
        self.creator_id = creator_id
        self.selected_member: discord.Member | None = None
        self.starts_at = ""
        self.ends_at = ""
        self.reason = ""

        self.member_select = discord.ui.UserSelect(
            placeholder="Wer ist abgemeldet?",
            min_values=1,
            max_values=1,
            row=0,
        )
        self.member_select.callback = self.on_member_select
        self.add_item(self.member_select)

        settings_button = discord.ui.Button(
            label="Zeitraum / Grund",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        settings_button.callback = self.on_settings
        self.add_item(settings_button)

        send_button = discord.ui.Button(
            label="Abmeldung senden",
            style=discord.ButtonStyle.success,
            row=1,
        )
        send_button.callback = self.on_send
        self.add_item(send_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.creator_id:
            await interaction.response.send_message(
                "Dieses Abmeldungsmenü wurde von einer anderen Person geöffnet.",
                ephemeral=True,
            )
            return False
        return True

    async def on_member_select(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not self.member_select.values:
            await interaction.response.send_message(
                "Kein gültiges Mitglied ausgewählt.",
                ephemeral=True,
            )
            return
        selected = self.member_select.values[0]
        if isinstance(selected, discord.Member):
            self.selected_member = selected
        else:
            try:
                self.selected_member = await interaction.guild.fetch_member(selected.id)
            except discord.NotFound:
                self.selected_member = None
        await interaction.response.send_message("Person ausgewählt.", ephemeral=True)

    async def on_settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AbmeldungSettingsModal(self))

    async def on_send(self, interaction: discord.Interaction) -> None:
        if not self.selected_member:
            await interaction.response.send_message(
                "Bitte zuerst auswählen, wer abgemeldet ist.",
                ephemeral=True,
            )
            return
        if not self.starts_at or not self.ends_at or not self.reason:
            await interaction.response.send_message(
                "Bitte zuerst Ab wann, Bis wann und Grund ausfüllen.",
                ephemeral=True,
            )
            return
        if not interaction.channel:
            await interaction.response.send_message(
                "In diesem Kanal kann keine Abmeldung gesendet werden.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="📅 Abmeldung",
            colour=discord.Colour.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="👤 Wer", value=self.selected_member.mention, inline=False)
        embed.add_field(name="🕐 Ab wann", value=self.starts_at, inline=True)
        embed.add_field(name="🕐 Bis wann", value=self.ends_at, inline=True)
        embed.add_field(name="📝 Grund", value=self.reason, inline=False)
        embed.add_field(name="🧑‍✈️ Eingetragen von", value=interaction.user.mention, inline=False)
        if interaction.guild:
            embed.set_footer(
                text=f"{interaction.guild.name} • Abmeldung | heute um "
                f"{discord.utils.utcnow().strftime('%H:%M')} Uhr"
            )
            if interaction.guild.icon:
                embed.set_thumbnail(url=interaction.guild.icon.url)

        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                "Die Abmeldung konnte nicht öffentlich gesendet werden. "
                "Prüfe die Bot-Berechtigung „Embed Links“.",
                ephemeral=True,
            )
            return

        bot = interaction.client
        if isinstance(bot, ModerationBot):
            await bot.send_log(
                interaction,
                action="Abmeldung",
                target=self.selected_member,
                reason=self.reason,
                details=f"Von: {self.starts_at} • Bis: {self.ends_at}",
            )
        await interaction.followup.send("Die Abmeldung wurde öffentlich gesendet.", ephemeral=True)
        for item in self.children:
            item.disabled = True
        if interaction.message:
            await interaction.message.edit(view=self)
        self.stop()


class PartnerschaftSettingsModal(discord.ui.Modal, title="Partnerschaft erstellen"):
    def __init__(self) -> None:
        super().__init__()
        self.server_name_input = discord.ui.TextInput(
            label="Name des Servers",
            placeholder="Zum Beispiel Mein Gaming Server",
            required=True,
            max_length=100,
        )
        self.server_link_input = discord.ui.TextInput(
            label="Discord-Server-Link",
            placeholder="https://discord.gg/dein-link",
            required=True,
            max_length=200,
        )
        self.add_item(self.server_name_input)
        self.add_item(self.server_link_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        server_name = str(self.server_name_input.value).strip()
        server_link = str(self.server_link_input.value).strip()
        parsed_link = urlparse(server_link)
        if parsed_link.scheme not in {"http", "https"} or not parsed_link.netloc:
            await interaction.response.send_message(
                "Bitte gib einen gültigen Discord-Link mit `https://` ein.",
                ephemeral=True,
            )
            return
        await _send_partnership_announcement(interaction, server_name, server_link)


async def _send_partnership_announcement(
    interaction: discord.Interaction,
    server_name: str,
    server_link: str,
) -> None:
    if not interaction.channel:
        await interaction.response.send_message(
            "In diesem Kanal kann keine Partnerschaft gesendet werden.",
            ephemeral=True,
        )
        return

    announcement = (
        "📢 **Offizielle Partnerschaft**\n\n"
        f"Mit großer Freude begrüßen wir **{server_name}** als unseren "
        "neuen offiziellen Partner! 🤝\n"
        "Wir bedanken uns für das Vertrauen und freuen uns auf eine "
        "langfristige, erfolgreiche Zusammenarbeit. Schaut gerne auf "
        "ihrem Server vorbei und lasst ihnen etwas Support da! 💙 "
        f"👉 {server_link} 👈"
    )

    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.send(
            announcement,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Die Partnerschaft konnte nicht öffentlich gesendet werden. "
            "Prüfe die Bot-Berechtigung „Nachrichten senden“.",
            ephemeral=True,
        )
        return

    bot = interaction.client
    if isinstance(bot, ModerationBot):
        await bot.send_log(
            interaction,
            action="Partnerschaft",
            reason=f"{server_name} — {server_link}",
        )
    await interaction.followup.send(
        "Die Partnerschaftsankündigung wurde öffentlich gesendet.",
        ephemeral=True,
    )


class ActionSettingsModal(discord.ui.Modal):
    def __init__(self, menu: "ModerationMenuView") -> None:
        super().__init__(title=f"{MODERATION_ACTIONS[menu.action]} konfigurieren")
        self.menu = menu
        self.reason_input = discord.ui.TextInput(
            label="Grund",
            placeholder="Optionaler Grund für die Aktion",
            required=False,
            max_length=500,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.reason_input)

        self.number_input: discord.ui.TextInput | None = None
        if menu.action == "timeout":
            self.number_input = discord.ui.TextInput(
                label="Dauer in Minuten",
                placeholder="Zum Beispiel 60",
                required=True,
                max_length=5,
            )
            self.add_item(self.number_input)
        elif menu.action == "ban":
            self.number_input = discord.ui.TextInput(
                label="Nachrichten löschen (Tage)",
                placeholder="0 bis 7",
                required=True,
                default="0",
                max_length=1,
            )
            self.add_item(self.number_input)
        elif menu.action == "clear":
            self.number_input = discord.ui.TextInput(
                label="Anzahl Nachrichten",
                placeholder="1 bis 100",
                required=True,
                default="10",
                max_length=3,
            )
            self.add_item(self.number_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self.menu.reason = str(self.reason_input.value).strip() or "Kein Grund angegeben"
        try:
            if self.menu.action == "timeout":
                minutes = int(str(self.number_input.value)) if self.number_input else 0
                if not 1 <= minutes <= 40320:
                    raise ValueError
                self.menu.minutes = minutes
            elif self.menu.action == "ban":
                days = int(str(self.number_input.value)) if self.number_input else 0
                if not 0 <= days <= 7:
                    raise ValueError
                self.menu.delete_message_days = days
            elif self.menu.action == "clear":
                amount = int(str(self.number_input.value)) if self.number_input else 0
                if not 1 <= amount <= 100:
                    raise ValueError
                self.menu.amount = amount
        except ValueError:
            await interaction.response.send_message(
                "Ungültiger Wert. Prüfe den erlaubten Zahlenbereich und versuche es erneut.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message("Die Einstellungen wurden gespeichert.", ephemeral=True)


class ModerationMenuView(discord.ui.View):
    def __init__(self, moderator_id: int, initial_action: str = "kick") -> None:
        super().__init__(timeout=300)
        self.moderator_id = moderator_id
        self.action = initial_action if initial_action in MODERATION_ACTIONS else "kick"
        self.selected_member: discord.Member | None = None
        self.selected_role: discord.Role | None = None
        self.reason = "Kein Grund angegeben"
        self.minutes = 60
        self.delete_message_days = 0
        self.amount = 10

        action_options = [
            discord.SelectOption(
                label=label,
                value=value,
                default=value == self.action,
            )
            for value, label in MODERATION_ACTIONS.items()
        ]
        self.action_select = discord.ui.Select(
            placeholder="Moderationsaktion auswählen",
            options=action_options,
            row=0,
        )
        self.action_select.callback = self.on_action_select
        self.add_item(self.action_select)

        self.member_select = discord.ui.UserSelect(
            placeholder="Mitglied auswählen",
            min_values=1,
            max_values=1,
            row=1,
        )
        self.member_select.callback = self.on_member_select
        self.add_item(self.member_select)

        self.role_select = discord.ui.RoleSelect(
            placeholder="Rolle auswählen (nur für Uprank/Downrank)",
            min_values=1,
            max_values=1,
            row=2,
        )
        self.role_select.callback = self.on_role_select
        self.add_item(self.role_select)

        settings_button = discord.ui.Button(
            label="Optionen / Grund",
            style=discord.ButtonStyle.secondary,
            row=3,
        )
        settings_button.callback = self.on_settings
        self.add_item(settings_button)

        execute_button = discord.ui.Button(
            label="Aktion ausführen",
            style=discord.ButtonStyle.success,
            row=3,
        )
        execute_button.callback = self.on_execute
        self.add_item(execute_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.moderator_id:
            await interaction.response.send_message(
                "Dieses Moderationsmenü wurde von einer anderen Person geöffnet.",
                ephemeral=True,
            )
            return False
        return True

    async def on_action_select(self, interaction: discord.Interaction) -> None:
        if self.action_select.values:
            self.action = self.action_select.values[0]
        await interaction.response.send_message(
            f"Aktion ausgewählt: **{MODERATION_ACTIONS[self.action]}**.",
            ephemeral=True,
        )

    async def on_member_select(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or not self.member_select.values:
            await interaction.response.send_message("Kein gültiges Mitglied ausgewählt.", ephemeral=True)
            return
        selected = self.member_select.values[0]
        if isinstance(selected, discord.Member):
            self.selected_member = selected
        else:
            try:
                self.selected_member = await interaction.guild.fetch_member(selected.id)
            except discord.NotFound:
                self.selected_member = None
        await interaction.response.send_message("Mitglied ausgewählt.", ephemeral=True)

    async def on_role_select(self, interaction: discord.Interaction) -> None:
        self.selected_role = self.role_select.values[0] if self.role_select.values else None
        await interaction.response.send_message("Rolle ausgewählt.", ephemeral=True)

    async def on_settings(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ActionSettingsModal(self))

    async def on_execute(self, interaction: discord.Interaction) -> None:
        await self.execute_action(interaction)

    def _has_required_permission(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            return False
        required_permissions = {
            "kick": "kick_members",
            "ban": "ban_members",
            "timeout": "moderate_members",
            "untimeout": "moderate_members",
            "clear": "manage_messages",
            "warn": "moderate_members",
            "warnings": "moderate_members",
            "clearwarnings": "moderate_members",
            "uprank": "manage_roles",
            "derank": "manage_roles",
        }
        return getattr(interaction.user.guild_permissions, required_permissions[self.action], False)

    async def execute_action(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message(
                "Dieses Menü funktioniert nur auf einem Server.",
                ephemeral=True,
            )
            return
        if not self._has_required_permission(interaction):
            await interaction.response.send_message(
                "Dir fehlt die Discord-Berechtigung für diese Aktion.",
                ephemeral=True,
            )
            return

        member_actions = {
            "kick",
            "ban",
            "timeout",
            "untimeout",
            "warn",
            "warnings",
            "clearwarnings",
            "uprank",
            "derank",
        }
        role_actions = {"uprank", "derank"}
        if self.action in member_actions and not self.selected_member:
            await interaction.response.send_message(
                "Bitte zuerst ein Mitglied auswählen.",
                ephemeral=True,
            )
            return
        if self.action in role_actions and not self.selected_role:
            await interaction.response.send_message(
                "Bitte zusätzlich eine Rolle auswählen.",
                ephemeral=True,
            )
            return
        if self.action in role_actions and self.selected_member and self.selected_role:
            try:
                _ensure_can_manage_role(interaction, self.selected_member, self.selected_role)
            except ModerationError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return
        elif self.selected_member and self.action in member_actions:
            try:
                _ensure_can_moderate(interaction, self.selected_member)
            except ModerationError as error:
                await interaction.response.send_message(str(error), ephemeral=True)
                return

        bot = interaction.client
        if not isinstance(bot, ModerationBot):
            await interaction.response.send_message("Interner Bot-Fehler.", ephemeral=True)
            return

        await interaction.response.defer()
        member = self.selected_member
        role = self.selected_role
        try:
            if self.action == "kick" and member:
                await member.kick(reason=f"{interaction.user}: {self.reason}")
                await interaction.followup.send(
                    embed=_build_public_action_embed(
                        interaction,
                        title="👢 Kick",
                        colour=discord.Colour.red(),
                        member=member,
                        reason=self.reason,
                        details="Mitglied wurde vom Server entfernt",
                    )
                )
                await bot.send_log(interaction, action="Kick", target=member, reason=self.reason)
            elif self.action == "ban" and member:
                await member.ban(
                    reason=f"{interaction.user}: {self.reason}",
                    delete_message_seconds=self.delete_message_days * 24 * 60 * 60,
                )
                await interaction.followup.send(
                    embed=_build_public_action_embed(
                        interaction,
                        title="🔨 Ban",
                        colour=discord.Colour.red(),
                        member=member,
                        reason=self.reason,
                        details=f"Nachrichten gelöscht: {self.delete_message_days} Tag(e)",
                    )
                )
                await bot.send_log(
                    interaction,
                    action="Ban",
                    target=member,
                    reason=self.reason,
                    details=f"Nachrichten gelöscht: {self.delete_message_days} Tag(e)",
                )
            elif self.action == "timeout" and member:
                until = discord.utils.utcnow() + timedelta(minutes=self.minutes)
                await member.timeout(until, reason=f"{interaction.user}: {self.reason}")
                await interaction.followup.send(
                    embed=_build_public_action_embed(
                        interaction,
                        title="🔇 Timeout",
                        colour=discord.Colour.orange(),
                        member=member,
                        reason=self.reason,
                        details=f"Dauer: {self.minutes} Minute(n)",
                    )
                )
                await bot.send_log(
                    interaction,
                    action="Timeout",
                    target=member,
                    reason=self.reason,
                    details=f"Dauer: {self.minutes} Minute(n)",
                )
            elif self.action == "untimeout" and member:
                await member.timeout(None, reason=f"{interaction.user}: {self.reason}")
                await interaction.followup.send(
                    embed=_build_public_action_embed(
                        interaction,
                        title="🔊 Untimeout",
                        colour=discord.Colour.green(),
                        member=member,
                        reason=self.reason,
                        details="Die Auszeit wurde beendet",
                    )
                )
                await bot.send_log(interaction, action="Untimeout", target=member, reason=self.reason)
            elif self.action == "clear":
                channel = interaction.channel
                if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                    raise ModerationError("In diesem Kanaltyp können keine Nachrichten gelöscht werden.")
                deleted = await channel.purge(limit=self.amount)
                await interaction.followup.send(
                    embed=_build_public_action_embed(
                        interaction,
                        title="🧹 Clear",
                        colour=discord.Colour.blurple(),
                        reason=self.reason,
                        details=f"Kanal: {channel.mention}; Anzahl: {len(deleted)}",
                    )
                )
                await bot.send_log(
                    interaction,
                    action="Clear",
                    reason="Nachrichtenbereinigung",
                    details=f"Kanal: {channel.mention}; Anzahl: {len(deleted)}",
                )
            elif self.action == "warn" and member:
                record = bot.store.add_warning(
                    guild_id=interaction.guild.id,
                    user_id=member.id,
                    moderator_id=interaction.user.id,
                    reason=self.reason,
                )
                await interaction.followup.send(
                    embed=_build_public_action_embed(
                        interaction,
                        title="⚠️ Warnung",
                        colour=discord.Colour.orange(),
                        member=member,
                        reason=self.reason,
                        details=f"Verwarnungsnummer: {record.id}",
                    )
                )
                try:
                    await member.send(
                        f"Du wurdest auf **{interaction.guild.name}** verwarnt.\nGrund: {self.reason}"
                    )
                except discord.Forbidden:
                    LOGGER.info("Could not DM warned member %s.", member.id)
                await bot.send_log(
                    interaction,
                    action="Warn",
                    target=member,
                    reason=self.reason,
                    details=f"Nummer: {record.id}",
                )
            elif self.action == "warnings" and member:
                records = bot.store.get_warnings(guild_id=interaction.guild.id, user_id=member.id)
                if not records:
                    await interaction.followup.send(
                        embed=_build_public_action_embed(
                            interaction,
                            title="📋 Verwarnungen",
                            colour=discord.Colour.orange(),
                            member=member,
                            reason="Keine gespeicherten Verwarnungen",
                        )
                    )
                else:
                    lines = [
                        f"`#{record.id}` <t:{int(discord.utils.parse_time(record.created_at).timestamp())}:d> "
                        f"von <@{record.moderator_id}> — {record.reason}"
                        for record in records[:10]
                    ]
                    embed = discord.Embed(
                        title=f"📋 Verwarnungen für {member.display_name}",
                        description="\n".join(lines),
                        colour=discord.Colour.orange(),
                    )
                    embed.add_field(name="🧑‍✈️ Moderator", value=interaction.user.mention, inline=False)
                    if interaction.guild.icon:
                        embed.set_thumbnail(url=interaction.guild.icon.url)
                    embed.set_footer(
                        text=f"{interaction.guild.name} • Verwarnungen | heute um "
                        f"{discord.utils.utcnow().strftime('%H:%M')} Uhr"
                    )
                    await interaction.followup.send(embed=embed)
            elif self.action == "clearwarnings" and member:
                deleted = bot.store.delete_warnings(guild_id=interaction.guild.id, user_id=member.id)
                await interaction.followup.send(
                    embed=_build_public_action_embed(
                        interaction,
                        title="🧹 Verwarnungen gelöscht",
                        colour=discord.Colour.blurple(),
                        member=member,
                        reason=self.reason,
                        details=f"Gelöschte Verwarnungen: {deleted}",
                    )
                )
                await bot.send_log(
                    interaction,
                    action="Clear Warnings",
                    target=member,
                    details=f"Gelöschte Verwarnungen: {deleted}",
                )
            elif self.action in role_actions and member and role:
                before_role = member.top_role
                if self.action == "uprank":
                    if role in member.roles:
                        raise ModerationError(f"{member.mention} hat die Rolle {role.mention} bereits.")
                    if role <= member.top_role:
                        raise ModerationError(
                            "Für eine Beförderung muss die Zielrolle über der höchsten aktuellen Rolle liegen."
                        )
                    await member.add_roles(role, reason=f"{interaction.user}: {self.reason}")
                    if not before_role.is_default():
                        await member.remove_roles(
                            before_role,
                            reason=f"{interaction.user}: Vorherige Rangrolle durch Uprank ersetzt",
                        )
                    after_role = role
                    title = "⬆️ Uprank"
                    colour = discord.Colour.green()
                else:
                    if role not in member.roles:
                        raise ModerationError(f"{member.mention} hat die Rolle {role.mention} nicht.")
                    remaining_roles = [candidate for candidate in member.roles if candidate != role]
                    after_role = max(remaining_roles, key=lambda candidate: candidate.position)
                    await member.remove_roles(role, reason=f"{interaction.user}: {self.reason}")
                    title = "⬇️ Downrank"
                    colour = discord.Colour.red()
                embed = discord.Embed(title=title, colour=colour, timestamp=discord.utils.utcnow())
                embed.add_field(name="👤 User", value=member.mention, inline=False)
                embed.add_field(name="🧑‍✈️ Moderator", value=interaction.user.mention, inline=False)
                embed.add_field(name="📈 Von Rolle", value=before_role.mention, inline=False)
                embed.add_field(name="📉 Auf Rolle", value=after_role.mention, inline=False)
                embed.add_field(name="📝 Grund", value=self.reason, inline=False)
                embed.set_footer(
                    text=f"{interaction.guild.name} • {self.action.title()} | heute um "
                    f"{discord.utils.utcnow().strftime('%H:%M')} Uhr"
                )
                if interaction.guild.icon:
                    embed.set_thumbnail(url=interaction.guild.icon.url)
                await interaction.followup.send(embed=embed)
                await bot.send_log(
                    interaction,
                    action=self.action.title(),
                    target=member,
                    reason=self.reason,
                    details=f"Von: {before_role.name} → Auf: {after_role.name}",
                )
        except (ModerationError, discord.Forbidden) as error:
            await interaction.followup.send(
                str(error)
                if isinstance(error, ModerationError)
                else "Discord hat die Aktion abgelehnt. Prüfe die Bot-Berechtigungen.",
                ephemeral=True,
            )
            return

        for item in self.children:
            item.disabled = True
        if interaction.message:
            await interaction.message.edit(view=self)
        self.stop()


async def open_moderation_menu(
    interaction: discord.Interaction,
    initial_action: str = "kick",
) -> None:
    embed = discord.Embed(
        title="Moderationsmenü",
        description=(
            "Wähle eine Aktion, ein Mitglied und bei Uprank/Downrank eine Rolle. "
            "Unter „Optionen / Grund“ kannst du Grund, Dauer oder Anzahl festlegen."
        ),
        colour=discord.Colour.blurple(),
    )
    await interaction.response.send_message(
        embed=embed,
        view=ModerationMenuView(interaction.user.id, initial_action=initial_action),
        ephemeral=True,
    )


@_guild_only()
@app_commands.command(name="ping", description="Prüft, ob der Bot erreichbar ist.")
async def ping(interaction: discord.Interaction) -> None:
    latency = round(interaction.client.latency * 1000)
    await interaction.response.send_message(f"Pong! Latenz: `{latency} ms`.", ephemeral=True)


@_guild_only()
@app_commands.command(name="serverinfo", description="Zeigt grundlegende Serverinformationen.")
async def serverinfo(interaction: discord.Interaction) -> None:
    guild = interaction.guild
    if not guild:
        raise ModerationError("Dieser Befehl funktioniert nur auf einem Server.")
    embed = discord.Embed(title=guild.name, colour=discord.Colour.blurple())
    embed.add_field(name="Mitglieder", value=str(guild.member_count or 0))
    embed.add_field(name="Kanäle", value=str(len(guild.channels)))
    embed.add_field(name="Server-ID", value=f"`{guild.id}`", inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@_guild_only()
@_has_permission("kick_members")
@app_commands.command(name="kick", description="Entfernt ein Mitglied vom Server.")
@app_commands.describe(member="Das zu entfernende Mitglied.", reason="Grund für den Kick.")
async def kick(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "Kein Grund angegeben",
) -> None:
    _ensure_can_moderate(interaction, member)
    await member.kick(reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"{member.mention} wurde vom Server entfernt.\nGrund: {reason}",
        ephemeral=False,
    )
    await interaction.client.send_log(interaction, action="Kick", target=member, reason=reason)  # type: ignore[attr-defined]


@_guild_only()
@_has_permission("ban_members")
@app_commands.command(name="ban", description="Bannt ein Mitglied vom Server.")
@app_commands.describe(
    member="Das zu bannende Mitglied.",
    reason="Grund für den Bann.",
    delete_message_days="Nachrichten der letzten 0 bis 7 Tage löschen.",
)
async def ban(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "Kein Grund angegeben",
    delete_message_days: app_commands.Range[int, 0, 7] = 0,
) -> None:
    _ensure_can_moderate(interaction, member)
    await member.ban(
        reason=f"{interaction.user}: {reason}",
        delete_message_seconds=int(delete_message_days) * 24 * 60 * 60,
    )
    await interaction.response.send_message(
        f"{member.mention} wurde gebannt.\nGrund: {reason}",
        ephemeral=False,
    )
    await interaction.client.send_log(  # type: ignore[attr-defined]
        interaction,
        action="Ban",
        target=member,
        reason=reason,
        details=f"Nachrichten gelöscht: {delete_message_days} Tag(e)",
    )


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="timeout", description="Gibt einem Mitglied eine Auszeit.")
@app_commands.describe(
    member="Das Mitglied für die Auszeit.",
    minutes="Dauer in Minuten (1 bis 40320, also maximal 28 Tage).",
    reason="Grund für die Auszeit.",
)
async def timeout(
    interaction: discord.Interaction,
    member: discord.Member,
    minutes: app_commands.Range[int, 1, 40320],
    reason: str = "Kein Grund angegeben",
) -> None:
    _ensure_can_moderate(interaction, member)
    until = discord.utils.utcnow() + timedelta(minutes=int(minutes))
    await member.timeout(until, reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"{member.mention} wurde für `{minutes}` Minute(n) stummgeschaltet.\nGrund: {reason}",
        ephemeral=False,
    )
    await interaction.client.send_log(  # type: ignore[attr-defined]
        interaction,
        action="Timeout",
        target=member,
        reason=reason,
        details=f"Dauer: {minutes} Minute(n)",
    )


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="untimeout", description="Beendet die Auszeit eines Mitglieds.")
@app_commands.describe(member="Das Mitglied, dessen Auszeit beendet werden soll.", reason="Grund.")
async def untimeout(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "Kein Grund angegeben",
) -> None:
    _ensure_can_moderate(interaction, member)
    await member.timeout(None, reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"Die Auszeit von {member.mention} wurde beendet.",
        ephemeral=False,
    )
    await interaction.client.send_log(interaction, action="Untimeout", target=member, reason=reason)  # type: ignore[attr-defined]


@_guild_only()
@_has_permission("manage_roles")
@app_commands.command(name="rankmenu", description="Öffnet das Menü für Uprank und Downrank.")
async def rankmenu(interaction: discord.Interaction) -> None:
    embed = discord.Embed(
        title="Rollenverwaltung",
        description=(
            "Wähle ein Mitglied und eine Rolle aus. "
            "Setze optional einen Grund und bestätige anschließend mit Befördern oder Downrank."
        ),
        colour=discord.Colour.blurple(),
    )
    await interaction.response.send_message(
        embed=embed,
        view=RankMenuView(interaction.user.id),
        ephemeral=True,
    )


@_guild_only()
@_has_permission("manage_roles")
@app_commands.command(name="uprank", description="Befördert ein Mitglied durch Vergabe einer Rolle.")
@app_commands.describe(
    member="Das zu befördernde Mitglied.",
    role="Die Rolle, die vergeben werden soll.",
    reason="Grund für die Beförderung.",
)
async def uprank(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role,
    reason: str = "Kein Grund angegeben",
) -> None:
    _ensure_can_manage_role(interaction, member, role)
    if role in member.roles:
        raise ModerationError(f"{member.mention} hat die Rolle {role.mention} bereits.")
    if role <= member.top_role:
        raise ModerationError(
            "Für eine Beförderung muss die Zielrolle über der höchsten aktuellen Rolle liegen."
        )
    await member.add_roles(role, reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"{member.mention} wurde mit {role.mention} befördert.\nGrund: {reason}",
        ephemeral=False,
    )
    await interaction.client.send_log(  # type: ignore[attr-defined]
        interaction,
        action="Uprank",
        target=member,
        reason=reason,
        details=f"Vergebene Rolle: {role.name}",
    )


@_guild_only()
@_has_permission("manage_roles")
@app_commands.command(name="derank", description="Stuft ein Mitglied durch Entfernen einer Rolle zurück.")
@app_commands.describe(
    member="Das zurückzustufende Mitglied.",
    role="Die Rolle, die entfernt werden soll.",
    reason="Grund für die Rückstufung.",
)
async def derank(
    interaction: discord.Interaction,
    member: discord.Member,
    role: discord.Role,
    reason: str = "Kein Grund angegeben",
) -> None:
    _ensure_can_manage_role(interaction, member, role)
    if role not in member.roles:
        raise ModerationError(f"{member.mention} hat die Rolle {role.mention} nicht.")
    await member.remove_roles(role, reason=f"{interaction.user}: {reason}")
    await interaction.response.send_message(
        f"{member.mention} wurde durch Entfernen von {role.mention} zurückgestuft.\nGrund: {reason}",
        ephemeral=False,
    )
    await interaction.client.send_log(  # type: ignore[attr-defined]
        interaction,
        action="Derank",
        target=member,
        reason=reason,
        details=f"Entfernte Rolle: {role.name}",
    )


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="clear", description="Löscht bis zu 100 Nachrichten im aktuellen Kanal.")
@app_commands.describe(amount="Anzahl der zu löschenden Nachrichten (1 bis 100).")
async def clear(
    interaction: discord.Interaction,
    amount: app_commands.Range[int, 1, 100],
) -> None:
    channel = interaction.channel
    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        raise ModerationError("In diesem Kanaltyp können keine Nachrichten gesammelt gelöscht werden.")
    await interaction.response.defer()
    deleted = await channel.purge(limit=int(amount))
    await interaction.followup.send(f"`{len(deleted)}` Nachricht(en) wurden gelöscht.")
    await interaction.client.send_log(  # type: ignore[attr-defined]
        interaction,
        action="Clear",
        reason="Nachrichtenbereinigung",
        details=f"Kanal: {channel.mention}; Anzahl: {len(deleted)}",
    )


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="warn", description="Vermerkt eine Verwarnung für ein Mitglied.")
@app_commands.describe(member="Das zu verwarnende Mitglied.", reason="Grund der Verwarnung.")
async def warn(
    interaction: discord.Interaction,
    member: discord.Member,
    reason: str = "Kein Grund angegeben",
) -> None:
    _ensure_can_moderate(interaction, member)
    if not interaction.guild:
        raise ModerationError("Dieser Befehl funktioniert nur auf einem Server.")
    bot = interaction.client
    if not isinstance(bot, ModerationBot):
        raise ModerationError("Interner Bot-Fehler.")
    record = bot.store.add_warning(
        guild_id=interaction.guild.id,
        user_id=member.id,
        moderator_id=interaction.user.id,
        reason=reason,
    )
    await interaction.response.send_message(
        f"{member.mention} wurde verwarnt. Verwarnungsnummer: `{record.id}`\nGrund: {reason}",
        ephemeral=False,
    )
    try:
        await member.send(f"Du wurdest auf **{interaction.guild.name}** verwarnt.\nGrund: {reason}")
    except discord.Forbidden:
        LOGGER.info("Could not DM warned member %s.", member.id)
    await bot.send_log(interaction, action="Warn", target=member, reason=reason, details=f"Nummer: {record.id}")


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="warnings", description="Zeigt die Verwarnungen eines Mitglieds.")
@app_commands.describe(member="Das Mitglied, dessen Verwarnungen angezeigt werden sollen.")
async def warnings(interaction: discord.Interaction, member: discord.Member) -> None:
    guild = interaction.guild
    bot = interaction.client
    if not guild or not isinstance(bot, ModerationBot):
        raise ModerationError("Dieser Befehl funktioniert nur auf einem Server.")
    records = bot.store.get_warnings(guild_id=guild.id, user_id=member.id)
    if not records:
        await interaction.response.send_message(
            f"{member.mention} hat keine gespeicherten Verwarnungen.",
            ephemeral=False,
        )
        return

    lines = [
        f"`#{record.id}` <t:{int(discord.utils.parse_time(record.created_at).timestamp())}:d> "
        f"von <@{record.moderator_id}> — {record.reason}"
        for record in records[:10]
    ]
    embed = discord.Embed(
        title=f"Verwarnungen für {member.display_name}",
        description="\n".join(lines),
        colour=discord.Colour.orange(),
    )
    if len(records) > 10:
        embed.set_footer(text=f"{len(records) - 10} weitere Verwarnung(en) nicht angezeigt")
    await interaction.response.send_message(embed=embed, ephemeral=False)


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="clearwarnings", description="Löscht alle Verwarnungen eines Mitglieds.")
@app_commands.describe(member="Das Mitglied, dessen Verwarnungen gelöscht werden sollen.")
async def clearwarnings(interaction: discord.Interaction, member: discord.Member) -> None:
    guild = interaction.guild
    bot = interaction.client
    if not guild or not isinstance(bot, ModerationBot):
        raise ModerationError("Dieser Befehl funktioniert nur auf einem Server.")
    deleted = bot.store.delete_warnings(guild_id=guild.id, user_id=member.id)
    await interaction.response.send_message(
        f"Für {member.mention} wurden `{deleted}` Verwarnung(en) gelöscht.",
        ephemeral=False,
    )
    await bot.send_log(
        interaction,
        action="Clear Warnings",
        target=member,
        details=f"Gelöschte Verwarnungen: {deleted}",
    )


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="modmenu", description="Öffnet das zentrale Moderationsmenü.")
async def modmenu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction)


@_guild_only()
@_has_permission("kick_members")
@app_commands.command(name="kick", description="Öffnet das Kick-Menü.")
async def kick_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "kick")


@_guild_only()
@_has_permission("ban_members")
@app_commands.command(name="ban", description="Öffnet das Ban-Menü.")
async def ban_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "ban")


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="timeout", description="Öffnet das Timeout-Menü.")
async def timeout_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "timeout")


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="untimeout", description="Öffnet das Untimeout-Menü.")
async def untimeout_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "untimeout")


@_guild_only()
@_has_permission("manage_roles")
@app_commands.command(name="rankmenu", description="Öffnet das Rollen-Menü.")
async def rankmenu_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "uprank")


@_guild_only()
@_has_permission("manage_roles")
@app_commands.command(name="uprank", description="Öffnet das Beförderungsmenü.")
async def uprank_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "uprank")


@_guild_only()
@_has_permission("manage_roles")
@app_commands.command(name="derank", description="Öffnet das Downrank-Menü.")
async def derank_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "derank")


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="clear", description="Öffnet das Nachrichten-Löschmenü.")
async def clear_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "clear")


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="warn", description="Öffnet das Verwarnungsmenü.")
async def warn_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "warn")


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="warnings", description="Öffnet die Verwarnungsübersicht.")
async def warnings_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "warnings")


@_guild_only()
@_has_permission("moderate_members")
@app_commands.command(name="clearwarnings", description="Öffnet das Menü zum Löschen von Verwarnungen.")
async def clearwarnings_menu_command(interaction: discord.Interaction) -> None:
    await open_moderation_menu(interaction, "clearwarnings")


@_guild_only()
@_has_role_at_or_above(TEST_SUPPORT_ROLE_NAME)
@app_commands.command(name="abmeldung", description="Öffnet das Menü für eine Abmeldung.")
async def abmeldung_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "📅 **Abmeldung erstellen**\n"
        "Wähle zuerst die Person aus. Danach kannst du Zeitraum und Grund eintragen.",
        view=AbmeldungMenuView(interaction.user.id),
        ephemeral=True,
    )


@_guild_only()
@_has_role_at_or_above(TEST_SUPPORT_ROLE_NAME)
@app_commands.command(name="abmelden", description="Öffnet das Menü zum Abmelden einer Person.")
async def abmelden_command(interaction: discord.Interaction) -> None:
    await abmeldung_command.callback(interaction)


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="partnerschaft", description="Öffnet das Menü für eine Partnerschaft.")
async def partnerschaft_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(PartnerschaftSettingsModal())


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="rp-start", description="Sendet die RP-Ansage für Imperia RP VC.")
async def rp_start_command(interaction: discord.Interaction) -> None:
    await _send_rp_announcement(interaction, RP_START_ANNOUNCEMENT)


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="rp-ende", description="Sendet die RP-Ende-Ansage für Imperia RP VC.")
async def rp_ende_command(interaction: discord.Interaction) -> None:
    await _send_rp_announcement(interaction, RP_END_ANNOUNCEMENT)


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="umfrage", description="Erstellt eine interaktive Umfrage.")
async def umfrage_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(UmfrageModal())


@_guild_only()
@_has_permission("manage_messages")
@app_commands.command(name="ankuendigung", description="Erstellt eine öffentliche Ankündigung.")
async def ankuendigung_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_modal(AnkuendigungModal())


@_guild_only()
@_has_teamkick_access
@app_commands.command(name="teamkick", description="Entfernt Teamrollen und gibt die Bürgerrolle.")
async def teamkick_command(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "🚪 **Teamkick**\n"
        "Wähle das Mitglied aus, dessen Teamrolle(n) entfernt werden sollen.",
        view=TeamkickMenuView(interaction.user.id),
        ephemeral=True,
    )


def register_commands(bot: ModerationBot) -> None:
    for command in (
        ping,
        serverinfo,
        modmenu_command,
        kick_menu_command,
        ban_menu_command,
        timeout_menu_command,
        untimeout_menu_command,
        rankmenu_menu_command,
        uprank_menu_command,
        derank_menu_command,
        clear_menu_command,
        warn_menu_command,
        warnings_menu_command,
        clearwarnings_menu_command,
        abmeldung_command,
        abmelden_command,
        partnerschaft_command,
        rp_start_command,
        rp_ende_command,
        umfrage_command,
        ankuendigung_command,
        teamkick_command,
    ):
        bot.tree.add_command(command)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        actual_error = error.original if isinstance(error, app_commands.CommandInvokeError) else error
        if isinstance(actual_error, ModerationError):
            message = str(actual_error)
        elif isinstance(actual_error, app_commands.MissingPermissions):
            message = "Dir fehlt die erforderliche Discord-Berechtigung für diesen Befehl."
        elif isinstance(actual_error, discord.Forbidden):
            message = "Discord hat die Aktion abgelehnt. Prüfe die Bot-Berechtigungen und Rollen-Hierarchie."
        elif isinstance(actual_error, app_commands.CheckFailure):
            if interaction.command and interaction.command.name == "teamkick":
                message = (
                    "Für `/teamkick` brauchst du die Rolle **Teamleitung** "
                    "oder eine höher eingestufte Rolle. Der Serverinhaber ist ebenfalls zugelassen."
                )
            else:
                message = "Dieser Befehl kann hier nicht ausgeführt werden."
        else:
            LOGGER.exception("Unhandled slash command error.", exc_info=actual_error)
            message = "Beim Ausführen ist ein unerwarteter Fehler aufgetreten."
        await _reply(interaction, message)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings.from_env()
    bot = ModerationBot(settings)
    register_commands(bot)
    bot.run(settings.token)


if __name__ == "__main__":
    main()
