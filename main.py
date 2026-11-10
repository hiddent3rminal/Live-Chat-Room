import hmac
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock

from flask import (
    Flask,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from flask_socketio import SocketIO, emit, join_room, leave_room
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

load_dotenv()

socketio = SocketIO(async_mode="threading")
presence: dict[str, dict[str, str]] = {}
presence_lock = RLock()

ROOM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{1,29}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
ALLOWED_UPLOADS = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "pdf": "application/pdf",
    "mp3": "audio/mpeg",
    "ogg": "audio/ogg",
    "wav": "audio/wav",
    "m4a": "audio/mp4",
    "webm": "video/webm",
    "mp4": "video/mp4",
}
MAX_MESSAGE_LENGTH = 2000


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 10000")
        g.db = db
    return g.db


def close_db(_error: BaseException | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    db.executescript(
        """
        PRAGMA journal_mode = WAL;
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'user' CHECK(role IN ('user', 'admin')),
            avatar_color TEXT NOT NULL DEFAULT '#6a11cb',
            avatar_filename TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            is_private INTEGER NOT NULL DEFAULT 0,
            owner_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS room_members (
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            joined_at TEXT NOT NULL,
            PRIMARY KEY(room_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS room_bans (
            room_id INTEGER NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            banned_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(room_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS uploads (
            attachment_id TEXT PRIMARY KEY,
            owner_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            stored_name TEXT NOT NULL,
            mime TEXT NOT NULL,
            created_at TEXT NOT NULL,
            used INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            room_id INTEGER REFERENCES rooms(id) ON DELETE CASCADE,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            recipient_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
            body TEXT NOT NULL DEFAULT '',
            attachment_name TEXT,
            attachment_id TEXT REFERENCES uploads(attachment_id) ON DELETE SET NULL,
            created_at TEXT NOT NULL,
            deleted_at TEXT,
            pinned INTEGER NOT NULL DEFAULT 0,
            CHECK(room_id IS NOT NULL OR recipient_id IS NOT NULL)
        );
        CREATE INDEX IF NOT EXISTS messages_room_id_id ON messages(room_id, id);
        CREATE INDEX IF NOT EXISTS messages_dm_pair_id ON messages(sender_id, recipient_id, id);
        CREATE TABLE IF NOT EXISTS reactions (
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            emoji TEXT NOT NULL,
            PRIMARY KEY(message_id, user_id, emoji)
        );
        CREATE TABLE IF NOT EXISTS message_reads (
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            read_at TEXT NOT NULL,
            PRIMARY KEY(message_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            reporter_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(message_id, reporter_id)
        );
        CREATE TABLE IF NOT EXISTS user_blocks (
            blocker_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            blocked_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            PRIMARY KEY(blocker_id, blocked_id),
            CHECK(blocker_id != blocked_id)
        );
        """
    )
    for room in ("General", "News", "Friendly"):
        db.execute(
            "INSERT OR IGNORE INTO rooms(name, created_at) VALUES (?, ?)",
            (room, utc_now()),
        )

    columns=db.execute("PRAGMA table_info(users)").fetchall()
    if not any(column["name"] == "avatar_filename" for column in columns):
        db.execute(
            "ALTER TABLE users ADD COLUMN avatar_filename TEXT"
        )
    db.commit()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE=os.environ.get(
            "DATABASE_PATH", str(Path(app.instance_path) / "chat.sqlite3")
        ),
        UPLOAD_FOLDER=os.environ.get(
            "UPLOAD_FOLDER", str(Path(app.instance_path) / "uploads")
        ),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "0") == "1",
        CORS_ORIGINS=os.environ.get("CORS_ORIGINS"),
        REQUIRE_SECRET_KEY=os.environ.get("REQUIRE_SECRET_KEY", "0") == "1",
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.instance_path, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        if app.config["REQUIRE_SECRET_KEY"]:
            raise RuntimeError(
                "SECRET_KEY must be configured with at least 32 characters."
            )
        secret_path = Path(app.instance_path) / "secret_key"
        if secret_path.exists():
            app.config["SECRET_KEY"] = secret_path.read_text(encoding="utf-8")
        else:
            app.config["SECRET_KEY"] = os.urandom(32).hex()
            secret_path.write_text(app.config["SECRET_KEY"], encoding="utf-8")
            secret_path.chmod(0o600)
    if (
        app.config["REQUIRE_SECRET_KEY"]
        and len(app.config["SECRET_KEY"]) < 32
    ):
        raise RuntimeError(
            "SECRET_KEY must be configured with at least 32 characters."
        )
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    app.teardown_appcontext(close_db)
    socketio.init_app(app, cors_allowed_origins=app.config["CORS_ORIGINS"])

    with app.app_context():
        init_db()

    register_routes(app)
    return app


def current_user() -> sqlite3.Row | None:
    user_id = session.get("user_id")
    if user_id is None:
        return None
    return get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            if request.path.startswith("/api/"):
                return jsonify(error="Authentication required."), 401
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def csrf_token() -> str:
    token = session.get("_csrf_token")
    if token is None:
        token = session["_csrf_token"] = uuid.uuid4().hex
    return token


def verify_csrf() -> bool:
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    return bool(supplied and supplied == session.get("_csrf_token"))


def can_access_room(room_id: int, user_id: int) -> bool:
    row = get_db().execute(
        """SELECT r.is_private, rm.user_id FROM rooms r
           LEFT JOIN room_members rm ON rm.room_id = r.id AND rm.user_id = ?
           WHERE r.id = ?""",
        (user_id, room_id),
    ).fetchone()
    return bool(row and (not row["is_private"] or row["user_id"] is not None))


def is_room_banned(room_id: int, user_id: int) -> bool:
    return get_db().execute(
        "SELECT 1 FROM room_bans WHERE room_id = ? AND user_id = ?",
        (room_id, user_id),
    ).fetchone() is not None


def users_are_blocked(first_user_id: int, second_user_id: int) -> bool:
    return get_db().execute(
        """SELECT 1 FROM user_blocks
           WHERE (blocker_id = ? AND blocked_id = ?)
              OR (blocker_id = ? AND blocked_id = ?)""",
        (first_user_id, second_user_id, second_user_id, first_user_id),
    ).fetchone() is not None


def serialize_message(row: sqlite3.Row, viewer_id: int) -> dict:
    db = get_db()
    reactions = db.execute(
        """SELECT r.emoji, COUNT(*) AS count,
                  SUM(CASE WHEN r.user_id = ? THEN 1 ELSE 0 END) AS mine
           FROM reactions r WHERE r.message_id = ? GROUP BY r.emoji""",
        (viewer_id, row["id"]),
    ).fetchall()
    return {
        "id": row["id"],
        "sender": row["username"],
        "sender_id": row["sender_id"],
        "avatar_url":(
            url_for("avatar_file", filename=row["avatar_filename"])
            if row["avatar_filename"]
            else url_for("static",filename="default-avatar.jpg")
        ),
        "room_id": row["room_id"],
        "body": row["body"],
        "created_at": row["created_at"],
        "attachment_id": row["attachment_id"],
        "attachment_name": row["attachment_name"],
        "pinned": bool(row["pinned"]),
        "deleted": bool(row["deleted_at"]),
        "read_count": db.execute(
            "SELECT COUNT(*) FROM message_reads WHERE message_id = ?",
            (row["id"],),
        ).fetchone()[0],
        "reactions": [
            {"emoji": r["emoji"], "count": r["count"], "mine": bool(r["mine"])}
            for r in reactions
        ],
    }


def register_routes(app: Flask) -> None:
    @app.context_processor
    def inject_csrf_token():
        return {"csrf_token": csrf_token}

    @app.before_request
    def protect_post_requests():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None

        if current_user() is None:
            if request.path in {"/login", "/register"}:
                if not verify_csrf():
                    if request.path.startswith("/api/"):
                        return jsonify(error="Invalid or missing CSRF token."), 400
                    abort(400, description="Invalid or missing CSRF token.")
                return None
            return None

        if not verify_csrf():
            if request.path.startswith("/api/"):
                return jsonify(error="Invalid or missing CSRF token."), 400
            abort(400, description="Invalid or missing CSRF token.")
        return None

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; "
            "script-src 'self' https://cdn.socket.io; "
            "style-src 'self'; "
            "img-src 'self' data: blob:; "
            "media-src 'self' blob:; "
            "connect-src 'self' ws: wss:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'",
        )
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(self)",
        )
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000"
            )
        return response

    @app.get("/health")
    def health():
        get_db().execute("SELECT 1")
        return jsonify(status="ok")

    @app.route("/")
    @login_required
    def index():
        user = current_user()
        rooms = get_db().execute(
            """SELECT r.*, (SELECT COUNT(*) FROM messages m WHERE m.room_id = r.id
                 AND m.deleted_at IS NULL) AS message_count
               FROM rooms r WHERE r.is_private = 0 OR EXISTS (
                 SELECT 1 FROM room_members rm WHERE rm.room_id = r.id AND rm.user_id = ?
               ) ORDER BY r.name COLLATE NOCASE""",
            (user["id"],),
        ).fetchall()
        return render_template("index.html", user=user, rooms=rooms)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            if not USERNAME_RE.fullmatch(username):
                flash("Usernames must be 3–24 letters, numbers, or underscores.", "error")
            elif len(password) < 10 or len(password) > 128:
                flash("Choose a password between 10 and 128 characters.", "error")
            elif (
                os.environ.get("ADMIN_USERNAME")
                and username.casefold() == os.environ["ADMIN_USERNAME"].casefold()
                and (
                    not os.environ.get("ADMIN_PASSWORD")
                    or not hmac.compare_digest(password, os.environ["ADMIN_PASSWORD"])
                )
            ):
                flash("That username is reserved for the configured administrator.", "error")
            else:
                db = get_db()
                try:
                    is_admin = (
                        os.environ.get("ADMIN_USERNAME")
                        and username.casefold() == os.environ["ADMIN_USERNAME"].casefold()
                        and hmac.compare_digest(password, os.environ["ADMIN_PASSWORD"])
                    )
                    cursor = db.execute(
                        """INSERT INTO users(username, password_hash, role, created_at)
                           VALUES (?, ?, ?, ?)""",
                        (
                            username,
                            generate_password_hash(password),
                            "admin" if is_admin else "user",
                            utc_now(),
                        ),
                    )
                    user_id = cursor.lastrowid
                    general = db.execute(
                        "SELECT id FROM rooms WHERE name = 'General'"
                    ).fetchone()
                    db.execute(
                        "INSERT INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
                        (general["id"], user_id, utc_now()),
                    )
                    db.commit()
                except sqlite3.IntegrityError:
                    db.rollback()
                    flash("That username is already taken.", "error")
                else:
                    session.clear()
                    session["user_id"] = user_id
                    csrf_token()
                    return redirect(url_for("index"))
            return render_template("auth.html", mode="register")
        return render_template("auth.html", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = get_db().execute(
                "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
            if (
                user
                and len(password) <= 128
                and check_password_hash(user["password_hash"], password)
            ):
                session.clear()
                session["user_id"] = user["id"]
                csrf_token()
                return redirect(url_for("index"))
            flash("Username or password is incorrect.", "error")
        return render_template("auth.html", mode="login")

    @app.post("/logout")
    @login_required
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.post("/api/profile")
    @login_required
    def update_profile():
        data = request.get_json(silent=True)
        username = data.get("username", "").strip() if isinstance(data, dict) and isinstance(data.get("username"), str) else ""
        if not USERNAME_RE.fullmatch(username):
            return jsonify(error="Username must be 3–24 letters, numbers, or underscores."), 400
        db = get_db()
        try:
            db.execute(
                "UPDATE users SET username = ? WHERE id = ?",
                (username, session["user_id"]),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return jsonify(error="That username is already taken."), 409
        with presence_lock:
            for entry in presence.values():
                if entry["user_id"] == str(session["user_id"]):
                    entry["username"] = username
        socketio.emit("presence", active_users_payload())
        return jsonify(username=username)


    @app.post("/api/avatar")
    @login_required
    def uploadavatar():
        uploaded=request.files.get("avatar")

        if uploaded is None or not uploaded.filename:
            return jsonify(error="Choose a picture"), 400

        extenention=uploaded.filename.rsplit(".",1)[-1].lower()
        if extenention not in {"png", "jpg" , "jpeg" , "webp"}:
            return jsonify(error="Use a png,jpg,jpeg or webp"), 415

        content=uploaded.read(4 * 1024 * 1024 + 1)
        if not content:
            return jsonify(error="The image is empty "), 400

        if len(content) > 4 * 1024 * 1024:
            return jsonify(error="The image must be 4MB or less"),413

        isPNG=content.startswith(b"\x89PNG\r\n\x1a\n")
        is_jpeg=content.startswith(b"\xff\xd8\xff")
        is_webp=(
            len(content)>=12
            and content[:4] == b"RIFF"
            and content[8:12]== b"WEBP"
        )

        if not (isPNG or is_jpeg or is_webp):
            return jsonify(error="This file isn't a supported image."),415


        extenention="jpg" if is_jpeg else extenention
        if is_jpeg and extenention=="jpeg":
            extenention="jpg"

        avatar_folder=Path(app.config["UPLOAD_FOLDER"]) / "avatars"
        avatar_folder.mkdir(parents=True, exist_ok=True)

        stored_name=f"{uuid.uuid4().hex}.{extenention}"
        file_path=avatar_folder/ stored_name

        db=get_db()
        old_user=db.execute(
            "SELECT avatar_filename FROM users WHERE id=?",
            (session["user_id"],),
        ).fetchone()

        file_path.write_bytes(content)


        try:
            db=get_db()
            db.execute(
                "UPDATE users SET avatar_filename=? WHERE id=?",
                (stored_name, session["user_id"]),
            )
            db.commit()

            user_id=session["user_id"]

            with presence_lock:
                for entry in presence.values():
                    if entry["user_id"] == str(user_id):
                        entry["avatar_filename"]= stored_name
            avatar_url=url_for("avatar_file", filename=stored_name)
            socketio.emit(
                "avatar_updated",
                {
                    "user_id" : user_id,
                    "avatar_url" : avatar_url,
                },
            )
            socketio.emit("presence", active_users_payload())
        except sqlite3.Error:
            file_path.unlink(missing_ok=True)
            db.rollback()
            current_app.logger.exception("Failed to Save Profile Picture")
            return jsonify(error="couldn not save profile picture."),500
        if old_user and old_user["avatar_filename"]:
            old_name=old_user["avatar_filename"]

            if re.fullmatch(r"[0-9a-f]{32}\.{?:png|jpg|webp}",old_name):
                (avatar_folder / old_name).unlink(missing_ok=True)

        return jsonify(
            avatar_url=url_for("avatar_file",filename=stored_name)
        ),200

    
    @app.get("/avatars/<filename>")
    @login_required
    def avatar_file(filename : str):
        if not re.fullmatch(r"[0-9a-f]{32}\.(png|jpg|webp)",filename):
            abort(404)

        avatar_folder=Path(app.config["UPLOAD_FOLDER"]) / "avatars"
        return send_from_directory(
            avatar_folder,
            filename,
            as_attachment=False,
            conditional=True
        )



    @app.get("/api/rooms")
    @login_required
    def list_rooms():
        user_id = session["user_id"]
        rows = get_db().execute(
            """SELECT r.id, r.name, r.description, r.is_private, r.owner_id,
                      EXISTS(SELECT 1 FROM room_members rm
                        WHERE rm.room_id = r.id AND rm.user_id = ?) AS member
               FROM rooms r WHERE r.is_private = 0 OR member = 1
               ORDER BY r.name COLLATE NOCASE""",
            (user_id,),
        ).fetchall()
        return jsonify(rooms=[dict(row) for row in rows])

    @app.post("/api/rooms")
    @login_required
    def create_room():
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        name = data.get("name", "").strip() if isinstance(data.get("name"), str) else ""
        description = data.get("description", "").strip()[:200] if isinstance(data.get("description"), str) else ""
        is_private = data.get("private", False)
        if not isinstance(is_private, bool):
            return jsonify(error="The private setting must be true or false."), 400
        if not ROOM_NAME_RE.fullmatch(name):
            return jsonify(error="Room names must be 2–30 letters, numbers, spaces, _ or -."), 400
        db = get_db()
        try:
            cursor = db.execute(
                """INSERT INTO rooms(name, description, is_private, owner_id, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (name, description, int(is_private), session["user_id"], utc_now()),
            )
            room_id = cursor.lastrowid
            db.execute(
                "INSERT INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
                (room_id, session["user_id"], utc_now()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            return jsonify(error="A room with that name already exists."), 409
        return jsonify(id=room_id, name=name, description=description, private=is_private), 201

    @app.post("/api/rooms/<int:room_id>/join")
    @login_required
    def join_private_room(room_id: int):
        db = get_db()
        room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if room is None:
            return jsonify(error="Room not found."), 404
        if is_room_banned(room_id, session["user_id"]):
            return jsonify(error="You are banned from this room."), 403
        membership = db.execute(
            "SELECT 1 FROM room_members WHERE room_id = ? AND user_id = ?",
            (room_id, session["user_id"]),
        ).fetchone()
        if room["is_private"] and not membership:
            return jsonify(error="This room is private; ask its owner for an invitation."), 403
        db.execute(
            "INSERT OR IGNORE INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
            (room_id, session["user_id"], utc_now()),
        )
        db.commit()
        return jsonify(ok=True)

    @app.post("/api/rooms/<int:room_id>/invite")
    @login_required
    def invite_to_room(room_id: int):
        data = request.get_json(silent=True)
        username = data.get("username", "").strip() if isinstance(data, dict) and isinstance(data.get("username"), str) else ""
        db = get_db()
        room = db.execute(
            "SELECT id, name, is_private, owner_id FROM rooms WHERE id = ?",
            (room_id,),
        ).fetchone()
        target = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        actor = current_user()
        if room is None or target is None:
            return jsonify(error="Room or user not found."), 404
        if room["owner_id"] != actor["id"] and actor["role"] != "admin":
            return jsonify(error="Only the room owner or an admin can invite users."), 403
        if db.execute(
            "SELECT 1 FROM room_bans WHERE room_id = ? AND user_id = ?",
            (room_id, target["id"]),
        ).fetchone():
            return jsonify(error="That user is banned from this room."), 409
        db.execute(
            "INSERT OR IGNORE INTO room_members(room_id, user_id, joined_at) VALUES (?, ?, ?)",
            (room_id, target["id"], utc_now()),
        )
        db.commit()
        socketio.emit(
            "room_invited",
            {"room": dict(room)},
            room=f"user:{target['id']}",
        )
        return jsonify(ok=True)

    @app.get("/api/private-history")
    @login_required
    def private_history():
        username = request.args.get("user", "").strip()
        other = get_db().execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if other is None:
            return jsonify(error="User not found."), 404
        if users_are_blocked(session["user_id"], other["id"]):
            return jsonify(error="This private conversation is unavailable."), 403
        rows = get_db().execute(
            """SELECT m.*, u.username, u.avatar_filename FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.room_id IS NULL
               AND ((m.sender_id = ? AND m.recipient_id = ?)
                 OR (m.sender_id = ? AND m.recipient_id = ?))
               ORDER BY m.id DESC LIMIT 100""",
            (session["user_id"], other["id"], other["id"], session["user_id"]),
        ).fetchall()
        messages = []
        for row in reversed(rows):
            message = serialize_message(row, session["user_id"])
            message["recipient"] = username if row["sender_id"] == session["user_id"] else current_user()["username"]
            messages.append(message)
        return jsonify(messages=messages)

    @app.get("/api/history")
    @login_required
    def message_history():
        room_id = request.args.get("room", type=int)
        before = request.args.get("before", type=int)
        if room_id is None or not can_access_room(room_id, session["user_id"]):
            return jsonify(error="Room not found or access denied."), 404
        if is_room_banned(room_id, session["user_id"]):
            return jsonify(error="You are banned from this room."), 403
        sql = """SELECT m.*, u.username , u.avatar_filename FROM messages m JOIN users u ON u.id = m.sender_id
                 WHERE m.room_id = ? AND m.deleted_at IS NULL"""
        params: list[int] = [room_id]
        if before is not None:
            sql += " AND m.id < ?"
            params.append(before)
        rows = get_db().execute(
            sql + " ORDER BY m.id DESC LIMIT 101",
            params,
        ).fetchall()
        has_more = len(rows) > 100
        rows = rows[:100]
        return jsonify(
            messages=[
                serialize_message(row, session["user_id"]) for row in reversed(rows)
            ],
            has_more=has_more,
            before=rows[-1]["id"] if rows else None,
        )

    @app.get("/api/search")
    @login_required
    def search_messages():
        query = request.args.get("q", "").strip()
        room_id = request.args.get("room", type=int)
        if len(query) < 2 or len(query) > 100:
            return jsonify(error="Search terms must be 2–100 characters."), 400
        if room_id is not None and not can_access_room(room_id, session["user_id"]):
            return jsonify(error="Room not found or access denied."), 404
        if room_id is not None and is_room_banned(room_id, session["user_id"]):
            return jsonify(error="You are banned from this room."), 403
        escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params: list = [
            f"%{escaped_query}%",
            session["user_id"],
            session["user_id"],
            session["user_id"],
            session["user_id"],
        ]
        scope = ""
        if room_id is not None:
            scope = "AND m.room_id = ?"
            params.append(room_id)
        rows = get_db().execute(
            """SELECT m.*, u.username FROM messages m JOIN users u ON u.id = m.sender_id
               WHERE m.body LIKE ? ESCAPE '\\' AND m.deleted_at IS NULL
               AND (m.room_id IN (SELECT r.id FROM rooms r WHERE r.is_private = 0
                    OR EXISTS (SELECT 1 FROM room_members rm WHERE rm.room_id = r.id
                               AND rm.user_id = ?))
                    OR m.sender_id = ? OR m.recipient_id = ?)
               AND (m.room_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM room_bans rb
                    WHERE rb.room_id = m.room_id AND rb.user_id = ?))
               AND (m.room_id IS NOT NULL OR NOT EXISTS (
                    SELECT 1 FROM user_blocks ub
                    WHERE (ub.blocker_id = m.sender_id AND ub.blocked_id = m.recipient_id)
                       OR (ub.blocker_id = m.recipient_id AND ub.blocked_id = m.sender_id)))
               """ + scope + " ORDER BY m.id DESC LIMIT 50",
            params,
        ).fetchall()
        return jsonify(messages=[
            serialize_message(row, session["user_id"]) for row in rows
        ])

    @app.post("/api/uploads")
    @login_required
    def upload_file():
        uploaded = request.files.get("file")
        if uploaded is None or not uploaded.filename:
            return jsonify(error="Choose a file to upload."), 400
        safe_name = secure_filename(uploaded.filename)
        extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
        if extension not in ALLOWED_UPLOADS:
            return jsonify(error="This file type is not allowed."), 415
        content = uploaded.read(app.config["MAX_CONTENT_LENGTH"] + 1)
        if len(content) > app.config["MAX_CONTENT_LENGTH"]:
            return jsonify(error="File is larger than 8 MB."), 413
        if not content:
            return jsonify(error="The uploaded file is empty."), 400
        attachment_id = uuid.uuid4().hex
        stored_name = f"{attachment_id}.{extension}"
        path = Path(app.config["UPLOAD_FOLDER"]) / stored_name
        path.write_bytes(content)
        db = get_db()
        db.execute(
            """INSERT INTO uploads(attachment_id, owner_id, filename, stored_name, mime, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                attachment_id,
                session["user_id"],
                safe_name[:180],
                stored_name,
                ALLOWED_UPLOADS[extension],
                utc_now(),
            ),
        )
        db.commit()
        return jsonify(
            attachment_id=attachment_id,
            name=safe_name[:180],
            url=url_for("download_file", attachment_id=attachment_id),
            mime=ALLOWED_UPLOADS[extension],
        ), 201

    @app.get("/uploads/<attachment_id>")
    @login_required
    def download_file(attachment_id: str):
        row = get_db().execute(
            """SELECT u.stored_name, u.filename, u.mime, m.room_id, m.sender_id, m.recipient_id
               FROM uploads u JOIN messages m ON m.attachment_id = u.attachment_id
               WHERE u.attachment_id = ? AND m.deleted_at IS NULL""",
            (attachment_id,),
        ).fetchone()
        if row is None or (
            row["room_id"] is not None
            and (
                not can_access_room(row["room_id"], session["user_id"])
                or is_room_banned(row["room_id"], session["user_id"])
            )
        ) or (
            row["room_id"] is None
            and (
                session["user_id"] not in (row["sender_id"], row["recipient_id"])
                or users_are_blocked(row["sender_id"], row["recipient_id"])
            )
        ):
            abort(404)
        return send_from_directory(
            app.config["UPLOAD_FOLDER"],
            row["stored_name"],
            as_attachment=False,
            download_name=row["filename"],
            mimetype=row["mime"],
            conditional=True,
        )

    @app.post("/api/messages/<int:message_id>/react")
    @login_required
    def react_to_message(message_id: int):
        data = request.get_json(silent=True)
        emoji = data.get("emoji", "") if isinstance(data, dict) else ""
        if emoji not in {"👍", "❤️", "😂", "🎉", "😮", "😢"}:
            return jsonify(error="Unsupported reaction."), 400
        db = get_db()
        message = db.execute("SELECT room_id FROM messages WHERE id = ?", (message_id,)).fetchone()
        if (
            message is None
            or message["room_id"] is None
            or not can_access_room(message["room_id"], session["user_id"])
            or is_room_banned(message["room_id"], session["user_id"])
        ):
            return jsonify(error="Message not found."), 404
        existing = db.execute(
            "SELECT 1 FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
            (message_id, session["user_id"], emoji),
        ).fetchone()
        if existing:
            db.execute(
                "DELETE FROM reactions WHERE message_id = ? AND user_id = ? AND emoji = ?",
                (message_id, session["user_id"], emoji),
            )
        else:
            db.execute(
                "INSERT INTO reactions(message_id, user_id, emoji) VALUES (?, ?, ?)",
                (message_id, session["user_id"], emoji),
            )
        db.commit()
        socketio.emit("message_update", {"id": message_id}, room=f"room:{message['room_id']}")
        return jsonify(ok=True, active=not existing)

    @app.post("/api/messages/<int:message_id>/pin")
    @login_required
    def pin_message(message_id: int):
        db = get_db()
        row = db.execute(
            """SELECT m.room_id, m.sender_id, r.owner_id, u.role
               FROM messages m JOIN rooms r ON r.id = m.room_id
               JOIN users u ON u.id = ? WHERE m.id = ?""",
            (session["user_id"], message_id),
        ).fetchone()
        if (
            row is None
            or not can_access_room(row["room_id"], session["user_id"])
            or is_room_banned(row["room_id"], session["user_id"])
        ):
            return jsonify(error="Message not found."), 404
        if row["sender_id"] != session["user_id"] and row["owner_id"] != session["user_id"] and row["role"] != "admin":
            return jsonify(error="Only the sender, room owner, or an admin can pin messages."), 403
        db.execute("UPDATE messages SET pinned = 1 - pinned WHERE id = ?", (message_id,))
        db.commit()
        socketio.emit("message_update", {"id": message_id}, room=f"room:{row['room_id']}")
        return jsonify(ok=True)

    @app.post("/api/messages/<int:message_id>/delete")
    @login_required
    def delete_message(message_id: int):
        db = get_db()
        row = db.execute(
            """SELECT m.room_id, m.sender_id, r.owner_id, u.role
               FROM messages m JOIN rooms r ON r.id = m.room_id
               JOIN users u ON u.id = ? WHERE m.id = ?""",
            (session["user_id"], message_id),
        ).fetchone()
        if row is None:
            return jsonify(error="Message not found."), 404
        if (
            not can_access_room(row["room_id"], session["user_id"])
            or is_room_banned(row["room_id"], session["user_id"])
        ):
            return jsonify(error="Message not found."), 404
        if row["sender_id"] != session["user_id"] and row["owner_id"] != session["user_id"] and row["role"] != "admin":
            return jsonify(error="You cannot delete this message."), 403
        db.execute("UPDATE messages SET body = '', deleted_at = ?, pinned = 0 WHERE id = ?", (utc_now(), message_id))
        db.execute("DELETE FROM reactions WHERE message_id = ?", (message_id,))
        db.commit()
        socketio.emit("message_update", {"id": message_id}, room=f"room:{row['room_id']}")
        return jsonify(ok=True)

    @app.post("/api/messages/<int:message_id>/report")
    @login_required
    def report_message(message_id: int):
        data = request.get_json(silent=True)
        reason = data.get("reason", "").strip()[:300] if isinstance(data, dict) and isinstance(data.get("reason"), str) else ""
        if len(reason) < 3:
            return jsonify(error="Please provide a short reason."), 400
        db = get_db()
        message = db.execute(
            """SELECT m.room_id, m.sender_id, m.recipient_id FROM messages m
               WHERE m.id = ? AND m.deleted_at IS NULL""",
            (message_id,),
        ).fetchone()
        user_id = session["user_id"]
        if message is None or (
            message["room_id"] is not None
            and (
                not can_access_room(message["room_id"], user_id)
                or is_room_banned(message["room_id"], user_id)
            )
        ) or (
            message["room_id"] is None
            and user_id not in (message["sender_id"], message["recipient_id"])
        ):
            return jsonify(error="Message not found."), 404
        try:
            db.execute(
                "INSERT INTO reports(message_id, reporter_id, reason, created_at) VALUES (?, ?, ?, ?)",
                (message_id, session["user_id"], reason, utc_now()),
            )
            db.commit()
        except sqlite3.IntegrityError:
            return jsonify(error="You have already reported this message."), 409
        return jsonify(ok=True), 201

    @app.get("/api/admin/reports")
    @login_required
    def list_reports():
        user = current_user()
        if user["role"] != "admin":
            return jsonify(error="Admin access required."), 403
        rows = get_db().execute(
            """SELECT r.id, r.reason, r.created_at, m.id AS message_id, m.body,
                      sender.username AS sender, reporter.username AS reporter
               FROM reports r JOIN messages m ON m.id = r.message_id
               JOIN users sender ON sender.id = m.sender_id
               JOIN users reporter ON reporter.id = r.reporter_id
               ORDER BY r.created_at DESC LIMIT 100"""
        ).fetchall()
        return jsonify(reports=[dict(row) for row in rows])

    @app.route("/api/users/<username>/block", methods=["GET", "POST", "DELETE"])
    @login_required
    def set_user_block(username: str):
        db = get_db()
        target = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if target is None or target["id"] == session["user_id"]:
            return jsonify(error="User not found."), 404
        if request.method == "GET":
            return jsonify(
                blocked=users_are_blocked(session["user_id"], target["id"])
            )
        if request.method == "POST":
            db.execute(
                "INSERT OR IGNORE INTO user_blocks(blocker_id, blocked_id, created_at) VALUES (?, ?, ?)",
                (session["user_id"], target["id"], utc_now()),
            )
        else:
            db.execute(
                "DELETE FROM user_blocks WHERE blocker_id = ? AND blocked_id = ?",
                (session["user_id"], target["id"]),
            )
        db.commit()
        return jsonify(blocked=request.method == "POST")

    @app.post("/api/rooms/<int:room_id>/ban")
    @login_required
    def ban_user(room_id: int):
        data = request.get_json(silent=True)
        target_name = data.get("username", "").strip() if isinstance(data, dict) and isinstance(data.get("username"), str) else ""
        db = get_db()
        room = db.execute("SELECT owner_id FROM rooms WHERE id = ?", (room_id,)).fetchone()
        target = db.execute("SELECT id FROM users WHERE username = ? COLLATE NOCASE", (target_name,)).fetchone()
        actor = current_user()
        if room is None or target is None:
            return jsonify(error="Room or user not found."), 404
        if room["owner_id"] != actor["id"] and actor["role"] != "admin":
            return jsonify(error="Only the room owner or an admin can ban users."), 403
        if target["id"] == actor["id"]:
            return jsonify(error="You cannot ban yourself."), 400
        db.execute(
            "INSERT OR IGNORE INTO room_bans(room_id, user_id, banned_by, created_at) VALUES (?, ?, ?, ?)",
            (room_id, target["id"], actor["id"], utc_now()),
        )
        db.execute("DELETE FROM room_members WHERE room_id = ? AND user_id = ?", (room_id, target["id"]))
        db.commit()
        with presence_lock:
            target_sids = [
                sid for sid, entry in presence.items()
                if entry["user_id"] == str(target["id"])
                and entry.get("room") == str(room_id)
            ]
            for sid in target_sids:
                leave_room(f"room:{room_id}", sid=sid, namespace="/")
                presence[sid]["room"] = ""
        socketio.emit(
            "room_banned",
            {"room_id": room_id},
            room=f"user:{target['id']}",
        )
        socketio.emit("presence", active_users_payload())
        return jsonify(ok=True)

    @app.errorhandler(413)
    def file_too_large(_error):
        return jsonify(error="Request is larger than the 8 MB upload limit."), 413


def active_users_payload(room_id: str | None = None) -> dict:
    with presence_lock:
        users = [
            {
    "user_id": entry["user_id"],
    "username": entry["username"],
    "room": entry.get("room", ""),
    "avatar_url": url_for("avatar_file", filename=entry["avatar_filename"])
        if entry.get("avatar_filename")
        else url_for("static", filename="default-avatar.jpg"),
}
            for entry in presence.values()
            if room_id is None or entry.get("room") == room_id
        ]
    return {"users": users}


@socketio.on("connect")
def on_connect(auth=None):
    user = current_user()
    if user is None:
        return False
    with presence_lock:
        presence[request.sid] = {
            "user_id": str(user["id"]),
            "username": user["username"],
            "avatar_filename": user["avatar_filename"],
            "sent_at": [],
        }
    join_room(f"user:{user['id']}")
    emit("presence", active_users_payload(), broadcast=True)


@socketio.on("disconnect")
def on_disconnect(_reason=None):
    with presence_lock:
        presence.pop(request.sid, None)
    emit("presence", active_users_payload(), broadcast=True)


@socketio.on("join")
def on_join(data):
    user = current_user()
    if user is None or not isinstance(data, dict):
        return
    try:
        room_id = int(data.get("room_id", 0))
    except (TypeError, ValueError):
        return
    db = get_db()
    room = db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
    if room is None or not can_access_room(room_id, user["id"]):
        emit("error_message", {"error": "Room not found or access denied."})
        return
    if is_room_banned(room_id, user["id"]):
        emit("error_message", {"error": "You are banned from this room."})
        return
    with presence_lock:
        old_room = presence.get(request.sid, {}).get("room")
        presence[request.sid]["room"] = str(room_id)
    if old_room:
        leave_room(f"room:{old_room}")
    join_room(f"room:{room_id}")
    history = db.execute(
        """SELECT m.*, u.username , u.avatar_filename FROM messages m JOIN users u ON u.id = m.sender_id
           WHERE m.room_id = ? AND m.deleted_at IS NULL ORDER BY m.id DESC LIMIT 100""",
        (room_id,),
    ).fetchall()
    emit(
        "history",
        {
            "room_id": room_id,
            "messages": [
                serialize_message(row, user["id"]) for row in reversed(history)
            ],
        },
    )
    emit("presence", active_users_payload(), broadcast=True)


@socketio.on("leave")
def on_leave(data):
    user = current_user()
    if user is None or not isinstance(data, dict):
        return
    try:
        room_id = int(data.get("room_id", 0))
    except (TypeError, ValueError):
        return
    room_name = f"room:{room_id}"
    with presence_lock:
        entry = presence.get(request.sid)
        if entry is None or entry.get("room") != str(room_id):
            return
        leave_room(room_name)
        entry["room"] = ""
    emit("presence", active_users_payload(), broadcast=True)


@socketio.on("message")
def on_message(data):
    user = current_user()
    if user is None:
        return

    def reject(error: str) -> dict[str, bool]:
        emit("error_message", {"error": error})
        return {"ok": False}

    if not isinstance(data, dict):
        return reject("Invalid message.")
    body = data.get("body", "")
    if not isinstance(body, str):
        return reject("Message must be text.")
    body = body.strip()
    attachment_id = data.get("attachment_id")
    if attachment_id is not None and (
        not isinstance(attachment_id, str) or not re.fullmatch(r"[0-9a-f]{32}", attachment_id)
    ):
        return reject("Invalid attachment.")
    attachment_name = None
    if attachment_id:
        attachment = get_db().execute(
            """SELECT filename FROM uploads WHERE attachment_id = ?
               AND owner_id = ? AND used = 0""",
            (attachment_id, user["id"]),
        ).fetchone()
        if attachment is None:
            return reject("Upload not found or already attached.")
        attachment_name = attachment["filename"]
    if not body and not attachment_name:
        return reject("Enter a message or attach a file.")
    if len(body) > MAX_MESSAGE_LENGTH:
        return reject(f"Messages are limited to {MAX_MESSAGE_LENGTH} characters.")
    now = datetime.now(timezone.utc).timestamp()
    with presence_lock:
        active = presence.get(request.sid)
        if active is None:
            return reject("Your chat connection is no longer active.")
        active["sent_at"] = [stamp for stamp in active["sent_at"] if now - stamp < 10]
        if len(active["sent_at"]) >= 10:
            return reject("You're sending messages too quickly. Try again shortly.")
        active["sent_at"].append(now)
    db = get_db()
    room_id = data.get("room_id")
    recipient_name = data.get("recipient")
    if recipient_name is not None and not isinstance(recipient_name, str):
        return reject("Invalid recipient.")
    if recipient_name:
        if not USERNAME_RE.fullmatch(recipient_name):
            return reject("Invalid recipient.")
        recipient = db.execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE",
            (recipient_name,),
        ).fetchone()
        if recipient is None or recipient["id"] == user["id"]:
            return reject("That recipient is unavailable.")
        if users_are_blocked(user["id"], recipient["id"]):
            return reject("This private conversation is unavailable.")
        cursor = db.execute(
            """INSERT INTO messages(sender_id, recipient_id, body, attachment_name,
                                    attachment_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user["id"], recipient["id"], body, attachment_name, attachment_id, utc_now()),
        )
        if attachment_id:
            db.execute(
                "UPDATE uploads SET used = 1 WHERE attachment_id = ?",
                (attachment_id,),
            )
        db.commit()
        message = {
            "id": cursor.lastrowid,
            "sender": user["username"],
            "sender_id": user["id"],
            "avatar_url":(
                url_for("avatar_file",filename=user["avatar_filename"])
                if user["avatar_filename"]
                else url_for("static",filename="default-avatar.jpg")
            ),
            "recipient": recipient_name,
            "body": body,
            "created_at": utc_now(),
            "attachment_id": attachment_id,
            "attachment_name": attachment_name,
            "pinned": False,
            "deleted": False,
            "reactions": [],
        }
        emit("private_message", message, room=f"user:{recipient['id']}")
        emit("private_message", message, room=f"user:{user['id']}")
        return {"ok": True}
    try:
        room_id = int(room_id)
    except (TypeError, ValueError):
        return reject("Select a room before sending.")
    if not can_access_room(room_id, user["id"]):
        return reject("Room not found or access denied.")
    if is_room_banned(room_id, user["id"]):
        return reject("You are banned from this room.")
    with presence_lock:
        joined_room = presence.get(request.sid, {}).get("room")
    if joined_room != str(room_id):
        return reject("Join this room before sending a message.")
    cursor = db.execute(
        """INSERT INTO messages(room_id, sender_id, body, attachment_name,
                                attachment_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (room_id, user["id"], body, attachment_name, attachment_id, utc_now()),
    )
    if attachment_id:
        db.execute(
            "UPDATE uploads SET used = 1 WHERE attachment_id = ?",
            (attachment_id,),
        )
    db.commit()
    row = db.execute(
        "SELECT m.*, u.username, u.avatar_filename FROM messages m JOIN users u ON u.id = m.sender_id WHERE m.id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    socketio.emit(
        "message",
        serialize_message(row, user["id"]),
        room=f"room:{room_id}",
    )
    return {"ok": True}


@socketio.on("typing")
def on_typing(data):
    user = current_user()
    if user is None or not isinstance(data, dict):
        return
    room_id = data.get("room_id")
    recipient = data.get("recipient")
    payload = {"username": user["username"], "typing": bool(data.get("typing"))}
    if recipient:
        if not isinstance(recipient, str):
            return
        target = get_db().execute(
            "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (recipient,)
        ).fetchone()
        if target and target["id"] != user["id"] and not users_are_blocked(user["id"], target["id"]):
            emit("typing", payload, room=f"user:{target['id']}", include_self=False)
    elif room_id:
        try:
            room_id = int(room_id)
        except (TypeError, ValueError):
            return
        with presence_lock:
            joined_room = presence.get(request.sid, {}).get("room")
        if (
            joined_room == str(room_id)
            and can_access_room(room_id, user["id"])
            and not is_room_banned(room_id, user["id"])
        ):
            emit("typing", payload, room=f"room:{room_id}", include_self=False)


@socketio.on("read")
def on_read(data):
    user = current_user()
    if user is None or not isinstance(data, dict):
        return
    room_id = data.get("room_id")
    try:
        room_id = int(room_id)
    except (TypeError, ValueError):
        return
    if (
        not room_id
        or not can_access_room(room_id, user["id"])
        or is_room_banned(room_id, user["id"])
    ):
        return
    with presence_lock:
        joined_room = presence.get(request.sid, {}).get("room")
    if joined_room != str(room_id):
        return
    db = get_db()
    db.execute(
        """INSERT OR IGNORE INTO message_reads(message_id, user_id, read_at)
           SELECT id, ?, ? FROM messages
           WHERE room_id = ? AND sender_id != ? AND deleted_at IS NULL""",
        (user["id"], utc_now(), room_id, user["id"]),
    )
    db.commit()
    socketio.emit("message_update", {}, room=f"room:{room_id}")


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(
        app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=port,
        debug=os.environ.get("FLASK_DEBUG", "").lower() in {"1", "true"},
        allow_unsafe_werkzeug=True,
    )
