"""幂等的启动期结构补丁。

项目以 ``Base.metadata.create_all()`` 作为首次建库入口，已有开发卷不会自动补字段。
本模块只在检测到列缺失时执行 ALTER，并对存量延误案回填泊位、轮次、批次与登记序号，
保证旧卷升级后批次规则仍可运行。全新数据库由 create_all 直接建出完整结构，补丁全部跳过。
"""

import logging

from sqlalchemy import text

from .database import engine
from .models import BATCH_SIZE

log = logging.getLogger('uvicorn.error')


def _existing_columns(conn, table: str) -> set[str]:
    rows = conn.execute(text(
        'SELECT column_name FROM information_schema.columns '
        'WHERE table_schema = DATABASE() AND table_name = :table'
    ), {'table': table})
    return {row[0] for row in rows}


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(text(
        'SELECT 1 FROM information_schema.tables '
        'WHERE table_schema = DATABASE() AND table_name = :table'
    ), {'table': table}).first())


def apply():
    with engine.begin() as conn:
        berth_cols = _existing_columns(conn, 'berths')
        if 'cycle_no' not in berth_cols:
            conn.execute(text('ALTER TABLE berths ADD COLUMN cycle_no INT NOT NULL DEFAULT 1'))
        if 'next_seq' not in berth_cols:
            conn.execute(text('ALTER TABLE berths ADD COLUMN next_seq INT NOT NULL DEFAULT 1'))

        if not _table_exists(conn, 'delay_incidents'):
            return  # 全新库由 create_all 建表，无需补丁。
        incident_cols = _existing_columns(conn, 'delay_incidents')
        additions = {
            'berth_code': "ALTER TABLE delay_incidents ADD COLUMN berth_code VARCHAR(12) NULL",
            'cycle_no': "ALTER TABLE delay_incidents ADD COLUMN cycle_no INT NULL",
            'seq_no': "ALTER TABLE delay_incidents ADD COLUMN seq_no INT NULL",
            'batch_no': "ALTER TABLE delay_incidents ADD COLUMN batch_no INT NULL",
            'in_margin_band': "ALTER TABLE delay_incidents ADD COLUMN in_margin_band TINYINT(1) NOT NULL DEFAULT 0",
            'auto_released_by': "ALTER TABLE delay_incidents ADD COLUMN auto_released_by VARCHAR(32) NULL",
            'auto_released_at': "ALTER TABLE delay_incidents ADD COLUMN auto_released_at DATETIME NULL",
            'auto_release_note': "ALTER TABLE delay_incidents ADD COLUMN auto_release_note VARCHAR(300) NULL",
        }
        for column, ddl in additions.items():
            if column not in incident_cols:
                conn.execute(text(ddl))

        # 回填存量案件：按泊位、观测时间（登记顺序的代理口径）编号，十案一批，首轮轮次 1。
        backfill = conn.execute(text("""
            UPDATE delay_incidents i
            JOIN (
                SELECT d.code,
                       ROW_NUMBER() OVER (PARTITION BY r.berth_code ORDER BY d.observed_at, d.code) AS rn,
                       r.berth_code AS berth
                FROM delay_incidents d JOIN berth_requests r ON r.code = d.request_code
            ) ordered ON ordered.code = i.code
            SET i.berth_code = ordered.berth,
                i.cycle_no = COALESCE(i.cycle_no, 1),
                i.seq_no = COALESCE(i.seq_no, ordered.rn),
                i.batch_no = COALESCE(i.batch_no, FLOOR((ordered.rn - 1) / :size) + 1)
            WHERE i.seq_no IS NULL
        """), {'size': BATCH_SIZE})
        if backfill.rowcount:
            log.info('Backfilled batch ordering for %s existing incidents', backfill.rowcount)
        # 历史间隔带案（含已签署的）补标记，便于暂停口径与页面展示一致。
        conn.execute(text(
            "UPDATE delay_incidents SET in_margin_band = 1 "
            "WHERE state = 'margin_review' AND in_margin_band = 0"
        ))
        conn.execute(text(
            "ALTER TABLE delay_incidents MODIFY berth_code VARCHAR(12) NOT NULL"
        ))
        conn.execute(text("ALTER TABLE delay_incidents MODIFY cycle_no INT NOT NULL"))
        conn.execute(text("ALTER TABLE delay_incidents MODIFY seq_no INT NOT NULL"))
        conn.execute(text("ALTER TABLE delay_incidents MODIFY batch_no INT NOT NULL"))

        # 旧索引名 state_idx 与新模型 incident_state_idx 不同；缺失唯一约束时补上（忽略已存在的情况）。
        existing_keys = {row[0] for row in conn.execute(text(
            "SELECT DISTINCT index_name FROM information_schema.statistics "
            "WHERE table_schema = DATABASE() AND table_name = 'delay_incidents'"
        ))}
        if 'incident_berth_sequence_uq' not in existing_keys:
            conn.execute(text(
                'ALTER TABLE delay_incidents ADD CONSTRAINT incident_berth_sequence_uq '
                'UNIQUE (berth_code, cycle_no, seq_no)'
            ))
        if 'incident_berth_batch_idx' not in existing_keys:
            conn.execute(text(
                'CREATE INDEX incident_berth_batch_idx '
                'ON delay_incidents (berth_code, cycle_no, batch_no, seq_no)'
            ))

        if not _table_exists(conn, 'berth_suspensions'):
            conn.execute(text("""
                CREATE TABLE berth_suspensions (
                    id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
                    berth_code VARCHAR(12) NOT NULL,
                    cycle_no INT NOT NULL,
                    batch_no INT NOT NULL,
                    trigger_incident_code VARCHAR(32) NOT NULL,
                    margin_cases INT NOT NULL,
                    reason VARCHAR(300) NOT NULL,
                    suspended_by VARCHAR(32) NOT NULL,
                    suspended_at DATETIME NOT NULL,
                    lifted_by VARCHAR(32) NULL,
                    lifted_at DATETIME NULL,
                    lift_note VARCHAR(300) NULL,
                    next_cycle_no INT NULL,
                    CONSTRAINT fk_suspension_berth FOREIGN KEY (berth_code) REFERENCES berths (code),
                    CONSTRAINT fk_suspension_incident FOREIGN KEY (trigger_incident_code)
                        REFERENCES delay_incidents (code),
                    CONSTRAINT fk_suspension_actor FOREIGN KEY (suspended_by) REFERENCES users (username),
                    CONSTRAINT fk_suspension_lifter FOREIGN KEY (lifted_by) REFERENCES users (username),
                    INDEX berth_suspension_open_idx (berth_code, lifted_at)
                )
            """))

        # 每个泊位的下一个序号对齐现有最大序号 + 1，避免重启后重号。
        conn.execute(text("""
            UPDATE berths b LEFT JOIN (
                SELECT berth_code, MAX(seq_no) AS max_seq
                FROM delay_incidents GROUP BY berth_code
            ) m ON m.berth_code = b.code
            SET b.next_seq = GREATEST(b.next_seq, COALESCE(m.max_seq, 0) + 1)
        """))
