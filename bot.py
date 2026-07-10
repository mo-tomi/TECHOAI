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
from openai import AsyncOpenAI
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

# 設定値は必ずこれらの関数経由で毎回 config から取得する（/reload が即座に反映されるように、
# モジュール読み込み時に値をコピーして固定してしまわないこと）
def cfg_ai() -> dict:
    return config.get("ai", {})

def cfg_welcome_on_join() -> dict:
    return config.get("welcome_on_join", {})

def cfg_welcome() -> dict:
    return config.get("welcome", {})

def cfg_daily_topic() -> dict:
    return config.get("daily_topic", {})

def cfg_channel_reminder() -> dict:
    return config.get("channel_reminder", {})

def cfg_empathy() -> dict:
    return config.get("empathy_reaction", {})

def cfg_self_check() -> dict:
    return config.get("self_check", {})

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

deepseek_client = AsyncOpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com"
)


async def generate_reply(message_content: str, history: list = None) -> str | None:
    """DeepSeek APIに問い合わせて返信文を生成する。失敗時は1回だけ2秒後にリトライし、
    それでも失敗したら例外を投げずNoneを返す（呼び出し側は返信をスキップしてログ出力）"""
    if history is None:
        history = []
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": message_content})

    for attempt in range(2):
        try:
            response = await deepseek_client.chat.completions.create(
                model=cfg_ai().get("model", "deepseek-chat"),
                messages=messages,
                max_tokens=200,
                timeout=30,
            )
            return response.choices[0].message.content
        except Exception as e:
            if attempt == 0:
                print(f"generate_reply エラー（リトライします）: {e}")
                await asyncio.sleep(2)
            else:
                print(f"generate_reply エラー（リトライ失敗）: {e}")
                return None


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
        if reply_text is None:
            print(f"  → AI応答が取得できなかったため返信をスキップしました")
            return
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
    reply_text = await generate_reply(message)
    if reply_text is None:
        print(f"  /ai コマンド: AI応答が取得できなかったため返信をスキップしました（{interaction.user}）")
        await interaction.followup.send("ごめんなさい、今AIの応答が取得できませんでした。少し時間をおいて試してください。")
        return
    await interaction.followup.send(f"> {message}\n\n{reply_text}")


# ============================================================
# 機能3: サーバー参加時ウェルカム（on_member_join）
# ============================================================
async def handle_member_join(member: discord.Member):
    """サーバー参加時にあいさつチャンネルへ歓迎メッセージを送る"""
    cfg = cfg_welcome_on_join()
    enabled = cfg.get("enabled", False)
    channel_id = cfg.get("channel_id")
    if not enabled or channel_id is None:
        return

    # 3〜15秒のランダム遅延（即レス感を消す）
    await asyncio.sleep(random.uniform(3, 15))

    try:
        channel = client.get_channel(channel_id)
        if channel is None:
            print(f"  参加ウェルカム: チャンネル {channel_id} が見つかりません")
            return

        messages = cfg.get("messages", [
            "やあ、{username}。ピザ持ってきたよね？ 🍕\n冗談だよ！ゆっくりしていってね。",
        ])
        template = random.choice(messages)
        welcome_text = template.replace("{username}", member.display_name)
        welcome_text = welcome_text.replace("{mention}", member.mention)

        await channel.send(welcome_text)
        print(f"  参加ウェルカム送信: {member.display_name}")
    except Exception as e:
        print(f"参加ウェルカムエラー: {e}")


# ============================================================
# 機能4: 自己紹介リプライ（既存のウェルカム案内）
# ============================================================
async def handle_welcome(msg: discord.Message):
    """自己紹介チャンネルへの投稿を検知して案内メッセージを送る"""
    cfg = cfg_welcome()
    enabled = cfg.get("enabled", False)
    channel_id = cfg.get("watch_channel_id")
    if not enabled or channel_id is None:
        return
    if msg.channel.id != channel_id:
        return

    await asyncio.sleep(random.uniform(3, 8))

    try:
        template = cfg.get("message", "ようこそ！")
        welcome_text = template.replace("{username}", msg.author.display_name)
        welcome_text = welcome_text.replace("{chat_channel}", str(cfg.get("chat_channel_id") or "雑談"))
        welcome_text = welcome_text.replace("{worry_channel}", str(cfg.get("worry_channel_id") or "悩み相談"))
        welcome_text = welcome_text.replace("{vc_channel}", str(cfg.get("vc_channel_id") or "VC"))
        await msg.reply(welcome_text, mention_author=False)
        print(f"  ウェルカム送信: {msg.author.display_name}")
    except Exception as e:
        print(f"ウェルカムエラー: {e}")


# ============================================================
# 機能5: 今日の話題
# ============================================================
topic_posted_today = False
used_topic_indices = []


async def daily_topic_loop():
    """毎分チェックして、指定時刻に今日の話題を投稿"""
    global topic_posted_today, used_topic_indices

    await client.wait_until_ready()
    print("今日の話題: 監視ループ開始")

    while not client.is_closed():
        cfg = cfg_daily_topic()
        enabled = cfg.get("enabled", False)
        channel_id = cfg.get("channel_id")
        hour = cfg.get("hour", 12)
        minute = cfg.get("minute", 0)
        topics = cfg.get("topics", [])

        if enabled and channel_id is not None:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))  # JST

            # 日付が変わったらリセット
            if now.hour == 0 and now.minute == 0:
                topic_posted_today = False

            # 投稿時刻チェック
            if now.hour == hour and now.minute == minute and not topic_posted_today and topics:
                topic_posted_today = True

                # トピック選択（全部使い切ったらリセット）
                available = [i for i in range(len(topics)) if i not in used_topic_indices]
                if not available:
                    used_topic_indices.clear()
                    available = list(range(len(topics)))

                idx = random.choice(available)
                used_topic_indices.append(idx)
                topic = topics[idx]

                try:
                    channel = client.get_channel(channel_id)
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
reminder_posted_this_month = False


def build_reminder_embed() -> discord.Embed:
    """チャンネル表示リマインド用のEmbedを作成"""
    cfg = cfg_channel_reminder()
    embed = discord.Embed(
        title=cfg.get("title", "📌 チャンネル表示設定のご案内"),
        description=cfg.get("message", ""),
        color=0x5865F2,
    )
    image_url = cfg.get("image_url", "")
    if image_url:
        embed.set_image(url=image_url)
    return embed


async def send_channel_reminder(channel_ids: list = None) -> int:
    """指定チャンネル（省略時はconfigのchannel_ids）にリマインドEmbedを送信し、成功数を返す"""
    target_ids = channel_ids if channel_ids is not None else cfg_channel_reminder().get("channel_ids", [])
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
    print("チャンネル表示リマインド: 監視ループ開始")

    while not client.is_closed():
        cfg = cfg_channel_reminder()
        enabled = cfg.get("enabled", False)
        channel_ids = cfg.get("channel_ids", [])
        day = cfg.get("day", 1)
        hour = cfg.get("hour", 20)
        minute = cfg.get("minute", 0)

        if enabled and channel_ids:
            now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))  # JST

            # 対象日を過ぎたらリセット（翌月に備える）
            if now.day != day:
                reminder_posted_this_month = False

            # 投稿時刻チェック
            if now.day == day and now.hour == hour and now.minute == minute and not reminder_posted_this_month:
                reminder_posted_this_month = True
                await send_channel_reminder(channel_ids)

        await asyncio.sleep(60)


# ============================================================
# 機能6: 共感リアクション
# ============================================================
async def send_crisis_alert(msg: discord.Message, matched_keyword: str):
    """危機ワード検知の通知Embedを管理者用チャンネルへ送る（本人には何も送らない）"""
    channel_id = cfg_empathy().get("crisis_alert_channel_id")
    if channel_id is None:
        print(f"  危機ワード検知: crisis_alert_channel_id が未設定のため通知をスキップ（{msg.author}: {matched_keyword}）")
        return
    channel = client.get_channel(channel_id)
    if channel is None:
        print(f"  危機ワード検知: 通知先チャンネル {channel_id} が見つかりません")
        return
    embed = discord.Embed(
        title="⚠️ 危機ワード検知",
        description=f"[メッセージへ移動する]({msg.jump_url})",
        color=0xE74C3C,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="投稿者", value=f"{msg.author.mention}（{msg.author}）", inline=False)
    embed.add_field(name="チャンネル", value=msg.channel.mention, inline=False)
    embed.add_field(name="検知ワード", value=matched_keyword, inline=False)
    if msg.content:
        embed.add_field(name="本文（抜粋）", value=msg.content[:200], inline=False)
    embed.set_footer(text=f"ユーザーID: {msg.author.id}")
    try:
        await channel.send(embed=embed)
        print(f"  危機ワード通知送信: {msg.author}（{matched_keyword}）")
    except discord.Forbidden:
        print(f"  危機ワード検知: 通知先チャンネル {channel_id} への送信権限がありません")


async def handle_crisis_check(msg: discord.Message) -> bool:
    """自傷・希死念慮を示す危機ワードを検知したら管理者用チャンネルへ通知する。
    プレッシャーを与えないため本人への自動送信（リアクション含む）は一切行わない。
    共感リアクションのON/OFFやwatch_channel_idsに関わらず全チャンネルで常時監視する"""
    crisis_kw = cfg_empathy().get("crisis_keywords", [])
    content = msg.content
    for kw in crisis_kw:
        if kw in content:
            await send_crisis_alert(msg, kw)
            return True
    return False


async def handle_empathy_reaction(msg: discord.Message):
    """メッセージ内のキーワードを検知してリアクションを付ける"""
    cfg = cfg_empathy()
    if not cfg.get("enabled", False):
        return

    watch_channel_ids = cfg.get("watch_channel_ids", [])
    # watch_channel_ids が空なら全チャンネル対象
    if watch_channel_ids and msg.channel.id not in watch_channel_ids:
        return

    content = msg.content
    if len(content) < 5:
        return

    reactions = cfg.get("reactions", {})
    positive_kw = cfg.get("positive_keywords", [])
    support_kw = cfg.get("support_keywords", [])
    empathy_kw = cfg.get("empathy_keywords", [])

    matched_category = None

    # つらい・しんどいメッセージ（最優先）
    for kw in support_kw:
        if kw in content:
            matched_category = "support"
            break

    # ポジティブなメッセージ
    if not matched_category:
        for kw in positive_kw:
            if kw in content:
                matched_category = "positive"
                break

    # 共感系メッセージ
    if not matched_category:
        for kw in empathy_kw:
            if kw in content:
                matched_category = "empathy"
                break

    if matched_category and matched_category in reactions:
        await asyncio.sleep(random.uniform(5, 30))
        try:
            emoji = random.choice(reactions[matched_category])
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
QUIZ_TIMEOUT_SECONDS = 600  # セルフチェック全体の制限時間（10分）
SELFCHECK_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selfcheck_state.json")


def load_selfcheck_state() -> dict:
    if not os.path.exists(SELFCHECK_STATE_PATH):
        return {}
    try:
        with open(SELFCHECK_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


selfcheck_state = load_selfcheck_state()  # { "ユーザーID(str)": "前回挑戦したISO日時" }


def record_selfcheck_attempt(user_id: int):
    selfcheck_state[str(user_id)] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with open(SELFCHECK_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(selfcheck_state, f, ensure_ascii=False, indent=2)


def get_selfcheck_cooldown_remaining(user_id: int):
    """前回挑戦からクールダウン期間が明けていなければ、残り時間(timedelta)を返す。明けていればNone"""
    last = selfcheck_state.get(str(user_id))
    if last is None:
        return None
    last_dt = datetime.datetime.fromisoformat(last)
    elapsed = datetime.datetime.now(datetime.timezone.utc) - last_dt
    cooldown_days = cfg_self_check().get("cooldown_days", 30)
    cooldown = datetime.timedelta(days=cooldown_days)
    if elapsed >= cooldown:
        return None
    return cooldown - elapsed


class QuizState:
    """1人分のセルフチェックの進行状況（合否判定まで使い捨て）。
    設問と合格ラインは開始時点のconfigをスナップショットして持ち、
    挑戦の途中で/reloadされても質問がすり替わらないようにする"""

    def __init__(self, user_id: int, questions: list, pass_score: int):
        self.user_id = user_id
        self.index = 0
        self.score = 0
        self.start_time = time.monotonic()
        self.message = None  # discord.Message、タイムアウト時の編集用
        self.questions = questions
        self.pass_score = pass_score


def format_question(state: QuizState) -> str:
    q = state.questions[state.index]
    return (
        f"📋 マナーセルフチェック（{state.index + 1}/{len(state.questions)}問目）\n"
        f"現在のスコア: {state.score}点\n\n"
        f"**Q{state.index + 1}. {q}**\n\n"
        "下のボタンで回答してね"
    )


def format_selfcheck_result(state: QuizState, passed: bool, promote_note: str) -> str:
    lines = [
        "🎉 セルフチェック合格です！おめでとう！" if passed else "😢 今回は合格ラインに届きませんでした。",
        f"スコア: {state.score} / {len(state.questions)}点（合格ライン: {state.pass_score}点）",
    ]
    if passed:
        if promote_note:
            lines.append(promote_note)
    else:
        lines.append("焦らなくて大丈夫です。何度でも再挑戦できるので、落ち着いたときにまた押してくださいね🌸")
    return "\n".join(lines)


async def send_selfcheck_log(interaction: discord.Interaction, state: QuizState, passed: bool, promote_note: str):
    log_channel_id = cfg_self_check().get("log_channel_id")
    if log_channel_id is None:
        return
    channel = client.get_channel(log_channel_id)
    if channel is None:
        print(f"  セルフチェック: ログチャンネル {log_channel_id} が見つかりません")
        return
    embed = discord.Embed(
        title="✅ セルフチェック合格" if passed else "❌ セルフチェック不合格",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="対象者", value=f"{interaction.user.mention}（{interaction.user}）", inline=False)
    embed.add_field(name="スコア", value=f"{state.score} / {len(state.questions)}点（合格ライン {state.pass_score}点）", inline=False)
    if promote_note:
        embed.add_field(name="ロール処理", value=promote_note, inline=False)
    embed.set_footer(text=f"ユーザーID: {interaction.user.id}")
    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print(f"  セルフチェック: ログチャンネル {log_channel_id} への送信権限がありません")


async def finish_selfcheck(interaction: discord.Interaction, state: QuizState):
    cfg = cfg_self_check()
    auto_promote = cfg.get("auto_promote", True)
    lv1_role_id = cfg.get("lv1_role_id")
    lv2_role_id = cfg.get("lv2_role_id")

    passed = state.score >= state.pass_score
    promote_note = ""

    if passed and auto_promote:
        guild = interaction.guild
        member = interaction.user
        lv2_role = guild.get_role(lv2_role_id) if lv2_role_id else None
        lv1_role = guild.get_role(lv1_role_id) if lv1_role_id else None

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

    record_selfcheck_attempt(interaction.user.id)

    result_text = format_selfcheck_result(state, passed, promote_note)
    await interaction.response.edit_message(content=result_text, view=None)
    await send_selfcheck_log(interaction, state, passed, promote_note)


async def advance_selfcheck(interaction: discord.Interaction, state: QuizState):
    if state.index < len(state.questions):
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

        cfg = cfg_self_check()
        questions = cfg.get("questions", [])
        pass_score = cfg.get("pass_score", 8)
        lv2_role_id = cfg.get("lv2_role_id")
        lv3_role_id = cfg.get("lv3_role_id")

        if not questions:
            await interaction.response.send_message(
                "設問が未設定です。config.json の self_check.questions を確認してください。", ephemeral=True
            )
            return

        member = interaction.user
        lv2_role = interaction.guild.get_role(lv2_role_id) if lv2_role_id else None
        lv3_role = interaction.guild.get_role(lv3_role_id) if lv3_role_id else None
        already_promoted = (lv2_role is not None and lv2_role in member.roles) or (
            lv3_role is not None and lv3_role in member.roles
        )
        if already_promoted:
            await interaction.response.send_message(
                "すでにLv2以上です🌸(Lv2またはLv3ロールをお持ちの方は対象外)セルフチェックは不要ですよ。",
                ephemeral=True,
            )
            return

        remaining = get_selfcheck_cooldown_remaining(member.id)
        if remaining is not None:
            next_dt = datetime.datetime.now(datetime.timezone.utc) + remaining
            jst = datetime.timezone(datetime.timedelta(hours=9))
            next_str = next_dt.astimezone(jst).strftime("%Y/%m/%d")
            await interaction.response.send_message(
                f"セルフチェックは月1回までです。次に挑戦できるのは {next_str}(JST)以降だよ🌸",
                ephemeral=True,
            )
            return

        state = QuizState(member.id, questions, pass_score)
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
    jw = cfg_welcome_on_join()
    print(f"  参加ウェルカム: {'ON' if jw.get('enabled', False) and jw.get('channel_id') else 'OFF'}")
    w = cfg_welcome()
    print(f"  自己紹介リプライ: {'ON' if w.get('enabled', False) and w.get('watch_channel_id') else 'OFF'}")
    t = cfg_daily_topic()
    print(f"  今日の話題: {'ON' if t.get('enabled', False) and t.get('channel_id') else 'OFF'}")
    r = cfg_channel_reminder()
    print(f"  チャンネル表示リマインド: {'ON' if r.get('enabled', False) and r.get('channel_ids') else 'OFF'}")
    print(f"  共感リアクション: {'ON' if cfg_empathy().get('enabled', False) else 'OFF'}")
    print(f"  マナーセルフチェック: {'ON' if cfg_self_check().get('enabled', False) else 'OFF'}")
    print(f"  キープアライブ: {'ON' if KOYEB_URL else 'OFF'}")
    print("─" * 40)

    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print("スラッシュコマンド登録完了")

    # バックグラウンドタスク開始
    # 各ループは内部で毎回configを見に行くため、/reload で後から有効化された場合にも
    # 再起動なしで反映される。そのため起動時のON/OFFにかかわらず常に起動しておく
    client.loop.create_task(daily_topic_loop())
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

    # 危機ワード検知（最優先・全チャンネル常時監視）
    crisis_matched = await handle_crisis_check(msg)

    # 自己紹介リプライ
    await handle_welcome(msg)

    # 共感リアクション（危機ワードに一致した場合は絵文字を付けない）
    if not crisis_matched:
        await handle_empathy_reaction(msg)


# ============================================================
# スラッシュ管理コマンド
# ============================================================
@tree.command(name="topic", description="今日の話題を手動で投稿する")
async def manual_topic(interaction: discord.Interaction):
    topics = cfg_daily_topic().get("topics", [])
    if not topics:
        await interaction.response.send_message("話題リストが空です", ephemeral=True)
        return

    topic = random.choice(topics)
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

    channel_ids = cfg_channel_reminder().get("channel_ids", [])
    if not channel_ids:
        await interaction.response.send_message(
            "チャンネル表示リマインドの投稿先が設定されていません。config.json の channel_reminder を確認してください",
            ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)
    success_count = await send_channel_reminder(channel_ids)
    await interaction.followup.send(
        f"チャンネル表示リマインドを {success_count} 件のチャンネルに投稿しました",
        ephemeral=True
    )


@tree.command(name="status", description="てちょうAIのステータスを表示")
async def status_command(interaction: discord.Interaction):
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    uptime = now.strftime("%Y/%m/%d %H:%M JST")

    jw = cfg_welcome_on_join()
    w = cfg_welcome()
    t = cfg_daily_topic()
    r = cfg_channel_reminder()

    embed = discord.Embed(title="📒 てちょうAI ステータス", color=0x5865F2)
    embed.add_field(name="現在時刻", value=uptime, inline=False)
    embed.add_field(name="AI自動返信", value=f"{len(WATCH_CHANNEL_IDS)}ch監視中（3〜30分遅延）", inline=True)
    embed.add_field(name="参加ウェルカム", value="ON" if jw.get("enabled", False) and jw.get("channel_id") else "OFF", inline=True)
    embed.add_field(name="自己紹介リプライ", value="ON" if w.get("enabled", False) and w.get("watch_channel_id") else "OFF", inline=True)
    embed.add_field(name="今日の話題", value="ON" if t.get("enabled", False) and t.get("channel_id") else "OFF", inline=True)
    embed.add_field(name="チャンネル表示リマインド", value="ON" if r.get("enabled", False) and r.get("channel_ids") else "OFF", inline=True)
    embed.add_field(name="共感リアクション", value="ON" if cfg_empathy().get("enabled", False) else "OFF", inline=True)
    embed.add_field(name="マナーセルフチェック", value="ON" if cfg_self_check().get("enabled", False) else "OFF", inline=True)
    embed.add_field(name="キープアライブ", value="ON" if KOYEB_URL else "OFF", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="reload", description="設定ファイルを再読み込みする")
async def reload_command(interaction: discord.Interaction):
    global config
    config = load_config()
    await interaction.response.send_message("設定を再読み込みしました", ephemeral=True)


@tree.command(name="test_welcome", description="参加ウェルカムのテスト（自分を新規参加者として送信）")
async def test_welcome_command(interaction: discord.Interaction):
    jw = cfg_welcome_on_join()
    if not jw.get("enabled", False) or jw.get("channel_id") is None:
        await interaction.response.send_message(
            "参加ウェルカムが無効です。config.json の welcome_on_join を確認してください",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"テスト送信中… → <#{jw.get('channel_id')}>",
        ephemeral=True
    )
    await handle_member_join(interaction.user)


@tree.command(name="セルフチェック設置", description="マナーセルフチェックのパネルを設置します（管理者専用）")
async def setup_selfcheck_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
        return

    cfg = cfg_self_check()
    if not cfg.get("enabled", False):
        await interaction.response.send_message(
            "セルフチェック機能が無効です。config.json の self_check.enabled を確認してください。", ephemeral=True
        )
        return

    questions = cfg.get("questions", [])
    pass_score = cfg.get("pass_score", 8)
    cooldown_days = cfg.get("cooldown_days", 30)

    embed = discord.Embed(
        title="📋 マナーセルフチェック",
        description=(
            f"Lv2への昇格には、マナーに関する全{len(questions)}問のセルフチェックに答えてね😊\n"
            f"{pass_score}点以上で合格すると、その場でLv2ロールが自動で付きます。\n"
            f"不合格の場合も再挑戦できますが、次のチャレンジまで{cooldown_days}日空くので、"
            "落ち着いてから挑戦してみてください🌸"
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="レベルについて",
        value=(
            "**Lv1（全員）**\n"
            "VC参加・発言・読み上げbotの利用はOK。画像/ファイル添付や音楽botの操作はまだ不可\n"
            "\n"
            "**Lv2（一般）**\n"
            "画像/ファイル添付、音楽bot等のコマンドも利用可能に\n"
            "\n"
            "**Lv3**\n"
            "管理人・副管理人が信頼できると判断したメンバーに手動で付与するロール（セルフチェック対象外）"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, view=SelfCheckPanelView())


# ============================================================
# BOT起動
# ============================================================
client.run(DISCORD_TOKEN)
