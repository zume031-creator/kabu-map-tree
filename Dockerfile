# ベースとなるPythonの公式イメージを指定
FROM python:3.11-slim

# 環境変数 PYTHONUNBUFFERED を設定し、ログがすぐに出力されるようにする
ENV PYTHONUNBUFFERED True

# コンテナ内の作業ディレクトリを作成・設定
WORKDIR /app

# requirements.txt をコンテナにコピー
COPY requirements.txt requirements.txt

# pipをアップグレードし、ライブラリをインストール
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# プロジェクトの全てのファイルをコンテナにコピー
COPY . .

# アプリケーションを起動するコマンドを指定
# gunicornを使い、app.pyの中のFlaskインスタンス(app)を起動する
# 0.0.0.0:8080 でリッスンする (Cloud Runの標準)
CMD exec gunicorn --bind 0.0.0.0:8080 --workers 1 --threads 8 --timeout 0 app:app