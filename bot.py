"""
てちょうAI - 手帳持ちの集い 統合サポートBOT
=============================================
機能:
  1. AI自動返信 — 指定チャンネルの投稿に3〜30分後にDeepSeek AIが返信
  2. /ai スラッシュコマンド — 直接AIに話しかける
  3. ウェルカム案内 — サーバー参加時にあいさつチャンネルへ歓迎メッセージ送信
  4. 自己紹介リプライ — 自己紹介チャンネルへの投稿を検知→やさしくチャンネル案内
  5. 今日の話題 — 毎日定時に話題を投稿してスレッド作成
  6. 共感リアクション — 感情キーワード検知→絵文字リアクション
  7. キープアライブ — Koyeb用の自己pingでスリープ防止
  8. マナーセルフチェック — Lv1メンバーがマナークイズに合格するとLv2ロールを自動付与

環境変数:
  DISCORD_TOKEN    — Discord BOTトークン
  DEEPSEEK_API_KEY — DeepSeek APIキー
  GUILD_ID         — サーバーID
  KOYEB_URL        — Koyebの公開URL（任意、キープアライブ用）
"""

import discord
from discord import app_commands
from openai import OpenAI
import asyncio
import os
import json
import random
import datetime
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import urllib.request

# ============================================================
# 環境変数
# ============================================================
DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
DEEPSEEK_API_KEY = os.environ["DEEPSEEK_API_KEY"]
GUILD_ID = int(os.environ["GUILD_ID"])
KOYEB_URL = os.environ.get("KOYEB_URL", "")

# ============================================================
# config.json 読み込み
# ============================================================
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()

# ============================================================
# ヘルスチェックサーバー（Koyeb用 port 8000）
# ============================================================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass

health_server = HTTPServer(("0.0.0.0", 8000), HealthHandler)
threading.Thread(target=health_server.serve_forever, daemon=True).start()
print("ヘルスチェックサーバー起動: port 8000")

# ============================================================
# Discord クライアント設定
# ============================================================
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.messages = True
intents.members = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

# ============================================================
# DeepSeek AI クライアント
# ============================================================
SYSTEM_PROMPT = """あなたは「手帳持ちの集い」というDiscordサーバーのサポートBotです。
以下のルールを必ず守ってください：
・返信は短く、1〜3文以内にまとめる
・どんなにネガティブな内容でも、ポジティブで中立な視点で返す
・共感を示しつつ、押しつけがましくならない
・断定や否定はせず、当たり障りのない温かい言葉を選ぶ
・絵文字は使わない"""

deepseek_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)


async def generate_reply(message_content: str, history: list = None) -> str:
    if history is None:
        history = []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": message_content})

    response = deepseek_client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        max_tokens=200,
    )
    return response.choices[0].message.content


# ============================================================
# 機能1: AI自動返信（3〜30分ランダム遅延）
# ============================================================
WATCH_CHANNEL_IDS = [
    1300764527109079071,
]

pending_tasks: dict[int, asyncio.Task] = {}


async def delayed_reply(msg: discord.Message):
    if random.random() >= 0.3:  # 70%はスキップ
        return
    delay = random.randint(600, 3600)  # 10分〜60分
    print(f"  自動返信予約: #{msg.channel.name} - {delay}秒後に返信予定")
    await asyncio.sleep(delay)
    try:
        history = []
        async for m in msg.channel.history(limit=6, before=msg):
            if m.author == client.user:
                history.insert(0, {"role": "assistant", "content": m.content})
            elif not m.author.bot:
                history.insert(0, {"role": "user", "content": m.content})

        print(f"  自動返信: #{msg.channel.name} - {msg.author}: {msg.content[:50]}")
        reply_text = await generate_reply(msg.content, history)
        await msg.channel.send(reply_text)
        print(f"  → 返信しました")
    except asyncio.CancelledError:
        pass
    except Exception as e:
        print(f"自動返信エラー: {e}")


# ============================================================
# 機能2: /ai スラッシュコマンド（既存機能）
# ============================================================
@tree.command(name="ai", description="てちょうAIに話しかける")
@app_commands.describe(message="AIへのメッセージ")
async def ai_command(interaction: discord.Interaction, message: str):
    await interaction.response.defer()
    try:
        reply_text = await generate_reply(message)
        await interaction.followup.send(f"> {message}\n\n{reply_text}")
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {e}")


# ============================================================
# 機能3: サーバー参加時ウェルカム（on_member_join）
# ============================================================
join_cfg = config.get("welcome_on_join", {})
JOIN_WELCOME_ENABLED = join_cfg.get("enabled", False)
JOIN_WELCOME_CHANNEL_ID = join_cfg.get("channel_id")
JOIN_WELCOME_MESSAGES = join_cfg.get("messages", [
    "やあ、{username}。ピザ持ってきたよね？ 🍕\n冗談だよ！ゆっくりしていってね。",
])


async def handle_member_join(member: discord.Member):
    """サーバー参加時にあいさつチャンネルへ歓迎メッセージを送る"""
    if not JOIN_WELCOME_ENABLED or JOIN_WELCOME_CHANNEL_ID is None:
        return

    # 3〜15秒のランダム遅延（即レス感を消す）
    await asyncio.sleep(random.uniform(3, 15))

    try:
        channel = client.get_channel(JOIN_WELCOME_CHANNEL_ID)
        if channel is None:
            print(f"  参加ウェルカム: チャンネル {JOIN_WELCOME_CHANNEL_ID} が見つかりません")
            return

        template = random.choice(JOIN_WELCOME_MESSAGES)
        welcome_text = template.replace("{username}", member.display_name)
        welcome_text = welcome_text.replace("{mention}", member.mention)

        await channel.send(welcome_text)
        print(f"  参加ウェルカム送信: {member.display_name}")
    except Exception as e:
        print(f"参加ウェルカムエラー: {e}")


# ============================================================
# 機能4: 自己紹介リプライ（既存のウェルカム案内）
# ============================================================
welcome_cfg = config.get("welcome", {})
WELCOME_ENABLED = welcome_cfg.get("enabled", False)
WELCOME_CHANNEL_ID = welcome_cfg.get("watch_channel_id")
WELCOME_MSG_TEMPLATE = welcome_cfg.get("message", "ようこそ！")
WELCOME_CHAT_CH = welcome_cfg.get("chat_channel_id")
WELCOME_WORRY_CH = welcome_cfg.get("worry_channel_id")
WELCOME_VC_CH = welcome_cfg.get("vc_channel_id")


async def handle_welcome(msg: discord.Message):
    """自己紹介チャンネルへの投稿を検知して案内メッセージを送る"""
    if not WELCOME_ENABLED or WELCOME_CHANNEL_ID is None:
        return
    if msg.channel.id != WELCOME_CHANNEL_ID:
        return

    await asyncio.sleep(random.uniform(3, 8))

    try:
        welcome_text = WELCOME_MSG_TEMPLATE.replace("{username}", msg.author.display_name)
        welcome_text = welcome_text.replace("{chat_channel}", str(WELCOME_CHAT_CH or "雑談"))
        welcome_text = welcome_text.replace("{worry_channel}", str(WELCOME_WORRY_CH or "悩み相談"))
        welcome_text = welcome_text.replace("{vc_channel}", str(WELCOME_VC_CH or "VC"))
        await msg.reply(welcome_text, mention_author=False)
        print(f"  ウェルカム送信: {msg.author.display_name}")
    except Exception as e:
        print(f"ウェルカムエラー: {e}")


# ============================================================
# 機能5: 今日の話題
# ============================================================
topic_cfg = config.get("daily_topic", {})
TOPIC_ENABLED = topic_cfg.get("enabled", False)
TOPIC_CHANNEL_ID = topic_cfg.get("channel_id")
TOPIC_HOUR = topic_cfg.get("hour", 12)
TOPIC_MINUTE = topic_cfg.get("minute", 0)
TOPICS = topic_cfg.get("topics", [])

topic_posted_today = False
used_topic_indices = []


async def daily_topic_loop():
    """毎分チェックして、指定時刻に今日の話題を投稿"""
    global topic_posted_today, used_topic_indices

    await client.wait_until_ready()

    if not TOPIC_ENABLED or TOPIC_CHANNEL_ID is None:
        print("今日の話題: 無効（チャンネル未設定）")
        return

    print(f"今日の話題: 有効（毎日 {TOPIC_HOUR}:{TOPIC_MINUTE:02d} JST に投稿）")

    while not client.is_closed():
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))  # JST

        # 日付が変わったらリセット
        if now.hour == 0 and now.minute == 0:
            topic_posted_today = False

        # 投稿時刻チェック
        if (now.hour == TOPIC_HOUR and now.minute == TOPIC_MINUTE
                and not topic_posted_today and TOPICS):
            topic_posted_today = True

            # トピック選択（全部使い切ったらリセット）
            available = [i for i in range(len(TOPICS)) if i not in used_topic_indices]
            if not available:
                used_topic_indices.clear()
                available = list(range(len(TOPICS)))

            idx = random.choice(available)
            used_topic_indices.append(idx)
            topic = TOPICS[idx]

            try:
                channel = client.get_channel(TOPIC_CHANNEL_ID)
                if channel:
                    today_str = now.strftime("%m/%d")

                    embed = discord.Embed(
                        title="📒 今日の話題",
                        description=topic,
                        color=0x5865F2,
                    )
                    embed.set_footer(text="答えなくても読むだけでもOKです 🌱")

                    sent_msg = await channel.send(embed=embed)
                    await sent_msg.create_thread(
                        name=f"今日の話題 - {today_str}",
                        auto_archive_duration=1440
                    )
                    print(f"  今日の話題投稿: {topic[:30]}")
            except Exception as e:
                print(f"今日の話題エラー: {e}")

        await asyncio.sleep(60)


# ============================================================
# 機能5.5: チャンネル表示リマインド（毎月指定日時に自動投稿）
# ============================================================
reminder_cfg = config.get("channel_reminder", {})
REMINDER_ENABLED = reminder_cfg.get("enabled", False)
REMINDER_CHANNEL_IDS = reminder_cfg.get("channel_ids", [])
REMINDER_DAY = reminder_cfg.get("day", 1)
REMINDER_HOUR = reminder_cfg.get("hour", 20)
REMINDER_MINUTE = reminder_cfg.get("minute", 0)
REMINDER_IMAGE_URL = reminder_cfg.get("image_url", "")
REMINDER_TITLE = reminder_cfg.get("title", "📌 チャンネル表示設定のご案内")
REMINDER_MESSAGE = reminder_cfg.get("message", "")

reminder_posted_this_month = False


def build_reminder_embed() -> discord.Embed:
    """チャンネル表示リマインド用のEmbedを作成"""
    embed = discord.Embed(
        title=REMINDER_TITLE,
        description=REMINDER_MESSAGE,
        color=0x5865F2,
    )
    if REMINDER_IMAGE_URL:
        embed.set_image(url=REMINDER_IMAGE_URL)
    return embed


async def send_channel_reminder(channel_ids: list = None) -> int:
    """指定チャンネル（省略時はREMINDER_CHANNEL_IDS）にリマインドEmbedを送信し、成功数を返す"""
    target_ids = channel_ids if channel_ids is not None else REMINDER_CHANNEL_IDS
    success_count = 0

    for channel_id in target_ids:
        try:
            channel = client.get_channel(channel_id)
            if channel is None:
                print(f"  チャンネル表示リマインド: チャンネル {channel_id} が見つかりません")
                continue
            embed = build_reminder_embed()
            await channel.send(embed=embed)
            success_count += 1
            print(f"  チャンネル表示リマインド送信: #{channel.name}")
        except Exception as e:
            print(f"チャンネル表示リマインドエラー: {e}")

    return success_count


async def monthly_reminder_loop():
    """毎分チェックして、毎月指定日時にチャンネル表示リマインドを投稿"""
    global reminder_posted_this_month

    await client.wait_until_ready()

    if not REMINDER_ENABLED or not REMINDER_CHANNEL_IDS:
        print("チャンネル表示リマインド: 無効（チャンネル未設定）")
        return

    print(f"チャンネル表示リマインド: 有効（毎月{REMINDER_DAY}日 {REMINDER_HOUR}:{REMINDER_MINUTE:02d} JST に投稿）")

    while not client.is_closed():
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))  # JST

        # 対象日を過ぎたらリセット（翌月に備える）
        if now.day != REMINDER_DAY:
            reminder_posted_this_month = False

        # 投稿時刻チェック
        if (now.day == REMINDER_DAY and now.hour == REMINDER_HOUR
                and now.minute == REMINDER_MINUTE and not reminder_posted_this_month):
            reminder_posted_this_month = True
            await send_channel_reminder()

        await asyncio.sleep(60)


# ============================================================
# 機能6: 共感リアクション
# ============================================================
empathy_cfg = config.get("empathy_reaction", {})
EMPATHY_ENABLED = empathy_cfg.get("enabled", False)
EMPATHY_CHANNEL_IDS = empathy_cfg.get("watch_channel_ids", [])
REACTIONS = empathy_cfg.get("reactions", {})
POSITIVE_KW = empathy_cfg.get("positive_keywords", [])
SUPPORT_KW = empathy_cfg.get("support_keywords", [])
EMPATHY_KW = empathy_cfg.get("empathy_keywords", [])


async def handle_empathy_reaction(msg: discord.Message):
    """メッセージ内のキーワードを検知してリアクションを付ける"""
    if not EMPATHY_ENABLED:
        return

    # watch_channel_ids が空なら全チャンネル対象
    if EMPATHY_CHANNEL_IDS and msg.channel.id not in EMPATHY_CHANNEL_IDS:
        return

    content = msg.content
    if len(content) < 5:
        return

    matched_category = None

    # つらい・しんどいメッセージ（最優先）
    for kw in SUPPORT_KW:
        if kw in content:
            matched_category = "support"
            break

    # ポジティブなメッセージ
    if not matched_category:
        for kw in POSITIVE_KW:
            if kw in content:
                matched_category = "positive"
                break

    # 共感系メッセージ
    if not matched_category:
        for kw in EMPATHY_KW:
            if kw in content:
                matched_category = "empathy"
                break

    if matched_category and matched_category in REACTIONS:
        await asyncio.sleep(random.uniform(5, 30))
        try:
            emoji = random.choice(REACTIONS[matched_category])
            await msg.add_reaction(emoji)
            print(f"  共感リアクション: {matched_category} → {emoji} ({msg.content[:30]})")
        except Exception:
            pass


# ============================================================
# 機能7: キープアライブ（Koyebスリープ防止）
# ============================================================
async def keepalive_loop():
    """5分ごとに自分自身にHTTPリクエストを送ってスリープ防止"""
    await client.wait_until_ready()

    if not KOYEB_URL:
        print("キープアライブ: 無効（KOYEB_URL未設定）")
        return

    print(f"キープアライブ: 有効（{KOYEB_URL}）")

    loop = asyncio.get_event_loop()
    while not client.is_closed():
        try:
            await loop.run_in_executor(None, lambda: urllib.request.urlopen(KOYEB_URL, timeout=10))
        except Exception:
            pass
        await asyncio.sleep(300)  # 5分間隔


# ============================================================
# 機能8: マナーセルフチェック（Lv1→Lv2昇格）
# ============================================================
selfcheck_cfg = config.get("self_check", {})
SELFCHECK_ENABLED = selfcheck_cfg.get("enabled", False)
LV1_ROLE_ID = selfcheck_cfg.get("lv1_role_id")
LV2_ROLE_ID = selfcheck_cfg.get("lv2_role_id")
LV3_ROLE_ID = selfcheck_cfg.get("lv3_role_id")
SELFCHECK_LOG_CHANNEL_ID = selfcheck_cfg.get("log_channel_id")
PASS_SCORE = selfcheck_cfg.get("pass_score", 8)
AUTO_PROMOTE = selfcheck_cfg.get("auto_promote", True)
QUESTIONS = selfcheck_cfg.get("questions", [])

QUIZ_TIMEOUT_SECONDS = 600  # セルフチェック全体の制限時間（10分）


class QuizState:
    """1人分のセルフチェックの進行状況（合否判定まで使い捨て）"""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.index = 0
        self.score = 0
        self.start_time = time.monotonic()
        self.message = None  # discord.Message、タイムアウト時の編集用


def format_question(state: QuizState) -> str:
    q = QUESTIONS[state.index]
    return (
        f"📋 マナーセルフチェック（{state.index + 1}/{len(QUESTIONS)}問目）\n"
        f"現在のスコア: {state.score}点\n\n"
        f"**Q{state.index + 1}. {q}**\n\n"
        "下のボタンで回答してね"
    )


def format_selfcheck_result(state: QuizState, passed: bool, promote_note: str) -> str:
    lines = [
        "🎉 セルフチェック合格です！おめでとう！" if passed else "😢 今回は合格ラインに届きませんでした。",
        f"スコア: {state.score} / {len(QUESTIONS)}点（合格ライン: {PASS_SCORE}点）",
    ]
    if passed:
        if promote_note:
            lines.append(promote_note)
    else:
        lines.append("焦らなくて大丈夫です。何度でも再挑戦できるので、落ち着いたときにまた押してくださいね🌸")
    return "\n".join(lines)


async def send_selfcheck_log(interaction: discord.Interaction, state: QuizState, passed: bool, promote_note: str):
    if SELFCHECK_LOG_CHANNEL_ID is None:
        return
    channel = client.get_channel(SELFCHECK_LOG_CHANNEL_ID)
    if channel is None:
        print(f"  セルフチェック: ログチャンネル {SELFCHECK_LOG_CHANNEL_ID} が見つかりません")
        return
    embed = discord.Embed(
        title="✅ セルフチェック合格" if passed else "❌ セルフチェック不合格",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="対象者", value=f"{interaction.user.mention}（{interaction.user}）", inline=False)
    embed.add_field(name="スコア", value=f"{state.score} / {len(QUESTIONS)}点（合格ライン {PASS_SCORE}点）", inline=False)
    if promote_note:
        embed.add_field(name="ロール処理", value=promote_note, inline=False)
    embed.set_footer(text=f"ユーザーID: {interaction.user.id}")
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print(f"  セルフチェック: ログチャンネル {SELFCHECK_LOG_CHANNEL_ID} への送信権限がありません")


async def finish_selfcheck(interaction: discord.Interaction, state: QuizState):
    passed = state.score >= PASS_SCORE
    promote_note = ""

    if passed and AUTO_PROMOTE:
        guild = interaction.guild
        member = interaction.user
        lv2_role = guild.get_role(LV2_ROLE_ID) if LV2_ROLE_ID else None
        lv1_role = guild.get_role(LV1_ROLE_ID) if LV1_ROLE_ID else None

        if lv2_role is None:
            promote_note = "⚠️ self_check.lv2_role_id の設定が正しくないため、ロールを付与できませんでした。手動付与をお願いします。"
        else:
            try:
                await member.add_roles(lv2_role, reason="セルフチェック合格")
                added = True
            except discord.Forbidden:
                added = False

            removed = True
            if added and lv1_role is not None and lv1_role in member.roles:
                try:
                    await member.remove_roles(lv1_role, reason="セルフチェック合格")
                except discord.Forbidden:
                    removed = False

            if not added:
                promote_note = (
                    "⚠️ Lv2ロールの付与に失敗しました（botのロール位置がLv2より下にある可能性があります）。"
                    "手動付与待ちの状態です。管理人にお声がけください。"
                )
            elif not removed:
                promote_note = (
                    "🎉 Lv2ロールは付与できましたが、Lv1ロールの解除に失敗しました（権限不足）。"
                    "Lv1ロールの手動解除をお願いします。"
                )
            else:
                promote_note = "🎉 Lv2ロールを自動付与しました！"
    elif passed:
        promote_note = "ℹ️ self_check.auto_promote=false のため、ロール変更は行われていません（ログのみ）。"

    result_text = format_selfcheck_result(state, passed, promote_note)
    await interaction.response.edit_message(content=result_text, view=None)
    await send_selfcheck_log(interaction, state, passed, promote_note)


async def advance_selfcheck(interaction: discord.Interaction, state: QuizState):
    if state.index < len(QUESTIONS):
        view = SelfCheckAnswerView(state)
        await interaction.response.edit_message(content=format_question(state), view=view)
        state.message = await interaction.original_response()
    else:
        await finish_selfcheck(interaction, state)


class SelfCheckAnswerView(discord.ui.View):
    def __init__(self, state: QuizState):
        remaining = QUIZ_TIMEOUT_SECONDS - (time.monotonic() - state.start_time)
        super().__init__(timeout=max(remaining, 1))
        self.state = state

    async def on_timeout(self):
        if self.state.message is None:
            return
        try:
            await self.state.message.edit(
                content="⌛ セルフチェックの制限時間（10分）が過ぎました。もう一度パネルのボタンから挑戦してください。",
                view=None,
            )
        except discord.HTTPException:
            pass

    async def _answer(self, interaction: discord.Interaction, is_yes: bool):
        if interaction.user.id != self.state.user_id:
            await interaction.response.send_message(
                "これはあなた専用のセルフチェックです。自分のパネルから挑戦してくださいね。", ephemeral=True
            )
            return
        if is_yes:
            self.state.score += 1
        self.state.index += 1
        self.stop()
        await advance_selfcheck(interaction, self.state)

    @discord.ui.button(label="はい", emoji="✅", style=discord.ButtonStyle.success, custom_id="selfcheck_quiz_yes")
    async def yes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, True)

    @discord.ui.button(label="いいえ", emoji="❌", style=discord.ButtonStyle.secondary, custom_id="selfcheck_quiz_no")
    async def no_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._answer(interaction, False)


class SelfCheckPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent view（再起動後も有効）

    @discord.ui.button(label="セルフチェックを始める", emoji="📋", style=discord.ButtonStyle.primary, custom_id="selfcheck_start_button")
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("このボタンはサーバー内でのみ使えます。", ephemeral=True)
            return
        if not QUESTIONS:
            await interaction.response.send_message(
                "設問が未設定です。config.json の self_check.questions を確認してください。", ephemeral=True
            )
            return

        member = interaction.user
        lv2_role = interaction.guild.get_role(LV2_ROLE_ID) if LV2_ROLE_ID else None
        lv3_role = interaction.guild.get_role(LV3_ROLE_ID) if LV3_ROLE_ID else None
        already_promoted = (lv2_role is not None and lv2_role in member.roles) or (
            lv3_role is not None and lv3_role in member.roles
        )
        if already_promoted:
            await interaction.response.send_message(
                "すでにLv2以上です🌸（Lv2ロールまたはLv3〈副管理人〉ロールをお持ちの方は対象外）セルフチェックは不要ですよ。",
                ephemeral=True,
            )
            return

        state = QuizState(member.id)
        view = SelfCheckAnswerView(state)
        await interaction.response.send_message(content=format_question(state), view=view, ephemeral=True)
        state.message = await interaction.original_response()


# ============================================================
# イベントハンドラ
# ============================================================
@client.event
async def setup_hook():
    client.add_view(SelfCheckPanelView())  # 再起動後もセルフチェックパネルのボタンを有効化


@client.event
async def on_ready():
    print(f"Bot起動: {client.user}")
    print(f"サーバー: {GUILD_ID}")
    print("─" * 40)
    print(f"  AI自動返信: {len(WATCH_CHANNEL_IDS)}チャンネル監視中（3〜30分遅延）")
    print(f"  参加ウェルカム: {'ON' if JOIN_WELCOME_ENABLED and JOIN_WELCOME_CHANNEL_ID else 'OFF'}")
    print(f"  自己紹介リプライ: {'ON' if WELCOME_ENABLED and WELCOME_CHANNEL_ID else 'OFF'}")
    print(f"  今日の話題: {'ON' if TOPIC_ENABLED and TOPIC_CHANNEL_ID else 'OFF'}")
    print(f"  チャンネル表示リマインド: {'ON' if REMINDER_ENABLED and REMINDER_CHANNEL_IDS else 'OFF'}")
    print(f"  共感リアクション: {'ON' if EMPATHY_ENABLED else 'OFF'}")
    print(f"  マナーセルフチェック: {'ON' if SELFCHECK_ENABLED else 'OFF'}")
    print(f"  キープアライブ: {'ON' if KOYEB_URL else 'OFF'}")
    print("─" * 40)

    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print("スラッシュコマンド登録完了")

    # バックグラウンドタスク開始
    if TOPIC_ENABLED and TOPIC_CHANNEL_ID:
        client.loop.create_task(daily_topic_loop())
    if REMINDER_ENABLED and REMINDER_CHANNEL_IDS:
        client.loop.create_task(monthly_reminder_loop())
    client.loop.create_task(keepalive_loop())


@client.event
async def on_member_join(member: discord.Member):
    """サーバーに新メンバーが参加したときに発火"""
    await handle_member_join(member)


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    # AI自動返信
    if msg.channel.id in WATCH_CHANNEL_IDS:
        if msg.channel.id in pending_tasks:
            pending_tasks[msg.channel.id].cancel()
        pending_tasks[msg.channel.id] = asyncio.ensure_future(delayed_reply(msg))

    # 自己紹介リプライ
    await handle_welcome(msg)

    # 共感リアクション
    await handle_empathy_reaction(msg)


# ============================================================
# スラッシュ管理コマンド
# ============================================================
@tree.command(name="topic", description="今日の話題を手動で投稿する")
async def manual_topic(interaction: discord.Interaction):
    if not TOPICS:
        await interaction.response.send_message("話題リストが空です", ephemeral=True)
        return

    topic = random.choice(TOPICS)
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_str = now.strftime("%m/%d")

    embed = discord.Embed(
        title="📒 今日の話題",
        description=topic,
        color=0x5865F2,
    )
    embed.set_footer(text="答えなくても読むだけでもOKです 🌱")

    await interaction.response.send_message(embed=embed)
    sent_msg = await interaction.original_response()
    try:
        await sent_msg.create_thread(
            name=f"今日の話題 - {today_str}",
            auto_archive_duration=1440
        )
    except Exception:
        pass


@tree.command(name="channel_reminder", description="チャンネル表示設定の案内を今すぐ投稿する（管理者用）")
async def channel_reminder_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "このコマンドはサーバー管理権限を持つ方のみ実行できます",
            ephemeral=True
        )
        return

    if not REMINDER_CHANNEL_IDS:
        await interaction.response.send_message(
            "チャンネル表示リマインドの投稿先が設定されていません。config.json の channel_reminder を確認してください",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    success_count = await send_channel_reminder()
    await interaction.followup.send(
        f"チャンネル表示リマインドを {success_count} 件のチャンネルに投稿しました",
        ephemeral=True
    )


@tree.command(name="status", description="てちょうAIのステータスを表示")
async def status_command(interaction: discord.Interaction):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    uptime = now.strftime("%Y/%m/%d %H:%M JST")

    embed = discord.Embed(title="📒 てちょうAI ステータス", color=0x5865F2)
    embed.add_field(name="現在時刻", value=uptime, inline=False)
    embed.add_field(name="AI自動返信", value=f"{len(WATCH_CHANNEL_IDS)}ch監視中（3〜30分遅延）", inline=True)
    embed.add_field(name="参加ウェルカム", value="ON" if JOIN_WELCOME_ENABLED and JOIN_WELCOME_CHANNEL_ID else "OFF", inline=True)
    embed.add_field(name="自己紹介リプライ", value="ON" if WELCOME_ENABLED and WELCOME_CHANNEL_ID else "OFF", inline=True)
    embed.add_field(name="今日の話題", value="ON" if TOPIC_ENABLED and TOPIC_CHANNEL_ID else "OFF", inline=True)
    embed.add_field(name="チャンネル表示リマインド", value="ON" if REMINDER_ENABLED and REMINDER_CHANNEL_IDS else "OFF", inline=True)
    embed.add_field(name="共感リアクション", value="ON" if EMPATHY_ENABLED else "OFF", inline=True)
    embed.add_field(name="マナーセルフチェック", value="ON" if SELFCHECK_ENABLED else "OFF", inline=True)
    embed.add_field(name="キープアライブ", value="ON" if KOYEB_URL else "OFF", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="reload", description="設定ファイルを再読み込みする")
async def reload_command(interaction: discord.Interaction):
    global config
    config = load_config()
    await interaction.response.send_message("設定を再読み込みしました", ephemeral=True)


@tree.command(name="test_welcome", description="参加ウェルカムのテスト（自分を新規参加者として送信）")
async def test_welcome_command(interaction: discord.Interaction):
    if not JOIN_WELCOME_ENABLED or JOIN_WELCOME_CHANNEL_ID is None:
        await interaction.response.send_message(
            "参加ウェルカムが無効です。config.json の welcome_on_join を確認してください",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"テスト送信中… → <#{JOIN_WELCOME_CHANNEL_ID}>",
        ephemeral=True
    )
    await handle_member_join(interaction.user)


@tree.command(name="セルフチェック設置", description="マナーセルフチェックのパネルを設置します（管理者専用）")
async def setup_selfcheck_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
        return
    if not SELFCHECK_ENABLED:
        await interaction.response.send_message(
            "セルフチェック機能が無効です。config.json の self_check.enabled を確認してください。", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="📋 マナーセルフチェック",
        description=(
            f"Lv2への昇格には、マナーに関する全{len(QUESTIONS)}問のセルフチェックに答えてね😊\n"
            f"{PASS_SCORE}点以上で合格すると、その場でLv2ロールが自動で付きます。\n"
            "不合格でも何度でも挑戦できるので、気軽にボタンを押してみてください🌸"
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="レベルについて",
        value=(
            "**Lv1（新規・様子見）**: VC参加・発言・読み上げbotの利用はOK。画像/ファイル添付や音楽botの操作はまだ不可\n"
            "**Lv2（一般）**: 画像/ファイル添付、音楽bot等のコマンドも利用可能に\n"
            "**Lv3（副管理人）**: Lv2の権限に加えて、副管理人としてサーバー運営をサポート"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, view=SelfCheckPanelView())


# ============================================================
# BOT起動
# ============================================================
client.run(DISCORD_TOKEN)
