# Flask.py
# -*- coding: utf-8 -*-

import sqlite3
from pathlib import Path
from flask import Flask, request, redirect, send_from_directory, abort

# --- إعدادات ---
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "database" / "Dash.db"
PAGES_DIR = BASE_DIR / "templates"

# --- Flask App ---
app = Flask(__name__)

def get_game_from_code(code: str):
    """جلب نوع اللعبة و page_key و game_id من قاعدة البيانات حسب الكود"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT game_type, page_key, game_id FROM game_codes WHERE code=?", (code,))
    row = cur.fetchone()
    conn.close()
    return row

@app.route("/")
def index():
    return send_from_directory(PAGES_DIR, "index.html")

@app.route("/games/<pagename>")
def serve_page(pagename):
    """عرض صفحة لعبة معينة"""
    if not pagename.endswith(".html"):
        pagename += ".html"
    page_path = Path(PAGES_DIR) / pagename
    if page_path.exists():
        return send_from_directory(PAGES_DIR, pagename)
    return abort(404, f"❌ الصفحة {pagename} غير موجودة")

@app.route("/play")
def play():
    """توجيه حسب الكود"""
    code = request.args.get("code", "").strip()
    if not code:
        return "❌ الرجاء إدخال كود", 400

    game = get_game_from_code(code)
    if not game:
        return f"❌ الكود '{code}' غير موجود في قاعدة البيانات", 404

    game_type, page_key, game_id = game

    if not page_key:
        return f"❌ اللعبة '{game_type}' ليس لها page_key محدد في قاعدة البيانات", 500

    page_filename = f"{page_key}.html"
    page_path = Path(PAGES_DIR) / page_filename

    if not page_path.exists():
        return f"❌ ملف الصفحة '{page_filename}' غير موجود في {PAGES_DIR}", 404

    return redirect(f"/games/{page_filename}?id={game_id}")

# شغل محلي فقط
if __name__ == "__main__":
    print("🚀 افتح المتصفح على: http://127.0.0.1:5000")
    app.run(debug=True, host="0.0.0.0", port=5000)
