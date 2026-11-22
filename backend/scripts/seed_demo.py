from datetime import datetime, timedelta

from sqlalchemy import func, select

from src.database import Session
from src.incidents import report
from src.models import Audit, Berth, BerthRequest, DelayIncident, DispatchReview, Role, TideWindow, Tug, TugAssignment, User
from src.planning import add_request, add_window, confirm, reserve_tug
from src.schemas import IncidentInput, RequestInput, TideInput
from src.security import password_hash

DEMO_PASSWORD = 'Harbor1074!'

# B07 第一批补充案件（DL-413 已占序号 1 且落在间隔带）。
# 前五个为间隔带案，凑满六件触发自动放行暂停；后四个为带外案，演示暂停期主管签放。
# 元组：(申请号, 计划引航, 引航偏离, 潮窗余量, 风速, 拖轮等候)
B07_BATCH = [
    ('BR-420', '10:40', 18.40, 26.70, 14.7, 11),
    ('BR-421', '11:30', 17.71, 24.54, 14.1, 12),
    ('BR-422', '12:20', 17.30, 24.40, 13.0, 13),
    ('BR-423', '13:10', 18.46, 25.80, 14.6, 11),
    ('BR-424', '14:00', 17.35, 24.67, 13.2, 13),
    ('BR-425', '14:50', 16.04, 27.36, 13.5, 14),
    ('BR-426', '15:40', 15.53, 29.59, 14.3, 10),
    ('BR-427', '16:30', 16.44, 26.70, 13.9, 12),
    ('BR-428', '17:20', 19.72, 21.97, 15.2, 13),
]
DEMO_DAY = '2026-11-21'


def _seed_b07_batch(db):
    for index, (code, eta_text, offset_min, reserve_min, wind, tug_wait) in enumerate(B07_BATCH, start=3):
        if db.get(BerthRequest, code):
            continue
        eta = datetime.strptime(f'{DEMO_DAY} {eta_text}', '%Y-%m-%d %H:%M')
        maneuver = 45
        window_code = f'TW-{index}'
        add_window(db, 'supervisor', TideInput(
            code=window_code, berth_code='B07',
            starts_at=eta - timedelta(minutes=20),
            ends_at=eta + timedelta(minutes=maneuver + reserve_min),
            max_draft_m=13.2, bulletin='TIDE-EAST-1121-PM.pdf'))
        add_request(db, 'operator', RequestInput(
            code=code, vessel=f'演示批次 {code[-3:]}', berth_code='B07', window_code=window_code,
            eta=eta, maneuver_minutes=maneuver, draft_m=11.9))
        db.flush()
        reserve_tug(db, 'operator', code, 'TG23')
        confirm(db, 'supervisor', code)
        report(db, 'operator', IncidentInput(
            code=f'DL-{code[-3:]}', request_code=code,
            radio_reference=f'VHF-12/{eta_text.replace(":", "")}-19',
            observed_at=eta + timedelta(minutes=offset_min),
            wind_kn=wind, tug_wait_min=tug_wait,
            event_note=f'{code} 现场延误观测，进入 B07 第一批批次计数。'))
        db.flush()


def seed():
    with Session.begin() as db:
        # worker 是批次暂停与模型审计的系统落库身份（外键要求 users 中存在），不用于交互登录。
        for username, role in [('operator', Role.OPERATOR), ('supervisor', Role.SUPERVISOR),
                               ('viewer', Role.VIEWER), ('worker', Role.SUPERVISOR)]:
            if not db.get(User, username):
                db.add(User(username=username, role=role, password_hash=password_hash(DEMO_PASSWORD)))
        for code, label in [('B07', '东七码头'), ('B09', '东九散货泊位'), ('B12', '南十二泊位')]:
            if not db.get(Berth, code):
                db.add(Berth(code=code, label=label))
        for code, label, strength in [('TG12', '海勤 12', 58), ('TG18', '海勤 18', 65), ('TG23', '远协 23', 72)]:
            if not db.get(Tug, code):
                db.add(Tug(code=code, label=label, bollard_pull_t=strength))
        for code, berth, start, end, draft in [
            ('TW-071', 'B07', '07:45', '09:10', 11.8), ('TW-072', 'B07', '09:10', '10:45', 13.2),
            ('TW-091', 'B09', '08:30', '11:40', 12.5), ('TW-121', 'B12', '07:25', '12:10', 13.5),
        ]:
            if not db.get(TideWindow, code):
                add_window(db, 'supervisor', TideInput(code=code, berth_code=berth,
                           starts_at=f'2026-11-21T{start}', ends_at=f'2026-11-21T{end}',
                           max_draft_m=draft, bulletin='TIDE-EAST-1121-AM.pdf'))
        db.flush()
        for code, vessel, berth, window, eta, minutes, draft in [
            ('BR-410', '海岚 27', 'B07', 'TW-071', '08:50', 45, 12.4),
            ('BR-411', '临海 308', 'B09', 'TW-091', '09:25', 55, 11.8),
            ('BR-412', '松航 16', 'B12', 'TW-121', '11:00', 45, 12.5),
            ('BR-413', '远桥 75', 'B07', 'TW-072', '09:30', 50, 11.9),
        ]:
            if db.get(BerthRequest, code):
                continue
            add_request(db, 'operator', RequestInput(code=code, vessel=vessel, berth_code=berth, window_code=window,
                        eta=f'2026-11-21T{eta}', maneuver_minutes=minutes, draft_m=draft))
            db.flush()
            if code in ('BR-411', 'BR-413'):
                reserve_tug(db, 'operator', code, 'TG12' if code == 'BR-411' else 'TG18')
                confirm(db, 'supervisor', code)
        if not db.get(DelayIncident, 'DL-413'):
            report(db, 'operator', IncidentInput(code='DL-413', request_code='BR-413',
                   radio_reference='VHF-12/0930-19', observed_at='2026-11-21T09:48', wind_kn=14,
                   tug_wait_min=12, event_note='引航员通报拖轮还在回转区，现场请示是否申请待命。'))
        db.flush()
        # B07 第一批补足十案：DL-413 为序号 1（间隔带），再补 5 件间隔带与 4 件带外案。
        _seed_b07_batch(db)
        db.flush()
        tables = (User, Berth, Tug, TideWindow, BerthRequest, TugAssignment, DelayIncident, DispatchReview, Audit)
        return {table.__tablename__: db.scalar(select(func.count()).select_from(table)) for table in tables}


if __name__ == '__main__':
    print(seed())
