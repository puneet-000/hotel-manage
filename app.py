from flask import Flask, render_template, request, jsonify, redirect, session, url_for, abort
from werkzeug.utils import secure_filename
from werkzeug.security import check_password_hash, generate_password_hash
import json
import os
import sqlite3
import time as pytime

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "punit-dev-secret-key")
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

OWNER_LOGIN_ID = os.environ.get("OWNER_LOGIN_ID", "ADMIN")
OWNER_LOGIN_PASSWORD = os.environ.get("OWNER_LOGIN_PASSWORD", "ADMIN123")

STATIC_UPLOAD_SUBDIR = os.path.join("uploads", "menu")
ALLOWED_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
DB_PATH = os.environ.get("DB_PATH") or os.path.join(app.root_path, "database.db")


def is_allowed_image(filename: str) -> bool:
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in ALLOWED_IMAGE_EXTS


def ensure_upload_dir() -> str:
    upload_dir = os.path.join(app.root_path, "static", STATIC_UPLOAD_SUBDIR)
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir

def ensure_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS menu (id INTEGER PRIMARY KEY AUTOINCREMENT, food TEXT, price INTEGER, food_type TEXT DEFAULT 'veg', image_path TEXT);
        CREATE TABLE IF NOT EXISTS tables (id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY AUTOINCREMENT, table_id INTEGER, food TEXT, status TEXT, time TEXT, accepted_at INTEGER, ready_at INTEGER, paid_at INTEGER);
        CREATE TABLE IF NOT EXISTS bill_history (id INTEGER PRIMARY KEY AUTOINCREMENT, table_id INTEGER NOT NULL, items TEXT NOT NULL, total REAL NOT NULL, created_at INTEGER NOT NULL, items_json TEXT);
        CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT);
        """
    )
    changed = False
    cols = conn.execute("PRAGMA table_info(menu)").fetchall()
    col_names = [col[1] for col in cols]
    if "food_type" not in col_names:
        conn.execute("ALTER TABLE menu ADD COLUMN food_type TEXT DEFAULT 'veg'")
        changed = True
    if "image_path" not in col_names:
        conn.execute("ALTER TABLE menu ADD COLUMN image_path TEXT")
        changed = True

    try:
        cols = conn.execute("PRAGMA table_info(orders)").fetchall()
        col_names = [col[1] for col in cols]
        if "accepted_at" not in col_names:
            conn.execute("ALTER TABLE orders ADD COLUMN accepted_at INTEGER")
            changed = True
        if "ready_at" not in col_names:
            conn.execute("ALTER TABLE orders ADD COLUMN ready_at INTEGER")
            changed = True
        if "paid_at" not in col_names:
            conn.execute("ALTER TABLE orders ADD COLUMN paid_at INTEGER")
            changed = True
    except sqlite3.OperationalError:
        # orders table may not exist during initial bootstrap
        pass

    try:
        cols = conn.execute("PRAGMA table_info(bill_history)").fetchall()
        col_names = [col[1] for col in cols]
        if "items_json" not in col_names:
            conn.execute("ALTER TABLE bill_history ADD COLUMN items_json TEXT")
            changed = True
    except sqlite3.OperationalError:
        pass
    conn.commit()


def get_setting(db, key: str):
    row = db.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(db, key: str, value: str):
    db.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )

def reset_table_sequence_if_empty(conn):
    row = conn.execute("SELECT COUNT(*) AS cnt FROM tables").fetchone()
    if row and row["cnt"] == 0:
        conn.execute("DELETE FROM sqlite_sequence WHERE name='tables'")


def table_exists(db, table_id) -> bool:
    try:
        table_num = int(table_id)
    except (TypeError, ValueError):
        return False
    row = db.execute("SELECT 1 FROM tables WHERE id=? LIMIT 1", (table_num,)).fetchone()
    return row is not None


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def promote_ready_after_eta(db, now_ts):
    db.execute(
        """
        UPDATE orders
        SET status='ready', ready_at=?
        WHERE status='accepted'
          AND accepted_at IS NOT NULL
          AND CAST(time AS INTEGER) > 0
          AND accepted_at + (CAST(time AS INTEGER) * 60) <= ?
        """,
        (now_ts, now_ts),
    )


@app.route("/")
def home():
    # Cache-bust front-page images so replacing a file shows up immediately.
    return render_template("index.html", cache_bust=int(pytime.time()))


def owner_is_logged_in() -> bool:
    return bool(session.get("owner_logged_in"))


def chef_is_logged_in() -> bool:
    return bool(session.get("chef_logged_in"))


def billing_is_logged_in() -> bool:
    return bool(session.get("billing_logged_in"))


def mark_role_logged_in(role_key: str) -> None:
    session.permanent = False
    session.pop("owner_logged_in", None)
    session.pop("chef_logged_in", None)
    session.pop("billing_logged_in", None)
    session[role_key] = True


@app.route("/owner_login", methods=["GET", "POST"])
def owner_login():
    next_url = (request.args.get("next") or "").strip()
    if request.method == "POST":
        next_url = (request.form.get("next") or next_url).strip()
        login_id = (request.form.get("login_id") or "").strip()
        password = (request.form.get("password") or "").strip()

        if login_id == OWNER_LOGIN_ID and password == OWNER_LOGIN_PASSWORD:
            mark_role_logged_in("owner_logged_in")
            return redirect(next_url or url_for("owner"))

        return render_template(
            "owner_login.html",
            error="Invalid ID or password",
            next_url=next_url,
        )

    if owner_is_logged_in():
        return redirect(url_for("owner"))

    return render_template("owner_login.html", error=None, next_url=next_url)


@app.route("/owner_logout", methods=["GET"])
def owner_logout():
    session.pop("owner_logged_in", None)
    return redirect(url_for("owner_login"))


# OWNER PAGE
@app.route("/owner", methods=["GET","POST"])
def owner():
    if not owner_is_logged_in():
        return redirect(url_for("owner_login", next=request.path))

    db = get_db()

    if request.method == "POST":

        if "food" in request.form and "price" in request.form:
            food = request.form["food"].strip()
            price = request.form["price"].strip()
            food_type = request.form.get("food_type", "veg").strip().lower()
            if food_type not in ("veg", "non-veg"):
                food_type = "veg"

            if food and price:
                image_path = None
                f = request.files.get("food_image")
                if f and f.filename:
                    if is_allowed_image(f.filename):
                        upload_dir = ensure_upload_dir()
                        safe_name = secure_filename(f.filename)
                        unique_name = f"{int(pytime.time())}_{safe_name}"
                        full_path = os.path.join(upload_dir, unique_name)
                        f.save(full_path)
                        image_path = os.path.join(STATIC_UPLOAD_SUBDIR, unique_name).replace("\\", "/")
                db.execute(
                    "INSERT INTO menu (food,price,food_type,image_path) VALUES (?,?,?,?)",
                    (food, price, food_type, image_path),
                )
                db.commit()

        if "food_remove" in request.form:
            food_id = request.form["food_remove"].strip()
            if food_id:
                row = db.execute("SELECT image_path FROM menu WHERE id=?", (food_id,)).fetchone()
                if row and row["image_path"]:
                    candidate = os.path.normpath(os.path.join(app.root_path, "static", row["image_path"]))
                    static_root = os.path.normpath(os.path.join(app.root_path, "static"))
                    if candidate.startswith(static_root) and os.path.isfile(candidate):
                        try:
                            os.remove(candidate)
                        except OSError:
                            pass
                db.execute("DELETE FROM menu WHERE id=?",(food_id,))
                db.commit()

        if "table_add" in request.form:
            db.execute("INSERT INTO tables DEFAULT VALUES")
            db.commit()

        if "table_remove" in request.form:
            table_id = request.form["table_remove"]
            db.execute("DELETE FROM tables WHERE id=?",(table_id,))
            db.execute("DELETE FROM orders WHERE table_id=?",(table_id,))
            reset_table_sequence_if_empty(db)
            db.commit()

        if request.form.get("staff_pw_save"):
            chef_pw = (request.form.get("chef_password") or "").strip()
            billing_pw = (request.form.get("billing_password") or "").strip()

            if chef_pw:
                set_setting(db, "chef_password_hash", generate_password_hash(chef_pw))
            if billing_pw:
                set_setting(db, "billing_password_hash", generate_password_hash(billing_pw))
            db.commit()

    menu = db.execute(
        """
        SELECT *
        FROM menu
        ORDER BY
          CASE LOWER(COALESCE(food_type, ''))
            WHEN 'veg' THEN 0
            WHEN 'non-veg' THEN 1
            WHEN 'non veg' THEN 1
            ELSE 2
          END,
          id ASC
        """
    ).fetchall()
    tables = db.execute("SELECT * FROM tables ORDER BY id ASC").fetchall()

    chef_pw_set = bool(get_setting(db, "chef_password_hash"))
    billing_pw_set = bool(get_setting(db, "billing_password_hash"))

    return render_template(
        "owner.html",
        menu=menu,
        tables=tables,
        chef_pw_set=chef_pw_set,
        billing_pw_set=billing_pw_set,
    )


# CUSTOMER PAGE
@app.route("/customer/table<int:table_id>", methods=["GET"])
def customer_table_landing(table_id):
    db = get_db()
    if not table_exists(db, table_id):
        abort(404)

    return render_template(
        "customer_table_landing.html",
        table_id=table_id,
        cache_bust=int(pytime.time()),
    )


@app.route("/customer", methods=["GET", "POST"])
@app.route("/customer/table<int:table_id>/order", methods=["GET", "POST"])
def customer(table_id=None):
    db = get_db()
    selected_table_id = table_id

    if selected_table_id is None:
        table_raw = (request.args.get("table") or "").strip()
        if table_raw:
            try:
                selected_table_id = int(table_raw)
            except (TypeError, ValueError):
                selected_table_id = None

    selected_table_valid = selected_table_id is not None and table_exists(db, selected_table_id)

    if request.method == "POST":
        table = request.form.get("table", "").strip()
        foods = request.form.getlist("foods")
        qtys = request.form.getlist("qtys")
        added = False

        if not table_exists(db, table):
            table = ""

        if table and foods and qtys:
            for food, qty_raw in zip(foods, qtys):
                food_name = (food or "").strip()
                try:
                    qty = int(qty_raw)
                except (TypeError, ValueError):
                    qty = 0

                if food_name and qty > 0:
                    for _ in range(qty):
                        db.execute(
                            "INSERT INTO orders (table_id,food,status) VALUES (?,?,?)",
                            (table,food_name,"waiting")
                        )
                    added = True
        else:
            # Backward compatibility for older single-item customer form.
            food = request.form.get("food", "").strip()
            if table and food:
                db.execute(
                    "INSERT INTO orders (table_id,food,status) VALUES (?,?,?)",
                    (table,food,"waiting")
                )
                added = True

        if added:
            db.commit()

    menu = db.execute(
        """
        SELECT *
        FROM menu
        ORDER BY
          CASE LOWER(COALESCE(food_type, ''))
            WHEN 'veg' THEN 0
            WHEN 'non-veg' THEN 1
            WHEN 'non veg' THEN 1
            ELSE 2
          END,
          id ASC
        """
    ).fetchall()
    tables = db.execute("SELECT * FROM tables ORDER BY id ASC").fetchall()

    return render_template(
        "customer.html",
        menu=menu,
        tables=tables,
        cache_bust=int(pytime.time()),
        preset_table_id=selected_table_id if selected_table_valid else None,
        table_link_mode=selected_table_valid,
    )


@app.route("/customer_orders", methods=["GET"])
def customer_orders():
    db = get_db()
    now_ts = int(pytime.time())

    promote_ready_after_eta(db, now_ts)
    db.commit()

    table_raw = (request.args.get("table") or "").strip()
    try:
        table_id = int(table_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid table"}), 400

    groups_raw = db.execute(
        """
        SELECT
          food,
          status,
          COUNT(*) AS qty,
          MIN(accepted_at) AS accepted_at,
          MAX(CAST(time AS INTEGER)) AS eta_minutes,
          MAX(ready_at) AS ready_at
        FROM orders
        WHERE table_id=?
          AND status IN ('waiting','accepted','ready','rejected','canceled')
        GROUP BY food, status
        ORDER BY
          CASE status
            WHEN 'waiting' THEN 0
            WHEN 'accepted' THEN 1
            WHEN 'ready' THEN 2
            WHEN 'rejected' THEN 3
            WHEN 'canceled' THEN 4
            ELSE 9
          END,
          food ASC
        """,
        (table_id,),
    ).fetchall()

    groups = []
    for row in groups_raw:
        d = dict(row)
        if d.get("status") == "accepted":
            accepted_at = d.get("accepted_at")
            eta_minutes = d.get("eta_minutes") or 0
            try:
                eta_minutes = int(eta_minutes)
            except (TypeError, ValueError):
                eta_minutes = 0

            if accepted_at and eta_minutes > 0:
                remaining = (int(accepted_at) + (eta_minutes * 60)) - now_ts
                d["remaining_seconds"] = max(0, int(remaining))
        groups.append(d)

    return jsonify({"ok": True, "table_id": table_id, "server_ts": now_ts, "groups": groups})


@app.route("/customer_cancel", methods=["POST"])
def customer_cancel():
    db = get_db()
    now_ts = int(pytime.time())

    payload = request.get_json(silent=True) or {}
    table_raw = str(payload.get("table_id") or "").strip()
    food = (payload.get("food") or "").strip()
    status = (payload.get("status") or "").strip().lower()

    try:
        table_id = int(table_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid table"}), 400

    allowed_statuses = {"waiting", "accepted"}
    if status and status not in allowed_statuses:
        return jsonify({"ok": False, "error": "Invalid status"}), 400

    if food:
        if status:
            db.execute(
                """
                UPDATE orders
                SET status='canceled'
                WHERE table_id=? AND food=? AND status=?
                """,
                (table_id, food, status),
            )
        else:
            db.execute(
                """
                UPDATE orders
                SET status='canceled'
                WHERE table_id=? AND food=? AND status IN ('waiting','accepted')
                """,
                (table_id, food),
            )
    else:
        # Cancel all pending items for the table.
        if status:
            db.execute(
                """
                UPDATE orders
                SET status='canceled'
                WHERE table_id=? AND status=?
                """,
                (table_id, status),
            )
        else:
            db.execute(
                """
                UPDATE orders
                SET status='canceled'
                WHERE table_id=? AND status IN ('waiting','accepted')
                """,
                (table_id,),
            )

    promote_ready_after_eta(db, now_ts)
    db.commit()
    return jsonify({"ok": True})


@app.route("/customer_status", methods=["GET"])
def customer_status():
    db = get_db()
    now_ts = int(pytime.time())

    promote_ready_after_eta(db, now_ts)
    db.commit()

    table_raw = (request.args.get("table") or "").strip()
    try:
        table_id = int(table_raw)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Invalid table"}), 400

    rows = db.execute(
        """
        SELECT time, accepted_at
        FROM orders
        WHERE table_id=?
          AND status='accepted'
        ORDER BY id ASC
        """,
        (table_id,),
    ).fetchall()

    eta_max = None
    remaining_max = None
    accepted_count = 0

    for r in rows:
        accepted_count += 1
        try:
            eta_minutes = int((r["time"] or "0").strip())
        except (TypeError, ValueError):
            eta_minutes = 0

        if eta_minutes > 0:
            if eta_max is None or eta_minutes > eta_max:
                eta_max = eta_minutes

        accepted_at = r["accepted_at"]
        if accepted_at and eta_minutes > 0:
            remaining_seconds = (accepted_at + (eta_minutes * 60)) - now_ts
            remaining_minutes = max(0, int((remaining_seconds + 59) // 60))
        else:
            # Fallback for legacy rows: show the ETA as "remaining".
            remaining_minutes = max(0, eta_minutes)

        if remaining_max is None or remaining_minutes > remaining_max:
            remaining_max = remaining_minutes

    return jsonify(
        {
            "ok": True,
            "table_id": table_id,
            "accepted_count": accepted_count,
            "eta_max": eta_max,
            "remaining_max": remaining_max,
        }
    )


# CHEF PAGE
@app.route("/chef", methods=["GET","POST"])
def chef():
    if not chef_is_logged_in():
        return redirect(url_for("chef_login", next=request.path))

    db = get_db()
    now_ts = int(pytime.time())

    if request.method == "POST":
        table_raw = request.form.get("group_table", "").strip()
        food = request.form.get("group_food", "").strip()
        try:
            table_id = int(table_raw)
        except (TypeError, ValueError):
            table_id = None

        if table_id is not None and food:
            # mark ready button was pressed (for entire group)
            if "ready" in request.form:
                db.execute(
                    """
                    UPDATE orders
                    SET status='ready', ready_at=?
                    WHERE table_id=? AND food=? AND status='accepted'
                    """,
                    (now_ts, table_id, food),
                )
                db.commit()
            # reject button was pressed (for entire group)
            elif "reject" in request.form:
                db.execute(
                    """
                    UPDATE orders
                    SET status='rejected'
                    WHERE table_id=? AND food=? AND status='waiting'
                    """,
                    (table_id, food),
                )
                db.commit()
            else:
                # accept action (for entire group)
                eta_raw = request.form.get("time", "").strip()
                try:
                    eta_minutes = int(eta_raw)
                except (TypeError, ValueError):
                    eta_minutes = 0

                if eta_minutes > 0:
                    db.execute(
                        """
                        UPDATE orders
                        SET status='accepted', time=?, accepted_at=?
                        WHERE table_id=? AND food=? AND status='waiting'
                        """,
                        (str(eta_minutes), now_ts, table_id, food),
                    )
                    db.commit()

    # Auto-move accepted items to billing after ETA.
    promote_ready_after_eta(db, now_ts)
    db.commit()

    orders_raw = db.execute(
        """
        SELECT
          table_id,
          food,
          status,
          COUNT(*) AS qty,
          MIN(accepted_at) AS accepted_at,
          MAX(CAST(time AS INTEGER)) AS time
        FROM orders
        WHERE status IN ('waiting','accepted')
        GROUP BY table_id, food, status
        ORDER BY
          CASE status
            WHEN 'waiting' THEN 0
            WHEN 'accepted' THEN 1
            ELSE 2
          END,
          table_id ASC,
          food ASC
        """
    ).fetchall()

    orders = []
    for row in orders_raw:
        d = dict(row)
        if d.get("status") == "accepted" and d.get("accepted_at") is not None:
            try:
                eta_minutes = int(d.get("time") or 0)
            except (TypeError, ValueError):
                eta_minutes = 0
            if eta_minutes > 0:
                remaining = (d["accepted_at"] + (eta_minutes * 60)) - now_ts
                d["remaining_seconds"] = max(0, int(remaining))
        orders.append(d)

    return render_template("chef.html",orders=orders)


@app.route("/chef_login", methods=["GET", "POST"])
def chef_login():
    next_url = (request.args.get("next") or "").strip()
    db = get_db()

    pw_hash = get_setting(db, "chef_password_hash")
    if not pw_hash:
        return render_template(
            "staff_login.html",
            role="Chef",
            error="Chef password not set. Please ask owner to set it.",
            next_url=next_url,
            action_url=url_for("chef_login"),
        )

    if request.method == "POST":
        next_url = (request.form.get("next") or next_url).strip()
        password = (request.form.get("password") or "").strip()

        if check_password_hash(pw_hash, password):
            mark_role_logged_in("chef_logged_in")
            return redirect(next_url or url_for("chef"))

        return render_template(
            "staff_login.html",
            role="Chef",
            error="Invalid password",
            next_url=next_url,
            action_url=url_for("chef_login"),
        )

    if chef_is_logged_in():
        return redirect(url_for("chef"))

    return render_template(
        "staff_login.html",
        role="Chef",
        error=None,
        next_url=next_url,
        action_url=url_for("chef_login"),
    )


@app.route("/chef_logout", methods=["GET"])
def chef_logout():
    session.pop("chef_logged_in", None)
    return redirect(url_for("chef_login"))


# BILLING PAGE
@app.route("/billing", methods=["GET", "POST"])
def billing():
    if not billing_is_logged_in():
        return redirect(url_for("billing_login", next=request.path))

    db = get_db()
    now_ts = int(pytime.time())

    promote_ready_after_eta(db, now_ts)
    db.commit()

    if request.method == "POST":
        table_raw = request.form.get("table_id", "").strip()
        try:
            table_id = int(table_raw)
        except (TypeError, ValueError):
            table_id = None

        if table_id is not None and request.form.get("bill_accept"):
            rows = db.execute(
                """
                SELECT o.food AS food,
                       COUNT(*) AS qty,
                       COALESCE(m.price, 0) AS price
                FROM orders o
                LEFT JOIN menu m ON o.food = m.food
                WHERE o.table_id=? AND o.status='ready'
                GROUP BY o.food
                ORDER BY o.food ASC
                """,
                (table_id,),
            ).fetchall()

            if rows:
                parts = []
                total = 0
                details = []
                for r in rows:
                    qty = int(r["qty"] or 0)
                    price = float(r["price"] or 0)
                    parts.append(f"{qty}* {r['food']}")
                    line_total = qty * price
                    total += line_total
                    details.append(
                        {
                            "food": r["food"],
                            "qty": qty,
                            "price": price,
                            "line_total": line_total,
                        }
                    )

                db.execute(
                    "INSERT INTO bill_history (table_id, items, total, created_at, items_json) VALUES (?,?,?,?,?)",
                    (table_id, ", ".join(parts), total, now_ts, json.dumps(details, ensure_ascii=False)),
                )
                db.execute(
                    """
                    UPDATE orders
                    SET status='paid', paid_at=?
                    WHERE table_id=? AND status='ready'
                    """,
                    (now_ts, table_id),
                )
                db.commit()

    table_rows = db.execute(
        """
        SELECT DISTINCT table_id
        FROM orders
        WHERE status='ready'
        ORDER BY table_id ASC
        """
    ).fetchall()

    bills = []
    for tr in table_rows:
        table_id = tr["table_id"]
        items = db.execute(
            """
            SELECT o.food AS food,
                   COUNT(*) AS qty,
                   COALESCE(m.price, 0) AS price
            FROM orders o
            LEFT JOIN menu m ON o.food = m.food
            WHERE o.table_id=? AND o.status='ready'
            GROUP BY o.food
            ORDER BY o.food ASC
            """,
            (table_id,),
        ).fetchall()

        total = 0
        item_list = []
        for r in items:
            qty = int(r["qty"] or 0)
            price = float(r["price"] or 0)
            line_total = qty * price
            total += line_total
            item_list.append(
                {"food": r["food"], "qty": qty, "price": price, "line_total": line_total}
            )

        bills.append({"table_id": table_id, "items": item_list, "total": total})

    return render_template("billing.html", bills=bills)


def _get_ready_bill_for_table(db, table_id: int):
    rows = db.execute(
        """
        SELECT o.food AS food,
               COUNT(*) AS qty,
               COALESCE(m.price, 0) AS price
        FROM orders o
        LEFT JOIN menu m ON o.food = m.food
        WHERE o.table_id=? AND o.status='ready'
        GROUP BY o.food
        ORDER BY o.food ASC
        """,
        (table_id,),
    ).fetchall()

    items = []
    total = 0.0
    for r in rows:
        qty = int(r["qty"] or 0)
        price = float(r["price"] or 0)
        line_total = qty * price
        total += line_total
        items.append(
            {
                "food": r["food"],
                "qty": qty,
                "price": price,
                "line_total": line_total,
            }
        )
    return items, total


@app.route("/billing_print")
def billing_print():
    if not billing_is_logged_in():
        return redirect(url_for("billing_login", next=request.path))

    db = get_db()
    now_ts = int(pytime.time())

    promote_ready_after_eta(db, now_ts)
    db.commit()

    table_raw = (request.args.get("table_id") or "").strip()
    try:
        table_id = int(table_raw)
    except (TypeError, ValueError):
        return render_template("billing_print.html", ok=False, error="Invalid table"), 400

    items, total = _get_ready_bill_for_table(db, table_id)
    if not items:
        return render_template(
            "billing_print.html", ok=False, error="No ready bill for this table."
        ), 404

    return render_template(
        "billing_print.html",
        ok=True,
        table_id=table_id,
        items=items,
        total=total,
        printed_at=now_ts,
    )


@app.route("/billing_history")
def billing_history():
    if not billing_is_logged_in():
        return redirect(url_for("billing_login", next=request.path))

    db = get_db()

    now_ts = int(pytime.time())
    period = (request.args.get("period") or "day").strip().lower()
    if period not in ("day", "week", "year"):
        period = "day"

    if period == "day":
        cutoff = now_ts - (24 * 60 * 60)
    elif period == "week":
        cutoff = now_ts - (7 * 24 * 60 * 60)
    else:
        cutoff = now_ts - (365 * 24 * 60 * 60)

    rows = db.execute(
        """
        SELECT id, table_id, items, total, created_at
        FROM bill_history
        WHERE created_at >= ?
        ORDER BY id DESC
        """,
        (cutoff,),
    ).fetchall()
    grand_total = sum(float(r["total"] or 0) for r in rows)
    return render_template(
        "billing_history.html", rows=rows, grand_total=grand_total, period=period
    )


def _parse_legacy_items(items_text: str):
    parts = [p.strip() for p in (items_text or "").split(",") if p.strip()]
    out = []
    for p in parts:
        # expected "2* Food Name"
        qty = 1
        food = p
        if "*" in p:
            left, right = p.split("*", 1)
            try:
                qty = int(left.strip())
            except (TypeError, ValueError):
                qty = 1
            food = right.strip()
        if food:
            out.append({"food": food, "qty": qty})
    return out


@app.route("/bill/<int:bill_id>")
def bill_detail(bill_id: int):
    if not billing_is_logged_in():
        return redirect(url_for("billing_login", next=request.path))

    db = get_db()
    row = db.execute(
        """
        SELECT id, table_id, items, total, created_at, items_json
        FROM bill_history
        WHERE id=?
        """,
        (bill_id,),
    ).fetchone()

    if not row:
        return render_template("bill_detail.html", ok=False, error="Bill not found"), 404

    items = []
    used_legacy = False

    if row["items_json"]:
        try:
            parsed = json.loads(row["items_json"])
            if isinstance(parsed, list):
                items = parsed
        except Exception:
            items = []

    if not items:
        used_legacy = True
        legacy = _parse_legacy_items(row["items"] or "")
        # try to enrich with current menu prices (best-effort)
        for it in legacy:
            food = it.get("food")
            qty = int(it.get("qty") or 0)
            price_row = db.execute(
                "SELECT COALESCE(price, 0) AS price FROM menu WHERE food=? LIMIT 1", (food,)
            ).fetchone()
            price = float(price_row["price"] or 0) if price_row else 0.0
            items.append(
                {
                    "food": food,
                    "qty": qty,
                    "price": price,
                    "line_total": qty * price,
                }
            )

    return render_template(
        "bill_detail.html",
        ok=True,
        bill=dict(row),
        items=items,
        used_legacy=used_legacy,
    )


@app.route("/billing_login", methods=["GET", "POST"])
def billing_login():
    next_url = (request.args.get("next") or "").strip()
    db = get_db()

    pw_hash = get_setting(db, "billing_password_hash")
    if not pw_hash:
        return render_template(
            "staff_login.html",
            role="Billing",
            error="Billing password not set. Please ask owner to set it.",
            next_url=next_url,
            action_url=url_for("billing_login"),
        )

    if request.method == "POST":
        next_url = (request.form.get("next") or next_url).strip()
        password = (request.form.get("password") or "").strip()

        if check_password_hash(pw_hash, password):
            mark_role_logged_in("billing_logged_in")
            return redirect(next_url or url_for("billing"))

        return render_template(
            "staff_login.html",
            role="Billing",
            error="Invalid password",
            next_url=next_url,
            action_url=url_for("billing_login"),
        )

    if billing_is_logged_in():
        return redirect(url_for("billing"))

    return render_template(
        "staff_login.html",
        role="Billing",
        error=None,
        next_url=next_url,
        action_url=url_for("billing_login"),
    )


@app.route("/billing_logout", methods=["GET"])
def billing_logout():
    session.pop("billing_logged_in", None)
    return redirect(url_for("billing_login"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    debug = os.environ.get("FLASK_DEBUG", "").strip() in ("1", "true", "True")
    app.run(host="0.0.0.0", port=port, debug=debug)
