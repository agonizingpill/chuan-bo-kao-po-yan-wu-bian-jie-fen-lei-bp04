import logging
import time
from pathlib import Path

from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from . import batching
from .classifier import classify
from .database import Session, WAKE_QUEUE, queue
from .models import DelayClass, DelayIncident, IncidentState
from .planning import audit

log = logging.getLogger(__name__)
heartbeat = Path('/tmp/berth-heartbeat')


def _claim(db):
    """领取下一个待计算案。

    全局按泊位、再按同泊位登记顺序（轮次、批次、序号）处理，保证同一泊位先登记的案先算；
    行锁 + skip_locked 让多个 worker 不会重复领取同一案。后批先算完时由批次闸门拦住，
    在 BATCH_HELD 等待前批算齐，不会越级先判。
    """
    return db.scalar(
        select(DelayIncident).where(DelayIncident.state == IncidentState.QUEUED)
        .order_by(DelayIncident.berth_code, DelayIncident.cycle_no,
                  DelayIncident.batch_no, DelayIncident.seq_no, DelayIncident.code)
        .limit(1).with_for_update(skip_locked=True)
    )


def process_next():
    with Session.begin() as db:
        incident = _claim(db)
        if not incident:
            return False
        incident.attempts += 1
        try:
            result = classify(incident.features, incident.input_digest)
        except Exception as exc:
            log.exception('Classification failed for %s', incident.code)
            # 失败保留原 cycle/seq/batch 与计数，主管重试后补回原位，不重新编号、不重复计案。
            incident.state = IncidentState.FAILED
            incident.error = str(exc)[:180]
            audit(db, 'worker', incident.code, 'MODEL_FAILED', incident.error)
            return True
        incident.result = result
        incident.in_margin_band = bool(result['needs_review'])
        target_state = IncidentState.MARGIN_REVIEW if result['needs_review'] else IncidentState.READY
        # 批次闸门：前批未算齐则先挂起，结论（含间隔带标记）已冻结，等前批算齐后原样放行。
        held = batching.gate_state(db, incident) is not None
        incident.state = IncidentState.BATCH_HELD if held else target_state
        if not held and target_state == IncidentState.READY:
            incident.decision = DelayClass(result['prediction'])
        audit(db, 'worker', incident.code, 'MODEL_CLASSIFIED',
              f"f(x)={result['score']:+.5f} · {incident.state} · "
              f"第 {incident.cycle_no} 轮第 {incident.batch_no} 批序号 {incident.seq_no}")
        log.info('%s %s', incident.code, incident.state)
        # 本案可能补齐前批，按登记顺序放行同泊位等待中的案。
        batching.promote_held(db, incident.berth_code)
        # 一批至少六案进入间隔带即暂停该泊位自动放行（等待中的间隔带案同样计入）。
        batching.suspend_if_threshold(db, 'worker', incident)
        return True


def run():
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            processed = process_next()
        except SQLAlchemyError:
            log.exception('Database unavailable')
            time.sleep(2)
            continue
        heartbeat.touch()
        if not processed:
            try:
                queue.brpop(WAKE_QUEUE, timeout=2)
            except RedisError:
                log.warning('Redis unavailable; using durable queue')
                time.sleep(2)


if __name__ == '__main__':
    run()
