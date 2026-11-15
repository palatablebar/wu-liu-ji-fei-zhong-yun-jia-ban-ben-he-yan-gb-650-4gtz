"""SQLite 存取：运行清单、运价版本、运单、隔离区、计费重行、账单行。

只放数据访问，不放业务规则。每次调用开一个连接，用完即关。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  input_name TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '',
  rules_version TEXT NOT NULL DEFAULT '',
  profile TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT 'preview',
  rows_in INTEGER NOT NULL DEFAULT 0,
  rows_ok INTEGER NOT NULL DEFAULT 0,
  rows_quarantined INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00',
  rate_digest TEXT NOT NULL DEFAULT '',
  stages TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rate_sets (
  carrier TEXT PRIMARY KEY,
  rate_set_version INTEGER NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rate_cards (
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  version_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  first_weight_kg TEXT NOT NULL,
  first_price TEXT NOT NULL,
  step_weight_kg TEXT NOT NULL,
  step_price TEXT NOT NULL,
  min_charge TEXT NOT NULL,
  volume_divisor INTEGER NOT NULL,
  fuel_pct TEXT NOT NULL,
  remote_fee TEXT NOT NULL,
  insurance_pct TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate_set_version INTEGER NOT NULL,
  PRIMARY KEY (carrier, service, zone, version_id)
);
CREATE TABLE IF NOT EXISTS waybills (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL,
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  pickup_utc TEXT NOT NULL,
  weight_kg TEXT NOT NULL,
  length_cm TEXT NOT NULL,
  width_cm TEXT NOT NULL,
  height_cm TEXT NOT NULL,
  declared_value TEXT NOT NULL,
  pieces INTEGER NOT NULL,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS quarantine (
  run_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '',
  code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  raw TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, line_no, code)
);
CREATE TABLE IF NOT EXISTS weight_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  actual_kg TEXT NOT NULL DEFAULT '0.000',
  volumetric_kg TEXT NOT NULL DEFAULT '0.000',
  source TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS charge_lines (
  run_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  version_id TEXT NOT NULL DEFAULT '',
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (run_id, waybill_no)
);
"""

# 关账（出账批次）表：正式账单只从这里的快照读，运行级的 weight_lines / charge_lines 只是预览。
CLOSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS close_periods (
  period TEXT PRIMARY KEY,
  note TEXT NOT NULL DEFAULT '',
  opened_at TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS close_sources (
  period TEXT NOT NULL,
  seq INTEGER NOT NULL,
  kind TEXT NOT NULL,
  source_id TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  input_name TEXT NOT NULL DEFAULT '',
  input_sha256 TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  added_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (period, source_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS close_sources_seq ON close_sources(period, seq);
CREATE TABLE IF NOT EXISTS close_versions (
  version_id TEXT PRIMARY KEY,
  period TEXT NOT NULL,
  version_no INTEGER NOT NULL,
  state TEXT NOT NULL,
  basis_digest TEXT NOT NULL,
  impact_digest TEXT NOT NULL DEFAULT '',
  rate_digest TEXT NOT NULL DEFAULT '',
  rate_set_version INTEGER NOT NULL DEFAULT 0,
  rules_version TEXT NOT NULL DEFAULT '',
  baseline_version_id TEXT NOT NULL DEFAULT '',
  source_count INTEGER NOT NULL DEFAULT 0,
  scope_count INTEGER NOT NULL DEFAULT 0,
  billed_count INTEGER NOT NULL DEFAULT 0,
  unbilled_count INTEGER NOT NULL DEFAULT 0,
  excluded_count INTEGER NOT NULL DEFAULT 0,
  superseded_count INTEGER NOT NULL DEFAULT 0,
  quarantined_count INTEGER NOT NULL DEFAULT 0,
  amount_total TEXT NOT NULL DEFAULT '0.00',
  balanced INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT '',
  published_at TEXT NOT NULL DEFAULT '',
  superseded_at TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS close_versions_basis ON close_versions(period, basis_digest);
CREATE UNIQUE INDEX IF NOT EXISTS close_versions_no ON close_versions(period, version_no);
CREATE TABLE IF NOT EXISTS close_invoice_lines (
  version_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  waybill_no TEXT NOT NULL,
  carrier TEXT NOT NULL DEFAULT '',
  service TEXT NOT NULL DEFAULT '',
  zone TEXT NOT NULL DEFAULT '',
  pickup_utc TEXT NOT NULL DEFAULT '',
  billable_kg TEXT NOT NULL DEFAULT '0.000',
  steps INTEGER NOT NULL DEFAULT 0,
  rate_version_id TEXT NOT NULL DEFAULT '',
  rate_set_version INTEGER NOT NULL DEFAULT 0,
  effective_from TEXT NOT NULL DEFAULT '',
  effective_to TEXT NOT NULL DEFAULT '',
  first_weight_kg TEXT NOT NULL DEFAULT '',
  first_price TEXT NOT NULL DEFAULT '',
  step_weight_kg TEXT NOT NULL DEFAULT '',
  step_price TEXT NOT NULL DEFAULT '',
  min_charge TEXT NOT NULL DEFAULT '',
  volume_divisor INTEGER NOT NULL DEFAULT 0,
  fuel_pct TEXT NOT NULL DEFAULT '',
  remote_fee TEXT NOT NULL DEFAULT '',
  insurance_pct TEXT NOT NULL DEFAULT '',
  base_amount TEXT NOT NULL DEFAULT '0.00',
  step_amount TEXT NOT NULL DEFAULT '0.00',
  remote_amount TEXT NOT NULL DEFAULT '0.00',
  fuel_amount TEXT NOT NULL DEFAULT '0.00',
  insurance_amount TEXT NOT NULL DEFAULT '0.00',
  total_amount TEXT NOT NULL DEFAULT '0.00',
  currency TEXT NOT NULL DEFAULT 'CNY',
  source_run_id TEXT NOT NULL DEFAULT '',
  source_input_name TEXT NOT NULL DEFAULT '',
  source_line_no INTEGER NOT NULL DEFAULT 0,
  codes TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (version_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS close_evidence_rows (
  version_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  row_kind TEXT NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (version_id, seq)
);
CREATE TABLE IF NOT EXISTS close_scope_rows (
  version_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  seq INTEGER NOT NULL,
  waybill_no TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  line_no INTEGER NOT NULL DEFAULT 0,
  code TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (version_id, kind, seq)
);
CREATE TABLE IF NOT EXISTS close_impacts (
  version_id TEXT NOT NULL,
  waybill_no TEXT NOT NULL,
  change TEXT NOT NULL,
  rate_changed INTEGER NOT NULL DEFAULT 0,
  weight_changed INTEGER NOT NULL DEFAULT 0,
  amount_changed INTEGER NOT NULL DEFAULT 0,
  old_billable_kg TEXT NOT NULL DEFAULT '',
  new_billable_kg TEXT NOT NULL DEFAULT '',
  old_version_id TEXT NOT NULL DEFAULT '',
  new_version_id TEXT NOT NULL DEFAULT '',
  old_rate_set_version INTEGER NOT NULL DEFAULT 0,
  new_rate_set_version INTEGER NOT NULL DEFAULT 0,
  old_total TEXT NOT NULL DEFAULT '0.00',
  new_total TEXT NOT NULL DEFAULT '0.00',
  delta_total TEXT NOT NULL DEFAULT '0.00',
  delta_billable_kg TEXT NOT NULL DEFAULT '0.000',
  changed_parts TEXT NOT NULL DEFAULT '',
  delta_parts TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (version_id, waybill_no)
);
CREATE TABLE IF NOT EXISTS close_manifests (
  version_id TEXT PRIMARY KEY,
  doc TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS close_reviews (
  period TEXT NOT NULL,
  version_id TEXT NOT NULL,
  reviewer TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  impact_digest TEXT NOT NULL DEFAULT '',
  reviewed_at TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (period, version_id, reviewer, reviewed_at)
);
CREATE TABLE IF NOT EXISTS close_official (
  period TEXT PRIMARY KEY,
  version_id TEXT NOT NULL,
  published_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS close_rate_cards (
  period TEXT NOT NULL,
  source_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  carrier TEXT NOT NULL,
  service TEXT NOT NULL,
  zone TEXT NOT NULL,
  version_id TEXT NOT NULL,
  effective_from TEXT NOT NULL,
  effective_to TEXT NOT NULL,
  first_weight_kg TEXT NOT NULL,
  first_price TEXT NOT NULL,
  step_weight_kg TEXT NOT NULL,
  step_price TEXT NOT NULL,
  min_charge TEXT NOT NULL,
  volume_divisor INTEGER NOT NULL,
  fuel_pct TEXT NOT NULL,
  remote_fee TEXT NOT NULL,
  insurance_pct TEXT NOT NULL,
  currency TEXT NOT NULL,
  rate_set_version INTEGER NOT NULL,
  PRIMARY KEY (period, source_id, carrier, service, zone, version_id)
);
"""

RATE_CARD_FIELDS = (
    "carrier", "service", "zone", "version_id", "effective_from", "effective_to",
    "first_weight_kg", "first_price", "step_weight_kg", "step_price", "min_charge",
    "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct", "currency",
    "rate_set_version",
)


def connect() -> sqlite3.Connection:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with session() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(CLOSE_SCHEMA)


def rows(sql: str, params: tuple = ()) -> list[dict]:
    with session() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> dict | None:
    got = rows(sql, params)
    return got[0] if got else None


def execute(sql: str, params: tuple = ()) -> None:
    with session() as conn:
        conn.execute(sql, params)


def executemany(sql: str, seq) -> None:
    with session() as conn:
        conn.executemany(sql, list(seq))


# --- 运行清单 -------------------------------------------------------------

RUN_COLUMNS = (
    "input_name", "input_sha256", "rules_version", "profile", "scope",
    "rows_in", "rows_ok", "rows_quarantined", "amount_total",
    "rate_digest", "stages", "notes", "created_at", "updated_at",
)


def get_run(run_id: str) -> dict | None:
    return one("SELECT * FROM runs WHERE run_id = ?", (run_id,))


def list_runs(limit: int = 20, scope: str = "preview") -> list[dict]:
    """默认只看预览口径的批次；关账采信批次不混进页面的批次列表。"""
    return rows(
        "SELECT * FROM runs WHERE scope = ? ORDER BY created_at DESC, run_id DESC LIMIT ?",
        (scope, limit),
    )


def upsert_run(run_id: str, data: dict) -> None:
    """并发安全的幂等写入：先按默认值占位，再只更新本次提供的字段。

    原先的“先查再插”在并发下会撞主键；改成 INSERT OR IGNORE + UPDATE 后，同一
    run_id 同时写多次也只会存在一行，且已有字段不会被空默认值冲掉。
    """
    fields = [c for c in RUN_COLUMNS if c in data]
    with session() as conn:
        payload = {c: data.get(c, "") for c in RUN_COLUMNS}
        payload["rows_in"] = int(payload.get("rows_in") or 0)
        payload["rows_ok"] = int(payload.get("rows_ok") or 0)
        payload["rows_quarantined"] = int(payload.get("rows_quarantined") or 0)
        cols = ("run_id",) + RUN_COLUMNS
        conn.execute(
            f"INSERT OR IGNORE INTO runs ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})",
            (run_id,) + tuple(payload[c] for c in RUN_COLUMNS),
        )
        if fields:
            assignments = ", ".join(f"{c} = ?" for c in fields)
            conn.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                tuple(data[c] for c in fields) + (run_id,),
            )


def add_stage(run_id: str, stage: str, updated_at: str) -> None:
    run = get_run(run_id)
    if not run:
        return
    parts = [p for p in (run.get("stages") or "").split(",") if p]
    if stage not in parts:
        parts.append(stage)
    execute(
        "UPDATE runs SET stages = ?, updated_at = ? WHERE run_id = ?",
        (",".join(parts), updated_at, run_id),
    )


# --- 运价版本 -------------------------------------------------------------

CARD_COLUMNS = (
    "carrier", "service", "zone", "version_id", "effective_from", "effective_to",
    "first_weight_kg", "first_price", "step_weight_kg", "step_price", "min_charge",
    "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct", "currency",
    "rate_set_version",
)


def replace_rate_cards(cards: list[dict], rate_set_version: int, updated_at: str) -> None:
    with session() as conn:
        conn.execute("DELETE FROM rate_cards")
        conn.executemany(
            f"INSERT INTO rate_cards ({', '.join(CARD_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in CARD_COLUMNS)})",
            [tuple(card[c] for c in CARD_COLUMNS) for card in cards],
        )
        carriers = sorted({card["carrier"] for card in cards})
        conn.execute("DELETE FROM rate_sets")
        conn.executemany(
            "INSERT INTO rate_sets (carrier, rate_set_version, updated_at) VALUES (?, ?, ?)",
            [(c, rate_set_version, updated_at) for c in carriers],
        )


def rate_cards() -> list[dict]:
    return rows("SELECT * FROM rate_cards ORDER BY carrier, service, zone, effective_from")


def rate_set_version(carrier: str) -> int:
    got = one("SELECT rate_set_version FROM rate_sets WHERE carrier = ?", (carrier,))
    return int(got["rate_set_version"]) if got else 0


def max_rate_set_version() -> int:
    got = one("SELECT MAX(rate_set_version) AS v FROM rate_sets")
    return int(got["v"]) if got and got["v"] is not None else 0


# --- 运单 / 隔离区 / 计费行 ------------------------------------------------

def replace_waybills(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM waybills WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO waybills (run_id, line_no, waybill_no, carrier, service, zone, "
            "pickup_utc, weight_kg, length_cm, width_cm, height_cm, declared_value, pieces, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["line_no"], it["waybill_no"], it["carrier"], it["service"],
                    it["zone"], it["pickup_utc"], it["weight_kg"], it["length_cm"],
                    it["width_cm"], it["height_cm"], it["declared_value"], it["pieces"],
                    it["codes"],
                )
                for it in items
            ],
        )


def waybills(run_id: str) -> list[dict]:
    return rows("SELECT * FROM waybills WHERE run_id = ? ORDER BY line_no", (run_id,))


def replace_quarantine(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM quarantine WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO quarantine (run_id, line_no, waybill_no, code, detail, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (run_id, it["line_no"], it["waybill_no"], it["code"], it["detail"], it["raw"])
                for it in items
            ],
        )


def quarantine(run_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM quarantine WHERE run_id = ? ORDER BY line_no, code", (run_id,)
    )


def replace_weight_lines(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM weight_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO weight_lines (run_id, waybill_no, version_id, actual_kg, volumetric_kg, "
            "source, billable_kg, steps, codes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["waybill_no"], it["version_id"], it["actual_kg"],
                    it["volumetric_kg"], it["source"], it["billable_kg"], it["steps"],
                    it.get("codes", ""),
                )
                for it in items
            ],
        )


def weight_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM weight_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def replace_charge_lines(run_id: str, items: list[dict]) -> None:
    with session() as conn:
        conn.execute("DELETE FROM charge_lines WHERE run_id = ?", (run_id,))
        conn.executemany(
            "INSERT INTO charge_lines (run_id, waybill_no, version_id, base_amount, step_amount, "
            "remote_amount, fuel_amount, insurance_amount, total_amount, codes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    run_id, it["waybill_no"], it["version_id"], it["base_amount"],
                    it["step_amount"], it["remote_amount"], it["fuel_amount"],
                    it["insurance_amount"], it["total_amount"], it["codes"],
                )
                for it in items
            ],
        )


def charge_lines(run_id: str) -> list[dict]:
    return rows("SELECT * FROM charge_lines WHERE run_id = ? ORDER BY waybill_no", (run_id,))


def latest_run_id(scope: str = "preview") -> str:
    """最新预览批次。关账用自己的 close_sources 记来源，不靠 runs 的“最新”。"""
    got = one(
        "SELECT run_id FROM runs WHERE scope = ? ORDER BY created_at DESC, run_id DESC LIMIT 1",
        (scope,),
    )
    return got["run_id"] if got else ""


# --- 关账：批次、来源、版本、发布 -------------------------------------------
#
# 关账期（如 2026-04）聚合多份运价文件与多份运单文件。正式账单只从版本快照读，
# 版本一经写入即不可变；发布只是把一个指针挪到新版本，旧版本整份保留。

CLOSE_DRAFT = "draft"
CLOSE_PUBLISHED = "published"
CLOSE_SUPERSEDED = "superseded"


def _insert_many(conn: sqlite3.Connection, table: str, cols: tuple[str, ...],
                 items: list[dict]) -> None:
    if not items:
        return
    conn.executemany(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [tuple(item[c] for c in cols) for item in items],
    )


def close_period(period: str) -> dict | None:
    return one("SELECT * FROM close_periods WHERE period = ?", (period,))


def upsert_close_period(period: str, note: str, now: str) -> None:
    with session() as conn:
        row = conn.execute("SELECT period FROM close_periods WHERE period = ?", (period,)).fetchone()
        if row:
            conn.execute(
                "UPDATE close_periods SET note = ?, updated_at = ? WHERE period = ?",
                (note, now, period),
            )
        else:
            conn.execute(
                "INSERT INTO close_periods (period, note, opened_at, updated_at) VALUES (?, ?, ?, ?)",
                (period, note, now, now),
            )


def close_sources(period: str, kind: str = "") -> list[dict]:
    if kind:
        return rows(
            "SELECT * FROM close_sources WHERE period = ? AND kind = ? ORDER BY seq",
            (period, kind),
        )
    return rows("SELECT * FROM close_sources WHERE period = ? ORDER BY seq", (period,))


def _next_source_seq(conn: sqlite3.Connection, period: str) -> int:
    """写锁内的下一个来源序号。调用方必须先 BEGIN IMMEDIATE。"""
    row = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) AS s FROM close_sources WHERE period = ?", (period,)
    ).fetchone()
    return int(row["s"]) + 1


def write_rate_source(period: str, source: dict, cards: list[dict]) -> bool:
    """登记运价来源并写入该来源的运价快照，同一个事务完成；已登记则返回 False。"""
    with session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM close_sources WHERE period = ? AND source_id = ?",
            (period, source["source_id"]),
        ).fetchone():
            return False
        seq = _next_source_seq(conn, period)
        conn.execute(
            "INSERT INTO close_sources (period, seq, kind, source_id, run_id, input_name, "
            "input_sha256, detail, added_at) VALUES (?, ?, 'rate', ?, '', ?, ?, ?, ?)",
            (
                period, seq, source["source_id"], source.get("input_name", ""),
                source.get("input_sha256", ""), source.get("detail", ""), source["added_at"],
            ),
        )
        conn.executemany(
            f"INSERT INTO close_rate_cards (period, source_id, seq, {', '.join(RATE_CARD_FIELDS)}) "
            f"VALUES ({', '.join('?' for _ in range(3 + len(RATE_CARD_FIELDS)))})",
            [
                (period, source["source_id"], i) + tuple(card[c] for c in RATE_CARD_FIELDS)
                for i, card in enumerate(cards, start=1)
            ],
        )
        return True


def write_waybill_source(period: str, source: dict) -> bool:
    """登记运单来源；同一 source_id 只登记一次，并发重复提交也只会成功一次。"""
    with session() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM close_sources WHERE period = ? AND source_id = ?",
            (period, source["source_id"]),
        ).fetchone():
            return False
        seq = _next_source_seq(conn, period)
        conn.execute(
            "INSERT INTO close_sources (period, seq, kind, source_id, run_id, input_name, "
            "input_sha256, detail, added_at) VALUES (?, ?, 'waybill', ?, ?, ?, ?, ?, ?)",
            (
                period, seq, source["source_id"], source.get("run_id", ""),
                source.get("input_name", ""), source.get("input_sha256", ""),
                source.get("detail", ""), source["added_at"],
            ),
        )
        return True


def close_rate_cards(period: str, source_id: str) -> list[dict]:
    """取某一登记来源的运价快照；范围解析会按承运商合并多个来源。"""
    return rows(
        "SELECT * FROM close_rate_cards WHERE period = ? AND source_id = ? ORDER BY seq",
        (period, source_id),
    )


def close_versions(period: str) -> list[dict]:
    return rows("SELECT * FROM close_versions WHERE period = ? ORDER BY version_no", (period,))


def close_version(version_id: str) -> dict | None:
    return one("SELECT * FROM close_versions WHERE version_id = ?", (version_id,))


def close_version_by_basis(period: str, basis_digest: str) -> dict | None:
    return one(
        "SELECT * FROM close_versions WHERE period = ? AND basis_digest = ?",
        (period, basis_digest),
    )


def next_close_version_no(period: str) -> int:
    got = one("SELECT MAX(version_no) AS n FROM close_versions WHERE period = ?", (period,))
    return int(got["n"]) + 1 if got and got["n"] is not None else 1


def update_close_version(version_id: str, fields: dict) -> None:
    if not fields:
        return
    assignments = ", ".join(f"{key} = ?" for key in fields)
    with session() as conn:
        conn.execute(
            f"UPDATE close_versions SET {assignments} WHERE version_id = ?",
            tuple(fields.values()) + (version_id,),
        )


def _point_official(conn: sqlite3.Connection, period: str, version_id: str,
                    published_at: str) -> None:
    """在调用方的事务里挪正式指针。"""
    conn.execute(
        "INSERT INTO close_official (period, version_id, published_at) VALUES (?, ?, ?) "
        "ON CONFLICT(period) DO UPDATE SET version_id = excluded.version_id, "
        "published_at = excluded.published_at",
        (period, version_id, published_at),
    )


def set_official_version(period: str, version_id: str, published_at: str) -> None:
    """只挪正式指针，不碰版本状态。"""
    with session() as conn:
        _point_official(conn, period, version_id, published_at)


def publish_close_version(period: str, version_id: str, now: str) -> dict:
    """原子发布：旧正式版置 superseded（保留整份），新版本置 published，再挪指针。

    返回 {"previous": 旧版本 id, "pointer": 新版本 id, "changed": 是否真的切换}。
    重复发布同一版本不改状态、不新增版本。三步在同一事务里，任一步失败全部回滚，
    旧正式版与旧导出原样可用。
    """
    with session() as conn:
        # 先拿写锁再读指针：并发发布同一版本时，后到者会看到已切换的正式指针，
        # 从而返回 changed=False，而不是把“换了两次”报给调用方。
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            "SELECT * FROM close_versions WHERE version_id = ? AND period = ?",
            (version_id, period),
        ).fetchone()
        if target is None:
            return {"previous": "", "pointer": "", "changed": False, "missing": True}
        current = conn.execute(
            "SELECT version_id FROM close_official WHERE period = ?", (period,)
        ).fetchone()
        previous = current["version_id"] if current else ""
        if previous == version_id and target["state"] == CLOSE_PUBLISHED:
            return {"previous": previous, "pointer": version_id, "changed": False}
        if previous and previous != version_id:
            conn.execute(
                "UPDATE close_versions SET state = ?, superseded_at = ? "
                "WHERE version_id = ? AND state = ?",
                (CLOSE_SUPERSEDED, now, previous, CLOSE_PUBLISHED),
            )
        conn.execute(
            "UPDATE close_versions SET state = ?, published_at = ? WHERE version_id = ?",
            (CLOSE_PUBLISHED, now, version_id),
        )
        _point_official(conn, period, version_id, now)
        return {"previous": previous, "pointer": version_id, "changed": True}


VERSION_COLUMNS = (
    "version_id", "period", "version_no", "state", "basis_digest", "impact_digest",
    "rate_digest", "rate_set_version", "rules_version", "baseline_version_id",
    "source_count", "scope_count", "billed_count", "unbilled_count", "excluded_count",
    "superseded_count", "quarantined_count", "amount_total", "balanced", "created_at",
    "published_at", "superseded_at",
)

INVOICE_COLUMNS = (
    "version_id", "seq", "waybill_no", "carrier", "service", "zone", "pickup_utc",
    "billable_kg", "steps", "rate_version_id", "rate_set_version", "effective_from",
    "effective_to", "first_weight_kg", "first_price", "step_weight_kg", "step_price",
    "min_charge", "volume_divisor", "fuel_pct", "remote_fee", "insurance_pct",
    "base_amount", "step_amount", "remote_amount", "fuel_amount", "insurance_amount",
    "total_amount", "currency", "source_run_id", "source_input_name", "source_line_no", "codes",
)

EVIDENCE_COLUMNS = ("version_id", "seq", "row_kind", "waybill_no", "payload")
SCOPE_COLUMNS = (
    "version_id", "kind", "seq", "waybill_no", "run_id", "line_no", "code", "detail", "payload",
)
IMPACT_COLUMNS = (
    "version_id", "waybill_no", "change", "rate_changed", "weight_changed", "amount_changed",
    "old_billable_kg", "new_billable_kg", "old_version_id", "new_version_id",
    "old_rate_set_version", "new_rate_set_version", "old_total",
    "new_total", "delta_total", "delta_billable_kg", "changed_parts", "delta_parts",
)


def _prepare_invoice(version_id: str, items: list[dict]) -> list[dict]:
    prepared = []
    for seq, item in enumerate(items, start=1):
        row = {c: item.get(c, "") for c in INVOICE_COLUMNS}
        row["version_id"] = version_id
        row["seq"] = seq
        row.update({
            "waybill_no": item["waybill_no"],
            "steps": int(item.get("steps") or 0),
            "volume_divisor": int(item.get("volume_divisor") or 0),
            "rate_set_version": int(item.get("rate_set_version") or 0),
            "source_line_no": int(item.get("source_line_no") or 0),
        })
        prepared.append(row)
    return prepared


def write_close_version(snapshot: dict) -> None:
    """把一份完整版本快照写进库；整段在一个事务里，中断即整份回滚。

    版本行与所有明细行同时落库，因此不存在“半个版本”：要么整版可见，要么旧版原样。
    """
    version = {c: snapshot["version"].get(c, "") for c in VERSION_COLUMNS}
    version["balanced"] = int(bool(snapshot["version"].get("balanced")))
    for key in ("version_no", "rate_set_version", "source_count", "scope_count",
                "billed_count", "unbilled_count", "excluded_count", "superseded_count",
                "quarantined_count"):
        version[key] = int(version.get(key) or 0)
    version_id = version["version_id"]

    evidence = []
    for seq, item in enumerate(snapshot["evidence"], start=1):
        evidence.append({
            "version_id": version_id, "seq": seq, "waybill_no": item.get("waybill_no", ""),
            "row_kind": item["row_kind"], "payload": item["payload"],
        })
    scope = []
    for kind in ("billed", "unbilled", "quarantined", "excluded", "superseded"):
        for seq, item in enumerate(snapshot["scope"].get(kind, []), start=1):
            scope.append({
                "version_id": version_id, "kind": kind, "seq": seq,
                "waybill_no": item.get("waybill_no", ""), "run_id": item.get("run_id", ""),
                "line_no": int(item.get("line_no") or 0), "code": item.get("code", ""),
                "detail": item.get("detail", ""), "payload": item.get("payload", "{}"),
            })
    impacts = []
    for item in snapshot["impacts"]:
        row = {c: item.get(c, "") for c in IMPACT_COLUMNS}
        row["version_id"] = version_id
        for flag in ("rate_changed", "weight_changed", "amount_changed"):
            row[flag] = int(bool(row.get(flag)))
        if not isinstance(row["delta_parts"], str):
            row["delta_parts"] = json.dumps(
                row["delta_parts"], ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            )
        for key in ("old_rate_set_version", "new_rate_set_version"):
            row[key] = int(row.get(key) or 0)
        impacts.append(row)

    with session() as conn:
        _insert_many(conn, "close_versions", VERSION_COLUMNS, [version])
        _insert_many(conn, "close_invoice_lines", INVOICE_COLUMNS,
                     _prepare_invoice(version_id, snapshot["invoice"]))
        _insert_many(conn, "close_evidence_rows", EVIDENCE_COLUMNS, evidence)
        _insert_many(conn, "close_scope_rows", SCOPE_COLUMNS, scope)
        _insert_many(conn, "close_impacts", IMPACT_COLUMNS, impacts)
        conn.execute(
            "INSERT INTO close_manifests (version_id, doc) VALUES (?, ?) "
            "ON CONFLICT(version_id) DO UPDATE SET doc = excluded.doc",
            (version_id, snapshot["manifest"]),
        )


def close_invoice_lines(version_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM close_invoice_lines WHERE version_id = ? ORDER BY seq", (version_id,)
    )


def close_evidence_rows(version_id: str, row_kind: str = "") -> list[dict]:
    if row_kind:
        return rows(
            "SELECT * FROM close_evidence_rows WHERE version_id = ? AND row_kind = ? ORDER BY seq",
            (version_id, row_kind),
        )
    return rows(
        "SELECT * FROM close_evidence_rows WHERE version_id = ? ORDER BY seq", (version_id,)
    )


def close_scope_rows(version_id: str, kind: str = "") -> list[dict]:
    if kind:
        return rows(
            "SELECT * FROM close_scope_rows WHERE version_id = ? AND kind = ? ORDER BY seq",
            (version_id, kind),
        )
    return rows(
        "SELECT * FROM close_scope_rows WHERE version_id = ? ORDER BY kind, seq", (version_id,)
    )


def close_impacts(version_id: str) -> list[dict]:
    return rows(
        "SELECT * FROM close_impacts WHERE version_id = ? ORDER BY waybill_no", (version_id,)
    )


def close_manifest(version_id: str) -> dict | None:
    return one("SELECT * FROM close_manifests WHERE version_id = ?", (version_id,))


def add_close_review(period: str, version_id: str, reviewer: str, note: str,
                     impact_digest: str, reviewed_at: str) -> None:
    """追加一条复核记录。同一复核人同一秒重复提交视为同一条，不报错也不重复。"""
    with session() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO close_reviews (period, version_id, reviewer, note, "
            "impact_digest, reviewed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (period, version_id, reviewer, note, impact_digest, reviewed_at),
        )


def close_reviews(period: str, version_id: str = "") -> list[dict]:
    if version_id:
        return rows(
            "SELECT * FROM close_reviews WHERE period = ? AND version_id = ? "
            "ORDER BY reviewed_at, reviewer",
            (period, version_id),
        )
    return rows(
        "SELECT * FROM close_reviews WHERE period = ? ORDER BY reviewed_at, reviewer",
        (period,),
    )


def official_version_id(period: str) -> str:
    got = one("SELECT version_id FROM close_official WHERE period = ?", (period,))
    return got["version_id"] if got else ""
