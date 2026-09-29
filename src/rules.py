from __future__ import annotations
import hashlib
import json
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
# 设备台账与离线补录（山谷火场）
DEVICE='设备'; TASK='任务'; OCCUPATION='设备占用'; DEVICE_ENTRY='设备记录'; INGEST='离线批次'
DEVICE_TYPES=['pump','chainsaw','troop_carrier']; DEVICE_TYPE_LABELS={'pump':'水泵组','chainsaw':'油锯','troop_carrier':'运兵车'}
ENTRY_DISPATCH='dispatch'; ENTRY_ARRIVAL='arrival'; ENTRY_EVACUATION='evacuation'; ENTRY_HANDOVER='handover'
ENTRY_KINDS=(ENTRY_DISPATCH,ENTRY_ARRIVAL,ENTRY_EVACUATION,ENTRY_HANDOVER); TERMINAL_ENTRY_KINDS=(ENTRY_EVACUATION,ENTRY_HANDOVER)
ENTRY_KIND_LABELS={ENTRY_DISPATCH:'派单',ENTRY_ARRIVAL:'到场',ENTRY_EVACUATION:'撤离',ENTRY_HANDOVER:'移交'}
DEVICE_ROLES=set(['field_commander','logistics']); TASK_CREATE_ROLES=set(['field_commander']); TASK_FINISH_ROLES=set(['field_commander','incident_commander']); INGEST_ROLES=set(['field_commander','logistics']); REVIEW_ROLES=set(['field_commander','incident_commander','logistics','viewer'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def device_label(device_type): return DEVICE_TYPE_LABELS.get(device_type,device_type)
def entry_label(kind): return ENTRY_KIND_LABELS.get(kind,kind)
# 补录条目内容指纹：同批次号重传时据此判定“同号只认首条，内容不一致留待核对”
_ENTRY_FINGERPRINT_FIELDS=(ENTRY_DISPATCH,ENTRY_ARRIVAL,ENTRY_EVACUATION,ENTRY_HANDOVER)
def entry_fingerprint(entry):
    kind=entry.get('kind')
    if kind not in ENTRY_KINDS: raise ValidationError("entry.kind不在允许范围内")
    material={'kind':kind,'device_code':entry.get('device_code'),'task_ref':entry.get('task_ref')}
    if kind==ENTRY_DISPATCH: material['task_title']=entry.get('task_title')
    else: material['receiver']=entry.get('receiver')
    raw=json.dumps(material,ensure_ascii=False,sort_keys=True,separators=(',',':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()
def batch_fingerprint(entries):
    return hashlib.sha256('|'.join(entry_fingerprint(e) for e in entries).encode('utf-8')).hexdigest()
def dispatch_conflict_message(conflicts):
    # conflicts: [{'device_code','device_type','task_ref'}]，说清冲突任务
    parts=[f"{device_label(c['device_type'])} {c['device_code']} 仍被未结束任务 {c['task_ref']} 占用" for c in conflicts]
    return "设备被未结束任务占用，整批不写："+"；".join(parts)
def equipment_close_blockers(open_occupations):
    # open_occupations: [{'device_code','device_type'}]，任务区关闭前每台设备必须有撤离或移交记录
    return [f"{device_label(o['device_type'])} {o['device_code']} 缺少撤离或移交记录" for o in open_occupations]
def task_finish_blockers(open_occupations):
    return [f"{device_label(o['device_type'])} {o['device_code']} 尚未撤离或移交" for o in open_occupations]
