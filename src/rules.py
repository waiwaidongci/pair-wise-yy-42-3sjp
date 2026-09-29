from __future__ import annotations
import hashlib
import json
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
# 现场离线补录：水泵组、油锯、运兵车
EQUIPMENT_KINDS={'pump':'水泵组','chainsaw':'油锯','transport':'运兵车'}
EQUIPMENT_KIND_ALIASES={v:k for k,v in EQUIPMENT_KINDS.items()}
FIELD_OPS=('arrival','dispatch','evacuation','handover')
FIELD_OP_LABELS={'arrival':'到场','dispatch':'派单','evacuation':'撤离','handover':'移交'}
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
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

# ---- 离线补录判定（纯函数，不碰台账） ----
def normalize_equipment_kind(value):
    """设备类型既接受 pump/chainsaw/transport，也接受中文名称。"""
    if isinstance(value,str):
        text=value.strip()
        if text in EQUIPMENT_KINDS: return text
        if text in EQUIPMENT_KIND_ALIASES: return EQUIPMENT_KIND_ALIASES[text]
    raise ValidationError("equipment_kind必须是pump/chainsaw/transport之一")

def normalize_field_op(value):
    if isinstance(value,str) and value.strip() in FIELD_OPS: return value.strip()
    raise ValidationError("op必须是arrival/dispatch/evacuation/handover之一")

def entry_fingerprint(entry):
    """同号只认首条：指纹覆盖设备、动作与内容，内容变化即可识别。"""
    payload={k:entry.get(k) for k in ("equipment_code","op","to_item_id","detail")}
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True,default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def batch_fingerprint(entries):
    """整批内容指纹：任一条到场/派单/撤离/移交记录变化都会不同。"""
    joined="\n".join(entry_fingerprint(e) for e in entries)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()

def classify_first_seen(first_status,first_fingerprint,fingerprint):
    """重复现场批次号的判定：首条接受后同号内容必须一致，否则留待核对。"""
    if first_status=='rejected':
        return 'retry', None
    if first_fingerprint==fingerprint:
        return 'duplicate', "批次号已处理，记录完全一致"
    return 'mismatch', "同号首条记录内容不一致，留待核对"

def dispatch_conflict(active,target_item_id):
    """设备被另一条未结束任务占用时，返回冲突说明；同任务重发按幂等跳过。"""
    if not active: return None
    if active['item_id']==target_item_id: return None
    return (f"设备{active['equipment_code']}已被未结束任务{active['item_id']}"
            f"（{active.get('item_title') or '未命名任务区'}）占用")

def equipment_blockers(open_occupations,arrivals_open):
    """任务区关闭前，每台设备都要有撤离或移交记录。"""
    blockers=[]
    codes=sorted({o['equipment_code'] for o in open_occupations} |
                 {a['equipment_code'] for a in arrivals_open})
    for code in codes:
        occupation=next((o for o in open_occupations if o['equipment_code']==code),None)
        arrived=any(a['equipment_code']==code for a in arrivals_open)
        if occupation and arrived:
            blockers.append(f"设备{code}缺少撤离或移交记录")
        elif occupation:
            blockers.append(f"设备{code}已派入但缺少撤离或移交记录")
        elif arrived:
            blockers.append(f"设备{code}已到场但缺少撤离或移交记录")
    return blockers
