from flask import Flask, request, redirect, url_for, jsonify, render_template_string, abort
from sqlalchemy import create_engine, text
from flask_socketio import SocketIO, emit, join_room, leave_room
import qrcode
import os
import secrets
from datetime import datetime, timedelta

# ================= إعدادات عامة =================
GRACE_SECONDS = 120          # مدة الاحتفاظ بالمقعد بعد الانقطاع
MAX_PLAYERS   = 4

def now():
    return datetime.utcnow()

app = Flask(__name__, static_folder="../static")
app.secret_key = os.getenv("APP_SECRET", "supersecret")
socketio = SocketIO(app, cors_allowed_origins="*")

# اتصال SQL Server
engine = create_engine(
    "mssql+pyodbc://DESKTOP-DAJF6EF/Game33?driver=ODBC+Driver+17+for+SQL+Server",
    pool_size=10, max_overflow=20, pool_timeout=30, pool_recycle=1800
)

# مسارات
base_dir         = os.path.dirname(__file__)
static_path      = os.path.join(base_dir, "..", "static")
pages_path_root  = os.path.join(base_dir, "..", "pages")
os.makedirs(static_path, exist_ok=True)
os.makedirs(pages_path_root, exist_ok=True)

# ================= أدوات مساعدة =================
def safe_upper(s):
    return (s or "").strip().upper()

def get_session_by_code(conn, code):
    return conn.execute(
        text("SELECT id, used, expires_at, token, status FROM sessions WHERE code=:code"),
        {"code": code}
    ).fetchone()

def get_join_count(conn, session_id):
    return conn.execute(
        text("SELECT COUNT(*) FROM session_players WHERE session_id=:sid"),
        {"sid": session_id}
    ).scalar()

def seat_occupied(conn, session_id, pnum):
    return conn.execute(
        text("SELECT COUNT(*) FROM session_players WHERE session_id=:sid AND player_number=:p"),
        {"sid": session_id, "p": pnum}
    ).scalar() > 0

def find_player_by_token(conn, session_id, player_token):
    return conn.execute(
        text("SELECT id, player_number FROM session_players WHERE session_id=:sid AND player_token=:pt"),
        {"sid": session_id, "pt": player_token}
    ).fetchone()

def find_player_by_number_and_token(conn, session_id, pnum, player_token):
    return conn.execute(
        text("""SELECT id FROM session_players 
                WHERE session_id=:sid AND player_number=:p AND player_token=:pt"""),
        {"sid": session_id, "p": pnum, "pt": player_token}
    ).fetchone()

def mark_player_connected(conn, player_id, connected=True):
    conn.execute(
        text("UPDATE session_players SET connected=:c, last_seen=:ls WHERE id=:id"),
        {"c": 1 if connected else 0, "ls": now(), "id": player_id}
    )

def free_stale_slots(conn, session_id):
    cutoff = now() - timedelta(seconds=GRACE_SECONDS)
    conn.execute(
        text("""
        DELETE FROM session_players
        WHERE session_id=:sid AND connected=0 AND (last_seen IS NULL OR last_seen < :cutoff)
        """),
        {"sid": session_id, "cutoff": cutoff}
    )

def generate_or_get_token(conn, session_id, existing_token):
    if existing_token:
        return existing_token
    token = secrets.token_urlsafe(24)
    conn.execute(text("UPDATE sessions SET token=:t WHERE id=:id"), {"t": token, "id": session_id})
    return token

def session_state_payload(conn, session_id):
    joined = get_join_count(conn, session_id)
    expires_at = conn.execute(
        text("SELECT expires_at FROM sessions WHERE id=:sid"),
        {"sid": session_id}
    ).scalar()
    remaining = max(0, MAX_PLAYERS - joined)
    expired = False
    remaining_time = None
    if expires_at:
        delta = expires_at - datetime.utcnow()
        remaining_time = max(0, int(delta.total_seconds()))
        expired = remaining_time <= 0
    return {
        "joined": joined,
        "remaining": remaining,
        "time_left": remaining_time,
        "expired": expired,
        "maxPlayers": MAX_PLAYERS
    }

def current_player_for_session(conn, session_id):
    """يرجع current_player للمسار الحالي مع افتراضي 1 إن كان NULL/خارج النطاق."""
    row = conn.execute(
        text("""SELECT TOP 1 current_path 
               FROM session_progress WHERE session_id=:sid ORDER BY timestamp DESC"""),
        {"sid": session_id}
    ).fetchone()
    if not row:
        return 1
    current_path = row[0]
    cp_row = conn.execute(
        text("SELECT current_player FROM paths WHERE id=:pid"),
        {"pid": current_path}
    ).fetchone()
    if not cp_row:
        return 1
    try:
        cp = int(cp_row[0]) if cp_row[0] is not None else 1
    except:
        cp = 1
    if cp < 1 or cp > MAX_PLAYERS:
        cp = 1
    return cp

# ---------- تجهيز جدول السجل ----------
def ensure_events_table():
    sql = """
    IF OBJECT_ID('dbo.session_events','U') IS NULL
    BEGIN
        CREATE TABLE dbo.session_events(
            id INT IDENTITY(1,1) PRIMARY KEY,
            session_id INT NOT NULL,
            at_utc DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
            player_number INT NULL,
            path_id INT NULL,
            choice_id INT NULL,
            choice_text NVARCHAR(300) NULL,
            event_text NVARCHAR(800) NOT NULL
        );
        CREATE INDEX IX_session_events_sid_id ON dbo.session_events(session_id, id DESC);
    END
    """
    with engine.begin() as conn:
        conn.execute(text(sql))

ensure_events_table()

# ================= صفحات أخطاء =================
ERROR_HTML = """
<!DOCTYPE html><html lang="ar" dir="rtl"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>خطأ</title>
<body style="font-family:Tahoma,Arial;max-width:720px;margin:40px auto">
  <h2>⚠️ حدث خطأ</h2>
  <p>{{msg}}</p>
  <p><a href="/">⬅️ رجوع للصفحة الرئيسية</a></p>
</body></html>"""

@app.errorhandler(404)
def not_found(e):
    return render_template_string(ERROR_HTML, msg="الصفحة غير موجودة"), 404

@app.errorhandler(500)
def server_error(e):
    return render_template_string(ERROR_HTML, msg="خطأ داخلي في الخادم"), 500

# ================= APIs إعادة التعيين المحسّنة =================
@app.route("/full_reset/<code>", methods=["POST"])
def full_reset(code):
    """إعادة تعيين كاملة للجلسة - حذف كل البيانات"""
    token = request.args.get("token", "")
    code = safe_upper(code)
    
    try:
        with engine.begin() as conn:
            # التحقق من الصلاحية
            s = conn.execute(
                text("SELECT id, token FROM sessions WHERE code=:c"),
                {"c": code}
            ).fetchone()
            if not s or not s[1] or s[1] != token:
                return render_template_string(ERROR_HTML, msg="❌ صلاحية غير صحيحة"), 403
            
            sid = s[0]
            
            # 1) حذف سجل الأحداث
            conn.execute(text("DELETE FROM session_events WHERE session_id=:sid"), {"sid": sid})
            # 2) حذف جميع اللاعبين
            conn.execute(text("DELETE FROM session_players WHERE session_id=:sid"), {"sid": sid})
            # 3) حذف تقدم الجلسة
            conn.execute(text("DELETE FROM session_progress WHERE session_id=:sid"), {"sid": sid})
            # 4) إعادة تعيين الجلسة للوضع الافتراضي
            start_time = datetime.utcnow()
            expires_at = start_time + timedelta(hours=2)
            conn.execute(text("""
                UPDATE sessions 
                SET used=0, status='waiting', start_time=:start, expires_at=:expires
                WHERE id=:sid
            """), {"sid": sid, "start": start_time, "expires": expires_at})
            # 5) إنشاء مسار افتراضي جديد
            conn.execute(text("""
                INSERT INTO session_progress (session_id, current_path) 
                VALUES (:sid, 1)
            """), {"sid": sid})
            # 6) تسجيل حدث
            conn.execute(text("""
                INSERT INTO session_events(session_id, player_number, event_text, at_utc)
                VALUES (:sid, NULL, N'🔄 تم إعادة تعيين الجلسة بالكامل', SYSUTCDATETIME())
            """), {"sid": sid})
            payload = session_state_payload(conn, sid)
        
        socketio.emit("session_update", payload, room=code)
        socketio.emit("story_changed", {"reset": True}, room=code)
        socketio.emit("event_appended", {
            "at": now().isoformat(),
            "player": None,
            "event_text": "🔄 تم إعادة تعيين الجلسة بالكامل"
        }, room=code)
        
        # رجوع لصفحة إدخال الكود مع رسالة نجاح
        return redirect(url_for("home", code=code, msg="✅ تمت إعادة التعيين بنجاح"))

    except Exception as ex:
        return render_template_string(ERROR_HTML, msg=f"❌ فشل في إعادة التعيين: {str(ex)}"), 500


@app.route("/transfer_player/<code>", methods=["POST"])
def transfer_player(code):
    """نقل لاعب لجهاز آخر - حذف الـ token فقط"""
    token = request.args.get("token", "")
    pnum = request.json.get("pnum")
    code = safe_upper(code)
    
    if pnum == 1:
        return jsonify({"error": "لا يمكن نقل المضيف"}), 400
    
    try:
        with engine.begin() as conn:
            s = conn.execute(text("SELECT id, token FROM sessions WHERE code=:c"), {"c": code}).fetchone()
            if not s or not s[1] or s[1] != token:
                return jsonify({"error": "صلاحية غير صحيحة"}), 403
            
            sid = s[0]
            conn.execute(text("""
                DELETE FROM session_players 
                WHERE session_id=:sid AND player_number=:p
            """), {"sid": sid, "p": pnum})
            conn.execute(text("""
                INSERT INTO session_events(session_id, player_number, event_text, at_utc)
                VALUES (:sid, :pnum, N'📱 تم نقل اللاعب لجهاز آخر', SYSUTCDATETIME())
            """), {"sid": sid, "pnum": pnum})
            payload = session_state_payload(conn, sid)
        
        socketio.emit("session_update", payload, room=code)
        socketio.emit("event_appended", {
            "at": now().isoformat(),
            "player": pnum,
            "event_text": f"📱 تم نقل اللاعب {pnum} لجهاز آخر"
        }, room=code)
        return jsonify({"success": True, "message": f"تم نقل اللاعب {pnum} بنجاح"})
        
    except Exception:
        # محاولة استخدام المسار القديم
        return kick_player(code)

# ================= الصفحة الرئيسية =================
# ================= الصفحة الرئيسية =================
@app.route("/", methods=["GET", "POST"])
def home():
    """
    نقطة دخول مضبوطة لتقليل اللبس:
    - عرض زر إعادة تعيين الكود إذا كان مستخدمًا بالفعل.
    - قفل صف الجلسة لمنع سباق تحديد المضيف.
    - Grace Reopen للجلسات المنتهية حديثًا.
    - إزالة تحويل تلقائي مربك في واجهة QR.
    - توليد رابط انضمام يعتمد على request.host_url.
    - توليد QR مع تحمّل الأخطاء (fallback للرابط فقط).
    - بعد إعادة التعيين: العودة لصفحة إدخال الكود معبأ مسبقًا.
    """

    # ========= دوال مساعدة محلية =========
    def get_session_id_from_code(conn, code):
        row = conn.execute(
            text("SELECT id FROM sessions WHERE code=:c"),
            {"c": code}
        ).fetchone()
        return row.id if row else None

    def get_join_count(conn, session_id):
        row = conn.execute(
            text("SELECT COUNT(*) FROM session_players WHERE session_id=:sid"),
            {"sid": session_id}
        ).fetchone()
        return row[0] if row else 0

    # ========= GET =========
    if request.method == "GET":
        prefill = request.args.get("code", "")  # لو جاي من إعادة التعيين
        msg = request.args.get("msg", "")       # رسالة نجاح/تنبيه اختيارية

        return f"""
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>🎮 اللعبة</title>
            <style>
                body {{
                    font-family: Tahoma, Arial, sans-serif;
                    text-align: center;
                    padding: 20px;
                    background: #f9f9f9;
                }}
                h1 {{ margin-bottom: 20px; color: #333; }}
                input {{
                    padding: 10px;
                    font-size: 18px;
                    border: 1px solid #ccc;
                    border-radius: 6px;
                    width: 260px;
                    text-align: center;
                }}
                button {{
                    padding: 10px 20px;
                    font-size: 18px;
                    margin-top: 10px;
                    cursor: pointer;
                    border: none;
                    border-radius: 6px;
                    background: #0078d7;
                    color: #fff;
                }}
                button:hover {{ background: #005bb5; }}
                .hint {{ color:#666; font-size:14px; margin-top:8px; }}
                .success {{ color: green; margin: 10px 0; font-weight: bold; }}
            </style>
        </head>
        <body>
            <h1>🎮 ادخل كود الجلسة</h1>
            {"<div class='success'>"+msg+"</div>" if msg else ""}
            <form method="post" novalidate>
                <input type="text" name="session_code" placeholder="اكتب الكود هنا" value="{prefill}" required>
                <br>
                <button type="submit">تأكيد</button>
            </form>
            <div class="hint">تأكد أن الكود مكتوب بشكل صحيح وبدون مسافات أو رموز غير لازمة.</div>
        </body>
        </html>
        """

    # ========= POST =========
    raw_code = request.form.get("session_code", "")
    code = safe_upper(raw_code)
    if not code or len(code) > 32:
        return render_template_string(ERROR_HTML, msg="❌ كود غير صالح. تأكد من كتابته صحيحًا وبلا مسافات.")

    GRACE_REOPEN_MIN = 5

    try:
        with engine.begin() as conn:
            # جلب الجلسة عن طريق الكود (مع id الرقمي)
            row = conn.execute(
                text("""
                    SELECT id, used, expires_at, token, status
                    FROM sessions WITH (UPDLOCK, ROWLOCK)
                    WHERE code=:code
                """),
                {"code": code}
            ).fetchone()

            if not row:
                return render_template_string(ERROR_HTML, msg="❌ الكود غير موجود. تأكد من كتابته بدقة.")

            session_id, used, expires_at, token, status = row
            utc_now = datetime.utcnow()

            # إذا الجلسة مستخدمة مسبقًا → اعرض زر إعادة التعيين
            if used == 1 and (status or "").lower() == "running":
                token = generate_or_get_token(conn, session_id, token)
                reset_url = url_for("full_reset", code=code, token=token)
                return f"""
                <!DOCTYPE html>
                <html lang="ar" dir="rtl">
                <head>
                    <meta charset="utf-8">
                    <meta name="viewport" content="width=device-width, initial-scale=1.0">
                    <title>إعادة تعيين الجلسة</title>
                    <style>
                        body {{ font-family: Tahoma, Arial; text-align: center; padding: 20px; }}
                        .warning {{ color: #b00; font-weight: bold; margin: 20px 0; }}
                        button {{ padding: 10px 20px; margin: 5px; font-size: 16px; cursor: pointer; }}
                    </style>
                </head>
                <body>
                    <h2>⚠️ تنبيه هام</h2>
                    <p class="warning">هذا الكود مستخدم بالفعل لجلسة أخرى.<br>
                    يجب إعادة تعيين الكود قبل أن تبدأ لعبة جديدة.</p>
                    <form method="post" action="{reset_url}" 
                          onsubmit="return confirm('سيتم مسح كل شيء: اللاعبين، الأحداث، القصة. هل أنت متأكد؟');">
                        <button type="submit" style="background:#c00;color:white;">🔄 إعادة تعيين الكود</button>
                    </form>
                    <p><a href="/?code={code}">⬅️ رجوع</a></p>
                </body>
                </html>
                """

            # جلسة منتهية؟ (Grace reopen)
            if expires_at and utc_now > expires_at:
                if (utc_now - expires_at) <= timedelta(minutes=GRACE_REOPEN_MIN):
                    start = utc_now
                    expires = start + timedelta(hours=2)
                    conn.execute(
                        text("""
                            UPDATE sessions
                            SET used=1, start_time=:start, expires_at=:expires, status='running'
                            WHERE id=:id
                        """),
                        {"start": start, "expires": expires, "id": session_id}
                    )
                    conn.execute(
                        text("""
                            IF NOT EXISTS (SELECT 1 FROM session_progress WHERE session_id=:sid)
                                INSERT INTO session_progress (session_id, current_path) VALUES (:sid, 1)
                        """),
                        {"sid": session_id}
                    )
                else:
                    return render_template_string(
                        ERROR_HTML,
                        msg="⏰ انتهت صلاحية الكود. اطلب كودًا جديدًا أو أعد تشغيل الجلسة."
                    )

            # تشغيل الجلسة لو جديدة
            if used == 0 or (status or "").lower() != "running":
                start = datetime.utcnow()
                expires = start + timedelta(hours=2)
                conn.execute(
                    text("""
                        UPDATE sessions
                        SET used=1, start_time=:start, expires_at=:expires, status='running'
                        WHERE id=:id
                    """),
                    {"start": start, "expires": expires, "id": session_id}
                )
                conn.execute(
                    text("""
                        IF NOT EXISTS (SELECT 1 FROM session_progress WHERE session_id=:sid)
                            INSERT INTO session_progress (session_id, current_path) VALUES (:sid, 1)
                    """),
                    {"sid": session_id}
                )

            token = generate_or_get_token(conn, session_id, token)

        # رابط الانضمام
        base = request.host_url.rstrip("/")
        join_url = f"{base}/join/{code}?token={token}"

        # QR مع تحمل الأخطاء
        qr_file = os.path.join(static_path, f"{code}.png")
        img_url = None
        try:
            qrcode.make(join_url).save(qr_file)
            img_url = url_for('static', filename=f"{code}.png")
        except Exception:
            img_url = None

        return f"""
        <!DOCTYPE html>
        <html lang="ar" dir="rtl">
        <head>
            <meta charset="utf-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
            <title>🎮 صفحة الدخول</title>
            <style>
                body {{ font-family: Tahoma, Arial; text-align: center; padding: 20px; }}
                img {{ max-width: 100%; height: auto; }}
                .link-container {{ margin: 20px 0; display:flex; gap:8px; justify-content:center; flex-wrap:wrap; }}
                .link-container input {{ width: 360px; max-width: 90%; }}
                button {{ padding: 10px 20px; margin: 5px; cursor:pointer; }}
                .muted {{ color:#666; font-size:14px; margin-top:6px; }}
            </style>
        </head>
        <body>
            <h1>🎮 صفحة الدخول</h1>
            <p>كود الجلسة: <b>{code}</b></p>
            {f'<img src="{img_url}" width="220" alt="QR">' if img_url else '<div class="muted">تعذّر إنشاء QR الآن — استخدم الرابط أدناه.</div>'}
            <div class="link-container">
                <input type="text" id="joinLink" value="{join_url}" readonly>
                <button onclick="copyLink()">📋 نسخ</button>
                <button onclick="window.open('{join_url}', '_blank')">🚀 فتح</button>
            </div>
            <p>دخل <span id="joinedCount">0</span> من {MAX_PLAYERS} لاعبين</p>
            <div class="muted">شارك رمز QR أو الرابط مع اللاعبين للانضمام بسرعة.</div>

            <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
            <script>
                function copyLink() {{
                    const el = document.getElementById("joinLink");
                    el.select(); document.execCommand("copy");
                    alert("✅ تم نسخ الرابط!");
                }}
                const socket = io();
                socket.emit("watch_session", {{ code: "{code}" }});
                socket.on("session_update", (data) => {{
                    document.getElementById("joinedCount").innerText = data.joined;
                }});
                // 🚫 لا نحول أي جهاز مباشرة. المضيف سيتحدد لاحقًا.
            </script>
        </body>
        </html>
        """

    except Exception as ex:
        return render_template_string(
            ERROR_HTML,
            msg=f"حدث خلل مؤقت أثناء تهيئة الجلسة. أعد المحاولة خلال لحظات. التفاصيل للمطوّر: {str(ex)}"
        )


# ================= الانضمام للجلسة =================
@app.route("/join/<code>")
def join(code):
    code = safe_upper(code)
    token = request.args.get("token", "")
    player_token = request.args.get("player_token")
    requested_pnum = request.args.get("pnum", type=int)

    try:
        with engine.begin() as conn:
            row = get_session_by_code(conn, code)
            if not row:
                return render_template_string(ERROR_HTML, msg="❌ الكود غير صالح")

            session_id, used, expires_at, db_token, status = row

            if not db_token or token != db_token:
                return render_template_string(ERROR_HTML, msg="❌ رابط الانضمام غير صالح")

            free_stale_slots(conn, session_id)

            # إعادة ربط لاعب قديم بالـ token
            if player_token:
                pl = find_player_by_token(conn, session_id, player_token)
                if pl:
                    player_id, player_number = pl
                    mark_player_connected(conn, player_id, True)
                    payload = session_state_payload(conn, session_id)
                    socketio.emit("session_update", payload, room=code)
                    dest = "player1.html" if player_number == 1 else "player.html"
                    return redirect(f"/pages/{dest}?code={code}&token={token}&player={player_number}&player_token={player_token}")

            # لاعب جديد
            current_count = get_join_count(conn, session_id)
            if current_count >= MAX_PLAYERS:
                return render_template_string(ERROR_HTML, msg=f"❌ الجلسة ممتلئة ({MAX_PLAYERS} لاعبين)")

            # حجز مقعد
            if requested_pnum:
                if requested_pnum < 1 or requested_pnum > MAX_PLAYERS:
                    return render_template_string(ERROR_HTML, msg="❌ رقم مقعد غير صالح")
                if seat_occupied(conn, session_id, requested_pnum):
                    return render_template_string(ERROR_HTML, msg="❌ هذا المقعد مشغول")
                player_number = requested_pnum
            else:
                player_number = current_count + 1

            new_player_token = secrets.token_urlsafe(24)
            conn.execute(
                text("""INSERT INTO session_players 
                        (session_id, player_number, player_token, last_seen, connected)
                        VALUES (:sid, :pnum, :pt, :ls, :c)"""),
                {"sid": session_id, "pnum": player_number, "pt": new_player_token, "ls": now(), "c": 1}
            )

            # بعد الإدراج، احسب العدد الجديد
            joined_after = get_join_count(conn, session_id)

            # ✅ عند اكتمال العدد: اجعل آخر لاعب هو المضيف (رقم 1) عبر SWAP آمن
            if joined_after >= MAX_PLAYERS:
                # إن كان أصلاً 1، لا حاجة للتبديل
                if player_number != 1:
                    # احصل على صف المضيف الحالي (إن وجد)
                    old_host = conn.execute(
                        text("SELECT TOP 1 id, player_token FROM session_players WHERE session_id=:sid AND player_number=1"),
                        {"sid": session_id}
                    ).fetchone()
                    new_player = conn.execute(
                        text("SELECT id FROM session_players WHERE session_id=:sid AND player_token=:pt"),
                        {"sid": session_id, "pt": new_player_token}
                    ).fetchone()

                    if new_player:
                        new_id = new_player[0]
                        if old_host:
                            old_id, old_host_token = old_host
                            # SWAP باستخدام قيمة مؤقتة لتفادي تضارب القيود
                            conn.execute(text("UPDATE session_players SET player_number=0 WHERE id=:id"), {"id": new_id})
                            conn.execute(text("UPDATE session_players SET player_number=:p WHERE id=:id"),
                                         {"p": player_number, "id": old_id})
                            conn.execute(text("UPDATE session_players SET player_number=1 WHERE id=:id"), {"id": new_id})
                            # بث اختيارية لإعلام العملاء (يمكن تجاهلها إن لم تكن الصفحات تستمع لها)
                            socketio.emit("role_swapped", {
                                "new_host_token": new_player_token,
                                "old_host_token": old_host_token,
                                "old_host_new_number": player_number
                            }, room=code)
                        else:
                            # لا يوجد لاعب 1 سابقاً (حالة نادرة)، عيّن الأخير مباشرةً كمضيف
                            conn.execute(text("UPDATE session_players SET player_number=1 WHERE id=:id"), {"id": new_id})

                # أعلم الجميع بمن أصبح المضيف (توكن المضيف الجديد)
                socketio.emit("host_assigned", {"token": new_player_token}, room=code)

            # نهاية المعاملة، نخرج بالقيم

        # بث حالة محدثة (خارج المعاملة للسرعة)
        with engine.connect() as conn2:
            payload = session_state_payload(conn2, session_id)
            # التحقق من رقم اللاعب بعد أي تبديل
            row_me = conn2.execute(
                text("SELECT player_number FROM session_players WHERE session_id=:sid AND player_token=:pt"),
                {"sid": session_id, "pt": new_player_token}
            ).fetchone()
            final_player_number = row_me[0] if row_me else player_number

        socketio.emit("session_update", payload, room=code)

        # التوجيه حسب الرقم النهائي بعد التبديل
        dest = "player1.html" if final_player_number == 1 else "player.html"
        return redirect(f"/pages/{dest}?code={code}&token={token}&player={final_player_number}&player_token={new_player_token}")

    except Exception as ex:
        return render_template_string(ERROR_HTML, msg=f"خطأ الانضمام: {ex}")

# ================= APIs للمضيف =================
@app.route("/session_links/<code>")
def session_links(code):
    token = request.args.get("token", "")
    code = safe_upper(code)
    with engine.connect() as conn:
        s = conn.execute(text("SELECT id, token FROM sessions WHERE code=:c"), {"c": code}).fetchone()
        if not s or not s[1] or s[1] != token:
            return jsonify({"error": "صلاحية غير صحيحة"}), 403
        sid, db_token = s
        occ = set([r[0] for r in conn.execute(
            text("SELECT player_number FROM session_players WHERE session_id=:sid"),
            {"sid": sid}
        ).fetchall()])
    base = f"http://localhost:5000/join/{code}?token={db_token}"
    per_seat = [{"number": i, "url": f"{base}&pnum={i}"} for i in range(2, MAX_PLAYERS+1) if i not in occ]
    return jsonify({"base_url": base, "seat_urls": per_seat})

@app.route("/reset_session/<code>", methods=["POST"])
def reset_session(code):
    token = request.args.get("token", "")
    code = safe_upper(code)
    with engine.begin() as conn:
        s = conn.execute(text("SELECT id, token FROM sessions WHERE code=:c"), {"c": code}).fetchone()
        if not s or not s[1] or s[1] != token:
            return jsonify({"error": "صلاحية غير صحيحة"}), 403
        sid = s[0]
        conn.execute(text("DELETE FROM session_players WHERE session_id=:sid"), {"sid": sid})
        payload = session_state_payload(conn, sid)
    socketio.emit("session_update", payload, room=code)
    return jsonify({"message": "تم تنظيف الجلسة. يمكنك استقبال لاعبين جدد."})

@app.route("/kick_player/<code>", methods=["POST"])
def kick_player(code):
    token = request.args.get("token", "")
    pnum  = request.json.get("pnum")
    code = safe_upper(code)
    if pnum == 1:
        return jsonify({"error": "لا يمكن طرد المضيف"}), 400
    with engine.begin() as conn:
        s = conn.execute(text("SELECT id, token FROM sessions WHERE code=:c"), {"c": code}).fetchone()
        if not s or not s[1] or s[1] != token:
            return jsonify({"error": "صلاحية غير صحيحة"}), 403
        sid = s[0]
        conn.execute(text("DELETE FROM session_players WHERE session_id=:sid AND player_number=:p"),
                     {"sid": sid, "p": pnum})
        payload = session_state_payload(conn, sid)
    socketio.emit("session_update", payload, room=code)
    return jsonify({"message": f"تم نقل اللاعب {pnum}."})

@app.route("/start_game/<code>", methods=["POST"])
def start_game(code):
    token = request.args.get("token", "")
    code = safe_upper(code)
    with engine.begin() as conn:
        s = conn.execute(text("SELECT id, token FROM sessions WHERE code=:c"), {"c": code}).fetchone()
        if not s or not s[1] or s[1] != token:
            return jsonify({"error": "صلاحية غير صحيحة"}), 403
        sid = s[0]
        conn.execute(text("""
            INSERT INTO session_events(session_id, player_number, path_id, choice_id, choice_text, event_text, at_utc)
            VALUES (:sid, NULL, NULL, NULL, NULL, N'🚀 بدأت الجولة الأولى', SYSUTCDATETIME())
        """), {"sid": sid})
    socketio.emit("event_appended", {
        "at": now().isoformat(), "player": None, "choice_text": None,
        "event_text": "🚀 بدأت الجولة الأولى", "path_id": None, "choice_id": None
    }, room=code)
    socketio.emit("story_changed", {"force": True}, room=code)
    return jsonify({"message": "تم تحديث القصة."})

# ================= Story / Status APIs =================
@app.route("/session_status/<code>")
def session_status(code):
    code = safe_upper(code)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM sessions WHERE code=:code"), {"code": code}).fetchone()
        if not row:
            return jsonify({"error": "جلسة غير موجودة"}), 404
        payload = session_state_payload(conn, row[0])
    return jsonify(payload)

@app.route("/story/<code>")
def story(code):
    code = safe_upper(code)
    with engine.connect() as conn:
        row = conn.execute(text("SELECT id FROM sessions WHERE code=:code"), {"code": code}).fetchone()
        if not row:
            return jsonify({"error": "جلسة غير موجودة"}), 404

        sid = row[0]
        row = conn.execute(text("""
            SELECT TOP 1 current_path 
            FROM session_progress 
            WHERE session_id=:sid
            ORDER BY timestamp DESC
        """), {"sid": sid}).fetchone()
        if not row:
            return jsonify({"error": "لا يوجد مسار لهذه الجلسة"}), 404

        current_path = row[0]

        path_row = conn.execute(
            text("SELECT description, current_player FROM paths WHERE id=:pid"),
            {"pid": current_path}
        ).fetchone()
        if not path_row:
            return jsonify({"error": "المسار غير موجود"}), 404

        description, current_player = path_row
        try:
            cp = int(current_player) if current_player is not None else 1
        except:
            cp = 1
        if cp < 1 or cp > MAX_PLAYERS:
            cp = 1

        choices = [
            {"id": r[0], "text": r[1], "next_path_id": r[2]}
            for r in conn.execute(
                text("SELECT id, choice_text, next_path_id FROM choices WHERE path_id=:pid"),
                {"pid": current_path}
            ).fetchall()
        ]

    return jsonify({
        "path_id": current_path,
        "description": description,
        "current_player": cp,
        "choices": choices
    })

@app.route("/events/<code>")
def events(code):
    code = safe_upper(code)
    limit = int(request.args.get("limit", 50))
    with engine.connect() as conn:
        s = conn.execute(text("SELECT id FROM sessions WHERE code=:c"), {"c": code}).fetchone()
        if not s:
            return jsonify([])
        sid = s[0]
        rows = conn.execute(text("""
            SELECT TOP (:lim) id, at_utc, player_number, path_id, choice_id, choice_text, event_text
            FROM session_events
            WHERE session_id=:sid
            ORDER BY id DESC
        """), {"sid": sid, "lim": limit}).fetchall()
    data = [{
        "id": r[0],
        "at": r[1].isoformat() if r[1] else now().isoformat(),
        "player": r[2],
        "path_id": r[3],
        "choice_id": r[4],
        "choice_text": r[5],
        "event_text": r[6],
    } for r in rows]
    return jsonify(list(reversed(data)))

# ================= تقديم صفحات HTML =================
@app.route("/pages/<page>")
def serve_page(page):
    page_path = os.path.join(pages_path_root, page)
    if os.path.exists(page_path):
        with open(page_path, encoding="utf-8") as f:
            return f.read()
    abort(404)

# ================= Socket.IO =================
# ============= Events for Socket.IO =============

@socketio.on("watch_session")
def watch_session(data):
    code = data.get("code")
    join_room(code)
    with engine.connect() as conn:
        payload = session_state_payload(conn, code)
    socketio.emit("session_update", payload, room=code)


@socketio.on("start_session")
def start_session(data):
    code = data.get("code")
    token = data.get("token")
    with engine.begin() as conn:
        row = conn.execute(
            text("SELECT id, token FROM sessions WHERE code=:c"),
            {"c": code}
        ).fetchone()
        if row and row.token == token:
            # ✅ المضيف فقط يقدر يبدأ الجلسة
            socketio.emit("go_host", {"token": token}, room=code)


@socketio.on("join_session_room")
def join_session_room(data):
    code = safe_upper(data.get("code"))
    join_room(code)


@socketio.on("leave_session_room")
def leave_session_room(data):
    code = safe_upper(data.get("code"))
    leave_room(code)

@socketio.on("heartbeat")
def heartbeat(data):
    code = safe_upper(data.get("code"))
    pt   = data.get("player_token")
    if not code or not pt:
        return
    with engine.begin() as conn:
        s = conn.execute(text("SELECT id FROM sessions WHERE code=:c"), {"c": code}).fetchone()
        if not s:
            return
        pl = conn.execute(text("SELECT id FROM session_players WHERE session_id=:sid AND player_token=:pt"),
                          {"sid": s[0], "pt": pt}).fetchone()
        if pl:
            mark_player_connected(conn, pl[0], True)
            socketio.emit("session_update", session_state_payload(conn, s[0]), room=code)

@socketio.on("goodbye")
def goodbye(data):
    code = safe_upper(data.get("code"))
    pt   = data.get("player_token")
    if not code or not pt:
        return
    with engine.begin() as conn:
        s = conn.execute(text("SELECT id FROM sessions WHERE code=:c"), {"c": code}).fetchone()
        if not s:
            return
        pl = conn.execute(text("SELECT id FROM session_players WHERE session_id=:sid AND player_token=:pt"),
                          {"sid": s[0], "pt": pt}).fetchone()
        if pl:
            conn.execute(text("UPDATE session_players SET connected=0 WHERE id=:id"), {"id": pl[0]})
            socketio.emit("session_update", session_state_payload(conn, s[0]), room=code)

@socketio.on("choose_option")
def choose_option(data):
    code        = safe_upper(data.get("code"))
    token       = data.get("token")
    choice_id   = data.get("choice_id")
    player_num  = data.get("player")
    player_tok  = data.get("player_token")

    try:
        player_num = int(player_num)
    except:
        emit("error_msg", {"message": "رقم لاعب غير صالح"}); return

    try:
        with engine.begin() as conn:
            s = conn.execute(text("SELECT id, token FROM sessions WHERE code=:c"), {"c": code}).fetchone()
            if not s or not s[1] or s[1] != token:
                emit("error_msg", {"message": "صلاحية غير صحيحة"}); return
            sid = s[0]

            pl = find_player_by_number_and_token(conn, sid, player_num, player_tok)
            if not pl:
                emit("error_msg", {"message": "هوية اللاعب غير صحيحة"}); return

            cp = current_player_for_session(conn, sid)
            if not (player_num == cp or player_num == 1):
                emit("error_msg", {"message": "ليس دورك الآن"}); return

            ch = conn.execute(text("SELECT id, choice_text, next_path_id FROM choices WHERE id=:cid"),
                              {"cid": choice_id}).fetchone()
            if not ch:
                emit("error_msg", {"message": "خيار غير صالح"}); return

            next_path = ch[2]
            conn.execute(
                text("INSERT INTO session_progress (session_id, current_path) VALUES (:sid, :pid)"),
                {"sid": sid, "pid": next_path}
            )

            nrow = conn.execute(text("SELECT description, current_player FROM paths WHERE id=:pid"),
                                {"pid": next_path}).fetchone()
            next_desc = nrow[0] if nrow else ""
            
            conn.execute(text("""
                INSERT INTO session_events(session_id, player_number, path_id, choice_id, choice_text, event_text, at_utc)
                VALUES (:sid, :pnum, :pid, :cid, :ctext, :evt, SYSUTCDATETIME())
            """), {
                "sid": sid, "pnum": player_num, "pid": next_path,
                "cid": ch[0], "ctext": ch[1],
                "evt": f"انتقلتم إلى مقطع جديد: {next_desc}"
            })

            socketio.emit("session_update", session_state_payload(conn, sid), room=code)
            emit("story_changed", {"next_path_id": next_path}, room=code)
            socketio.emit("event_appended", {
                "at": now().isoformat(),
                "player": player_num,
                "path_id": next_path,
                "choice_id": ch[0],
                "choice_text": ch[1],
                "event_text": f"انتقلتم إلى مقطع جديد: {next_desc}"
            }, room=code)

    except Exception as ex:
        emit("error_msg", {"message": f"خطأ في الاختيار: {ex}"})

if __name__ == "__main__":
    socketio.run(app, debug=True, host='0.0.0.0', port=5000)
