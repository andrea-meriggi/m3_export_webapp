import io
import os
from datetime import datetime

import psycopg2
import pytz
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Font


DB_SCHEMA_INDEX = os.getenv("DB_SCHEMA_INDEX", "m3data")
DB_TABLE_INDEX = os.getenv("DB_TABLE_INDEX", "aggregation_index")
DB_SCHEMA_DATA = os.getenv("DB_SCHEMA_DATA", "aggregatedData")

APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Europe/Rome")
LOCAL_TZ = pytz.timezone(APP_TIMEZONE)
UTC_TZ = pytz.utc

MAX_15M_DAYS = int(os.getenv("MAX_15M_DAYS", "90"))


def get_pg_connection():
    return psycopg2.connect(
        host=os.getenv("PGHOST", "127.0.0.1"),
        port=os.getenv("PGPORT", "5432"),
        dbname=os.getenv("PGDATABASE", "postgres"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", "postgres"),
    )


def local_input_to_utc_epoch(dt_str):
    naive_dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M")
    local_dt = LOCAL_TZ.localize(naive_dt)
    utc_dt = local_dt.astimezone(UTC_TZ)
    return int(utc_dt.timestamp())


def epoch_to_local_string(ts_epoch, resolution):
    dt = datetime.utcfromtimestamp(ts_epoch).replace(tzinfo=UTC_TZ).astimezone(LOCAL_TZ)

    if resolution == "month":
        return dt.strftime("%m/%Y")
    elif resolution == "day":
        return dt.strftime("%d/%m/%Y")
    else:
        return dt.strftime("%d/%m/%Y %H:%M")


def get_bucket_sql(resolution):
    if resolution == "hour":
        return "extract(epoch from date_trunc('hour', timezone('Europe/Rome', to_timestamp(ts))))::bigint"
    elif resolution == "day":
        return "extract(epoch from date_trunc('day', timezone('Europe/Rome', to_timestamp(ts))))::bigint"
    elif resolution == "month":
        return "extract(epoch from date_trunc('month', timezone('Europe/Rome', to_timestamp(ts))))::bigint"
    else:
        return "ts"


def validate_export_size(date_from_str, date_to_str, resolution):
    dt_from = datetime.strptime(date_from_str, "%Y-%m-%dT%H:%M")
    dt_to = datetime.strptime(date_to_str, "%Y-%m-%dT%H:%M")
    delta_days = (dt_to - dt_from).total_seconds() / 86400.0

    if resolution == "15m" and delta_days > MAX_15M_DAYS:
        raise ValueError(
            "Con aggregazione a 15 minuti non è possibile esportare più di {} giorni. "
            "Riduci il periodo oppure usa aggregazione oraria, giornaliera o mensile.".format(MAX_15M_DAYS)
        )


def build_excel_bytes(selected_rows, date_from_str, date_to_str, resolution="15m"):
    selected_rows = list(dict.fromkeys(selected_rows))

    if not selected_rows:
        raise ValueError("Nessuna riga selezionata")

    validate_export_size(date_from_str, date_to_str, resolution)

    date_from_epoch = local_input_to_utc_epoch(date_from_str)
    date_to_epoch = local_input_to_utc_epoch(date_to_str)

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
        raise ValueError("Selezione non valida")

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
            meta_key = (row[0], row[1], row[3])
            if meta_key not in seen_meta:
                seen_meta.add(meta_key)
                metadata_rows.append(row)

    if not metadata_rows:
        cur.close()
        conn.close()
        raise ValueError("Nessun dato trovato in aggregation_index")

    bucket_sql = get_bucket_sql(resolution)

    # columns_data = [
    #   {"name": "T/R(um)", "values": {ts: value, ...}},
    #   ...
    # ]
    columns_data = []
    all_timestamps = set()

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
        rows = cur.fetchall()

        series_map = {}
        for ts, value in rows:
            series_map[int(ts)] = None if value is None else round(float(value), 3)

        if series_map:
            all_timestamps.update(series_map.keys())
            columns_data.append({
                "name": excel_col_name,
                "values": series_map
            })

    cur.close()
    conn.close()

    if not columns_data or not all_timestamps:
        raise ValueError("Nessun dato trovato nel periodo selezionato")

    sorted_timestamps = sorted(all_timestamps)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Export")

    # header bold
    header = []
    first_cell = WriteOnlyCell(ws, value="Data")
    first_cell.font = Font(bold=True)
    header.append(first_cell)

    for col in columns_data:
        c = WriteOnlyCell(ws, value=col["name"])
        c.font = Font(bold=True)
        header.append(c)

    ws.append(header)

    for ts in sorted_timestamps:
        row = [epoch_to_local_string(ts, resolution)]
        for col in columns_data:
            row.append(col["values"].get(ts))
        ws.append(row)

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)
    return output
