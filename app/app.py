import json
import os
import re
from datetime import datetime
from functools import wraps

import pymysql
from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from passlib.hash import bcrypt as passlib_bcrypt

from export_utils import build_excel_bytes, get_pg_connection

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-now")

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SELECTIONS_DIR = os.path.join(BASE_DIR, "storage", "selections")
SCHEDULES_DIR = os.path.join(BASE_DIR, "storage", "schedules")

DB_SCHEMA_INDEX = os.getenv("DB_SCHEMA_INDEX", "m3data")
DB_TABLE_INDEX = os.getenv("DB_TABLE_INDEX", "aggregation_index")

MYSQL_TABLE_USERS = os.getenv("MYSQL_TABLE_USERS", "users")


def ensure_storage_dirs():
    os.makedirs(SELECTIONS_DIR, exist_ok=True)
    os.makedirs(SCHEDULES_DIR, exist_ok=True)


def sanitize_filename(name):
    name = name.strip()
    name = re.sub(r'[\\/*?:"<>|]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        name = "selezione"
    return name


def get_user_selection_dir(user_id):
    ensure_storage_dirs()
    user_dir = os.path.join(SELECTIONS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


def get_user_schedule_dir(user_id):
    ensure_storage_dirs()
    user_dir = os.path.join(SCHEDULES_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    return user_dir


def get_user_selection_filepath(user_id, selection_name):
    safe_name = sanitize_filename(selection_name)
    return os.path.join(get_user_selection_dir(user_id), safe_name + ".json")


def get_user_schedule_filepath(user_id, schedule_name):
    safe_name = sanitize_filename(schedule_name)
    return os.path.join(get_user_schedule_dir(user_id), safe_name + ".json")


def load_user_selections(user_id):
    user_dir = get_user_selection_dir(user_id)
    result = {}

    try:
        for filename in os.listdir(user_dir):
            if not filename.lower().endswith(".json"):
                continue

            filepath = os.path.join(user_dir, filename)
            selection_name = filename[:-5]

            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, dict):
                    keys = data.get("keys", [])
                elif isinstance(data, list):
                    keys = data
                else:
                    keys = []

                if isinstance(keys, list):
                    result[selection_name] = keys
            except Exception:
                continue
    except Exception:
        return {}

    return result


def save_user_selection(user_id, selection_name, keys):
    filepath = get_user_selection_filepath(user_id, selection_name)
    payload = {
        "name": selection_name,
        "keys": keys
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def delete_user_selection(user_id, selection_name):
    filepath = get_user_selection_filepath(user_id, selection_name)
    if os.path.exists(filepath):
        os.remove(filepath)


def load_user_schedules(user_id):
    user_dir = get_user_schedule_dir(user_id)
    result = []

    try:
        for filename in os.listdir(user_dir):
            if not filename.lower().endswith(".json"):
                continue

            filepath = os.path.join(user_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    result.append(data)
            except Exception:
                continue
    except Exception:
        return []

    return sorted(result, key=lambda x: x.get("name", "").lower())


def save_user_schedule(user_id, schedule_data):
    schedule_name = schedule_data["name"]
    filepath = get_user_schedule_filepath(user_id, schedule_name)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(schedule_data, f, ensure_ascii=False, indent=2)


def delete_user_schedule(user_id, schedule_name):
    filepath = get_user_schedule_filepath(user_id, schedule_name)
    if os.path.exists(filepath):
        os.remove(filepath)


def get_mysql_connection():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "fits_core"),
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True
    )


def verify_user_credentials(username, password):
    conn = get_mysql_connection()
    try:
        with conn.cursor() as cur:
            sql = f"""
                SELECT id, username, name, email, password, deleted_at, is_web_visible
                FROM {MYSQL_TABLE_USERS}
                WHERE username = %s
                LIMIT 1
            """
            cur.execute(sql, (username,))
            user = cur.fetchone()

            if not user:
                return None

            if user.get("deleted_at") is not None:
                return None

            if int(user.get("is_web_visible", 1)) != 1:
                return None

            password_hash = user.get("password")
            if not password_hash:
                return None

            if passlib_bcrypt.verify(password, password_hash):
                return {
                    "id": user["id"],
                    "username": user["username"],
                    "name": user.get("name") or user["username"],
                    "email": user.get("email", "")
                }

            return None
    finally:
        conn.close()


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)
    return wrapped_view


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            error = "Inserire username e password."
        else:
            user = verify_user_credentials(username, password)
            if user:
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["name"] = user["name"]
                session["email"] = user["email"]
                return redirect(url_for("index"))
            else:
                error = "Credenziali non valide."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        logged_user_name=session.get("name", session.get("username", "Utente"))
    )


@app.route("/api/index_rows")
@login_required
def api_index_rows():
    conn = get_pg_connection()
    cur = conn.cursor()

    sql = f'''
        SELECT target, resource, um, table_uuid, crescente
        FROM "{DB_SCHEMA_INDEX}"."{DB_TABLE_INDEX}"
        ORDER BY target, resource
    '''
    cur.execute(sql)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    result = []
    for row in rows:
        result.append({
            "target": row[0],
            "resource": row[1],
            "um": row[2] if row[2] is not None else "",
            "table_uuid": row[3],
            "crescente": row[4]
        })

    return jsonify(result)


@app.route("/api/saved_selections", methods=["GET"])
@login_required
def get_saved_selections():
    user_id = session["user_id"]
    selections = load_user_selections(user_id)

    result = []
    for name in sorted(selections.keys()):
        keys = selections.get(name, [])
        if not isinstance(keys, list):
            keys = []
        result.append({
            "name": name,
            "keys": keys
        })

    return jsonify(result)


@app.route("/api/saved_selections", methods=["POST"])
@login_required
def save_selection():
    try:
        user_id = session["user_id"]
        payload = request.get_json(silent=True) or {}

        name = (payload.get("name") or "").strip()
        keys = payload.get("keys") or []

        if not name:
            return jsonify({"error": "Nome selezione obbligatorio"}), 400

        if not isinstance(keys, list) or not keys:
            return jsonify({"error": "Nessuna risorsa selezionata"}), 400

        cleaned_keys = []
        seen = set()

        for item in keys:
            if isinstance(item, str) and "|" in item and item not in seen:
                seen.add(item)
                cleaned_keys.append(item)

        if not cleaned_keys:
            return jsonify({"error": "Selezione non valida"}), 400

        save_user_selection(user_id, name, cleaned_keys)
        return jsonify({"ok": True})

    except Exception as e:
        app.logger.exception("Errore salvataggio selezione")
        return jsonify({"error": str(e)}), 500


@app.route("/api/saved_selections/<selection_name>", methods=["DELETE"])
@login_required
def delete_selection(selection_name):
    user_id = session["user_id"]

    try:
        delete_user_selection(user_id, selection_name)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("Errore eliminazione selezione")
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedules", methods=["GET"])
@login_required
def get_schedules():
    user_id = session["user_id"]
    return jsonify(load_user_schedules(user_id))


@app.route("/api/schedules", methods=["POST"])
@login_required
def save_schedule():
    try:
        user_id = session["user_id"]
        payload = request.get_json(silent=True) or {}

        name = (payload.get("name") or "").strip()
        email_to = (payload.get("email_to") or "").strip()
        period = (payload.get("period") or "").strip()
        resolution = (payload.get("resolution") or "").strip()
        selected_keys = payload.get("selected_keys") or []
        frequency = (payload.get("frequency") or "").strip()
        run_time = (payload.get("time") or "").strip()
        weekday = payload.get("weekday")
        day_of_month = payload.get("day_of_month")

        if not name:
            return jsonify({"error": "Nome schedulazione obbligatorio"}), 400
        if not email_to:
            return jsonify({"error": "Email destinatario obbligatoria"}), 400
        if not period:
            return jsonify({"error": "Periodo obbligatorio"}), 400
        if not resolution:
            return jsonify({"error": "Risoluzione obbligatoria"}), 400
        if not isinstance(selected_keys, list) or not selected_keys:
            return jsonify({"error": "Selezionare almeno una grandezza"}), 400
        if frequency not in ["daily", "weekly", "monthly"]:
            return jsonify({"error": "Frequenza non valida"}), 400
        if not run_time:
            return jsonify({"error": "Orario obbligatorio"}), 400

        cleaned_keys = []
        seen = set()
        for item in selected_keys:
            if isinstance(item, str) and "|" in item and item not in seen:
                seen.add(item)
                cleaned_keys.append(item)

        if not cleaned_keys:
            return jsonify({"error": "Selezione non valida"}), 400

        schedule_data = {
            "name": name,
            "email_to": email_to,
            "period": period,
            "resolution": resolution,
            "selected_keys": cleaned_keys,
            "frequency": frequency,
            "time": run_time,
            "weekday": weekday,
            "day_of_month": day_of_month,
            "enabled": True,
            "last_run": None,
            "updated_at": datetime.now().isoformat()
        }

        save_user_schedule(user_id, schedule_data)
        return jsonify({"ok": True})

    except Exception as e:
        app.logger.exception("Errore salvataggio schedulazione")
        return jsonify({"error": str(e)}), 500


@app.route("/api/schedules/<schedule_name>", methods=["DELETE"])
@login_required
def delete_schedule(schedule_name):
    user_id = session["user_id"]
    try:
        delete_user_schedule(user_id, schedule_name)
        return jsonify({"ok": True})
    except Exception as e:
        app.logger.exception("Errore eliminazione schedulazione")
        return jsonify({"error": str(e)}), 500


@app.route("/export", methods=["POST"])
@login_required
def export_excel():
    selected_rows = request.form.getlist("selected_rows")
    date_from_str = request.form.get("date_from")
    date_to_str = request.form.get("date_to")
    resolution = request.form.get("resolution", "15m")

    try:
        output = build_excel_bytes(selected_rows, date_from_str, date_to_str, resolution)
    except Exception as e:
        return str(e), 400

    filename = "export_{}.xlsx".format(datetime.now().strftime("%Y%m%d_%H%M%S"))

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    ensure_storage_dirs()
    app.run(
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8080")),
        debug=os.getenv("APP_DEBUG", "False").lower() == "true"
    )
