import os
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
    QuickReply,
    QuickReplyItem,
    MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
import anthropic

app = Flask(__name__)

line_configuration = Configuration(
    access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
)
handler = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
claude_client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ===================================================
# メモリ上のデータ（Renderスリープ時にリセットされます）
# ===================================================

# 通常会話の履歴  {user_id: [{"role": ..., "content": ...}, ...]}
conversation_histories: dict[str, list[dict]] = {}
MAX_HISTORY_TURNS = 10

# ヒアリング中のユーザー状態
# {user_id: {"step": 1〜4, "answers": {"industry": ..., ...}}}
hearing_states: dict[str, dict] = {}


# ===================================================
# ヒアリング設定
# ===================================================

# 4つの質問（step 1〜4 に対応）
HEARING_QUESTIONS = [
    (
        "Q1/4｜業種を教えてください😊\n"
        "（例：飲食・製造・医療・不動産・小売・IT など）"
    ),
    (
        "Q2/4｜今一番お困りの作業は何ですか？\n"
        "できるだけ具体的に教えていただけると、ぴったりのご提案ができます✨"
    ),
    (
        "Q3/4｜その作業に1日どのくらいの時間がかかっていますか？\n"
        "（例：30分、2時間、半日 など）"
    ),
    (
        "Q4/4｜従業員は何人いらっしゃいますか？\n"
        "（例：5人、20人、100人以上 など）"
    ),
]

# answers dict のキー名（step-1 がインデックスに対応）
HEARING_KEYS = ["industry", "problem", "hours_per_day", "employee_count"]

# ヒアリング開始トリガーとなるキーワード
HEARING_TRIGGERS = [
    "無料診断", "ヒアリング", "ヒアリングを受けたい",
    "提案してほしい", "提案を聞きたい", "詳しく提案",
    "診断してほしい", "相談したい", "無料相談",
]

# ヒアリング中断キーワード
CANCEL_KEYWORDS = ["キャンセル", "やめる", "中断", "戻る", "最初から"]


# ===================================================
# AI社員のシステムプロンプト
# ===================================================

SYSTEM_PROMPT = """あなたは「AIくん」という名前のAI事業会社の優秀な営業担当AI社員です。
お客様のご質問に丁寧・的確にお答えし、AI導入のご支援をすることがあなたの使命です。

## 会社情報
- 事業内容：AI活用ソリューションの開発・提供・コンサルティング
- ターゲット：中小企業〜中堅企業のDX推進・業務効率化を目指す経営者・担当者

## 提供サービス
1. AIチャットボット開発
   - LINE・Webサイト・社内ツール向けのカスタムチャットボット
   - 24時間対応・多言語対応可能

2. 業務自動化AI
   - 繰り返し作業（データ入力・仕分け・集計など）をAIで自動化
   - 人的ミスの削減、業務時間の大幅短縮

3. AIコンサルティング
   - 貴社ビジネスへの最適なAI導入戦略を立案
   - 既存システムへのAI組み込み提案・PoC支援

## 料金プラン（税別）
- スタータープラン：¥50,000〜
  内容：基本チャットボット、FAQ対応（最大20件）、1ヶ月サポート

- スタンダードプラン：¥150,000〜
  内容：カスタムAIチャットボット、業務システム連携、3ヶ月サポート

- エンタープライズプラン：¥300,000〜（要相談）
  内容：フルカスタム開発、専任サポート、保守・運用込み

## 導入の流れ
STEP1：無料相談（30分）→ STEP2：ご提案・見積もり（1週間以内）→ STEP3：開発スタート（最短2週間）

## お問い合わせ先
- メール：info@example.com
- 電話：03-XXXX-XXXX（平日 10:00〜18:00）
- Webフォーム：https://example.com/contact

## 行動指針
- 専門用語は使いすぎず、わかりやすい日本語で説明してください
- 返答はLINEチャットに適した簡潔さ（3〜6行程度）を心がけてください
- 会話の流れを覚えており、前の質問を踏まえた返答をしてください
"""


# ===================================================
# クイックリプライボタン
# ===================================================

def make_main_quick_reply() -> QuickReply:
    """通常時のクイックリプライ"""
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label="✅ 無料診断を受ける", text="無料診断")),
            QuickReplyItem(action=MessageAction(label="サービス内容", text="サービス内容を教えて")),
            QuickReplyItem(action=MessageAction(label="料金プラン", text="料金プランを教えて")),
            QuickReplyItem(action=MessageAction(label="お問い合わせ", text="お問い合わせしたい")),
        ]
    )

def make_cancel_quick_reply() -> QuickReply:
    """ヒアリング中のキャンセルボタン"""
    return QuickReply(
        items=[
            QuickReplyItem(action=MessageAction(label="❌ キャンセル", text="キャンセル")),
        ]
    )


# ===================================================
# ヒアリング機能
# ===================================================

def is_hearing_trigger(text: str) -> bool:
    return any(trigger in text for trigger in HEARING_TRIGGERS)

def is_cancel(text: str) -> bool:
    return any(kw in text for kw in CANCEL_KEYWORDS)

def start_hearing(user_id: str) -> tuple[str, QuickReply]:
    """ヒアリングを開始し、最初の質問を返す"""
    hearing_states[user_id] = {"step": 1, "answers": {}}
    msg = (
        "ありがとうございます！🎉\n"
        "貴社に最適なAIソリューションをご提案するために、\n"
        "4つだけ質問させてください。\n\n"
        "（途中でやめる場合は「キャンセル」とお送りください）\n\n"
        + HEARING_QUESTIONS[0]
    )
    return msg, make_cancel_quick_reply()

def advance_hearing(user_id: str, user_text: str) -> tuple[str, QuickReply | None]:
    """ヒアリングを1ステップ進める。全問完了なら提案を生成して返す"""
    state = hearing_states[user_id]
    step = state["step"]

    # 現在ステップの回答を保存
    key = HEARING_KEYS[step - 1]
    state["answers"][key] = user_text

    if step < 4:
        # 次の質問へ
        state["step"] += 1
        transition_msgs = [
            "なるほど！\n\n",
            "ありがとうございます😊\n\n",
            "わかりました！\n\n",
        ]
        prefix = transition_msgs[step - 1]
        reply = prefix + HEARING_QUESTIONS[step]
        return reply, make_cancel_quick_reply()
    else:
        # 全問完了 → 提案生成
        answers = state["answers"]
        del hearing_states[user_id]
        proposal = generate_proposal(user_id, answers)
        return proposal, make_main_quick_reply()

def generate_proposal(user_id: str, answers: dict) -> str:
    """ヒアリング結果を元にClaudeが提案文と見積もりを生成する"""
    prompt = f"""以下のヒアリング結果を元に、このお客様への提案と概算見積もりを作成してください。

【ヒアリング結果】
・業種：{answers['industry']}
・一番お困りの作業：{answers['problem']}
・その作業にかかる時間：{answers['hours_per_day']}／日
・従業員数：{answers['employee_count']}

【出力の構成（この順番で書いてください）】
1. お客様の状況まとめ（1〜2文）
2. おすすめのサービスとその理由（具体的に。業種・作業内容に合わせて）
3. 概算見積もり（料金プランから最適なものを提示。根拠も一言添えて）
4. 次のステップ（無料相談への自然な誘導）

【注意事項】
- LINE向けに読みやすく、簡潔にまとめてください
- 親しみやすく前向きなトーンで
- 絵文字を適度に使ってOK
- 全体で200〜300文字程度を目安に
"""

    try:
        response = claude_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        proposal_text = response.content[0].text
    except anthropic.APIError:
        proposal_text = (
            "ヒアリングありがとうございました！\n"
            "現在、提案の生成に失敗しました。\n"
            "お手数ですが、直接お問い合わせください😊\n\n"
            "📧 info@example.com\n"
            "📞 03-XXXX-XXXX"
        )

    return "✨ ヒアリング完了！貴社への提案です ✨\n──────────────\n" + proposal_text


# ===================================================
# 通常会話（Claude API）
# ===================================================

def get_claude_reply(user_id: str, user_text: str) -> tuple[str, QuickReply | None]:
    history = conversation_histories.setdefault(user_id, [])
    history.append({"role": "user", "content": user_text})

    try:
        response = claude_client.messages.create(
            model="claude-opus-4-6",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=history,
        )
        reply_text = response.content[0].text
        history.append({"role": "assistant", "content": reply_text})

        # 古い履歴を削除
        max_messages = MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            conversation_histories[user_id] = history[-max_messages:]

    except anthropic.APIError as e:
        reply_text = (
            "申し訳ありません、エラーが発生しました🙏\n"
            "しばらく経ってから再度お試しください。\n"
            f"（エラーコード: {e.status_code}）"
        )

    contact_keywords = ["問い合わせ", "連絡先", "電話", "メール", "contact"]
    show_quick = not any(kw in user_text for kw in contact_keywords)
    return reply_text, make_main_quick_reply() if show_quick else None


# ===================================================
# Webhook エンドポイント
# ===================================================

@app.route("/webhook", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_text = event.message.text.strip()

    # ─── ① ヒアリング中のユーザー ───
    if user_id in hearing_states:
        if is_cancel(user_text):
            del hearing_states[user_id]
            reply_text = (
                "ヒアリングをキャンセルしました。\n"
                "またいつでもお気軽にどうぞ😊"
            )
            quick_reply = make_main_quick_reply()
        else:
            reply_text, quick_reply = advance_hearing(user_id, user_text)

    # ─── ② ヒアリング開始トリガー ───
    elif is_hearing_trigger(user_text):
        reply_text, quick_reply = start_hearing(user_id)

    # ─── ③ 通常の会話 ───
    else:
        reply_text, quick_reply = get_claude_reply(user_id, user_text)

    with ApiClient(line_configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text, quick_reply=quick_reply)],
            )
        )


# ===================================================
# サーバー起動
# ===================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
