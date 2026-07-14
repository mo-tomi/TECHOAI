"""
てちょうAI - 手帳持ちの集い 統合サポートBOT
=============================================
機能:
  1. AI自動返信 — 指定チャンネルの投稿に10〜60分後にDeepSeek AIが返信
  2. /ai スラッシュコマンド — 直接AIに話しかける
  3. ウェルカム案内 — サーバー参加時にあいさつチャンネルへ歓迎メッセージ送信
  4. 自己紹介リプライ — 自己紹介チャンネルへの投稿を検知→やさしくチャンネル案内
  5. 今日の話題 — 毎日定時に話題を投稿してスレッド作成
  6. 共感リアクション — 感情キーワード検知→絵文字リアクション
  7. キープアライブ — Koyeb用の自己pingでスリープ防止
  8. マナーセルフチェック — Lv1メンバーがマナークイズに合格するとLv2ロールを自動付与
  9. 消えるつぶやき — 指定チャンネルの投稿を1〜60分後に自動削除し、非公開ログチャンネルへ記録
 10. しんどいレベル — 数字だけの投稿を記録し /graph /graph-all でグラフ表示
 11. 匿名ノック — 満室VCへ匿名でノックを送り、部屋側はボタンで返信

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
import io
import os
import json
import random
import re
import datetime
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import urllib.request

import matplotlib
matplotlib.use("Agg")  # サーバー上で画面なしにPNGを描くため（pyplotのimportより先に指定）
import matplotlib.dates as mdates
from matplotlib import font_manager
from matplotlib import pyplot as plt

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

def cfg_ai_auto_reply() -> dict:
    return config.get("ai_auto_reply", {})

def cfg_ephemeral() -> dict:
    return config.get("ephemeral_tweet", {})

def cfg_level_tracker() -> dict:
    return config.get("level_tracker", {})

def cfg_knock() -> dict:
    return config.get("knock", {})

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
SYSTEM_PROMPT = """あなたは「てちょうAI」。障害者手帳を持つ人たちが集まるDiscordサーバー「手帳持ちの集い」の住人です。

# 話し方
・日本語で、短く1〜3文。絵文字・Markdown記法は使わない
・友達のような柔らかい口調（です・ます調は崩してOK）
・共感を最初に一言、その後に軽い相槌や質問をひとつ添えてもよい
・毎回同じ書き出し（「そうなんですね」等）を避け、表現を変える
・相手の発言をそのまま繰り返さない

# やってはいけないこと
・医療的な診断、薬の増減や通院に関する具体的な指示
・「頑張って」など無理を促す言葉、安易な励まし
・説教、正論の押しつけ、長文

# つらそうな発言への対応
・まず気持ちを受け止める。解決策を急がない
・自傷や希死念慮が読み取れる場合は、否定せず寄り添い、
  信頼できる人や相談窓口（よりそいホットライン等）に話すことをそっと提案する

# 返答例
ユーザー「今日眠れなかった」
→「眠れない夜はしんどいよね。今日は無理せずゆっくり過ごせますように。」
ユーザー「バイト受かった！」
→「おお、それはうれしい報告！準備がんばった成果だね。」"""

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
# 機能1: AI自動返信（10〜60分ランダム遅延）
# ============================================================
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
            if not m.content:
                continue  # Embedのみ等、本文が空のメッセージは履歴から除外
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
        self.answers = []  # 設問ごとの回答（True=はい / False=いいえ）、運営ログ用


def question_text(q) -> str:
    """設問データから本文を取り出す。文字列でも{text, correct}オブジェクトでもよい"""
    return q.get("text", "") if isinstance(q, dict) else q


def question_expects_yes(q) -> bool:
    """その設問の正解が「はい」かどうか。correctが「いいえ」のときだけFalse（文字列や未指定は従来通りはい正解）"""
    if isinstance(q, dict):
        return q.get("correct", "はい") != "いいえ"
    return True


def format_question(state: QuizState) -> str:
    q = question_text(state.questions[state.index])
    return (
        f"📋 マナーセルフチェック（{state.index + 1}/{len(state.questions)}問目）\n\n"
        f"**Q{state.index + 1}. {q}**\n\n"
        "下のボタンで回答してね"
    )


def format_answer_lines(state: QuizState) -> list:
    return [
        f"Q{i + 1}. {question_text(q)}（あなたの回答: {'はい' if ans else 'いいえ'}）"
        for i, (q, ans) in enumerate(zip(state.questions, state.answers))
    ]


def format_selfcheck_result(state: QuizState, passed: bool, promote_note: str, dm_sent: bool) -> str:
    lines = [
        "📋 セルフチェックお疲れさまでした！",
    ]
    if passed:
        if promote_note:
            lines.append(promote_note)
    else:
        lines.append(
            "今回はLv2の付与に至りませんでした。焦らなくて大丈夫、"
            "落ち着いたときにまた挑戦してくださいね🌸"
        )
    lines.append("")
    if dm_sent:
        lines.append("📝 あなたの回答（DMにも控えを送りました）")
    else:
        lines.append("📝 あなたの回答（この画面は閉じると消えるので、必要ならスクリーンショットで保存してね）")
    lines.extend(format_answer_lines(state))
    return "\n".join(lines)


async def send_selfcheck_dm_copy(user, state: QuizState, passed: bool) -> bool:
    """回答者本人へ回答の控えをDMで送る。DMを閉じている場合はFalseを返す"""
    embed = discord.Embed(
        title="📋 セルフチェックの回答控え",
        description=(
            ("🌸 Lv2ロールが付与されました" if passed else "今回はLv2の付与に至りませんでした")
            + "\n\n"
            + "\n".join(format_answer_lines(state))
        ),
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    try:
        await user.send(embed=embed)
        return True
    except discord.HTTPException:
        return False  # DMを受け取らない設定の場合など


async def send_selfcheck_log(interaction: discord.Interaction, state: QuizState, passed: bool, promote_note: str):
    log_channel_id = cfg_self_check().get("log_channel_id")
    if log_channel_id is None:
        return
    channel = client.get_channel(log_channel_id)
    if channel is None:
        # 再起動直後などキャッシュにない場合はAPIから直接取得する
        try:
            channel = await client.fetch_channel(log_channel_id)
        except discord.HTTPException as e:
            print(f"  セルフチェック: ログチャンネル {log_channel_id} を取得できません（{e}）")
            return
    embed = discord.Embed(
        title="✅ Lv2付与" if passed else "⬜ Lv2未付与",
        color=0x5865F2,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="対象者", value=f"{interaction.user.mention}（{interaction.user}）", inline=False)
    embed.add_field(name="「はい」の数", value=f"{state.score} / {len(state.questions)}問（付与ライン {state.pass_score}問）", inline=False)
    answer_lines = [
        f"{'✅' if ans == question_expects_yes(q) else '⚠️'} Q{i + 1}. {question_text(q)}"
        f"（回答: {'はい' if ans else 'いいえ'}）"
        for i, (q, ans) in enumerate(zip(state.questions, state.answers))
    ]
    # embedフィールドは1024文字上限のため、超える場合は複数フィールドに分割する
    chunk = ""
    part = 1
    for line in answer_lines:
        if chunk and len(chunk) + 1 + len(line) > 1024:
            embed.add_field(name="回答内容" if part == 1 else f"回答内容（続き{part}）", value=chunk, inline=False)
            part += 1
            chunk = line
        else:
            chunk = f"{chunk}\n{line}" if chunk else line
    if chunk:
        embed.add_field(name="回答内容" if part == 1 else f"回答内容（続き{part}）", value=chunk, inline=False)
    if promote_note:
        embed.add_field(name="ロール処理", value=promote_note, inline=False)
    embed.set_footer(text=f"ユーザーID: {interaction.user.id}")
    try:
        await channel.send(embed=embed)
    except discord.HTTPException as e:
        print(f"  セルフチェック: ログチャンネル {log_channel_id} への送信に失敗しました（{e}）")


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
                await member.add_roles(lv2_role, reason="セルフチェック完了によるLv2付与")
                added = True
            except discord.Forbidden:
                added = False

            removed = True
            if added and lv1_role is not None and lv1_role in member.roles:
                try:
                    await member.remove_roles(lv1_role, reason="セルフチェック完了によるLv2付与")
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

    dm_sent = await send_selfcheck_dm_copy(interaction.user, state, passed)
    result_text = format_selfcheck_result(state, passed, promote_note, dm_sent)
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
        current_q = self.state.questions[self.state.index]
        if is_yes == question_expects_yes(current_q):
            self.state.score += 1
        self.state.answers.append(is_yes)
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
            cooldown_days = cfg.get("cooldown_days", 30)
            await interaction.response.send_message(
                f"セルフチェックは{cooldown_days}日に1回までです。次に挑戦できるのは {next_str}(JST)以降だよ🌸",
                ephemeral=True,
            )
            return

        state = QuizState(member.id, questions, pass_score)
        view = SelfCheckAnswerView(state)
        await interaction.response.send_message(content=format_question(state), view=view, ephemeral=True)
        state.message = await interaction.original_response()


vc_reminder_last_sent = {}  # { user_id: 最後にVC入室案内を送ったUTC日時 }（再起動でリセットされる）


async def handle_vc_selfcheck_reminder(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Lv2未満のメンバーがVCに入室したら、そのVCのチャットにセルフチェック案内をメンション付きで送る"""
    if member.bot:
        return
    # 新規入室のみ対象（ミュート切替・画面共有・VC間の移動では送らない）
    if before.channel is not None or after.channel is None:
        return
    cfg = cfg_self_check()
    if not cfg.get("enabled", False) or not cfg.get("vc_reminder_enabled", False):
        return
    role_ids = {role.id for role in member.roles}
    if cfg.get("lv2_role_id") in role_ids or cfg.get("lv3_role_id") in role_ids:
        return
    # 出入りのたびに連投しないよう、同じ人への案内はクールダウンを空ける
    cooldown = datetime.timedelta(hours=cfg.get("vc_reminder_cooldown_hours", 12))
    now = datetime.datetime.now(datetime.timezone.utc)
    last = vc_reminder_last_sent.get(member.id)
    if last is not None and now - last < cooldown:
        return
    vc_reminder_last_sent[member.id] = now
    panel_channel_id = cfg.get("vc_reminder_panel_channel_id")
    if panel_channel_id:
        panel_link = f"https://discord.com/channels/{member.guild.id}/{panel_channel_id} から"
    else:
        panel_link = ""
    # 自己紹介botの入室通知より後に表示されるよう、少し待ってから送る
    await asyncio.sleep(cfg.get("vc_reminder_delay_seconds", 7))
    try:
        await after.channel.send(
            f"{member.mention}\n"
            "いらっしゃい🌸 ゆっくりしていってね\n"
            f"お時間がある際に {panel_link}セルフチェックをお願いします📋\n"
            "回答内容に応じてLv2になると、画像の添付や画面共有などができるようになりますよ✨"
        )
    except discord.HTTPException:
        print(f"  VCセルフチェック案内: {after.channel} への送信に失敗しました")


# ============================================================
# 機能9: 消えるつぶやき（1〜60分後に自動削除→ログチャンネルへ記録）
# ============================================================
def _fnv_hash_unit(text: str) -> float:
    """メッセージIDから0〜1の固定値を作る（再起動しても同じ値になる: FNV-1aハッシュ）"""
    h = 2166136261
    for ch in text:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return (h % 100000) / 100000


def ephemeral_delete_after_ms(msg: discord.Message) -> int:
    """そのメッセージの「消えるまでの時間」をmin〜max分でランダムに決める（IDで固定）"""
    cfg = cfg_ephemeral()
    min_ms = cfg.get("min_minutes", 1) * 60_000
    max_ms = cfg.get("max_minutes", 60) * 60_000
    return min_ms + int(_fnv_hash_unit(str(msg.id)) * (max_ms - min_ms))


async def log_deleted_message(msg: discord.Message) -> bool:
    """削除するメッセージをログチャンネルへ記録する（添付画像も再アップロードして残す）。
    記録できなかった場合はFalseを返し、呼び出し側は削除を見送る（記録なしで消さない）"""
    log_channel_id = cfg_ephemeral().get("log_channel_id")
    channel = client.get_channel(log_channel_id) if log_channel_id else None
    if channel is None:
        return False

    embed = discord.Embed(
        title="🗑️ 消えるつぶやき 削除ログ",
        description=msg.content if msg.content else "（本文なし）",
        color=0x99AAB5,
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="投稿者", value=f"{msg.author.mention}（{msg.author}）", inline=False)
    embed.add_field(name="投稿日時", value=f"<t:{int(msg.created_at.timestamp())}:f>", inline=False)
    embed.set_footer(text=f"メッセージID: {msg.id}")

    files = []
    failed_urls = []
    for att in msg.attachments:
        try:
            files.append(await att.to_file())
        except discord.HTTPException:
            failed_urls.append(att.url)
    if failed_urls:
        embed.add_field(name="保存できなかった添付", value="\n".join(failed_urls)[:1024], inline=False)

    try:
        await channel.send(embed=embed, files=files)
        return True
    except discord.HTTPException:
        if files:
            # 添付が大きすぎる等で送れない場合は、URLだけ記録して本文は残す
            embed.add_field(name="添付（再アップロード失敗・URLのみ）",
                            value="\n".join(a.url for a in msg.attachments)[:1024], inline=False)
            try:
                await channel.send(embed=embed)
                return True
            except discord.HTTPException as e:
                print(f"消えるつぶやき: ログ送信失敗 {e}")
        return False


ephemeral_log_warned = False


async def ephemeral_sweep():
    """対象チャンネルを巡回して、期限を過ぎた投稿を記録してから削除する"""
    cfg = cfg_ephemeral()
    channel = client.get_channel(cfg.get("channel_id"))
    if channel is None:
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    deleted = 0

    async for msg in channel.history(limit=100):
        if cfg.get("keep_pinned", True) and msg.pinned:
            continue
        age_ms = (now - msg.created_at).total_seconds() * 1000
        if age_ms < ephemeral_delete_after_ms(msg):
            continue
        if not await log_deleted_message(msg):
            continue  # 記録できないうちは消さない
        try:
            await msg.delete()
            deleted += 1
        except discord.NotFound:
            pass  # 既に消えている
        except discord.Forbidden:
            print("消えるつぶやき: メッセージの管理権限がありません")
            return

    if deleted > 0:
        print(f"消えるつぶやき: {deleted}件削除しました")


async def ephemeral_sweep_loop():
    """1分ごとに対象チャンネルを巡回する"""
    global ephemeral_log_warned
    await client.wait_until_ready()
    print("消えるつぶやき: 巡回ループ開始")

    while not client.is_closed():
        cfg = cfg_ephemeral()
        if cfg.get("enabled", False) and cfg.get("channel_id"):
            if not cfg.get("log_channel_id"):
                if not ephemeral_log_warned:
                    print("消えるつぶやき: log_channel_id が未設定のため削除を停止しています")
                    ephemeral_log_warned = True
            else:
                try:
                    await ephemeral_sweep()
                except Exception as e:
                    print(f"消えるつぶやきエラー: {e}")
        await asyncio.sleep(60)


# ============================================================
# 機能10: しんどいレベル記録・グラフ（/graph /graph-all）
# ============================================================
# 「数字のみ」の投稿だけを値として認める（範囲・複数値・文章混じりは対象外）
LEVEL_NUM_RE = re.compile(r"^\d+(\.\d+)?$")
LEVEL_COLORS = [
    "#e63946", "#457b9d", "#2a9d8f", "#f4a261", "#8338ec",
    "#ff006e", "#3a86ff", "#606c38", "#bc6c25", "#6a4c93",
]

# 日本語フォント（assets/fonts に同梱。Koyebのコンテナには日本語フォントが無いため）
LEVEL_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts", "NotoSansJP.ttf")
if os.path.exists(LEVEL_FONT_PATH):
    font_manager.fontManager.addfont(LEVEL_FONT_PATH)
    matplotlib.rcParams["font.family"] = "Noto Sans JP"

# Koyebはディスクが再デプロイで消えるため、記録はメモリ上に持ち、
# 起動のたびにチャンネル履歴から全件を再構築する（backfill）
# { user_id: {"name": 表示名, "ids": 取込済みメッセージID集合, "records": [(unix_ms, 値), ...] } }
level_users: dict[int, dict] = {}


def level_add_record(user_id: int, name: str, message_id: int, timestamp_ms: int, value: float) -> bool:
    """記録を1件追加する。同じメッセージIDは重複追加しない"""
    user = level_users.setdefault(user_id, {"name": name, "ids": set(), "records": []})
    user["name"] = name  # 最新の表示名で更新
    if message_id in user["ids"]:
        return False
    user["ids"].add(message_id)
    user["records"].append((timestamp_ms, value))
    user["records"].sort()
    return True


async def level_backfill():
    """チャンネルの過去投稿を全件読み直して記録を再構築する（起動時に毎回実行）"""
    cfg = cfg_level_tracker()
    if not cfg.get("enabled", False) or not cfg.get("channel_id"):
        print("しんどいレベル: 無効")
        return
    channel = client.get_channel(cfg.get("channel_id"))
    if channel is None:
        print(f"しんどいレベル: チャンネル {cfg.get('channel_id')} が見つかりません")
        return

    total = 0
    async for msg in channel.history(limit=None, oldest_first=True):
        if msg.author.bot:
            continue
        text = (msg.content or "").strip()
        if not LEVEL_NUM_RE.match(text):
            continue
        name = msg.author.display_name
        if level_add_record(msg.author.id, name, msg.id, int(msg.created_at.timestamp() * 1000), float(text)):
            total += 1
    print(f"しんどいレベル: 過去投稿から{total}件を取り込みました")


def render_level_chart(title: str, series: list) -> bytes:
    """折れ線グラフPNGを描く。series = [(表示名, [(unix_ms, 値), ...]), ...]"""
    jst = datetime.timezone(datetime.timedelta(hours=9))
    fig, ax = plt.subplots(figsize=(10, 6), dpi=100)
    for i, (label, records) in enumerate(series):
        xs = [datetime.datetime.fromtimestamp(t / 1000, jst) for t, _ in records]
        ys = [v for _, v in records]
        ax.plot(xs, ys, color=LEVEL_COLORS[i % len(LEVEL_COLORS)],
                linewidth=2, marker="o", markersize=5, label=label)
    ax.set_title(title)
    ax.set_ylim(0, 100)
    ax.set_yticks(range(0, 101, 20))
    ax.grid(axis="y", color="#dddddd")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m/%d", tz=jst))
    if len(series) > 1:
        ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0))
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


@tree.command(name="graph", description="指定したユーザーのしんどいレベルの推移をグラフで表示します")
@app_commands.describe(user="対象ユーザー")
async def graph_command(interaction: discord.Interaction, user: discord.User):
    record = level_users.get(user.id)
    if not record or not record["records"]:
        await interaction.response.send_message(f"{user.name} さんの記録はまだありません", ephemeral=True)
        return
    await interaction.response.defer()
    png = render_level_chart(
        f"{record['name']} さんのしんどいレベル（0=いい方 / 100=悪い方）",
        [(record["name"], record["records"])],
    )
    await interaction.followup.send(file=discord.File(io.BytesIO(png), "graph.png"))


@tree.command(name="graph-all", description="全員のしんどいレベルの推移を1枚のグラフで重ねて表示します")
async def graph_all_command(interaction: discord.Interaction):
    series = [(u["name"], u["records"]) for u in level_users.values() if u["records"]]
    if not series:
        await interaction.response.send_message("記録がまだありません", ephemeral=True)
        return
    await interaction.response.defer()
    png = render_level_chart("しんどいレベル（全員, 0=いい方 / 100=悪い方）", series)
    await interaction.followup.send(file=discord.File(io.BytesIO(png), "graph-all.png"))


# ============================================================
# 機能11: 匿名ノック（満室VCへの入室希望をボタンで送る）
# ============================================================
KNOCK_PANEL_TITLE = "通話募集・入室希望"
KNOCK_RESPONSES = {
    "sorry": ("ごめんなさい今は難しい", discord.ButtonStyle.danger, "🙏 ごめんなさい、今は難しいです"),
    "wait": ("ちょっと待ってね", discord.ButtonStyle.secondary, "⏳ ちょっと待ってね"),
    "move": ("部屋移動するね", discord.ButtonStyle.success, "🔀 部屋を移動するね"),
}

knock_panel_msg_id: int | None = None


class KnockResponseButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"knock_resp:(?P<key>sorry|wait|move):(?P<knocker>[0-9]+)",
):
    """ノックへの返信ボタン。ノック主のIDをcustom_idに埋め込むことで、
    DBなし・再起動後でも誰に通知すべきかが分かる（Koyebのディスクは消えるため）"""

    def __init__(self, key: str, knocker_id: int):
        label, style, _ = KNOCK_RESPONSES[key]
        super().__init__(discord.ui.Button(
            label=label, style=style, custom_id=f"knock_resp:{key}:{knocker_id}",
        ))
        self.key = key
        self.knocker_id = knocker_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match):
        return cls(match["key"], int(match["knocker"]))

    async def callback(self, interaction: discord.Interaction):
        text = KNOCK_RESPONSES[self.key][2]
        # 部屋チャットの表示を更新（誰が答えたか付き）してボタンを閉じる
        embed = interaction.message.embeds[0]
        embed.description += f"\n\n**応答:** {text}（{interaction.user.display_name}）"
        await interaction.response.edit_message(embed=embed, view=None)
        # ノック主にDMで通知
        try:
            user = client.get_user(self.knocker_id) or await client.fetch_user(self.knocker_id)
            await user.send(f"ノックした部屋から返信がありました：\n{text}")
        except discord.HTTPException:
            pass  # DM拒否設定等は無視


def build_knock_response_view(knocker_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for key in KNOCK_RESPONSES:
        view.add_item(KnockResponseButton(key, knocker_id))
    return view


class KnockVCSelect(discord.ui.ChannelSelect):
    def __init__(self, knocker_id: int):
        self.knocker_id = knocker_id
        super().__init__(channel_types=[discord.ChannelType.voice],
                         placeholder="ボイスチャンネルを選択", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        ch = interaction.guild.get_channel(self.values[0].id)
        if ch is None:
            await interaction.response.send_message("チャンネルが見つかりませんでした", ephemeral=True)
            return
        embed = discord.Embed(
            description="🚪 **どうやら入りたい人がいるようです**\nノックがありました。よければ返信してあげてください。",
            color=0x5865F2,
        )
        await ch.send(embed=embed, view=build_knock_response_view(self.knocker_id))
        await interaction.response.send_message(f"✅ {ch.mention} にノックを送りました", ephemeral=True)


class KnockSelectView(discord.ui.View):
    def __init__(self, knocker_id: int):
        super().__init__(timeout=120)
        self.add_item(KnockVCSelect(knocker_id))


class KnockPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent view（再起動後も有効）

    @discord.ui.button(label="部屋をノックする", style=discord.ButtonStyle.primary,
                       emoji="🔔", custom_id="knock_panel_button")
    async def knock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "入りたい部屋を選んでください（あなたの名前は相手に表示されません）",
            view=KnockSelectView(interaction.user.id), ephemeral=True)


def build_knock_panel_embed() -> discord.Embed:
    return discord.Embed(
        title=KNOCK_PANEL_TITLE,
        description=("満室部屋に入りたいときは、下のボタンで匿名ノックできます。\n"
                     "※相手がDMを拒否する設定をしている場合、返信が来ないことがあります。"),
        color=0x5865F2,
    )


async def knock_repost_panel(channel):
    """パネルを最下部に貼り直す"""
    global knock_panel_msg_id
    if knock_panel_msg_id:
        try:
            old = await channel.fetch_message(knock_panel_msg_id)
            await old.delete()
        except discord.HTTPException:
            pass
    msg = await channel.send(embed=build_knock_panel_embed(), view=KnockPanelView())
    knock_panel_msg_id = msg.id


async def knock_ensure_panel():
    """起動時にパネルの有無を確認し、無ければ設置する。
    （メッセージIDはDBに保存できないため、チャンネル履歴から自分のパネルを探す）"""
    global knock_panel_msg_id
    cfg = cfg_knock()
    if not cfg.get("enabled", False) or not cfg.get("panel_channel_id"):
        print("匿名ノック: 無効")
        return
    channel = client.get_channel(cfg.get("panel_channel_id"))
    if channel is None:
        print(f"匿名ノック: チャンネル {cfg.get('panel_channel_id')} が見つかりません")
        return
    async for msg in channel.history(limit=50):
        if msg.author == client.user and msg.embeds and msg.embeds[0].title == KNOCK_PANEL_TITLE:
            knock_panel_msg_id = msg.id
            break
    if knock_panel_msg_id is None:
        await knock_repost_panel(channel)
    print("匿名ノック: 準備完了")


# ============================================================
# イベントハンドラ
# ============================================================
background_tasks_started = False


@client.event
async def setup_hook():
    client.add_view(SelfCheckPanelView())  # 再起動後もセルフチェックパネルのボタンを有効化
    client.add_view(KnockPanelView())      # 再起動後もノックパネルのボタンを有効化
    client.add_dynamic_items(KnockResponseButton)  # 再起動前に送ったノックの返信ボタンを有効化


@client.event
async def on_ready():
    print(f"Bot起動: {client.user}")
    print(f"サーバー: {GUILD_ID}")
    print("─" * 40)
    print(f"  AI自動返信: {len(cfg_ai_auto_reply().get('watch_channel_ids', []))}チャンネル監視中（10〜60分遅延）")
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
    e = cfg_ephemeral()
    print(f"  消えるつぶやき: {'ON' if e.get('enabled', False) and e.get('channel_id') else 'OFF'}")
    lv = cfg_level_tracker()
    print(f"  しんどいレベル: {'ON' if lv.get('enabled', False) and lv.get('channel_id') else 'OFF'}")
    k = cfg_knock()
    print(f"  匿名ノック: {'ON' if k.get('enabled', False) and k.get('panel_channel_id') else 'OFF'}")
    print(f"  キープアライブ: {'ON' if KOYEB_URL else 'OFF'}")
    print("─" * 40)

    guild = discord.Object(id=GUILD_ID)
    tree.copy_global_to(guild=guild)
    await tree.sync(guild=guild)
    print("スラッシュコマンド登録完了")

    # バックグラウンドタスク開始
    # 各ループは内部で毎回configを見に行くため、/reload で後から有効化された場合にも
    # 再起動なしで反映される。そのため起動時のON/OFFにかかわらず常に起動しておく
    global background_tasks_started
    if not background_tasks_started:  # on_readyは再接続時にも発火するため二重起動を防ぐ
        background_tasks_started = True
        client.loop.create_task(daily_topic_loop())
        client.loop.create_task(monthly_reminder_loop())
        client.loop.create_task(keepalive_loop())
        client.loop.create_task(ephemeral_sweep_loop())
        client.loop.create_task(level_backfill())
        client.loop.create_task(knock_ensure_panel())


@client.event
async def on_member_join(member: discord.Member):
    """サーバーに新メンバーが参加したときに発火"""
    await handle_member_join(member)


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """VCの入退室・状態変化のたびに発火"""
    await handle_vc_selfcheck_reminder(member, before, after)


@client.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return

    # AI自動返信
    if msg.channel.id in cfg_ai_auto_reply().get("watch_channel_ids", []):
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

    # しんどいレベル記録（数字だけの投稿を取り込む）
    lv_cfg = cfg_level_tracker()
    if lv_cfg.get("enabled", False) and msg.channel.id == lv_cfg.get("channel_id"):
        text = (msg.content or "").strip()
        if LEVEL_NUM_RE.match(text):
            level_add_record(msg.author.id, msg.author.display_name, msg.id,
                             int(msg.created_at.timestamp() * 1000), float(text))

    # 匿名ノック: パネルチャンネルで誰かが発言→パネルを最下部に貼り直す
    k_cfg = cfg_knock()
    if k_cfg.get("enabled", False) and msg.channel.id == k_cfg.get("panel_channel_id"):
        await knock_repost_panel(msg.channel)


# ============================================================
# スラッシュ管理コマンド
# ============================================================
@tree.command(name="topic", description="今日の話題を手動で投稿する")
async def manual_topic(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "このコマンドはサーバー管理権限を持つ方のみ実行できます",
            ephemeral=True
        )
        return

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
    embed.add_field(name="AI自動返信", value=f"{len(cfg_ai_auto_reply().get('watch_channel_ids', []))}ch監視中（10〜60分遅延）", inline=True)
    embed.add_field(name="参加ウェルカム", value="ON" if jw.get("enabled", False) and jw.get("channel_id") else "OFF", inline=True)
    embed.add_field(name="自己紹介リプライ", value="ON" if w.get("enabled", False) and w.get("watch_channel_id") else "OFF", inline=True)
    embed.add_field(name="今日の話題", value="ON" if t.get("enabled", False) and t.get("channel_id") else "OFF", inline=True)
    embed.add_field(name="チャンネル表示リマインド", value="ON" if r.get("enabled", False) and r.get("channel_ids") else "OFF", inline=True)
    embed.add_field(name="共感リアクション", value="ON" if cfg_empathy().get("enabled", False) else "OFF", inline=True)
    embed.add_field(name="マナーセルフチェック", value="ON" if cfg_self_check().get("enabled", False) else "OFF", inline=True)
    e = cfg_ephemeral()
    embed.add_field(name="消えるつぶやき", value="ON" if e.get("enabled", False) and e.get("channel_id") and e.get("log_channel_id") else "OFF", inline=True)
    lv = cfg_level_tracker()
    embed.add_field(name="しんどいレベル", value="ON" if lv.get("enabled", False) and lv.get("channel_id") else "OFF", inline=True)
    k = cfg_knock()
    embed.add_field(name="匿名ノック", value="ON" if k.get("enabled", False) and k.get("panel_channel_id") else "OFF", inline=True)
    embed.add_field(name="キープアライブ", value="ON" if KOYEB_URL else "OFF", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="reload", description="設定ファイルを再読み込みする")
async def reload_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "このコマンドはサーバー管理権限を持つ方のみ実行できます",
            ephemeral=True
        )
        return

    global config
    config = load_config()
    await interaction.response.send_message("設定を再読み込みしました", ephemeral=True)


@tree.command(name="test_welcome", description="参加ウェルカムのテスト（自分を新規参加者として送信）")
async def test_welcome_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "このコマンドはサーバー管理権限を持つ方のみ実行できます",
            ephemeral=True
        )
        return

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
    cooldown_days = cfg.get("cooldown_days", 30)

    if cooldown_days > 0:
        retry_note = (
            f"すぐにLv2が付かなかったときも、次のチャレンジまで{cooldown_days}日空きますが、"
            "落ち着いてからまた挑戦してみてください🌸"
        )
    else:
        retry_note = "すぐにLv2が付かなかったときも、落ち着いたタイミングで何度でも挑戦できます🌸"

    embed = discord.Embed(
        title="📋 マナーセルフチェック",
        description=(
            f"Lv2への昇格には、マナーに関する全{len(questions)}問のセルフチェックに答えてね😊\n"
            "\n"
            "⚠️ 答える前に、こちらのお知らせを必ず確認してね👇\n"
            "https://discord.com/channels/1300291307314610316/1404397500957200474\n"
            "\n"
            "セルフチェックを終えると、回答内容に応じてその場でLv2ロールが付くことがあります。\n"
            "（付与の基準についてはお答えできません🙏 自分の言葉で、正直に答えてくださいね）\n"
            f"{retry_note}\n"
            "🔄 セルフチェックは1か月に1回リセットされます（次の挑戦までしばらく空きます）。\n"
            "\n"
            "📝 回答内容は、運営（管理人・副管理人）にて保存・確認させていただきます。"
        ),
        color=0x5865F2,
    )
    embed.add_field(
        name="レベルについて",
        value=(
            "Lv1〜Lv3の数字は上下関係や偉さを表すものではなく、使える機能の範囲の違いです。\n"
            "\n"
            "**Lv1（全員）**\n"
            "サーバーに入ると全員に付くロール。一部制限はありますが、VC参加・発言・読み上げbotの利用は問題なくできます\n"
            "\n"
            "**Lv2（一般）**\n"
            "セルフチェックの回答内容に応じて自動付与。画像/ファイル添付、画面共有、音楽botの操作などができるようになります\n"
            "\n"
            "**Lv3**\n"
            "管理人・副管理人が信頼できると判断したメンバーに手動で付与するロール（セルフチェック対象外）\n"
            "\n"
            "※マナーが守れていない場合や、セルフチェックの回答と普段の様子に差異があると感じた場合、"
            "管理人・副管理人の判断でLvを下げる場合がございます"
        ),
        inline=False,
    )
    await interaction.response.send_message(embed=embed, view=SelfCheckPanelView())


@tree.command(name="lv1一括付与", description="レベルロールを持っていない全メンバーにLv1を付与します（管理者専用）")
async def bulk_assign_lv1_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("このコマンドは管理者専用です。", ephemeral=True)
        return

    cfg = cfg_self_check()
    lv1_role = interaction.guild.get_role(cfg.get("lv1_role_id") or 0)
    lv2_role_id = cfg.get("lv2_role_id")
    lv3_role_id = cfg.get("lv3_role_id")
    if lv1_role is None:
        await interaction.response.send_message(
            "Lv1ロールが見つかりません。config.json の self_check.lv1_role_id を確認してください。", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    added = 0
    skipped = 0
    failed = 0
    async for member in interaction.guild.fetch_members(limit=None):
        if member.bot:
            continue
        role_ids = {role.id for role in member.roles}
        if lv1_role.id in role_ids or lv2_role_id in role_ids or lv3_role_id in role_ids:
            skipped += 1
            continue
        try:
            await member.add_roles(lv1_role, reason="Lv1一括付与コマンド")
            added += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1

    await interaction.followup.send(
        f"Lv1一括付与が完了しました：付与 {added}名 / スキップ（既にLv1〜Lv3を所持） {skipped}名 / 失敗 {failed}名",
        ephemeral=True,
    )


# ============================================================
# BOT起動
# ============================================================
client.run(DISCORD_TOKEN)
