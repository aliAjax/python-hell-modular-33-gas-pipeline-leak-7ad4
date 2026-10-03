from .domain import DomainError

ENTITY_TYPE = "pipeline_leak"
INITIAL_STATUS = "reported"
CREATE_ROLES = {"dispatcher", "responder"}
SOURCE_ROLES = {"dispatcher", "responder", "patrol", "sensor"}
ACTION_ROLES = {
    "verify": {"dispatcher", "responder"},
    "isolate": {"supervisor", "responder"},
    "repair": {"technician"},
    "pressure_test": {"technician"},
    "restore": {"supervisor"},
    "cancel": {"supervisor"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
# 对外现场操作必须携带依据版本，版本落后或依据对不上时拒绝执行
ACTION_REQUIRES_BASIS_VERSION = {"isolate", "repair", "pressure_test", "restore"}

# 评定依据字段：来源记录聚合到现场依据
BASIS_KEYS = ("pressure_drop_kpa", "sensor_value_ppm", "odor_reports")
# 来源更新后一并作废的处置结论
VOID_KEYS = ("verification", "valve_sequence", "repair", "pressure_test", "restoration")


def assess(payload):
    pressure = float(payload.get("pressure_drop_kpa", 0))
    ppm = float(payload.get("sensor_value_ppm", 0))
    odor = int(payload.get("odor_reports", 0))
    score = min(100.0, pressure * 2.0 + min(ppm, 500.0) * 0.1 + odor * 5.0)
    if score >= 75:
        level = "critical"
    elif score >= 45:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level}


def compute_basis(item, sources):
    """从来源记录聚合出现场依据：以最新观测值覆盖初报值。

    同一指标取观测时间最晚的来源记录；来源未覆盖的指标保留初报值。
    """
    basis = {key: item["payload"].get(key, 0) for key in BASIS_KEYS}
    for source in sorted(sources, key=lambda s: s["observed_at"], reverse=True):
        sp = source["payload"]
        for key in BASIS_KEYS:
            value = sp.get(key)
            if value is not None:
                basis[key] = value
    return basis


def basis_diff(current, submitted):
    """返回当前依据与提交依据之间的差异（仅列出不一致字段）。"""
    diff = {}
    for key in BASIS_KEYS:
        cur = current.get(key)
        sub = submitted.get(key) if submitted else None
        if cur != sub:
            diff[key] = {"current": cur, "submitted": sub}
    return diff


def void_conclusions(payload):
    """作废来源更新前的处置结论，返回被作废的字段列表。"""
    voided = []
    for key in VOID_KEYS:
        value = payload.get(key)
        if value:  # 仅作废有实际内容的结论（非空列表、非 None、非 False）
            del payload[key]
            voided.append(key)
    if payload.get("hazards_clear"):
        payload["hazards_clear"] = False
        if "hazards_clear" not in voided:
            voided.append("hazards_clear")
    return voided


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"reported", "verified"})
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能完成核验", 409)
        confirmed = bool(payload.get("field_confirmed"))
        if not confirmed:
            raise DomainError("field_confirmation_required", "需要现场确认", 409)
        current["verification"] = {"confirmed": True, "note": payload.get("note", "")}
        return "verified", current, {"verification": current["verification"]}

    if action == "isolate":
        _need_status(item, {"verified"})
        sequence = payload.get("valve_sequence")
        if not isinstance(sequence, list) or len(sequence) < 2:
            raise DomainError("valve_sequence_required", "至少需要提交两个阀门及顺序")
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能隔离", 409)
        if not all(isinstance(value, str) and value.strip() for value in sequence):
            raise DomainError("invalid_valve_sequence", "阀门顺序格式无效")
        current["valve_sequence"] = [value.strip() for value in sequence]
        return "isolated", current, {"valve_sequence": current["valve_sequence"]}

    if action == "repair":
        _need_status(item, {"isolated", "repaired"})
        work_order = _text(payload, "work_order")
        current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
        return "repaired", current, {"work_order": work_order}

    if action == "pressure_test":
        _need_status(item, {"repaired", "tested"})
        if not payload.get("test_passed"):
            raise DomainError("pressure_test_failed", "压力测试未通过，不能恢复供气", 409)
        pressure = float(payload.get("pressure_kpa", 0))
        minimum = float(payload.get("minimum_pressure_kpa", 100))
        if pressure < minimum:
            raise DomainError("pressure_below_threshold", "试验压力低于最低要求", 409)
        current["pressure_test"] = {"passed": True, "pressure_kpa": pressure, "minimum_pressure_kpa": minimum}
        return "tested", current, {"pressure_test": current["pressure_test"]}

    if action == "restore":
        _need_status(item, {"tested"})
        if not payload.get("hazards_clear"):
            raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
        if not current.get("pressure_test", {}).get("passed"):
            raise DomainError("pressure_test_missing", "缺少通过的压力测试", 409)
        current["hazards_clear"] = True
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"reported", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
