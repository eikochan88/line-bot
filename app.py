"""
Aiden 完全自動AI会社システム
──────────────────────────────────────────────────────────────
LINE受注 → ヒアリング(木村) → 提案承認(大山→栄子) → 契約(林)
→ コード生成(田中+Claude) → GitHub Push → Render Deploy
→ 請求書(中井) → SNS告知(山本) → 納品(上田) → 報告(石田)
──────────────────────────────────────────────────────────────
"""
import os, json, re, base64, time, threading
from datetime import datetime
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage, QuickReply, QuickReplyItem, MessageAction,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from openai import OpenAI
import requests as http

# ════════════════════════════════════════════════════════════════════
# 初期化
# ════════════════════════════════════════════════════════════════════

app = Flask(__name__)

line_cfg  = Configuration(access_token=os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"))
handler   = WebhookHandler(os.environ.get("LINE_CHANNEL_SECRET"))
openai_cl = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
GITHUB_USER       = os.environ.get("GITHUB_USERNAME", "eikochan88")
RENDER_API_KEY    = os.environ.get("RENDER_API_KEY", "")
RENDER_OWNER_ID   = os.environ.get("RENDER_OWNER_ID", "")
EIKO_UID          = os.environ.get("EIKO_LINE_USER_ID", "")
PAYMENT_LINK      = os.environ.get("PAYMENT_LINK", "https://aiden.co.jp/payment")

# ════════════════════════════════════════════════════════════════════
# ステート定義
# ════════════════════════════════════════════════════════════════════

S_IDLE       = "idle"
S_HEARING    = "hearing"          # 木村ヒアリング中
S_APPROVING  = "approving"        # 栄子承認待ち
S_CONTRACT   = "contract_sent"    # 顧客署名待ち
S_DEVELOPING = "developing"       # 自動開発中
S_COMPLETED  = "completed"
S_SURVEY     = "survey"           # AI導入ヒアリングシート

# セッション: {user_id: dict}
sessions: dict[str, dict] = {}
# 栄子の承認キュー: {EIKO_UID: customer_uid}
approval_queue: dict[str, str] = {}
# 通常会話履歴
conv_hist: dict[str, list] = {}


def sess(uid: str) -> dict:
    if uid not in sessions:
        sessions[uid] = {
            "state": S_IDLE, "step": 0, "answers": {},
            "plan": "", "contract": "", "repo": "", "deploy_url": "",
        }
    return sessions[uid]

def reset(uid: str):
    sessions[uid] = {
        "state": S_IDLE, "step": 0, "answers": {},
        "plan": "", "contract": "", "repo": "", "deploy_url": "",
    }

# ════════════════════════════════════════════════════════════════════
# Aidenサービス定義
# ════════════════════════════════════════════════════════════════════

SERVICES = """
【Aidenサービスと料金】
・LINEチャットボット：初期20万円〜 / 月額3万円〜
・AI業務自動化：初期30万円〜 / 月額5万円〜
・会社紹介動画：1本5万円〜
・SNS広告動画：月額10万円〜
・AI導入コンサル：月額10万円〜
"""

# ════════════════════════════════════════════════════════════════════
# AI社員システムプロンプト
# ════════════════════════════════════════════════════════════════════

EMP = {
    "kimura": f"あなたはAiden株式会社の木村忠史（営業部長）です。明るく親しみやすく、お客様のニーズをヒアリングして最適なAIソリューションを提案します。LINE向けに4〜6行・絵文字適度に。{SERVICES}",
    "oyama":  f"あなたはAiden株式会社の大山光（代表取締役社長）です。お客様の要望を分析し具体的なプロジェクト提案書を作成します。品質・コスト・納期のバランスを明確に示します。{SERVICES}",
    "tanaka": "あなたはAiden株式会社の田中功（開発部長）です。技術仕様を設計し、Claude APIを活用したコード生成を指揮します。実装の詳細を明確に説明し技術的な問題を迅速に解決します。",
    "hayashi":"あなたはAiden株式会社の林佳代（人事・法務部長）です。業務委託契約書を作成し、お客様に分かりやすく説明します。法的要件を満たしつつ平易な日本語で記述します。",
    "nakai":  "あなたはAiden株式会社の中井誠（経理部長）です。正確な請求書を作成し支払い条件を明確に伝えます。着手金・完了金の管理と入金確認を迅速に行います。",
    "yamamoto":"あなたはAiden株式会社の山本彩（マーケティング部長）です。プロジェクト完成後のSNS告知文・動画台本を作成します。Instagram・X・TikTok向けに魅力的なコンテンツを提供します。",
    "ueda":   "あなたはAiden株式会社の上田恵（カスタマーサポート部長）です。納品完了の連絡をお客様に丁寧に伝え、アフターサポートを案内します。お客様の満足度を最大化するフォローアップを行います。",
    "ishida": "あなたはAiden株式会社の石田圭（専務取締役）です。プロジェクト全体の進捗を管理し栄子会長への報告書を作成します。リスク管理と各部署の連携を統括します。",
}


def ai(emp_id: str, prompt: str, max_tokens: int = 700) -> str:
    try:
        res = openai_cl.chat.completions.create(
            model="gpt-4o-mini", max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": EMP.get(emp_id, "")},
                {"role": "user",   "content": prompt},
            ],
        )
        return res.choices[0].message.content
    except Exception as e:
        return f"（エラー: {e}）"

# ════════════════════════════════════════════════════════════════════
# 1. ヒアリング（木村忠史）
# ════════════════════════════════════════════════════════════════════

QUESTIONS = [
    "Q1/5｜業種を教えてください😊\n（例：飲食・医療・製造・ITなど）",
    "Q2/5｜一番お困りの業務は何ですか？\n具体的に教えていただけると✨",
    "Q3/5｜従業員は何人いらっしゃいますか？",
    "Q4/5｜ご予算感を教えてください💰\n（例：20万・50万・100万以上）",
    "Q5/5｜導入希望時期を教えてください📅\n（例：来月・3ヶ月以内・半年以内）",
]
KEYS     = ["industry", "problem", "employees", "budget", "timeline"]
TRIGGERS = ["相談", "提案", "依頼", "作って", "導入", "始めたい", "お願い", "無料", "診断", "頼みたい"]
CANCELS  = ["キャンセル", "やめる", "中断", "最初から"]
STEP_PFX = ["なるほど！\n\n", "ありがとうございます😊\n\n", "わかりました！\n\n", "承知しました✨\n\n"]

# ── AI導入ヒアリングシート用定数 ───────────────────────────────────────
SURVEY_TRIGGERS = ["ヒアリング", "導入相談"]

SURVEY_SERVICES = {
    "1": "LINEチャットボット",
    "2": "メール自動返信",
    "3": "請求書自動作成",
    "4": "SNS広告動画制作",
    "5": "複数まとめて導入したい",
}

SURVEY_PRICING = {
    "1": "初期費用：20万円〜 / 月額：3万円〜 / 納期：2〜4週間",
    "2": "初期費用：15万円〜 / 月額：2万円〜 / 納期：1〜3週間",
    "3": "初期費用：15万円〜 / 月額：2万円〜 / 納期：1〜3週間",
    "4": "月額：10万円〜（撮影・編集込） / 納期：2〜4週間",
    "5": "お見積もり対応（複数割引あり） / 納期：要相談",
}

SURVEY_Q = [
    "Q1/4｜業種を教えてください😊\n（例：飲食・医療・製造・ITなど）",
    "Q2/4｜現在の業務対応方法は？\n（例：手作業・Excel管理・外部委託など）",
    "Q3/4｜導入後に期待する成果は？\n（例：作業時間削減・コスト削減・売上向上など）",
    "Q4/4｜心配な点はありますか？\n（例：コスト・セキュリティ・操作の複雑さなど）",
]
SURVEY_KEYS = ["s_industry", "s_current", "s_expectation", "s_concern"]

# ════════════════════════════════════════════════════════════════════
# AI導入ヒアリングシート：提案生成（木村忠史）
# ════════════════════════════════════════════════════════════════════

def gen_survey_proposal(answers: dict) -> str:
    service  = answers.get("s_service_name", "AIサービス")
    pricing  = answers.get("s_pricing", "")
    p = f"""お客様のヒアリング結果から、AI導入の提案を作成してください。

【選択サービス】{service}
【業種】{answers.get('s_industry')}
【現在の業務方法】{answers.get('s_current')}
【期待する成果】{answers.get('s_expectation')}
【心配な点】{answers.get('s_concern')}
【料金目安】{pricing}

LINE向け・300文字以内・絵文字適度に。以下の構成で：
①選択サービスの概要（1〜2文）
②お客様の課題への具体的な解決策（2〜3点）
③期待できる効果
④「ご興味があれば下記の決済リンクからお申込みいただけます！」で締める。"""
    return "✨ 木村忠史からのご提案 ✨\n" + "━"*18 + "\n" + ai("kimura", p, 700)

# ════════════════════════════════════════════════════════════════════
# 2. 提案書生成（大山社長）
# ════════════════════════════════════════════════════════════════════

def gen_plan(answers: dict) -> str:
    p = f"""ヒアリング結果から詳細なプロジェクト提案書を作成してください。

業種：{answers.get('industry')} / 課題：{answers.get('problem')}
従業員：{answers.get('employees')} / 予算：{answers.get('budget')} / 時期：{answers.get('timeline')}

構成：①プロジェクト概要(2文) ②推奨サービスと理由 ③具体的な機能(3〜5項目)
     ④料金(初期・月額) ⑤納期(週単位) ⑥期待効果

LINE向け・400文字以内・絵文字適度に。最後に「「申し込む」とお送りください😊」と書く。"""
    return "✨ 大山社長からのご提案 ✨\n" + "━"*18 + "\n" + ai("oyama", p, 700)

# ════════════════════════════════════════════════════════════════════
# 3. 契約書生成（林佳代）
# ════════════════════════════════════════════════════════════════════

def gen_contract(answers: dict, plan: str) -> str:
    today = datetime.now().strftime("%Y年%m月%d日")
    p = f"""業務委託契約書を作成してください。

契約日：{today} / 業種：{answers.get('industry')}
提案内容：{plan[:200]}

構成：タイトル / 委託者・受託者 / 業務内容 / 料金・支払条件(着手金50%/完了50%) / 納期 / 著作権 / 秘密保持

350文字以内。最後に「「同意します」で契約完了・開発スタートです✅」と書く。"""
    return "📋 業務委託契約書 📋\n" + "━"*18 + "\n" + ai("hayashi", p, 700)

# ════════════════════════════════════════════════════════════════════
# 4. コード生成（田中功 + Claude API）
# ════════════════════════════════════════════════════════════════════

def gen_code(answers: dict, plan: str) -> dict:
    """Claude APIでFlaskアプリを生成。フォールバックあり。"""
    prompt = f"""You are an expert Python/Flask developer. Generate a complete, working web application.

Customer requirements:
- Industry: {answers.get('industry')}
- Problem to solve: {answers.get('problem')}
- Project plan summary: {plan[:400]}

Return ONLY a valid JSON object with these exact keys (no markdown, no explanation):
{{
  "app.py": "complete flask application code here",
  "requirements.txt": "flask==3.0.3\\ngunicorn==22.0.0",
  "Procfile": "web: gunicorn app:app",
  "templates/index.html": "complete html with bootstrap 5 CDN"
}}

Requirements:
- app.py: Complete Flask app, at least 3 routes, practical functionality for the customer's use case
- index.html: Clean Japanese UI with Bootstrap 5, responsive design
- Must run on Render (PORT env var support)
- All UI text in Japanese"""

    # Try Claude API first
    if ANTHROPIC_API_KEY:
        try:
            import anthropic as _ant
            cl = _ant.Anthropic(api_key=ANTHROPIC_API_KEY)
            res = cl.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=6000,
                messages=[{"role": "user", "content": prompt}],
            )
            content = res.content[0].text
            match = re.search(r'\{[\s\S]*\}', content)
            if match:
                return json.loads(match.group())
        except Exception as e:
            print(f"Claude API error: {e}")

    # Fallback: OpenAI with json_object
    try:
        res = openai_cl.chat.completions.create(
            model="gpt-4o-mini", max_tokens=4000,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": EMP["tanaka"]},
                {"role": "user",   "content": prompt},
            ],
        )
        return json.loads(res.choices[0].message.content)
    except Exception as e:
        print(f"OpenAI code gen error: {e}")

    # Final fallback: minimal app
    industry = answers.get('industry', '')
    problem  = answers.get('problem', '')
    return {
        "app.py": f'''import os
from flask import Flask, render_template, request, jsonify
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    return jsonify({{"status": "ok", "service": "Aiden制作"}})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
''',
        "requirements.txt": "flask==3.0.3\ngunicorn==22.0.0",
        "Procfile": "web: gunicorn app:app",
        "templates/index.html": f'''<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AIソリューション | {industry}</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
</head>
<body class="bg-light">
<div class="container py-5 text-center">
  <h1 class="display-5 mb-3">✨ {industry}向け AIソリューション</h1>
  <p class="lead text-muted">{problem} を解決します</p>
  <div class="card mt-4 shadow-sm">
    <div class="card-body py-4">
      <p class="mb-0">Aiden株式会社が制作しました。</p>
    </div>
  </div>
</div>
</body>
</html>''',
    }

# ════════════════════════════════════════════════════════════════════
# 5. GitHub API（田中功）
# ════════════════════════════════════════════════════════════════════

GH_API = "https://api.github.com"

def gh_headers() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

def gh_create_repo(name: str) -> bool:
    r = http.post(f"{GH_API}/user/repos", headers=gh_headers(),
                  json={"name": name, "private": False, "auto_init": False})
    return r.status_code in (201, 422)

def gh_push_file(repo: str, path: str, content: str, msg: str = "Add file") -> bool:
    encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    url = f"{GH_API}/repos/{GITHUB_USER}/{repo}/contents/{path}"
    # Get existing SHA (for updates)
    existing = http.get(url, headers=gh_headers())
    data = {"message": msg, "content": encoded, "branch": "main"}
    if existing.status_code == 200:
        data["sha"] = existing.json().get("sha", "")
    r = http.put(url, headers=gh_headers(), json=data)
    return r.status_code in (200, 201)

def gh_push_all(repo: str, files: dict) -> bool:
    if not GITHUB_TOKEN:
        return False
    if not gh_create_repo(repo):
        return False
    time.sleep(1)
    for path, content in files.items():
        if not gh_push_file(repo, path, content, f"Aiden: add {path}"):
            return False
        time.sleep(0.4)
    return True

# ════════════════════════════════════════════════════════════════════
# 6. Render API（田中功）
# ════════════════════════════════════════════════════════════════════

def render_deploy(repo: str, svc_name: str) -> dict:
    repo_url = f"https://github.com/{GITHUB_USER}/{repo}"
    if not RENDER_API_KEY or not RENDER_OWNER_ID:
        return {"url": repo_url, "note": "Render API未設定 → GitHubから手動デプロイしてください"}

    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"}
    body = {
        "autoDeploy": "yes",
        "branch": "main",
        "name": svc_name,
        "ownerId": RENDER_OWNER_ID,
        "plan": "free",
        "repo": repo_url,
        "type": "web_service",
        "envSpecificDetails": {
            "buildCommand": "pip install -r requirements.txt",
            "startCommand": "gunicorn app:app",
        },
        "serviceDetails": {"env": "python", "pullRequestPreviewsEnabled": "no"},
    }
    try:
        r = http.post("https://api.render.com/v1/services", json=body, headers=headers, timeout=30)
        if r.status_code in (200, 201):
            svc = r.json().get("service", {})
            svc_id = svc.get("id", "")
            url = f"https://{svc_name}.onrender.com"
            return {"url": url, "service_id": svc_id}
        else:
            return {"url": repo_url, "error": r.text[:200]}
    except Exception as e:
        return {"url": repo_url, "error": str(e)}

# ════════════════════════════════════════════════════════════════════
# 7. 請求書生成（中井誠）
# ════════════════════════════════════════════════════════════════════

def gen_invoice(answers: dict, plan: str) -> str:
    today = datetime.now().strftime("%Y年%m月%d日")
    due   = datetime.now().strftime("%Y年%m月") + "末日"
    p = f"""着手金の請求書を作成してください。

発行日：{today} / 支払期限：{due}
業種：{answers.get('industry')} / サービス：{plan[:120]}

構成：タイトル「請求書（着手金50%）」/ 発行Aiden / 日付・期限 / 請求内容・金額 / 消費税10% / 合計 / 振込先（みずほ銀行 渋谷支店 普通 1234567 カ）ライデン） / 備考「ご入金確認後、開発を継続いたします」

250文字以内。"""
    return "🧾 請求書（着手金） 🧾\n" + "━"*18 + "\n" + ai("nakai", p, 600)

# ════════════════════════════════════════════════════════════════════
# 8. SNS告知文生成（山本彩）
# ════════════════════════════════════════════════════════════════════

def gen_sns(answers: dict, deploy_url: str) -> str:
    p = f"""プロジェクト完成のSNS告知文を3パターン作成してください。

業種：{answers.get('industry')} / 解決課題：{answers.get('problem')} / URL：{deploy_url}

①X（旧Twitter）用・140文字・ハッシュタグ5個
②Instagram用・200文字・絵文字多め・ハッシュタグ10個
③TikTok用・動画台本アウトライン（15秒）

各パターンをラベルを付けて区切って書いてください。"""
    return ai("yamamoto", p, 600)

# ════════════════════════════════════════════════════════════════════
# 自動開発パイプライン（バックグラウンドスレッド）
# ════════════════════════════════════════════════════════════════════

def push_to(uid: str, text: str):
    """顧客 + 栄子への進捗プッシュ"""
    _push_line(uid, text)
    if EIKO_UID and uid != EIKO_UID:
        _push_line(EIKO_UID, f"【進捗】{text[:200]}")


def run_pipeline(customer_uid: str):
    """契約完了後に自動実行される全工程パイプライン"""
    s = sess(customer_uid)
    answers = s["answers"]
    plan    = s["plan"]

    try:
        # ── Step1: 開始通知（石田圭） ──────────────────────────────
        ishida_msg = ai("ishida",
            f"プロジェクト開始の報告を栄子会長と顧客に送ってください。\n"
            f"業種：{answers.get('industry')} / 課題：{answers.get('problem')}\n"
            f"LINE向け・100文字以内・前向きに", 200)
        push_to(customer_uid, f"🚀 石田圭（専務）\n{ishida_msg}")
        time.sleep(1)

        # ── Step2: コード生成（田中功 + Claude） ───────────────────
        push_to(customer_uid,
            "⚙️ 田中功（開発部長）\n"
            "Claudeが要件分析・設計・コーディング中です...\n"
            "しばらくお待ちください🔧")
        code_files = gen_code(answers, plan)
        push_to(customer_uid, f"✅ 田中功\nコード生成完了！（{len(code_files)}ファイル）\nGitHubにプッシュします📦")

        # ── Step3: GitHubプッシュ ──────────────────────────────────
        repo = re.sub(r'[^a-z0-9\-]', '',
            f"aiden-{answers.get('industry','proj')[:8].lower().replace(' ','-')}"
            f"-{datetime.now().strftime('%m%d%H%M')}"
        )[:40]
        s["repo"] = repo
        gh_ok = gh_push_all(repo, code_files)

        if gh_ok:
            push_to(customer_uid,
                f"✅ 田中功\nGitHubプッシュ完了！\n"
                f"🔗 https://github.com/{GITHUB_USER}/{repo}")
        else:
            push_to(customer_uid,
                f"⚠️ 田中功\nGitHubプッシュをスキップ\n"
                f"（GITHUB_TOKEN未設定または接続エラー）")

        # ── Step4: Renderデプロイ ──────────────────────────────────
        push_to(customer_uid,
            "🌐 田中功\nRenderにデプロイ中...\n"
            "通常2〜3分かかります⏳")
        deploy = render_deploy(repo, repo[:50])
        deploy_url = deploy.get("url", f"https://github.com/{GITHUB_USER}/{repo}")
        s["deploy_url"] = deploy_url

        if "error" in deploy or "note" in deploy:
            note = deploy.get("note") or deploy.get("error", "")
            push_to(customer_uid,
                f"⚠️ 田中功\nRenderデプロイ情報：\n{note}\n"
                f"GitHub: https://github.com/{GITHUB_USER}/{repo}")
        else:
            push_to(customer_uid,
                f"✅ 田中功\nデプロイ完了！\n🔗 {deploy_url}")
        time.sleep(1)

        # ── Step5: 請求書（中井誠） ────────────────────────────────
        invoice = gen_invoice(answers, plan)
        _push_line(customer_uid, invoice)
        push_to(customer_uid,
            "💰 中井誠（経理部長）\n"
            "請求書を送付いたしました。\n"
            "ご確認の上、お支払いをお願いいたします🙏")

        # ── Step6: SNS告知文（山本彩 → 栄子のみ） ─────────────────
        if EIKO_UID:
            sns = gen_sns(answers, deploy_url)
            _push_line(EIKO_UID,
                f"📣 山本彩（マーケ）SNS告知文\n{'━'*18}\n{sns}")

        # ── Step7: 納品連絡（上田恵） ─────────────────────────────
        delivery = ai("ueda",
            f"納品完了の連絡をお客様に送ってください。\n"
            f"納品URL：{deploy_url}\n業種：{answers.get('industry')}\n"
            f"解決課題：{answers.get('problem')}\n"
            f"LINE向け・150文字・丁寧に・アフターサポートも案内", 400)
        _push_line(customer_uid,
            f"🎁 上田恵（CSマネージャー）より納品のご連絡\n{'━'*18}\n"
            f"{delivery}\n\n🔗 納品URL：{deploy_url}")

        # ── Step8: 完了報告（石田圭 → 栄子） ──────────────────────
        s["state"] = S_COMPLETED
        if EIKO_UID:
            _push_line(EIKO_UID,
                f"🎉 石田圭（専務）完了報告\n{'━'*18}\n"
                f"【プロジェクト完了】\n"
                f"業種：{answers.get('industry')}\n"
                f"課題：{answers.get('problem')}\n"
                f"納品URL：{deploy_url}\n"
                f"GitHub：https://github.com/{GITHUB_USER}/{repo}\n"
                f"全工程が完了しました✨")

    except Exception as e:
        err = f"⚠️ システムエラー\n{str(e)[:150]}\n\n担当者より改めてご連絡します。\n📧 info@aiden.co.jp"
        _push_line(customer_uid, err)
        if EIKO_UID:
            _push_line(EIKO_UID, f"❌ パイプラインエラー\nUID: {customer_uid}\n{str(e)[:300]}")

# ════════════════════════════════════════════════════════════════════
# LINEメッセージユーティリティ
# ════════════════════════════════════════════════════════════════════

def qr(*pairs) -> QuickReply:
    return QuickReply(items=[
        QuickReplyItem(action=MessageAction(label=l, text=t)) for l, t in pairs
    ])

QR_MAIN     = lambda: qr(("✅ 相談を始める", "相談したい"), ("サービスを見る", "サービス内容を教えて"), ("料金を聞く", "料金を教えて"))
QR_CANCEL   = lambda: qr(("❌ キャンセル", "キャンセル"))
QR_APPLY    = lambda: qr(("✍️ 申し込む", "申し込む"), ("検討します", "少し検討します"))
QR_AGREE    = lambda: qr(("✅ 同意します", "同意します"), ("質問があります", "質問があります"))
QR_EIKO     = lambda: qr(("✅ 承認", "承認"), ("❌ 却下", "却下"))
QR_DONE     = lambda: qr(("追加のご相談", "相談したい"), ("お問い合わせ", "お問い合わせしたい"))
QR_SERVICES = lambda: qr(
    ("1.LINEチャットボット", "1"),
    ("2.メール自動返信", "2"),
    ("3.請求書自動作成", "3"),
    ("4.SNS広告動画制作", "4"),
    ("5.複数まとめて導入", "5"),
)


def _push_line(uid: str, text: str):
    if not uid:
        return
    try:
        with ApiClient(line_cfg) as api:
            MessagingApi(api).push_message(
                PushMessageRequest(to=uid, messages=[TextMessage(text=text[:4999])])
            )
    except Exception as e:
        print(f"push error: {e}")


def _reply(token: str, items: list):
    """[(text, qr_or_None), ...]"""
    msgs = []
    for i, (text, qr_obj) in enumerate(items):
        m = TextMessage(text=str(text)[:4999])
        if qr_obj and i == len(items) - 1:
            m.quick_reply = qr_obj
        msgs.append(m)
    try:
        with ApiClient(line_cfg) as api:
            MessagingApi(api).reply_message(
                ReplyMessageRequest(reply_token=token, messages=msgs[:5])
            )
    except Exception as e:
        print(f"reply error: {e}")

# ════════════════════════════════════════════════════════════════════
# 通常会話
# ════════════════════════════════════════════════════════════════════

def general_chat(uid: str, text: str) -> str:
    hist = conv_hist.setdefault(uid, [])
    hist.append({"role": "user", "content": text})
    try:
        res = openai_cl.chat.completions.create(
            model="gpt-4o-mini", max_tokens=400,
            messages=[{"role": "system", "content": EMP["kimura"]}] + hist[-10:],
        )
        reply = res.choices[0].message.content
        hist.append({"role": "assistant", "content": reply})
        if len(hist) > 20:
            conv_hist[uid] = hist[-20:]
        return reply
    except Exception as e:
        return f"申し訳ありません🙏 エラーが発生しました。({e})"

# ════════════════════════════════════════════════════════════════════
# Webhook ハンドラ
# ════════════════════════════════════════════════════════════════════

@app.route("/webhook", methods=["POST"])
def callback():
    sig  = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, sig)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    uid  = event.source.user_id
    text = event.message.text.strip()

    # ══════════════════════════════════════════════════════════════
    # 栄子さんの承認フロー
    # ══════════════════════════════════════════════════════════════
    if uid == EIKO_UID and uid in approval_queue:
        customer_uid = approval_queue[uid]

        if any(w in text for w in ["承認", "OK", "ok", "よし", "いいね", "進めて"]):
            del approval_queue[uid]
            cs = sess(customer_uid)
            contract = gen_contract(cs["answers"], cs["plan"])
            cs["contract"] = contract
            cs["state"]    = S_CONTRACT

            _push_line(customer_uid, contract)
            _push_line(customer_uid,
                "ご確認ください😊\n"
                "「同意します」とお送りいただくと\n"
                "契約完了・開発スタートです🚀")
            _reply(event.reply_token, [("✅ 承認しました。契約書を顧客に送付しました。", None)])

        elif any(w in text for w in ["却下", "NG", "ng", "修正", "やり直し", "待って"]):
            customer_uid2 = approval_queue.pop(uid)
            reason = re.sub(r'(却下|NG|ng|修正|やり直し|待って)', '', text).strip()
            cs = sess(customer_uid2)
            cs["state"] = S_IDLE
            _push_line(customer_uid2,
                "ご提案の内容を調整いたします。\n"
                "改めてご連絡いたしますので\n少々お待ちください🙏")
            _reply(event.reply_token, [(
                f"❌ 却下しました。顧客に調整中と連絡済みです。\n理由メモ：{reason or '（なし）'}", None
            )])

        else:
            # 通常会話（承認待ち中も普通に会話できる）
            reply = general_chat(uid, text)
            pending_info = f"\n\n📌 現在、承認待ち案件あり\n「承認」または「却下」でお返事ください。"
            _reply(event.reply_token, [(reply + pending_info, QR_EIKO())])
        return

    # ══════════════════════════════════════════════════════════════
    # 顧客フロー（ステートマシン）
    # ══════════════════════════════════════════════════════════════
    s     = sess(uid)
    state = s["state"]

    # LINE ユーザーID確認コマンド
    if "自分のID" in text or "マイID" in text:
        _reply(event.reply_token, [(f"あなたのLINEユーザーIDは：\n\n{uid}\n\nこのIDを .env の EIKO_LINE_USER_ID に設定してください。", None)])
        return

    # キャンセル（開発中・完了以外で有効）
    if any(w in text for w in CANCELS) and state not in (S_DEVELOPING, S_COMPLETED):
        reset(uid)
        _reply(event.reply_token, [(
            "ご相談をキャンセルしました。\nまたいつでもお気軽にご連絡ください😊", QR_MAIN()
        )])
        return

    # ── IDLE ──────────────────────────────────────────────────────
    if state == S_IDLE:
        if any(t in text for t in SURVEY_TRIGGERS):
            # AI導入ヒアリングシートフロー
            s["state"] = S_SURVEY
            s["step"]  = 0
            s["answers"] = {}
            _reply(event.reply_token, [(
                "AI導入ヒアリングシートへようこそ！🎉\n"
                "木村（営業）が担当します😊\n\n"
                "まず、ご興味のあるサービスを\n"
                "選んでください👇\n\n"
                "1️⃣ LINEチャットボット\n"
                "2️⃣ メール自動返信\n"
                "3️⃣ 請求書自動作成\n"
                "4️⃣ SNS広告動画制作\n"
                "5️⃣ 複数まとめて導入したい",
                QR_SERVICES()
            )])
        elif any(t in text for t in TRIGGERS):
            s["state"] = S_HEARING
            s["step"]  = 0
            _reply(event.reply_token, [(
                "ありがとうございます！🎉\n"
                "木村（営業）が担当いたします！\n\n"
                "最適なAIソリューションをご提案するために\n"
                "5つだけ質問させてください✨\n\n"
                "（「キャンセル」でいつでも中断できます）\n\n"
                + QUESTIONS[0], QR_CANCEL()
            )])
        else:
            _reply(event.reply_token, [(general_chat(uid, text), QR_MAIN())])

    # ── SURVEY（AI導入ヒアリングシート） ──────────────────────────
    elif state == S_SURVEY:
        step = s["step"]

        if step == 0:
            # サービス選択
            if text in SURVEY_SERVICES:
                s["answers"]["s_service"]      = text
                s["answers"]["s_service_name"] = SURVEY_SERVICES[text]
                s["answers"]["s_pricing"]      = SURVEY_PRICING[text]
                s["step"] = 1
                _reply(event.reply_token, [(
                    f"「{SURVEY_SERVICES[text]}」を選択しました✅\n\n"
                    f"料金目安：{SURVEY_PRICING[text]}\n\n"
                    f"続けて詳しくお聞かせください😊\n\n"
                    + SURVEY_Q[0], QR_CANCEL()
                )])
            else:
                _reply(event.reply_token, [(
                    "1〜5の番号でお選びください😊\n"
                    "下のボタンをタップしてください👇",
                    QR_SERVICES()
                )])

        elif 1 <= step <= 4:
            # Q1〜Q4の回答を保存
            q_idx = step - 1
            s["answers"][SURVEY_KEYS[q_idx]] = text
            s["step"] += 1

            if s["step"] <= 4:
                pfx = STEP_PFX[min(step - 1, len(STEP_PFX) - 1)]
                _reply(event.reply_token, [(
                    pfx + SURVEY_Q[s["step"] - 1], QR_CANCEL()
                )])
            else:
                # 全質問完了 → 提案生成（バックグラウンド）
                _reply(event.reply_token, [(
                    "ありがとうございました！🙏\n"
                    "木村が提案を作成中です...\n"
                    "少々お待ちください✨", None
                )])

                def _gen_survey():
                    captured_answers = dict(s["answers"])
                    captured_uid = uid

                    # 提案書送付
                    proposal = gen_survey_proposal(captured_answers)
                    _push_line(captured_uid, proposal)

                    # 料金・納期送付
                    _push_line(captured_uid,
                        f"💰 料金・納期の目安\n{'━'*18}\n"
                        f"【{captured_answers.get('s_service_name', '')}】\n"
                        f"{captured_answers.get('s_pricing', '')}\n\n"
                        f"※正式なお見積もりはお申込み後にご提出します。"
                    )

                    # 決済リンク送付
                    _push_line(captured_uid,
                        f"📲 お申込み・お支払いはこちら\n{'━'*18}\n"
                        f"{PAYMENT_LINK}\n\n"
                        f"ご不明な点はお気軽にどうぞ😊\n"
                        f"📧 info@aiden.co.jp"
                    )

                    # 栄子さんに通知
                    if EIKO_UID:
                        _push_line(EIKO_UID,
                            f"📋 ヒアリングシート完了通知\n{'━'*18}\n"
                            f"【サービス】{captured_answers.get('s_service_name')}\n"
                            f"【業種】{captured_answers.get('s_industry')}\n"
                            f"【現在の方法】{captured_answers.get('s_current')}\n"
                            f"【期待成果】{captured_answers.get('s_expectation')}\n"
                            f"【心配事】{captured_answers.get('s_concern')}\n"
                            f"{'━'*18}\n"
                            f"提案書・料金・決済リンクを送付済みです✅"
                        )

                    # セッションをIDLEに戻す
                    reset(captured_uid)

                threading.Thread(target=_gen_survey, daemon=True).start()

    # ── HEARING（木村忠史） ────────────────────────────────────────
    elif state == S_HEARING:
        step = s["step"]
        s["answers"][KEYS[step]] = text
        s["step"] += 1

        if s["step"] < 5:
            _reply(event.reply_token, [(
                STEP_PFX[s["step"] - 1] + QUESTIONS[s["step"]], QR_CANCEL()
            )])
        else:
            # ヒアリング完了 → 大山社長が提案書生成（バックグラウンド）
            _reply(event.reply_token, [(
                "ありがとうございました！🙏\n"
                "大山社長が提案書を作成中です...\n"
                "少々お待ちください✨", None
            )])

            def _gen_plan():
                plan = gen_plan(s["answers"])
                s["plan"]  = plan
                s["state"] = S_APPROVING

                if EIKO_UID:
                    # 栄子に承認依頼
                    approval_queue[EIKO_UID] = uid
                    _push_line(EIKO_UID,
                        f"📋 大山社長より承認依頼\n{'━'*18}\n"
                        f"{plan}\n\n"
                        f"「承認」または「却下」とお返事ください。")
                    _push_line(uid,
                        "📤 提案書を作成しました！\n"
                        "栄子会長の承認後、ご連絡いたします🙏\n\n"
                        "通常数分以内にお送りします。")
                else:
                    # 栄子未設定 → 自動承認・顧客に提案書送付
                    _push_line(uid, plan)
                    _push_line(uid, "「申し込む」とお送りください😊")

            threading.Thread(target=_gen_plan, daemon=True).start()

    # ── APPROVING（栄子承認待ち中の顧客） ─────────────────────────
    elif state == S_APPROVING:
        if any(w in text for w in ["申し込む", "申込"]):
            if not EIKO_UID:
                # 自動承認モード
                contract = gen_contract(s["answers"], s["plan"])
                s["contract"] = contract
                s["state"]    = S_CONTRACT
                _reply(event.reply_token, [(contract, QR_AGREE())])
            else:
                _reply(event.reply_token, [(
                    "ありがとうございます！\n"
                    "栄子会長の承認後、契約書をお送りします😊\n"
                    "もう少しだけお待ちください。", None
                )])
        else:
            _reply(event.reply_token, [(general_chat(uid, text), QR_APPLY())])

    # ── CONTRACT_SENT（林佳代 → 顧客署名待ち） ─────────────────────
    elif state == S_CONTRACT:
        if "同意" in text:
            s["state"] = S_DEVELOPING
            _reply(event.reply_token, [(
                "🎉 ご契約ありがとうございます！\n\n"
                "ただいまより開発を開始します🚀\n"
                "進捗は随時このLINEでご報告いたします。\n"
                "完成まで少々お待ちください✨", None
            )])
            # バックグラウンドでパイプライン実行
            threading.Thread(target=run_pipeline, args=(uid,), daemon=True).start()

        elif any(w in text for w in ["質問", "聞きたい", "教えて"]):
            _reply(event.reply_token, [(
                "ご質問をどうぞ😊\n"
                "準備ができましたら「同意します」とお送りください✅", QR_AGREE()
            )])
        else:
            _reply(event.reply_token, [(general_chat(uid, text), QR_AGREE())])

    # ── DEVELOPING（自動開発中） ────────────────────────────────────
    elif state == S_DEVELOPING:
        _reply(event.reply_token, [(
            "現在、全力で開発中です🔧✨\n"
            "完成次第すぐにご連絡いたしますので\n"
            "もう少しだけお待ちください😊", None
        )])

    # ── COMPLETED（完了） ───────────────────────────────────────────
    elif state == S_COMPLETED:
        deploy_url = s.get("deploy_url", "")
        if any(t in text for t in TRIGGERS):
            reset(uid)
            s2 = sess(uid)
            s2["state"] = S_HEARING
            s2["step"]  = 0
            _reply(event.reply_token, [(
                "追加のご相談ありがとうございます！😊\n\n" + QUESTIONS[0], QR_CANCEL()
            )])
        else:
            _reply(event.reply_token, [(
                f"ご契約済みです🎉\n\n"
                f"{'🔗 ' + deploy_url if deploy_url else ''}\n\n"
                f"ご不明な点・追加のご相談はお気軽に😊\n"
                f"📧 info@aiden.co.jp", QR_DONE()
            )])


# ════════════════════════════════════════════════════════════════════
# 起動
# ════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)
