import io
import os
from datetime import datetime
from functools import wraps

import pandas as pd
import psycopg2
import pymysql
import pytz
from flask import Flask, jsonify, redirect, render_template, request, send_file, session, url_for
from openpyxl.styles import Font
from passlib.hash import bcrypt as passlib_bcrypt

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-me-now")

DB_SCHEMA_INDEX = os.getenv("DB_SCHEMA_INDEX", "m3data")
DB_TABLE_INDEX = os.getenv("DB_TABLE_INDEX", "aggregation_index")
DB_SCHEMA_DATA = os.getenv("DB_SCHEMA_DATA", "aggregatedData")

MYSQL_TABLE_USERS = os.getenv("MYSQL_TABLE_USERS", "users")

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Rome")
LOCAL_TZ = pytz.timezone(APP_TIMEZONE)
UTC_TZ = pytz.utc


def get_pg_connection():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "postgres"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", "postgres"),
    )


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


def local_input_to_utc_epoch(dt_str):
    naive_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")
    local_dt = LOCAL_TZ.localize(naive_dt)
    utc_dt = local_dt.astimezone(UTC_TZ)
    return int(utc_dt.timestamp())


def epoch_series_to_local_datetime(series):
    return pd.to_datetime(series, unit="s", utc=True).dt.tz_convert(APP_TIMEZONE).dt.tz_localize(None)


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


def get_bucket_sql(resolution):
    if resolution == "hour":
        return "extract(epoch from date_trunc('hour', timezone('Europe/Rome', to_timestamp(ts))))::bigint"
    elif resolution == "day":
        return "extract(epoch from date_trunc('day', timezone('Europe/Rome', to_timestamp(ts))))::bigint"
    elif resolution == "month":
        return "extract(epoch from date_trunc('month', timezone('Europe/Rome', to_timestamp(ts))))::bigint"
    else:
        return "ts"


def get_datetime_format(resolution):
    if resolution == "month":
        return "%m/%Y"
    elif resolution == "day":
        return "%d/%m/%Y"
    else:
        return "%d/%m/%Y %H:%M"


@app.route("/export", methods=["POST"])
@login_required
def export_excel():
    selected_rows = request.form.getlist("selected_rows")
    selected_rows = list(dict.fromkeys(selected_rows))

    date_from_str = request.form.get("date_from")
    date_to_str = request.form.get("date_to")
    resolution = request.form.get("resolution", "15m")

    if not selected_rows:
        return "Nessuna riga selezionata", 400

    if not date_from_str or not date_to_str:
        return "Data inizio e data fine obbligatorie", 400

    try:
        date_from_epoch = local_input_to_utc_epoch(date_from_str)
        date_to_epoch = local_input_to_utc_epoch(date_to_str)
    except ValueError:
        return "Formato data non valido", 400

    selected_pairs = []
    seen_pairs = set()

    for item in selected_rows:
        parts = item.split("|", 1)
        if len(parts) == 2:
            pair = (parts[0], parts[1])
            if pair not in seen_pairs:
                seen_pairs.add(pair)
                selected_pairs.append(pair)

    if not selected_pairs:
        return "Selezione non valida", 400

    conn = get_pg_connection()
    cur = conn.cursor()

    sql_meta = f'''
        SELECT target, resource, um, table_uuid, crescente
        FROM "{DB_SCHEMA_INDEX}"."{DB_TABLE_INDEX}"
        WHERE target = %s AND resource = %s
    '''

    metadata_rows = []
    seen_meta = set()

    for target, resource in selected_pairs:
        cur.execute(sql_meta, (target, resource))
        row = cur.fetchone()
        if row:
            meta_key = (row[0], row[1], row[3])  # target, resource, table_uuid
            if meta_key not in seen_meta:
                seen_meta.add(meta_key)
                metadata_rows.append(row)

    if not metadata_rows:
        cur.close()
        conn.close()
        return "Nessun dato trovato in aggregation_index", 404

    merged_df = None
    bucket_sql = get_bucket_sql(resolution)
    date_format = get_datetime_format(resolution)

    for meta in metadata_rows:
        target = meta[0]
        resource = meta[1]
        um = meta[2] if meta[2] is not None else ""
        table_uuid = meta[3]
        crescente = meta[4]

        if int(crescente) == 1:
            source_col = "delta"
            agg_sql = "SUM(delta)"
        else:
            source_col = "avg"
            agg_sql = "AVG(avg)"

        excel_col_name = "{}/{}({})".format(target, resource, um)

        if resolution == "15m":
            sql_data = f'''
                SELECT ts, {source_col} AS value
                FROM "{DB_SCHEMA_DATA}"."{table_uuid}"
                WHERE ts >= %s AND ts < %s
                ORDER BY ts
            '''
        else:
            sql_data = f'''
                SELECT
                    {bucket_sql} AS ts,
                    {agg_sql} AS value
                FROM "{DB_SCHEMA_DATA}"."{table_uuid}"
                WHERE ts >= %s AND ts < %s
                GROUP BY {bucket_sql}
                ORDER BY ts
            '''

        cur.execute(sql_data, (date_from_epoch, date_to_epoch))
        data_rows = cur.fetchall()

        if not data_rows:
            continue

        df = pd.DataFrame(data_rows, columns=["ts", excel_col_name])
        df["Data"] = epoch_series_to_local_datetime(df["ts"])
        df = df.drop(columns=["ts"])
        df = df[["Data", excel_col_name]]

        if merged_df is None:
            merged_df = df
        else:
            merged_df = pd.merge(merged_df, df, on="Data", how="outer")

    cur.close()
    conn.close()

    if merged_df is None or merged_df.empty:
        return "Nessun dato trovato nel periodo selezionato", 404

    merged_df = merged_df.sort_values("Data")

    numeric_cols = [col for col in merged_df.columns if col != "Data"]
    for col in numeric_cols:
        merged_df[col] = pd.to_numeric(merged_df[col], errors="coerce").round(2)

    merged_df["Data"] = merged_df["Data"].dt.strftime(date_format)

    output = io.BytesIO()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        merged_df.to_excel(writer, index=False, sheet_name="Export")
        worksheet = writer.sheets["Export"]
        bold_font = Font(bold=True)

        for cell in worksheet[1]:
            cell.font = bold_font

        for row in worksheet.iter_rows(min_row=2, min_col=2):
            for cell in row:
                if cell.value is not None and isinstance(cell.value, (int, float)):
                    cell.number_format = '0.00'

        for column_cells in worksheet.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter
            for cell in column_cells:
                value = "" if cell.value is None else str(cell.value)
                if len(value) > max_length:
                    max_length = len(value)
            worksheet.column_dimensions[column_letter].width = max_length + 2

    output.seek(0)

    filename = "export_{}.xlsx".format(datetime.now().strftime("%Y%m%d_%H%M%S"))

    return send_file(
        output,
        as_attachment=True,
        download_name=filename,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


if __name__ == "__main__":
    app.run(
        host=os.getenv("APP_HOST", "0.0.0.0"),
        port=int(os.getenv("APP_PORT", "8080")),
        debug=os.getenv("APP_DEBUG", "False").lower() == "true"
    )
