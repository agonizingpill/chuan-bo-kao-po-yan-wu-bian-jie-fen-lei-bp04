"""同一泊位延误案的批次闸门与自动放行暂停规则。

规则口径：
- 同一泊位的延误案按登记顺序编号，十案一批；后批即使先算完，也必须等前批算齐才能判。
- 一批中至少六案进入间隔带（人工复核带），即暂停该泊位的自动放行。
- 计算失败后重试，沿用原登记序号与批次，不重复计案。
- 暂停期间未发令的带外案也须主管签放后才能发令；已发令的照常执行。
- 暂停与发令并发，以先落定（先提交）的为准。
- 主管核实后解除暂停，解除后从新登记的案重新计数（开启新一轮）。
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DatabaseSession

from .models import (
    BATCH_SIZE,
    SUSPEND_MARGIN_CASES,
    Berth,
    BerthSuspension,
    DelayClass,
    DelayIncident,
    IncidentState,
)
from .planning import Rejected, audit, find, utc_now

# 尚未得出有效计算结论的状态：排队中、计算失败。
# BATCH_HELD 的案已经算完，只是在等前批，不算未算齐，否则批次会自我死锁。
UNRESOLVED_STATES = (IncidentState.QUEUED, IncidentState.FAILED)


def batch_of(seq_no: int) -> int:
    """登记序号（从 1 起）对应的批次号（从 1 起），十案一批。"""
    return (seq_no - 1) // BATCH_SIZE + 1


def register_order(db: DatabaseSession, berth_code: str):
    """在泊位行锁内领取登记顺序。重试走原序号，不会进入这里，因此不重复计案。"""
    berth = find(db, Berth, berth_code, lock=True)
    cycle_no, seq_no = berth.cycle_no, berth.next_seq
    berth.next_seq = seq_no + 1
    return cycle_no, seq_no, batch_of(seq_no)


def active_suspension(db: DatabaseSession, berth_code: str, *, lock: bool = False):
    """该泊位当前生效（未解除）的暂停；同一泊位至多一条。"""
    statement = select(BerthSuspension).where(
        BerthSuspension.berth_code == berth_code,
        BerthSuspension.lifted_at.is_(None),
    ).order_by(BerthSuspension.id.desc()).limit(1)
    if lock:
        statement = statement.with_for_update()
    return db.scalar(statement)


def batch_members(db: DatabaseSession, berth_code: str, cycle_no: int, batch_no: int, *, lock: bool = False):
    statement = select(DelayIncident).where(
        DelayIncident.berth_code == berth_code,
        DelayIncident.cycle_no == cycle_no,
        DelayIncident.batch_no == batch_no,
    ).order_by(DelayIncident.seq_no)
    if lock:
        statement = statement.with_for_update()
    return list(db.scalars(statement))


def earlier_batches_sealed(db: DatabaseSession, incident: DelayIncident) -> bool:
    """前批是否都已封口（各满十案）。

    批次按登记顺序切分：只有满十案的批次才构成闸门；不满十案的尾批永远不会有后批，
    因此这里只需校验已封口的批次是否还有未决案。
    """
    if incident.batch_no <= 1:
        return True
    first_seq = (incident.batch_no - 2) * BATCH_SIZE + 1
    last_seq = (incident.batch_no - 1) * BATCH_SIZE
    sealed_count = db.scalar(
        select(func.count()).select_from(DelayIncident).where(
            DelayIncident.berth_code == incident.berth_code,
            DelayIncident.cycle_no == incident.cycle_no,
            DelayIncident.seq_no.between(first_seq, last_seq),
        )
    )
    # 前批未填满十案（尾批场景），不构成闸门；否则要求前批已全部算出结论。
    if sealed_count != BATCH_SIZE * (incident.batch_no - 1):
        return True
    pending_in_earlier = db.scalar(
        select(func.count()).select_from(DelayIncident).where(
            DelayIncident.berth_code == incident.berth_code,
            DelayIncident.cycle_no == incident.cycle_no,
            DelayIncident.seq_no <= last_seq,
            DelayIncident.state.in_(UNRESOLVED_STATES),
        )
    )
    return pending_in_earlier == 0


def batch_complete(db: DatabaseSession, incident: DelayIncident) -> bool:
    """本批是否算齐：不存在仍排队或计算失败的案。BATCH_HELD 已算完，不计入未决。"""
    pending = db.scalar(
        select(func.count()).select_from(DelayIncident).where(
            DelayIncident.berth_code == incident.berth_code,
            DelayIncident.cycle_no == incident.cycle_no,
            DelayIncident.batch_no == incident.batch_no,
            DelayIncident.state.in_(UNRESOLVED_STATES),
        )
    )
    return pending == 0


def gate_state(db: DatabaseSession, incident: DelayIncident) -> IncidentState | None:
    """计算完成后决定案件落点。

    返回 None 表示可直接进入分类结论状态（间隔带复核或待发令）；
    返回 BATCH_HELD 表示前批尚未算齐，本案先等待，前批算齐后由 promote_held 放行。
    """
    if earlier_batches_sealed(db, incident) and batch_complete(db, incident):
        return None
    return IncidentState.BATCH_HELD


def promote_held(db: DatabaseSession, berth_code: str) -> list[DelayIncident]:
    """前批算齐后，把同泊位等待中的案按登记顺序放行到其真实结论状态。

    批次按序号推进：一批算齐才放行该批，避免后批越过前批。可被各状态变更入口重复调用，
    已放行的案不再处于 BATCH_HELD，天然幂等。
    """
    promoted = []
    held = list(db.scalars(
        select(DelayIncident).where(
            DelayIncident.berth_code == berth_code,
            DelayIncident.state == IncidentState.BATCH_HELD,
        ).order_by(DelayIncident.cycle_no, DelayIncident.seq_no).with_for_update()
    ))
    for incident in held:
        if gate_state(db, incident) is not None:
            continue
        if incident.in_margin_band:
            incident.state = IncidentState.MARGIN_REVIEW
        else:
            # 计算结论在挂起时已冻结，放行时恢复模型建议作为初始分类。
            incident.state = IncidentState.READY
            if not incident.decision and incident.result:
                incident.decision = DelayClass(incident.result['prediction'])
        audit(db, 'worker', incident.code, 'BATCH_RELEASED',
              f'第 {incident.cycle_no} 轮第 {incident.batch_no} 批已算齐，按登记顺序放行')
        promoted.append(incident)
    return promoted


def margin_count(db: DatabaseSession, incident: DelayIncident) -> int:
    """同批次已得出间隔带结论的案数（含等待中的间隔带案，它们的计算已经完成）。"""
    return db.scalar(
        select(func.count()).select_from(DelayIncident).where(
            DelayIncident.berth_code == incident.berth_code,
            DelayIncident.cycle_no == incident.cycle_no,
            DelayIncident.batch_no == incident.batch_no,
            DelayIncident.in_margin_band.is_(True),
        )
    )


def suspend_if_threshold(db: DatabaseSession, actor: str, incident: DelayIncident) -> BerthSuspension | None:
    """一批至少六案进入间隔带则暂停该泊位自动放行；同一轮次已有生效暂停则不重复暂停。"""
    if margin_count(db, incident) < SUSPEND_MARGIN_CASES:
        return None
    # 与发令、解除并发时以泊位行锁串行，先落定者生效。
    berth = find(db, Berth, incident.berth_code, lock=True)
    # 暂停解除会开启新一轮；旧轮次案件（如解除后才重试成功的失败案）不得重新触发暂停。
    if berth.cycle_no != incident.cycle_no:
        return None
    existing = active_suspension(db, incident.berth_code, lock=True)
    if existing:
        return existing
    total = len(batch_members(db, incident.berth_code, incident.cycle_no, incident.batch_no))
    reason = (f'第 {incident.cycle_no} 轮第 {incident.batch_no} 批已有 {SUSPEND_MARGIN_CASES} 案进入间隔带'
              f'（批次当前 {total} 案），暂停自动放行，待主管核实')
    suspension = BerthSuspension(
        berth_code=incident.berth_code, cycle_no=incident.cycle_no, batch_no=incident.batch_no,
        trigger_incident_code=incident.code, margin_cases=margin_count(db, incident),
        reason=reason, suspended_by=actor, suspended_at=utc_now(),
    )
    db.add(suspension)
    db.flush()
    audit(db, actor, incident.code, 'AUTO_RELEASE_SUSPENDED',
          f'{reason} · 触发批次第 {incident.batch_no} 批 · 暂停记录 #{suspension.id}')
    return suspension


def require_dispatch_allowed(db: DatabaseSession, incident: DelayIncident):
    """发令前核验暂停规则。暂停与发令并发时，泊位行锁保证先落定的为准。"""
    find(db, Berth, incident.berth_code, lock=True)
    suspension = active_suspension(db, incident.berth_code, lock=True)
    if not suspension:
        return
    # 间隔带案本身必须经主管人工复核签署，不属于自动放行范畴，签署后即可发令；
    # 暂停额外约束的是原本可自动流转发令的带外案。
    if incident.in_margin_band:
        return
    if not incident.auto_released_by:
        raise Rejected(
            'BERTH_SUSPENDED_SIGN_OFF',
            f'泊位 {incident.berth_code} 自动放行已暂停（第 {suspension.batch_no} 批触发），'
            '带外案须主管签放后才能发令',
            incident.code,
        )


def sign_off_auto(db: DatabaseSession, actor: str, code: str, note: str) -> DelayIncident:
    """暂停期间主管对未发令带外案的单独签放。"""
    incident = find(db, DelayIncident, code, lock=True)
    find(db, Berth, incident.berth_code, lock=True)
    suspension = active_suspension(db, incident.berth_code)
    if not suspension:
        raise Rejected('NO_ACTIVE_SUSPENSION', '该泊位当前没有生效的自动放行暂停，无需签放', code)
    if incident.in_margin_band:
        raise Rejected('MARGIN_SIGN_OFF', '间隔带案请在人工复核中签署分类，不走带外签放', code)
    if incident.state not in (IncidentState.READY, IncidentState.BATCH_HELD):
        raise Rejected('SIGN_OFF_STATE', '仅待发令或等待批次放行的带外案可以签放', code)
    if incident.auto_released_by:
        raise Rejected('ALREADY_SIGNED_OFF', f'该案已由 {incident.auto_released_by} 签放', code)
    incident.auto_released_by = actor
    incident.auto_released_at = utc_now()
    incident.auto_release_note = note
    audit(db, actor, code, 'AUTO_CASE_SIGNED_OFF',
          f'泊位 {incident.berth_code} 暂停期间带外签放 · {note}')
    return incident


def lift_suspension(db: DatabaseSession, actor: str, berth_code: str, note: str) -> BerthSuspension:
    """主管核实后解除暂停，并开启新一轮：此后新登记的案从第 1 轮第 1 案重新计数。"""
    berth = find(db, Berth, berth_code, lock=True)
    suspension = active_suspension(db, berth_code, lock=True)
    if not suspension:
        raise Rejected('NO_ACTIVE_SUSPENSION', '该泊位当前没有生效的自动放行暂停', berth_code)
    next_cycle = berth.cycle_no + 1
    suspension.lifted_by = actor
    suspension.lifted_at = utc_now()
    suspension.lift_note = note
    suspension.next_cycle_no = next_cycle
    berth.cycle_no = next_cycle
    berth.next_seq = 1
    audit(db, actor, berth_code, 'AUTO_RELEASE_RESUMED',
          f'第 {suspension.cycle_no} 轮暂停解除（触发批次第 {suspension.batch_no} 批）· '
          f'开启第 {next_cycle} 轮重新计数 · {note}')
    return suspension
