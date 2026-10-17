from dotenv import load_dotenv
load_dotenv()
import os
import json
import re
from datetime import datetime, timezone, timedelta, time as dt_time
import discord as dc
from discord.ext import commands, tasks
from discord import app_commands

TAIPEI = timezone(timedelta(hours=8))
DATA_FILE = "checkins.json"
MONTHLY_LEAVE_LIMIT = 1

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"keywords": ["今日已完成","對題數"], "checkins": {}, "reminder_channels": {}, "participants": {}, "leaves": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        d = json.load(f)
        d.setdefault("keywords", ["今日已完成","對題數"])
        d.setdefault("checkins", {})
        d.setdefault("reminder_channels", {})
        d.setdefault("participants", {})
        d.setdefault("leaves", {})
    new_checkins = {}
    for uid, val in d["checkins"].items():
        if isinstance(val, list):
            new_checkins[uid] = {date: {"correct": None, "total": 10} for date in val}
        else:
            new_checkins[uid] = val
    d["checkins"] = new_checkins
    return d

def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def today_str():
    return datetime.now(TAIPEI).strftime("%Y-%m-%d")

def parse_score(text):
    if not text:
        return None, None
    m = re.findall(r"(\d+)\s*/\s*(\d+)", text)
    if m:
        return int(m[-1][0]), int(m[-1][1])
    return None, None

def get_leaves(gid, uid):
    return data.get("leaves", {}).get(str(gid), {}).get(str(uid), {})

def get_leave_set(gid, uid):
    return set(get_leaves(gid, uid).keys())

def count_leaves_in_month(gid, uid, ym):
    leaves = get_leaves(gid, uid)
    return sum(1 for d in leaves if d.startswith(ym))

def get_participant_ids(gid):
    return data.get("participants", {}).get(str(gid), [])

def calc_streak(date_keys, leave_keys=None):
    date_set = set(date_keys)
    leave_set = set(leave_keys) if leave_keys else set()
    cur = datetime.now(TAIPEI).date()
    streak = 0
    for _ in range(400):
        s = cur.strftime("%Y-%m-%d")
        if s in date_set:
            streak += 1
            cur -= timedelta(days=1)
        elif s in leave_set:
            cur -= timedelta(days=1)
        else:
            break
    return streak

def calc_max_streak(date_keys, leave_keys=None):
    if not date_keys:
        return 0
    leave_set = set(leave_keys) if leave_keys else set()
    sd = sorted([datetime.strptime(d, "%Y-%m-%d").date() for d in date_keys])
    max_s = cur = 1
    for i in range(1, len(sd)):
        diff = (sd[i] - sd[i-1]).days
        if diff == 1:
            cur += 1
        elif diff > 1:
            all_leave = all((sd[i-1] + timedelta(days=d)).strftime("%Y-%m-%d") in leave_set for d in range(1, diff))
            cur = cur + 1 if all_leave else 1
        max_s = max(max_s, cur)
    return max_s

data = load_data()
intents = dc.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"已上線：{bot.user}")
    GUILD_ID = 1431221984792019050
    if GUILD_ID != 0:
        g = dc.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=g)
        synced = await bot.tree.sync(guild=g)
        print(f"已同步 {len(synced)} 個指令到 {GUILD_ID}")
    else:
        synced = await bot.tree.sync()
        print(f"已同步 {len(synced)} 個指令")
    if not daily_reminder.is_running():
        daily_reminder.start()

async def do_checkin(user, channel, raw_text=None, guild_id=None):
    uid = str(user.id)
    today = today_str()
    data["checkins"].setdefault(uid, {})
    if guild_id and today in get_leaves(guild_id, uid):
        del data["leaves"][str(guild_id)][uid][today]
    if today in data["checkins"][uid]:
        await channel.send(embed=dc.Embed(title="你今天已經打卡過啦", color=0xffd700))
        return False
    c, t = parse_score(raw_text)
    data["checkins"][uid][today] = {"correct": c, "total": t if t else 10}
    save_data(data)
    leaves = get_leave_set(guild_id, uid) if guild_id else set()
    streak = calc_streak(data["checkins"][uid].keys(), leaves)
    desc = f"{user.mention} 今日完成！ 連勝 {streak} 天"
    if c is not None:
        desc += f"\n對題數：{c}/{t}"
    await channel.send(embed=dc.Embed(title="打卡成功", description=desc, color=0x00ff88).set_footer(text=f"日期：{today}"))
    return True

@bot.event
async def on_message(message):
    if message.author.bot:
        return
    norm = message.content.replace(" ", "").replace("　", "")
    if "今日已完成" in norm or "對題數" in norm:
        await do_checkin(message.author, message.channel, message.content, message.guild.id if message.guild else None)
    await bot.process_commands(message)

@bot.tree.command(name="今日戰況", description="查看今天狀況")
async def today_status(interaction: dc.Interaction):
    today = today_str()
    gid = str(interaction.guild.id)
    members = [interaction.guild.get_member(int(uid)) for uid in get_participant_ids(gid)] if get_participant_ids(gid) else [m for m in interaction.guild.members if not m.bot]
    checked, not_checked, on_leave = [], [], []
    for m in members:
        if not m:
            continue
        uid = str(m.id)
        rec = data["checkins"].get(uid, {}).get(today)
        leave = get_leaves(gid, uid).get(today)
        streak = calc_streak(data["checkins"].get(uid, {}).keys(), get_leave_set(gid, uid))
        if rec:
            c = rec.get("correct")
            t = rec.get("total", 10)
            if c is not None:
                checked.append(f"{m.display_name} (連勝{streak}天) - 答對數/答題數 {c}/{t}")
            else:
                checked.append(f"{m.display_name} (連勝{streak}天)")
        elif leave:
            on_leave.append(f"{m.display_name} (請假：{leave})")
        else:
            not_checked.append(f"{m.display_name} (連勝{streak}天)")
    text = f"📊 今日戰況 ({today})\n"
    text += f"✅ 已完成 ({len(checked)})\n" + ("\n".join(checked) if checked else "無") + "\n\n"
    text += f"💤 請假中 ({len(on_leave)})\n" + ("\n".join(on_leave) if on_leave else "無") + "\n\n"
    text += f"❌ 未完成 ({len(not_checked)})\n" + ("\n".join(not_checked) if not_checked else "全員完成！")
    await interaction.response.send_message(text)

@bot.tree.command(name="請假", description="請假一天，每月限1次")
@app_commands.describe(日期="YYYY-MM-DD 預設今天", 理由="原因")
async def leave_one(interaction: dc.Interaction, 日期: str = None, 理由: str = "有事"):
    target = 日期 if 日期 else today_str()
    try:
        datetime.strptime(target, "%Y-%m-%d")
    except:
        await interaction.response.send_message("日期格式用 YYYY-MM-DD", ephemeral=True)
        return
    gid = interaction.guild.id
    uid = str(interaction.user.id)
    ym = target[:7]
    if count_leaves_in_month(gid, uid, ym) >= MONTHLY_LEAVE_LIMIT:
        await interaction.response.send_message(f"❌ 你 {ym} 已經請過 {MONTHLY_LEAVE_LIMIT} 次了", ephemeral=True)
        return
    data["leaves"].setdefault(str(gid), {}).setdefault(uid, {})[target] = 理由
    save_data(data)
    await interaction.response.send_message(f"💤 {interaction.user.mention} 已請假 {target} ({理由})", ephemeral=False)

@bot.tree.command(name="請假多日", description="一次請多天")
@app_commands.describe(開始日期="YYYY-MM-DD", 結束日期="YYYY-MM-DD", 理由="原因")
async def leave_range(interaction: dc.Interaction, 開始日期: str, 結束日期: str, 理由: str = "有事"):
    try:
        s = datetime.strptime(開始日期, "%Y-%m-%d").date()
        e = datetime.strptime(結束日期, "%Y-%m-%d").date()
    except:
        await interaction.response.send_message("日期格式錯誤", ephemeral=True)
        return
    gid = interaction.guild.id
    uid = str(interaction.user.id)
    dates = []
    cur = s
    while cur <= e:
        dates.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)
    for d in dates:
        if count_leaves_in_month(gid, uid, d[:7]) >= MONTHLY_LEAVE_LIMIT and d not in get_leaves(gid, uid):
            await interaction.response.send_message(f"❌ {d[:7]} 已達上限", ephemeral=True)
            return
    data["leaves"].setdefault(str(gid), {}).setdefault(uid, {})
    for d in dates:
        data["leaves"][str(gid)][uid][d] = 理由
    save_data(data)
    await interaction.response.send_message(f"💤 已請假 {開始日期} ~ {結束日期} ({理由})", ephemeral=False)

@bot.tree.command(name="幫別人請假", description="管理員幫別人請假")
@app_commands.describe(成員="要幫誰請", 日期="YYYY-MM-DD", 理由="原因", 強制請假="是否無視上限")
@app_commands.checks.has_permissions(manage_guild=True)
async def leave_for_other(interaction: dc.Interaction, 成員: dc.Member, 日期: str = None, 理由: str = "有事", 強制請假: bool = False):
    target = 日期 if 日期 else today_str()
    gid = interaction.guild.id
    uid = str(成員.id)
    ym = target[:7]
    if not 強制請假 and count_leaves_in_month(gid, uid, ym) >= MONTHLY_LEAVE_LIMIT:
        await interaction.response.send_message(f"❌ {成員.display_name} {ym} 已達上限，要強制請把 強制請假 選 是", ephemeral=True)
        return
    data["leaves"].setdefault(str(gid), {}).setdefault(uid, {})[target] = 理由
    save_data(data)
    await interaction.response.send_message(f"✅ 已幫 {成員.display_name} 請假 {target} ({理由})", ephemeral=True)

@bot.tree.command(name="取消請假", description="取消請假")
@app_commands.describe(日期="YYYY-MM-DD")
async def cancel_leave(interaction: dc.Interaction, 日期: str = None):
    target = 日期 if 日期 else today_str()
    gid = str(interaction.guild.id)
    uid = str(interaction.user.id)
    if target in data.get("leaves", {}).get(gid, {}).get(uid, {}):
        del data["leaves"][gid][uid][target]
        save_data(data)
        await interaction.response.send_message(f"✅ 已取消 {target}", ephemeral=True)
    else:
        await interaction.response.send_message("那天沒請假", ephemeral=True)

@bot.tree.command(name="查看請假", description="查看請假")
async def view_leaves(interaction: dc.Interaction):
    gid = str(interaction.guild.id)
    today = today_str()
    all_l = data.get("leaves", {}).get(gid, {})
    lines = []
    for uid, ds in all_l.items():
        for d, r in ds.items():
            if d >= today:
                m = interaction.guild.get_member(int(uid))
                name = m.display_name if m else uid
                lines.append(f"{name} - {d}：{r}")
    msg = "\n".join(lines) if lines else "沒有未來的請假"
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="打卡", description="打卡")
async def checkin_slash(interaction: dc.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok = await do_checkin(interaction.user, interaction.channel, None, interaction.guild.id)
    await interaction.followup.send("打卡成功！" if ok else "已打過卡", ephemeral=True)

@bot.tree.command(name="我的連勝", description="查看連勝")
async def my_streak(interaction: dc.Interaction):
    uid = str(interaction.user.id)
    gid = interaction.guild.id
    keys = list(data["checkins"].get(uid, {}).keys())
    leaves = get_leave_set(gid, uid)
    ym = today_str()[:7]
    await interaction.response.send_message(f"{interaction.user.mention} 連勝 {calc_streak(keys, leaves)} 天 / 最高 {calc_max_streak(keys, leaves)} 天 / 本月請假 {count_leaves_in_month(gid, uid, ym)}/{MONTHLY_LEAVE_LIMIT}")

@bot.tree.command(name="排行榜", description="排行榜")
async def leaderboard(interaction: dc.Interaction):
    gid = str(interaction.guild.id)
    members = [interaction.guild.get_member(int(uid)) for uid in get_participant_ids(gid)] if get_participant_ids(gid) else [m for m in interaction.guild.members if not m.bot]
    ranking = []
    for m in members:
        if not m:
            continue
        uid = str(m.id)
        keys = list(data["checkins"].get(uid, {}).keys())
        if keys:
            ranking.append((m.display_name, calc_streak(keys, get_leave_set(gid, uid)), len(keys)))
    ranking.sort(key=lambda x: (x[1], x[2]), reverse=True)
    desc = "\n".join([f"{i}. {n} - 連勝 {s}天 / 累計 {t}天" for i, (n, s, t) in enumerate(ranking[:10], 1)]) if ranking else "無紀錄"
    await interaction.response.send_message(embed=dc.Embed(title="🏆 排行榜", description=desc, color=0xffd700))

@bot.tree.command(name="新增參加者", description="加入提醒名單")
@app_commands.describe(成員1="第1位", 成員2="第2位")
@app_commands.checks.has_permissions(manage_guild=True)
async def add_part(interaction: dc.Interaction, 成員1: dc.Member, 成員2: dc.Member = None):
    gid = str(interaction.guild.id)
    data["participants"].setdefault(gid, [])
    for m in [成員1, 成員2]:
        if m and str(m.id) not in data["participants"][gid]:
            data["participants"][gid].append(str(m.id))
    save_data(data)
    await interaction.response.send_message("✅ 已更新參加名單", ephemeral=True)

@bot.tree.command(name="設定提醒頻道", description="設定23:00提醒頻道")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_ch(interaction: dc.Interaction):
    data["reminder_channels"][str(interaction.guild.id)] = interaction.channel.id
    save_data(data)
    await interaction.response.send_message("✅ 已設定", ephemeral=True)

@tasks.loop(time=dt_time(hour=23, minute=0, tzinfo=TAIPEI))
async def daily_reminder():
    today = today_str()
    for gid_str, ch_id in data.get("reminder_channels", {}).items():
        guild = bot.get_guild(int(gid_str))
        if not guild:
            continue
        ch = guild.get_channel(int(ch_id))
        if not ch:
            continue
        p_ids = get_participant_ids(gid_str)
        members = [guild.get_member(int(uid)) for uid in p_ids] if p_ids else [m for m in guild.members if not m.bot]
        not_checked = [m.mention for m in members if m and today not in data["checkins"].get(str(m.id), {}) and today not in data.get("leaves", {}).get(gid_str, {}).get(str(m.id), {})]
        if not_checked:
            await ch.send(f"⏰ 還沒打卡：{' '.join(not_checked)}")
        else:
            await ch.send("🎉 全員完成！")

bot.run(os.getenv("DISCORD_TOKEN"))
