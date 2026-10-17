from dotenv import load_dotenv
load_dotenv()
import os
import json
import re
from datetime import datetime, timezone, timedelta, time as dt_time
import discord as dc
from discord.ext import commands, tasks
from discord import app_commands
from flask import Flask
import threading

TAIPEI = timezone(timedelta(hours=8))
DATA_FILE = "checkins.json"
MONTHLY_LEAVE_LIMIT = 1

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Supabase 已連線")
    except Exception as e:
        print(f"Supabase 連線失敗，改用本地檔案: {e}")
        supabase = None
else:
    print("未設定 SUPABASE_URL / KEY，改用本地檔案")

def load_data():
    base = {"keywords": ["今日已完成","對題數"], "checkins": {}, "reminder_channels": {}, "participants": {}, "leaves": {}}
    if not supabase:
        if not os.path.exists(DATA_FILE):
            return base
        try:
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
        except:
            return base
    try:
        r = supabase.table("checkins").select("*").execute()
        for row in r.data or []:
            uid = str(row["user_id"])
            base["checkins"].setdefault(uid, {})
            base["checkins"][uid][row["date"]] = {"correct": row.get("correct"), "total": row.get("total") if row.get("total") else 10}
        r = supabase.table("leaves").select("*").execute()
        for row in r.data or []:
            gid = str(row["guild_id"])
            uid = str(row["user_id"])
            base["leaves"].setdefault(gid, {}).setdefault(uid, {})[row["date"]] = row.get("reason") or "有事"
        r = supabase.table("participants").select("*").execute()
        for row in r.data or []:
            gid = str(row["guild_id"])
            base["participants"].setdefault(gid, [])
            if str(row["user_id"]) not in base["participants"][gid]:
                base["participants"][gid].append(str(row["user_id"]))
        r = supabase.table("reminder_channels").select("*").execute()
        for row in r.data or []:
            base["reminder_channels"][str(row["guild_id"])] = int(row["channel_id"]) if str(row["channel_id"]).isdigit() else row["channel_id"]
        print(f"已從 Supabase 載入 {len(base['checkins'])} 人的打卡")
    except Exception as e:
        print(f"讀取 Supabase 失敗: {e}")
    return base

def save_data(data):
    if not supabase:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

def db_upsert_checkin(uid, username, date_str, correct, total):
    if not supabase:
        return
    try:
        supabase.table("checkins").upsert(
            {"user_id": str(uid), "username": str(username), "date": date_str, "correct": correct, "total": total},
            on_conflict="user_id,date"
        ).execute()
    except Exception as e:
        print(f"upsert checkin 失敗: {e}")

def db_delete_leave(gid, uid, date_str):
    if not supabase:
        return
    try:
        supabase.table("leaves").delete().eq("guild_id", str(gid)).eq("user_id", str(uid)).eq("date", date_str).execute()
    except Exception as e:
        print(f"delete leave 失敗: {e}")

def db_add_leaves(gid, uid, dates, reason):
    if not supabase:
        return
    try:
        rows = [{"guild_id": str(gid), "user_id": str(uid), "date": d, "reason": reason} for d in dates]
        supabase.table("leaves").upsert(rows, on_conflict="guild_id,user_id,date").execute()
    except Exception as e:
        print(f"add leaves 失敗: {e}")

def db_set_participants(gid, uid_list):
    if not supabase:
        return
    try:
        rows = [{"guild_id": str(gid), "user_id": str(uid)} for uid in uid_list]
        if rows:
            supabase.table("participants").upsert(rows, on_conflict="guild_id,user_id").execute()
    except Exception as e:
        print(f"set participants 失敗: {e}")

def db_set_reminder_channel(gid, ch_id):
    if not supabase:
        return
    try:
        supabase.table("reminder_channels").upsert(
            {"guild_id": str(gid), "channel_id": str(ch_id)},
            on_conflict="guild_id"
        ).execute()
    except Exception as e:
        print(f"set reminder 失敗: {e}")

def today_str():
    return datetime.now(TAIPEI).strftime("%Y-%m-%d")

def parse_score(text):
    # 修復：不管傳進來是 int 還是 str 都能處理
    if text is None:
        return None, None
    if isinstance(text, int):
        return text, 10
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return None, None
    m = re.findall(r"(\d+)\s*/\s*(\d+)", text)
    if m:
        return int(m[-1][0]), int(m[-1][1])
    # 如果只打一個數字，例如 "8"，當作答對 8 題
    m2 = re.findall(r"(\d+)", text)
    if m2:
        return int(m2[-1]), 10
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
        db_delete_leave(guild_id, uid, today)
    if today in data["checkins"][uid]:
        await channel.send(embed=dc.Embed(title="你今天已經打卡過啦", color=0xffd700))
        return False
    c, t = parse_score(raw_text)
    data["checkins"][uid][today] = {"correct": c, "total": t if t else 10}
    db_upsert_checkin(uid, str(user), today, c, t if t else 10)
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
    await interaction.response.defer(ephemeral=False)
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
    text += f"❌ 未完成 ({len(not_checked)})\n" + ("\n".join(not_checked) if not_checked else "無")
    await interaction.followup.send(text)

@bot.tree.command(name="請假", description="請假")
@app_commands.describe(開始日期="YYYY-MM-DD", 結束日期="YYYY-MM-DD", 理由="請假原因")
async def leave_cmd(interaction: dc.Interaction, 開始日期: str = None, 結束日期: str = None, 理由: str = "有事"):
    await interaction.response.defer(ephemeral=False)
    try:
        s = datetime.strptime(開始日期, "%Y-%m-%d").date() if 開始日期 else datetime.now(TAIPEI).date()
        e = datetime.strptime(結束日期, "%Y-%m-%d").date() if 結束日期 else s
    except:
        await interaction.followup.send("日期格式錯誤", ephemeral=True)
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
            await interaction.followup.send(f"❌ {d[:7]} 已達上限", ephemeral=True)
            return
    data["leaves"].setdefault(str(gid), {}).setdefault(uid, {})
    for d in dates:
        data["leaves"][str(gid)][uid][d] = 理由
    db_add_leaves(gid, uid, dates, 理由)
    save_data(data)
    await interaction.followup.send(f"💤 已請假 {開始日期} ~ {結束日期} ({理由})")

@bot.tree.command(name="補打卡", description="補打過去的卡")
@app_commands.describe(日期="YYYY-MM-DD", 對題數="例如 8/10 或 8")
async def bu_daka(interaction: dc.Interaction, 日期: str, 對題數: str = None):
    await interaction.response.defer(ephemeral=True)
    try:
        target = datetime.strptime(日期, "%Y-%m-%d").date()
        target_str = target.strftime("%Y-%m-%d")
    except:
        await interaction.followup.send("日期格式錯誤，請用 YYYY-MM-DD，例如 2025-10-17", ephemeral=True)
        return
    uid = str(interaction.user.id)
    if target_str in data["checkins"].get(uid, {}):
        await interaction.followup.send(f"{target_str} 已經打過卡了", ephemeral=True)
        return
    c, t = parse_score(對題數)
    data["checkins"].setdefault(uid, {})[target_str] = {"correct": c, "total": t if t else 10}
    db_upsert_checkin(uid, str(interaction.user), target_str, c, t if t else 10)
    save_data(data)
    gid = interaction.guild.id
    if target_str in get_leaves(gid, uid):
        del data["leaves"][str(gid)][uid][target_str]
        db_delete_leave(gid, uid, target_str)
    await interaction.followup.send(f"✅ 已補打 {target_str} {f'對題數 {對題數}' if 對題數 else ''}", ephemeral=True)

@bot.tree.command(name="幫別人請假", description="管理員幫別人請假")
@app_commands.describe(成員="要幫誰請", 日期="YYYY-MM-DD", 理由="原因", 強制請假="是否無視上限")
@app_commands.checks.has_permissions(manage_guild=True)
async def leave_for_other(interaction: dc.Interaction, 成員: dc.Member, 日期: str = None, 理由: str = "有事", 強制請假: bool = False):
    await interaction.response.defer(ephemeral=True)
    target = 日期 if 日期 else today_str()
    gid = interaction.guild.id
    uid = str(成員.id)
    ym = target[:7]
    if not 強制請假 and count_leaves_in_month(gid, uid, ym) >= MONTHLY_LEAVE_LIMIT:
        await interaction.followup.send(f"❌ {成員.display_name} {ym} 已達上限，要強制請把 強制請假 選 是", ephemeral=True)
        return
    data["leaves"].setdefault(str(gid), {}).setdefault(uid, {})[target] = 理由
    db_add_leaves(gid, uid, [target], 理由)
    save_data(data)
    await interaction.followup.send(f"✅ 已幫 {成員.display_name} 請假 {target} ({理由})", ephemeral=True)

@bot.tree.command(name="取消請假", description="取消請假")
@app_commands.describe(日期="YYYY-MM-DD")
async def cancel_leave(interaction: dc.Interaction, 日期: str = None):
    await interaction.response.defer(ephemeral=True)
    target = 日期 if 日期 else today_str()
    gid = str(interaction.guild.id)
    uid = str(interaction.user.id)
    if target in data.get("leaves", {}).get(gid, {}).get(uid, {}):
        del data["leaves"][gid][uid][target]
        db_delete_leave(gid, uid, target)
        save_data(data)
        await interaction.followup.send(f"✅ 已取消 {target}", ephemeral=True)
    else:
        await interaction.followup.send("那天沒請假", ephemeral=True)

@bot.tree.command(name="查看請假", description="查看請假")
async def view_leaves(interaction: dc.Interaction):
    await interaction.response.defer(ephemeral=True)
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
    await interaction.followup.send(msg, ephemeral=True)

@bot.tree.command(name="打卡", description="打卡")
async def checkin_slash(interaction: dc.Interaction):
    await interaction.response.defer(ephemeral=True)
    ok = await do_checkin(interaction.user, interaction.channel, None, interaction.guild.id)
    await interaction.followup.send("打卡成功！" if ok else "已打過卡", ephemeral=True)

@bot.tree.command(name="我的連勝", description="查看連勝")
async def my_streak(interaction: dc.Interaction):
    await interaction.response.defer(ephemeral=True)
    uid = str(interaction.user.id)
    gid = interaction.guild.id
    keys = list(data["checkins"].get(uid, {}).keys())
    leaves = get_leave_set(gid, uid)
    ym = today_str()[:7]
    await interaction.followup.send(f"{interaction.user.mention} 連勝 {calc_streak(keys, leaves)} 天 / 最高 {calc_max_streak(keys, leaves)} 天 / 本月請假 {count_leaves_in_month(gid, uid, ym)}/{MONTHLY_LEAVE_LIMIT}", ephemeral=True)

@bot.tree.command(name="排行榜", description="排行榜")
async def leaderboard(interaction: dc.Interaction):
    await interaction.response.defer(ephemeral=False)
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
    await interaction.followup.send(embed=dc.Embed(title="🏆 排行榜", description=desc, color=0xffd700))

@bot.tree.command(name="新增參加者", description="加入提醒名單")
@app_commands.describe(成員1="第1位", 成員2="第2位")
@app_commands.checks.has_permissions(manage_guild=True)
async def add_part(interaction: dc.Interaction, 成員1: dc.Member, 成員2: dc.Member = None):
    await interaction.response.defer(ephemeral=True)
    gid = str(interaction.guild.id)
    data["participants"].setdefault(gid, [])
    new_ids = []
    for m in [成員1, 成員2]:
        if m and str(m.id) not in data["participants"][gid]:
            data["participants"][gid].append(str(m.id))
            new_ids.append(str(m.id))
    if new_ids:
        db_set_participants(gid, new_ids)
    save_data(data)
    await interaction.followup.send("✅ 已更新參加名單", ephemeral=True)

@bot.tree.command(name="設定提醒頻道", description="設定23:00提醒頻道")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_ch(interaction: dc.Interaction):
    await interaction.response.defer(ephemeral=True)
    data["reminder_channels"][str(interaction.guild.id)] = interaction.channel.id
    db_set_reminder_channel(interaction.guild.id, interaction.channel.id)
    save_data(data)
    await interaction.followup.send("✅ 已設定", ephemeral=True)

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

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is alive!"

def run_web():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 10000)))

threading.Thread(target=run_web).start()

bot.run(os.getenv("DISCORD_TOKEN"))
