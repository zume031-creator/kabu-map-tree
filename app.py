from flask import Flask, render_template, request, redirect, url_for, flash, jsonify
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import select, asc, desc
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv
import os
import json
import re
from operator import itemgetter
# Vertex AI 関連のライブラリをインポート
import vertexai
from vertexai.generative_models import GenerativeModel

load_dotenv()
app = Flask(__name__)

# --- ▼▼▼ Renderデプロイ用に修正した箇所 ▼▼▼ ---

# SECRET_KEYを環境変数から取得するように変更
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default-secret-key-for-local-dev')

# データベース接続設定を環境変数に応じて切り替える
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    # Render環境のPostgreSQLに接続
    app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL.replace('postgres://', 'postgresql://', 1)
else:
    # ローカル開発環境のSQLiteに接続
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# --- ▲▲▲ 修正箇所ここまで ▲▲▲ ---

# 開発中はTrueにするとAPIを消費せずに固定データを返す
DEV_MODE = False
# --- Vertex AI の初期化 ---
try:
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    location = os.environ.get("GOOGLE_CLOUD_LOCATION")
    # ローカル認証とGCP環境での認証を両立させるため、projectとlocationが取得できた場合のみ初期化
    if project_id and location:
        vertexai.init(project=project_id, location=location)
except Exception as e:
    print(f"Vertex AIの初期化中にエラーが発生しました: {e}")
    # アプリケーションの起動は続行する

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

SECTOR_TRANSLATION = {
    'Consumer Cyclical': '一般消費財', 'Technology': 'テクノロジー', 'Industrials': '資本財',
    'Healthcare': 'ヘルスケア', 'Financial Services': '金融', 'Communication Services': '通信サービス',
    'Consumer Defensive': '生活必需品', 'Basic Materials': '素材', 'Real Estate': '不動産',
    'Utilities': '公共事業', 'Energy': 'エネルギー', 'N/A': 'N/A'
}
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    stocks = db.relationship('StockItem', backref='owner', lazy=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
class StockItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ticker = db.Column(db.String(20), nullable=False)
    company_name = db.Column(db.String(100), nullable=True)
    company_name_en = db.Column(db.String(100), nullable=True)
    sector = db.Column(db.String(50), nullable=True)
    memo = db.Column(db.Text, nullable=True)
    rating = db.Column(db.String(10), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    entry_price = db.Column(db.Float, nullable=False)
    current_price = db.Column(db.Float, nullable=True)
    rating_date = db.Column(db.DateTime, nullable=True)
    per = db.Column(db.Float, nullable=True)
    pbr = db.Column(db.Float, nullable=True)
    dividend_yield = db.Column(db.Float, nullable=True)
    analysis_text = db.Column(db.Text, nullable=True)
    has_update = db.Column(db.Boolean, default=False, nullable=False)
    @property
    def performance(self):
        if self.current_price and self.entry_price and self.entry_price > 0:
            return (self.current_price / self.entry_price - 1) * 100
        return 0
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))
def extract_json(text):
    text = re.sub(r'```json\s*(.*?)\s*```', r'\1', text, flags=re.DOTALL)
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        return match.group(0)
    return text
def get_japanese_name_by_gemini(ticker):
    if DEV_MODE:
        return "（開発モード名称）"
    try:
        model = GenerativeModel("gemini-2.5-flash")
        prompt = f"""
        証券コード「{ticker}」の正式な日本語社名を教えてください。
        以下のJSON形式のみで、他の文字列は一切含めずに回答してください。
        {{ "japanese_name": "日本語の正式社名" }}
        """
        response = model.generate_content(prompt)
        json_string = extract_json(response.text)
        if json_string:
            data = json.loads(json_string)
            if "japanese_name" in data:
                return data["japanese_name"]
    except Exception as e:
        print(f"Error getting Japanese name from Gemini for {ticker}: {e}")
    return ticker
def generate_initial_analysis(ticker, company_name):
    """Gemini を使って銘柄の初期分析を生成する"""
    if DEV_MODE:
        return "これは開発モードの分析テキストです。"
    try:
        model = GenerativeModel("gemini-2.5-flash")
        prompt = f"""
        あなたはプロの証券アナリストです。日本の企業「{company_name}（証券コード: {ticker}）」について、以下の観点から詳細な分析レポートを作成してください。

        ### 1. 外部環境分析
        - 業界の動向や市場規模、競合状況について記述してください。
        - 政治、経済、社会、技術（PEST）の観点から、この企業に影響を与える可能性のある要因を挙げてください。

        ### 2. SWOT分析
        - 強み (Strengths)
        - 弱み (Weaknesses)
        - 機会 (Opportunities)
        - 脅威 (Threats)
        それぞれについて、具体的な根拠とともに箇条書きで記述してください。

        ### 3. 将来性
        - これまでの分析を踏まえ、この企業の短期・中期・長期的な将来性についてあなたの見解を述べてください。
        - 特に注目すべき成長ドライバーやリスク要因を明確にしてください。

        以上の内容を、平易な日本語で、マークダウン形式でまとめてください。
        """
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Error generating initial analysis for {ticker}: {e}")
        return "分析の生成中にエラーが発生しました。"
def update_analysis_with_news(ticker, company_name, old_analysis):
    """Gemini を使って最新ニュースを基に分析内容を更新する"""
    if DEV_MODE:
        return old_analysis + "\n\n---\n\n**【2025-09-04 更新】**\n- 開発モードでの更新テストです。"
    try:
        model = GenerativeModel("gemini-2.5-flash")
        prompt = f"""
        あなたはプロの証券アナリストです。日本の企業「{company_name}（証券コード: {ticker}）」に関する既存の分析レポートを、最新情報で更新するタスクです。

        ### 既存の分析レポート
        ```
        {old_analysis}
        ```

        ### あなたのタスク
        1.  直近数ヶ月のこの企業に関する重要なニュースや決算情報を wewnętrznie（内部で）検索・考慮してください。
        2.  その最新情報を踏まえて、上記の「既存の分析レポート」の内容を評価してください。
        3.  分析内容（外部環境、SWOT、将来性）に**重要な変更が必要な場合のみ**、内容を修正してください。
        4.  修正した場合、レポートの末尾に**水平線(`---`)**を追加し、その下に「**【YYYY-MM-DD 更新】**」という見出しを付けて、**変更点やその根拠となったニュースの要約**を箇条書きで追記してください。
        5.  最新情報を踏まえても既存の分析から**大きな変更が必要ない場合**は、既存の分析レポートを**完全にそのまま**返してください。追記は不要です。

        最終的なアウトプットは、更新後の完全な分析レポートのテキストのみとしてください。
        """
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"Error updating analysis for {ticker}: {e}")
        return old_analysis
@app.route('/')
def home():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_template('home.html')
@app.route('/generate_analysis', methods=['POST'])
@login_required
def generate_analysis_route():
    ticker = request.form.get('ticker')
    if not ticker:
        return jsonify({"error": "ティッカーがありません"}), 400
    try:
        info = yf.Ticker(ticker).info
        company_name = info.get('longName', ticker)
        analysis_text = generate_initial_analysis(ticker, company_name)
        return jsonify({"analysis_text": analysis_text})
    except Exception as e:
        return jsonify({"error": f"分析生成エラー: {e}"}), 500
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    if request.method == 'POST':
        ticker = request.form.get('ticker')
        memo = request.form.get('memo')
        rating = request.form.get('rating')
        analysis_text = request.form.get('analysis_text')

        if not ticker or not rating:
            flash('ティッカーシンボルと評価は必須です。')
        else:
            try:
                stock_data = yf.Ticker(ticker)
                info = stock_data.info
                current_price = info.get('currentPrice') or info.get('regularMarketPrice')

                if current_price is None:
                    flash(f'ティッカーシンボル「{ticker}」の株価を取得できませんでした。')
                    return redirect(url_for('dashboard'))

                company_name_en = info.get('longName', ticker)
                company_name_jp = get_japanese_name_by_gemini(ticker)
                sector_en = info.get('sector', 'N/A')
                sector_jp = SECTOR_TRANSLATION.get(sector_en, sector_en)
                
                per = info.get('forwardPE') or info.get('trailingPE')
                pbr = info.get('priceToBook')
                dividend_yield = info.get('dividendYield')

                new_stock = StockItem(
                    ticker=ticker.upper(),
                    company_name=company_name_jp,
                    company_name_en=company_name_en,
                    sector=sector_jp,
                    memo=memo,
                    rating=rating,
                    owner=current_user,
                    entry_price=current_price,
                    current_price=current_price,
                    per=per,
                    pbr=pbr,
                    dividend_yield=dividend_yield,
                    analysis_text=analysis_text,
                    has_update=False
                )
                
                if rating == '買い':
                    new_stock.rating_date = datetime.utcnow()
                else:
                    new_stock.rating_date = None
                    
                db.session.add(new_stock)
                db.session.commit()
                flash('新しい銘柄をリストに追加しました。')
            except Exception as e:
                flash(f'エラーが発生しました: {e}')
        return redirect(url_for('dashboard'))

    sort_by = request.args.get('sort_by', 'company_name')
    order = request.args.get('order', 'asc')
    filter_sector = request.args.get('filter_sector', '')
    filter_rating = request.args.get('filter_rating', '')
    query = db.session.query(StockItem).filter(StockItem.user_id == current_user.id)
    if filter_sector:
        query = query.filter(StockItem.sector == filter_sector)
    if filter_rating:
        query = query.filter(StockItem.rating == filter_rating)
    if sort_by != 'performance':
        sort_column = getattr(StockItem, sort_by, StockItem.company_name)
        if order == 'desc':
            query = query.order_by(desc(sort_column))
        else:
            query = query.order_by(asc(sort_column))
    user_stocks = query.all()
    if sort_by == 'performance':
        user_stocks.sort(key=lambda x: x.performance, reverse=(order == 'desc'))
    all_user_stocks = db.session.query(StockItem.sector).filter(StockItem.user_id == current_user.id).distinct().all()
    unique_sectors = sorted([s[0] for s in all_user_stocks if s[0]])
    
    return render_template('dashboard.html', 
                           username=current_user.username, 
                           stocks=user_stocks,
                           unique_sectors=unique_sectors,
                           current_filters={'sector': filter_sector, 'rating': filter_rating},
                           current_sort={'by': sort_by, 'order': order})
@app.route('/update_financial_data')
@login_required
def update_financial_data():
    """株価と財務指標のみを更新する"""
    stocks_to_update = current_user.stocks
    for stock in stocks_to_update:
        try:
            stock_data = yf.Ticker(stock.ticker)
            info = stock_data.info
            
            stock.current_price = info.get('currentPrice') or info.get('regularMarketPrice')
            stock.per = info.get('forwardPE') or info.get('trailingPE')
            stock.pbr = info.get('priceToBook')
            stock.dividend_yield = info.get('dividendYield')
        except Exception as e:
            print(f"Could not update financial data for {stock.ticker}: {e}")
            
    db.session.commit()
    flash('株価と財務指標を更新しました。')
    return redirect(url_for('dashboard'))
@app.route('/update_analysis_data')
@login_required
def update_analysis_data():
    """AIによる分析内容のみを更新する"""
    stocks_to_update = current_user.stocks
    updated_count = 0
    for stock in stocks_to_update:
        try:
            if stock.analysis_text:
                new_analysis = update_analysis_with_news(stock.ticker, stock.company_name, stock.analysis_text)
                if new_analysis.strip() != stock.analysis_text.strip():
                    stock.analysis_text = new_analysis
                    stock.has_update = True
                    updated_count += 1
                else:
                    stock.has_update = False
        except Exception as e:
            print(f"Could not update analysis for {stock.ticker}: {e}")
    
    db.session.commit()
    if updated_count > 0:
        flash(f'{updated_count}件の銘柄で分析内容が更新されました。')
    else:
        flash('分析内容をチェックしましたが、大きな変更はありませんでした。')
    return redirect(url_for('dashboard'))
@app.route('/delete_stock/<int:stock_id>', methods=['POST'])
@login_required
def delete_stock(stock_id):
    stock_to_delete = db.session.get(StockItem, stock_id)
    if not stock_to_delete or stock_to_delete.owner != current_user:
        flash('権限がありません。')
        return redirect(url_for('dashboard'))
    db.session.delete(stock_to_delete)
    db.session.commit()
    flash('銘柄を削除しました。')
    return redirect(url_for('dashboard'))
@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        existing_user = db.session.scalar(select(User).where(User.username == username))
        if existing_user:
            flash('このユーザー名は既に使用されています。')
            return redirect(url_for('register'))
        new_user = User(username=username)
        new_user.set_password(password)
        db.session.add(new_user)
        db.session.commit()
        flash('登録が完了しました。ログインしてください。')
        return redirect(url_for('login'))
    return render_template('register.html')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        user = db.session.scalar(select(User).where(User.username == username))
        if user and user.check_password(password):
            login_user(user)
            return redirect(url_for('dashboard'))
        else:
            flash('ユーザー名またはパスワードが正しくありません。')
            return redirect(url_for('login'))
    return render_template('login.html')
@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('ログアウトしました。')
    return redirect(url_for('home'))
@app.route('/edit_stock/<int:stock_id>', methods=['GET', 'POST'])
@login_required
def edit_stock(stock_id):
    stock_to_edit = db.session.get(StockItem, stock_id)
    if not stock_to_edit or stock_to_edit.owner != current_user:
        flash('編集権限がありません。')
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        original_rating = stock_to_edit.rating
        new_rating = request.form.get('rating')
        
        stock_to_edit.company_name = request.form.get('company_name')
        stock_to_edit.memo = request.form.get('memo')
        stock_to_edit.rating = new_rating
        
        if new_rating == '買い':
            if original_rating != '買い' or stock_to_edit.rating_date is None:
                stock_to_edit.rating_date = datetime.utcnow()
        else:
            stock_to_edit.rating_date = None

        db.session.commit()
        flash('銘柄の情報を更新しました。')
        return redirect(url_for('dashboard'))

    return render_template('edit_stock.html', stock=stock_to_edit)
def get_news_from_ai():
    if DEV_MODE:
        return ["（開発モード）...", "（開発モード）...", "（開発モード）...", "（開発モード）...", "（開発モード）..."]
    try:
        model = GenerativeModel("gemini-2.5-flash")
        current_time_str = datetime.now().strftime("%Y年%m月%d日 %H時%M分%S秒")
        prompt = f"""
        現在の時刻は {current_time_str} です。この現時刻の情報を元に、日本の経済や株式市場に影響を与えそうな、最新のニュースヘッドラインを5つ生成してください。
        以下のJSON形式のみで回答してください。{{"headlines": ["ニュース1", "ニュース2", "ニュース3", "ニュース4", "ニュース5"]}}
        """
        generation_config = {"temperature": 1.0}
        response = model.generate_content(prompt, generation_config=generation_config)
        json_string = extract_json(response.text)
        if not json_string:
            raise ValueError("AI response did not contain a valid JSON object.")
        data = json.loads(json_string)
        return data.get("headlines", [])
    except Exception as e:
        print(f"--- Error getting news from AI ---")
        print(f"Error: {e}")
        if 'response' in locals() and hasattr(response, 'text'):
            print(f"Raw AI Response: {response.text}")
        print("---------------------------------")
        return ["（AIからのニュース取得に失敗しました）", "政府、子育て支援を強化...", "国内旅行が活況...", "AI半導体の需要が世界的に拡大...", "再生可能エネルギーへの投資が加速..."]
def process_ai_request(prompt):
    if DEV_MODE:
        sample_data = {"name": "開発モード", "children": [{"name": "サンプル分野", "children": [{"name": "サンプル企業A", "ticker": "1111.T"}, {"name": "サンプル企業B", "ticker": "2222.T"}]}]}
        return jsonify(sample_data), 200
    try:
        model = GenerativeModel("gemini-2.5-flash")
        generation_config = {"temperature": 0.7}
        response = model.generate_content(prompt, generation_config=generation_config)
        
        if not response.candidates:
            if hasattr(response, 'prompt_feedback') and response.prompt_feedback:
                 print(f"Prompt Feedback: {response.prompt_feedback}")
            return jsonify({"error": "AIからの応答がありませんでした。ブロックされた可能性があります。"}), 500
            
        json_string = extract_json(response.text)
        if not json_string:
            print(f"--- No JSON found in AI response ---")
            print(f"Raw AI Response: {response.text}")
            print("------------------------------------")
            return jsonify({"error": "AIの応答から有効なデータ形式を抽出できませんでした。"}), 500
        
        json_response = json.loads(json_string)
        print("--- Sending this JSON data to frontend ---")
        print(json.dumps(json_response, indent=2, ensure_ascii=False))
        print("------------------------------------------")
        return jsonify(json_response), 200
    except json.JSONDecodeError as e:
        print(f"--- JSON Parsing Error ---")
        print(f"Error: {e}")
        print(f"Raw content being parsed: {json_string}")
        print("--------------------------")
        return jsonify({"error": "AIが不正な形式のデータを返しました。"}), 500
    except Exception as e:
        print(f"--- An unexpected error occurred ---")
        print(f"Error: {e}")
        if 'response' in locals() and hasattr(response, 'text'):
            print(f"Original AI response: {response.text}")
        print("---------------------------------")
        return jsonify({"error": "マップの生成中に予期せぬエラーが発生しました。"}), 500
@app.route('/idea_prism')
@login_required
def idea_prism():
    initial_news = get_news_from_ai()
    return render_template('idea_prism.html', sample_news=initial_news)
@app.route('/get_latest_news', methods=['GET'])
@login_required
def get_latest_news():
    news_list = get_news_from_ai()
    return jsonify(headlines=news_list)
@app.route('/generate_map', methods=['POST'])
@login_required
def generate_map():
    keyword = request.form.get('keyword')
    if not keyword:
        return jsonify({"error": "キーワードがありません"}), 400
    
    prompt = f"""
    「{keyword}」というキーワードから連想される「モノやコト」を5つ挙げ、それぞれに関連する日本の主要な上場企業を3社ずつ挙げてください。
    各企業について、以下の情報を必ず含めてください。
    - name: 企業名
    - ticker: .T を含む日本の証券コード
    - reason: 「{keyword}」とどう関連するかの短い根拠
    - description: その企業がどんな会社かの「ひとこと紹介」
    絶対にJSON形式のみで、他の文章は含めずに回答してください。
    {{ "name": "{keyword}", "children": [ {{ "name": "モノやコト1", "children": [ {{ "name": "企業名A", "ticker": "XXXX.T", "reason": "...", "description": "..." }} ]}} ] }}
    """
    return process_ai_request(prompt)
@app.route('/generate_map_from_news', methods=['POST'])
@login_required
def generate_map_from_news():
    news_headline = request.form.get('news_headline')
    if not news_headline:
        return jsonify({"error": "ニュースが選択されていません"}), 400

    prompt = f"""
    「{news_headline}」というニュースから恩恵を受けると考えられる「分野」を5つ挙げ、それぞれに関連する日本の主要な上場企業を3社ずつ挙げてください。
    各企業について、以下の情報を必ず含めてください。
    - name: 企業名
    - ticker: .T を含む日本の証券コード
    - reason: そのニュースから恩恵を受ける具体的な理由
    - description: その企業がどんな会社かの「ひとこと紹介」
    絶対にJSON形式のみで、他の文章は含めずに回答してください。
    {{ "name": "{news_headline}", "children": [ {{ "name": "恩恵を受ける分野1", "children": [ {{ "name": "企業名A", "ticker": "XXXX.T", "reason": "...", "description": "..." }} ]}} ] }}
    """
    return process_ai_request(prompt)
@app.route('/add_stock_from_prism', methods=['POST'])
@login_required
def add_stock_from_prism():
    ticker = request.form.get('ticker')
    if not ticker:
        return jsonify(success=False, message="ティッカーシンボルがありません。"), 400
    try:
        stmt = select(StockItem).where(StockItem.user_id == current_user.id, StockItem.ticker == ticker.upper())
        existing_stock = db.session.scalar(stmt)
        if existing_stock:
            return jsonify(success=False, message="この銘柄は既に追加されています。"), 409
        
        stock_data = yf.Ticker(ticker)
        info = stock_data.info
        current_price = info.get('currentPrice') or info.get('regularMarketPrice')
        
        if current_price is None:
            return jsonify(success=False, message=f"ティッカー「{ticker}」の株価を取得できませんでした。"), 404

        company_name_en = info.get('longName', ticker)
        company_name_jp = get_japanese_name_by_gemini(ticker)
        sector_en = info.get('sector', 'N/A')
        sector_jp = SECTOR_TRANSLATION.get(sector_en, sector_en)
        per = info.get('forwardPE') or info.get('trailingPE')
        pbr = info.get('priceToBook')
        dividend_yield = info.get('dividendYield')

        analysis_text = generate_initial_analysis(ticker, company_name_jp)

        new_stock = StockItem(
            ticker=ticker.upper(),
            company_name=company_name_jp,
            company_name_en=company_name_en,
            sector=sector_jp,
            memo="",
            rating="中立",
            owner=current_user,
            entry_price=current_price, 
            current_price=current_price,
            rating_date=None,
            per=per,
            pbr=pbr,
            dividend_yield=dividend_yield,
            analysis_text=analysis_text,
            has_update=False
        )
        db.session.add(new_stock)
        db.session.commit()
        return jsonify(success=True, message=f"{company_name_jp} をマイリストに追加しました。")
    except Exception as e:
        print(f"Error adding stock from prism: {e}")
        return jsonify(success=False, message="銘柄の追加中にエラーが発生しました。"), 500
if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    # ポート番号を環境変数から取得し、デフォルトを8080に設定
    port = int(os.environ.get('PORT', 8080))
    app.run(debug=True, host='0.0.0.0', port=port)