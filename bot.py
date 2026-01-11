import discord
from discord.ext import commands
from discord import app_commands
import yt_dlp
import asyncio
from functools import partial
import datetime
import os  # ← Thêm cái này để lấy token từ env

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="/", intents=intents, help_command=None)

# yt-dlp config
ytdl = yt_dlp.YoutubeDL({
    'format': 'bestaudio/best',
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'noplaylist': False,
})

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# ==================== MUSIC COG ====================
class SearchView(discord.ui.View):
    def __init__(self, entries, user):
        super().__init__(timeout=60)
        self.chosen_url = None
        self.user = user

        select = discord.ui.Select(placeholder="Chọn bài hát để phát...")
        for i, entry in enumerate(entries[:10], 1):
            label = f"{i}. {entry['title'][:80]}"
            desc = entry.get('uploader', 'Unknown')[:50]
            select.add_option(label=label, description=desc, value=entry['webpage_url'])
        select.callback = self.callback
        self.add_item(select)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user != self.user:
            await interaction.response.send_message("Chỉ người dùng lệnh mới được chọn!", ephemeral=True)
            return
        self.chosen_url = interaction.data['values'][0]
        await interaction.response.defer()
        self.stop()

class MusicCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.queues = {}
        self.current_song = {}
        self.last_channel = {}
        self.volumes = {}

    def get_queue(self, guild_id):
        if guild_id not in self.queues:
            self.queues[guild_id] = []
        return self.queues[guild_id]

    async def extract_info(self, url):
        loop = self.bot.loop
        partial_func = partial(ytdl.extract_info, url, download=False)
        return await loop.run_in_executor(None, partial_func)

    async def prepare_songs(self, url, requester):
        data = await self.extract_info(url)
        if not data:
            return []

        entries = data['entries'] if 'entries' in data else [data]
        songs = []
        for entry in entries:
            if entry:
                songs.append({
                    'title': entry.get('title', 'Unknown'),
                    'url': entry.get('webpage_url', url),
                    'stream_url': entry['url'],
                    'duration': entry.get('duration'),
                    'thumbnail': f"https://i.ytimg.com/vi/{entry['id']}/hqdefault.jpg" if entry.get('id') else None,
                    'requester': requester
                })
        return songs

    async def join_voice(self, interaction):
        if not interaction.user.voice:
            await interaction.response.send_message("Bạn phải vào voice channel trước!", ephemeral=True)
            return None
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)
        return vc

    def create_np_embed(self, song):
        embed = discord.Embed(title="Đang phát 🎵", color=discord.Color.green())
        embed.description = f"**[{song['title']}]({song['url']})**\nYêu cầu bởi {song['requester'].mention}"
        if song['thumbnail']:
            embed.set_thumbnail(url=song['thumbnail'])
        if song['duration']:
            embed.add_field(name="Thời lượng", value=str(datetime.timedelta(seconds=song['duration'])))
        queue_len = len(self.get_queue(song['requester'].guild.id))
        embed.set_footer(text=f"Queue: {queue_len} bài tiếp theo")
        return embed

    async def play_next(self, guild):
        queue = self.get_queue(guild.id)
        if not queue:
            return

        song = queue.pop(0)
        self.current_song[guild.id] = song

        source = discord.FFmpegPCMAudio(song['stream_url'], **ffmpeg_options)
        volume = self.volumes.get(guild.id, 1.0)
        player = discord.PCMVolumeTransformer(source, volume=volume)

        def after(err):
            if err:
                print(f"Error: {err}")
            asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)

        guild.voice_client.play(player, after=after)

        channel_id = self.last_channel.get(guild.id)
        if channel_id:
            channel = self.bot.get_channel(channel_id)
            if channel:
                await channel.send(embed=self.create_np_embed(song))

    @app_commands.command(name="play", description="Phát nhạc (tìm kiếm hoặc link YouTube)")
    @app_commands.describe(query="Tên bài hát hoặc link YouTube/playlist")
    async def play(self, interaction: discord.Interaction, query: str):
        await interaction.response.defer()

        vc = await self.join_voice(interaction)
        if not vc:
            return

        self.last_channel[interaction.guild.id] = interaction.channel_id

        is_url = query.startswith(('http://', 'https://'))

        if not is_url:
            search_url = f"ytsearch10:{query}"
            data = await self.extract_info(search_url)
            if not data or 'entries' not in data or not data['entries']:
                await interaction.followup.send("Không tìm thấy kết quả nào!")
                return

            view = SearchView(data['entries'], interaction.user)
            embed = discord.Embed(title=f"Kết quả tìm kiếm: {query}", color=discord.Color.blurple())
            embed.description = "\n".join(
                f"`{i+1}.` [{e['title']}]({e['webpage_url']})" for i, e in enumerate(data['entries'][:10])
            )
            await interaction.followup.send(embed=embed, view=view)
            await view.wait()

            if not view.chosen_url:
                await interaction.edit_original_response(content="Hủy lệnh (hết thời gian chọn).", embed=None, view=None)
                return

            songs = await self.prepare_songs(view.chosen_url, interaction.user)
            await interaction.edit_original_response(content=f"Đã chọn và thêm **{songs[0]['title']}** vào queue!", embed=None, view=None)
        else:
            songs = await self.prepare_songs(query, interaction.user)
            if not songs:
                await interaction.followup.send("Không thể lấy thông tin từ link!")
                return

        if not songs:
            await interaction.followup.send("Lỗi khi xử lý bài hát!")
            return

        queue = self.get_queue(interaction.guild.id)
        previous_len = len(queue)
        queue.extend(songs)

        if vc.is_playing() or vc.is_paused():
            await interaction.followup.send(f"✅ Đã thêm **{len(songs)}** bài vào queue (từ vị trí #{previous_len + 1})")
        else:
            await self.play_next(interaction.guild)
            await interaction.followup.send(embed=self.create_np_embed(songs[0]))

        if len(songs) > 1:
            await interaction.channel.send(f"📑 Đã thêm playlist với **{len(songs)}** bài!")

    @app_commands.command(name="skip", description="Bỏ qua bài hiện tại")
    async def skip(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭ Đã skip bài hát!")
        else:
            await interaction.response.send_message("Không có bài nào đang phát!", ephemeral=True)

    @app_commands.command(name="pause", description="Tạm dừng nhạc")
    async def pause(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸ Đã tạm dừng")
        else:
            await interaction.response.send_message("Không có nhạc đang phát!", ephemeral=True)

    @app_commands.command(name="resume", description="Tiếp tục phát nhạc")
    async def resume(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc and vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶ Đã tiếp tục")
        else:
            await interaction.response.send_message("Nhạc không đang dừng!", ephemeral=True)

    @app_commands.command(name="stop", description="Dừng nhạc và rời voice")
    async def stop(self, interaction: discord.Interaction):
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
            self.get_queue(interaction.guild.id).clear()
            await vc.disconnect()
            await interaction.response.send_message("⏹ Đã dừng nhạc và rời voice")
        else:
            await interaction.response.send_message("Bot không ở trong voice!", ephemeral=True)

    @app_commands.command(name="queue", description="Xem queue nhạc")
    async def queue(self, interaction: discord.Interaction):
        queue = self.get_queue(interaction.guild.id)
        current = self.current_song.get(interaction.guild.id)

        embed = discord.Embed(title="Queue nhạc", color=discord.Color.blue())
        if current:
            embed.add_field(name="Đang phát", value=f"**{current['title']}** - {current['requester'].mention}", inline=False)

        if queue:
            lines = [f"`{i+1}.` **{s['title']}** - {s['requester'].mention}" for i, s in enumerate(queue[:15])]
            embed.add_field(name=f"Tiếp theo ({len(queue)} bài)", value="\n".join(lines), inline=False)
            if len(queue) > 15:
                embed.set_footer(text=f"... và {len(queue)-15} bài khác")
        else:
            embed.description = "Queue đang trống!"

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="volume", description="Đặt volume (0-200)")
    @app_commands.describe(value="Mức volume %")
    async def volume(self, interaction: discord.Interaction, value: app_commands.Range[int, 0, 200]):
        vc = interaction.guild.voice_client
        if vc and vc.source:
            vc.source.volume = value / 100
            self.volumes[interaction.guild.id] = value / 100
            await interaction.response.send_message(f"🔊 Volume đặt thành **{value}%**")
        else:
            await interaction.response.send_message("Bot không đang phát nhạc!", ephemeral=True)

# ==================== OTHER COGS ====================
class GeneralCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="avt", description="Xem avatar của thành viên")
    @app_commands.describe(member="Thành viên (để trống = bạn)")
    async def avt(self, interaction: discord.Interaction, member: discord.Member = None):
        member = member or interaction.user
        embed = discord.Embed(title=f"Avatar của {member.display_name}", color=member.color)
        embed.set_image(url=member.display_avatar.url)
        await interaction.response.send_message(embed=embed)

class ModCog(commands.Cog):
    @app_commands.command(name="role", description="Gán role cho thành viên")
    @app_commands.describe(member="Thành viên", role="Role")
    @commands.has_permissions(manage_roles=True)
    async def role(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        await member.add_roles(role)
        await interaction.response.send_message(f"✅ Đã gán {role.mention} cho {member.mention}")

    @app_commands.command(name="camchat", description="Cấm chat tạm thời")
    @app_commands.describe(member="Thành viên", time="Số lượng", unit="Đơn vị")
    @app_commands.choices(unit=[app_commands.Choice(name="phút", value="minutes"),
                                app_commands.Choice(name="giờ", value="hours"),
                                app_commands.Choice(name="ngày", value="days")])
    @commands.has_permissions(moderate_members=True)
    async def camchat(self, interaction: discord.Interaction, member: discord.Member, time: int, unit: str):
        if time <= 0:
            await interaction.response.send_message("Thời gian phải > 0!", ephemeral=True)
            return
        duration = datetime.timedelta(**{unit: time})
        await member.timeout(discord.utils.utcnow() + duration, reason=f"Timeout bởi {interaction.user}")
        await interaction.response.send_message(f"🔇 Đã cấm chat {member.mention} trong {time} {unit}")

    @app_commands.command(name="kick", description="Kick thành viên")
    @app_commands.describe(member="Thành viên", reason="Lý do")
    @commands.has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Không có lý do"):
        await member.kick(reason=reason)
        await interaction.response.send_message(f"👢 Đã kick {member.mention} | Lý do: {reason}")

    @app_commands.command(name="ban", description="Ban thành viên")
    @app_commands.describe(member="Thành viên", reason="Lý do")
    @commands.has_permissions(ban_members=True)
    async def ban(self, interaction: discord.Interaction, member: discord.Member, reason: str = "Không có lý do"):
        await member.ban(reason=reason)
        await interaction.response.send_message(f"🔨 Đã ban {member.mention} | Lý do: {reason}")

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    print(f"Bot đã sẵn sàng: {bot.user}")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.listening, name="/play | Bot Xịn Xò"))
    synced = await bot.tree.sync()
    print(f"Đã sync {len(synced)} lệnh slash")

@bot.event
async def on_member_join(member):
    channel = member.guild.system_channel
    if channel:
        embed = discord.Embed(
            title="Chào mừng thành viên mới! 🎉",
            description=f"{member.mention} đã gia nhập server!\nChúc bạn có thời gian vui vẻ nhé!",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"Thành viên thứ {member.guild.member_count}")
        await channel.send(embed=embed)

@bot.event
async def on_member_remove(member):
    channel = member.guild.system_channel
    if channel:
        await channel.send(f"😢 {member.display_name} đã rời server...")

# ==================== SETUP ====================
if __name__ == "__main__":
    bot.add_cog(MusicCog(bot))
    bot.add_cog(GeneralCog(bot))
    bot.add_cog(ModCog(bot))
    bot.run(os.getenv("DISCORD_TOKEN"))
