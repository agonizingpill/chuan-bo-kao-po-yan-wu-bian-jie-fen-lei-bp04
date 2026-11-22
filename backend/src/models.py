from enum import StrEnum

from sqlalchemy import Boolean, CheckConstraint, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, mapped_column


class Role(StrEnum):
    OPERATOR = 'operator'
    SUPERVISOR = 'supervisor'
    VIEWER = 'viewer'


class RequestState(StrEnum):
    REQUESTED = 'requested'
    TUG_RESERVED = 'tug_reserved'
    CONFIRMED = 'confirmed'
    COMPLETED = 'completed'


class IncidentState(StrEnum):
    QUEUED = 'queued'
    FAILED = 'failed'
    BATCH_HELD = 'batch_held'
    MARGIN_REVIEW = 'margin_review'
    READY = 'ready'
    ACTIONED = 'actioned'
    CLOSED = 'closed'


class DelayClass(StrEnum):
    ROUTINE = 'routine'
    PRIORITY = 'priority'


class Action(StrEnum):
    MONITOR = 'monitor'
    RETIME = 'retime'
    STANDBY = 'standby'


def enum(kind):
    return Enum(kind, values_callable=lambda members: [member.value for member in members])


# 同一泊位按登记顺序十案一批；一批至少六案进入间隔带即暂停该泊位自动放行。
BATCH_SIZE = 10
SUSPEND_MARGIN_CASES = 6


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    username = mapped_column(String(32), primary_key=True)
    role = mapped_column(enum(Role), nullable=False)
    password_hash = mapped_column(String(180), nullable=False)


class Berth(Base):
    __tablename__ = 'berths'
    code = mapped_column(String(12), primary_key=True)
    label = mapped_column(String(60), nullable=False)
    # 暂停解除后从新登记的案重新计数：轮次加一，序号从 1 重新开始。
    cycle_no = mapped_column(Integer, nullable=False, default=1, server_default=text('1'))
    next_seq = mapped_column(Integer, nullable=False, default=1, server_default=text('1'))


class TideWindow(Base):
    __tablename__ = 'tide_windows'
    __table_args__ = (CheckConstraint('ends_at > starts_at AND max_draft_m > 0'),)
    code = mapped_column(String(32), primary_key=True)
    berth_code = mapped_column(ForeignKey('berths.code'), nullable=False)
    starts_at = mapped_column(DateTime, nullable=False)
    ends_at = mapped_column(DateTime, nullable=False)
    max_draft_m = mapped_column(Float, nullable=False)
    bulletin = mapped_column(String(120), nullable=False)


class Tug(Base):
    __tablename__ = 'tugs'
    code = mapped_column(String(12), primary_key=True)
    label = mapped_column(String(60), nullable=False)
    bollard_pull_t = mapped_column(Integer, nullable=False)


class BerthRequest(Base):
    __tablename__ = 'berth_requests'
    __table_args__ = (
        CheckConstraint('draft_m > 0 AND maneuver_minutes > 0'),
        Index('berth_schedule_idx', 'berth_code', 'state', 'eta'),
    )
    code = mapped_column(String(32), primary_key=True)
    vessel = mapped_column(String(80), nullable=False)
    berth_code = mapped_column(ForeignKey('berths.code'), nullable=False)
    window_code = mapped_column(ForeignKey('tide_windows.code'), nullable=False)
    eta = mapped_column(DateTime, nullable=False)
    maneuver_minutes = mapped_column(Integer, nullable=False)
    draft_m = mapped_column(Float, nullable=False)
    state = mapped_column(enum(RequestState), nullable=False, default=RequestState.REQUESTED)


class TugAssignment(Base):
    __tablename__ = 'tug_assignments'
    __table_args__ = (
        CheckConstraint('ends_at > starts_at'),
        Index('tug_calendar_idx', 'tug_code', 'released_at', 'starts_at', 'ends_at'),
    )
    id = mapped_column(Integer, primary_key=True)
    request_code = mapped_column(ForeignKey('berth_requests.code'), nullable=False)
    tug_code = mapped_column(ForeignKey('tugs.code'), nullable=False)
    starts_at = mapped_column(DateTime, nullable=False)
    ends_at = mapped_column(DateTime, nullable=False)
    released_at = mapped_column(DateTime)


class DelayIncident(Base):
    __tablename__ = 'delay_incidents'
    __table_args__ = (
        UniqueConstraint('berth_code', 'cycle_no', 'seq_no', name='incident_berth_sequence_uq'),
        Index('incident_berth_batch_idx', 'berth_code', 'cycle_no', 'batch_no', 'seq_no'),
        Index('incident_state_idx', 'state'),
    )
    code = mapped_column(String(32), primary_key=True)
    request_code = mapped_column(ForeignKey('berth_requests.code'), nullable=False, unique=True)
    berth_code = mapped_column(ForeignKey('berths.code'), nullable=False)
    # 登记顺序在同一泊位、同一轮次内单调递增；重试复用原序号，不重复计案。
    cycle_no = mapped_column(Integer, nullable=False)
    seq_no = mapped_column(Integer, nullable=False)
    batch_no = mapped_column(Integer, nullable=False)
    radio_reference = mapped_column(String(64), nullable=False, unique=True)
    observed_at = mapped_column(DateTime, nullable=False)
    event_note = mapped_column(String(300), nullable=False)
    state = mapped_column(enum(IncidentState), nullable=False, default=IncidentState.QUEUED, index=True)
    in_margin_band = mapped_column(Boolean, nullable=False, default=False, server_default=text('0'))
    # 暂停期间带外（非间隔带）案须主管签放后才能发布指令。
    auto_released_by = mapped_column(ForeignKey('users.username'))
    auto_released_at = mapped_column(DateTime)
    auto_release_note = mapped_column(String(300))
    features = mapped_column(JSON, nullable=False)
    input_digest = mapped_column(String(64), nullable=False)
    result = mapped_column(JSON)
    error = mapped_column(String(180))
    attempts = mapped_column(Integer, nullable=False, default=0)
    decision = mapped_column(enum(DelayClass))
    decided_by = mapped_column(ForeignKey('users.username'))
    decision_reason = mapped_column(String(300))
    decided_at = mapped_column(DateTime)


class BerthSuspension(Base):
    __tablename__ = 'berth_suspensions'
    __table_args__ = (
        Index('berth_suspension_open_idx', 'berth_code', 'lifted_at'),
    )
    id = mapped_column(Integer, primary_key=True)
    berth_code = mapped_column(ForeignKey('berths.code'), nullable=False)
    cycle_no = mapped_column(Integer, nullable=False)
    batch_no = mapped_column(Integer, nullable=False)
    # 触发暂停的那一案（该批次第六件间隔带案），案件页面据此反查触发批次。
    trigger_incident_code = mapped_column(ForeignKey('delay_incidents.code'), nullable=False)
    margin_cases = mapped_column(Integer, nullable=False)
    reason = mapped_column(String(300), nullable=False)
    suspended_by = mapped_column(ForeignKey('users.username'), nullable=False)
    suspended_at = mapped_column(DateTime, nullable=False)
    lifted_by = mapped_column(ForeignKey('users.username'))
    lifted_at = mapped_column(DateTime)
    lift_note = mapped_column(String(300))
    next_cycle_no = mapped_column(Integer)


class DispatchReview(Base):
    __tablename__ = 'dispatch_reviews'
    incident_code = mapped_column(ForeignKey('delay_incidents.code'), primary_key=True)
    action = mapped_column(enum(Action), nullable=False)
    instruction = mapped_column(String(300), nullable=False)
    issued_by = mapped_column(ForeignKey('users.username'), nullable=False)
    issued_at = mapped_column(DateTime, nullable=False)
    all_fast_at = mapped_column(DateTime)
    outcome = mapped_column(String(300))
    closed_by = mapped_column(ForeignKey('users.username'))
    closed_at = mapped_column(DateTime)


class Audit(Base):
    __tablename__ = 'audit_events'
    id = mapped_column(Integer, primary_key=True)
    event_at = mapped_column(DateTime, nullable=False, server_default=text('CURRENT_TIMESTAMP'))
    actor = mapped_column(String(32), nullable=False)
    reference = mapped_column(String(100), nullable=False, index=True)
    event = mapped_column(String(48), nullable=False)
    note = mapped_column(String(400), nullable=False)
