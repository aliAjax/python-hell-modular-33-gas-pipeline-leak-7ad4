from .domain import ConflictError, DomainError

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
ACTION_REQUIRES_VERSION = {"isolate", "repair", "pressure_test", "restore", "cancel"}
# 对外提交的处置动作必须携带依据版本
ACTION_REQUIRES_BASIS = {"isolate", "repair", "pressure_test", "restore"}

# 来源更新后不再允许改动的终态
SOURCE_LOCKED_STATUS = {"restored", "cancelled"}


def assess(values):
    pressure = float(values.get("pressure_drop_kpa", 0) or 0)
    ppm = float(values.get("sensor_value_ppm", 0) or 0)
    odor = int(values.get("odor_reports", 0) or 0)
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


def _evidence_identity(evidence):
    return evidence["source_type"], evidence["external_id"]


def build_basis(initial, active_sources):
    """把初报与全部有效来源现场依据聚合成一次评定依据。"""
    evidence = [{
        "kind": "dispatch",
        "source_type": "dispatch_report",
        "external_id": initial["reporter"],
        "observed_at": initial["reported_at"],
        "pressure_drop_kpa": initial["pressure_drop_kpa"],
        "sensor_value_ppm": initial["sensor_value_ppm"],
        "odor_reports": initial["odor_reports"],
    }]
    for source in sorted(active_sources, key=lambda row: (row["observed_at"], row["id"])):
        record = source["payload"]
        evidence.append({
            "kind": "source",
            "source_type": source["source_type"],
            "external_id": source["external_id"],
            "source_id": source["id"],
            "observed_at": source["observed_at"],
            "pressure_drop_kpa": record.get("pressure_drop_kpa"),
            "sensor_value_ppm": record.get("sensor_value_ppm"),
            "odor_reports": int(record.get("odor_reports", 0) or 0),
        })
    values = {
        "pressure_drop_kpa": max([float(item["pressure_drop_kpa"] or 0) for item in evidence] or [0]),
        "sensor_value_ppm": max([float(item["sensor_value_ppm"] or 0) for item in evidence] or [0]),
        "odor_reports": sum(int(item["odor_reports"] or 0) for item in evidence),
    }
    result = assess(values)
    values.update(result)
    values["evidence"] = evidence
    return values


def void_conclusions(status, current):
    """依据变更后作废已有的核验/隔离/抢修/试压结论，并逐条留痕。"""
    voided = list(current.get("voided_conclusions", []))
    trace = []

    def _void(kind, snapshot, reason="basis_superseded"):
        entry = {
            "kind": kind,
            "basis_revision": snapshot.get("basis_revision"),
            "reason": reason,
            "record": snapshot,
        }
        voided.append(entry)
        trace.append(entry)

    if current.get("assessment"):
        _void("assessment", current["assessment"])
    if current.get("verification"):
        snapshot = dict(current["verification"])
        snapshot["assessment"] = current.get("assessment")
        _void("verification", snapshot)
    if current.get("valve_sequence"):
        _void("isolation", {"valve_sequence": current["valve_sequence"]})
    if current.get("repair"):
        _void("repair", current["repair"])
    if current.get("pressure_test"):
        _void("pressure_test", current["pressure_test"])

    current.pop("assessment", None)
    current.pop("verification", None)
    current.pop("valve_sequence", None)
    current.pop("repair", None)
    current.pop("pressure_test", None)
    current["hazards_clear"] = False
    current["voided_conclusions"] = voided
    return trace


def basis_diff(expected, current, expected_revision=None, current_revision=None):
    """给出两份依据版本之间的差异，供冲突响应展示。"""
    diff = {
        "expected_revision": expected_revision if expected_revision is not None else expected.get("revision"),
        "current_revision": current_revision if current_revision is not None else current.get("revision"),
        "changed_fields": [],
        "evidence_added": [],
        "evidence_removed": [],
        "evidence_replaced": [],
    }
    for field in ("pressure_drop_kpa", "sensor_value_ppm", "odor_reports", "level", "score"):
        old_value = expected.get(field)
        new_value = current.get(field)
        if old_value != new_value:
            diff["changed_fields"].append({"field": field, "expected": old_value, "current": new_value})
    old_map = {_evidence_identity(item): item for item in expected.get("evidence", [])}
    new_map = {_evidence_identity(item): item for item in current.get("evidence", [])}
    for key, item in new_map.items():
        if key not in old_map:
            diff["evidence_added"].append(item)
    for key, item in old_map.items():
        if key not in new_map:
            diff["evidence_removed"].append(item)
        elif item != new_map[key]:
            changes = {}
            for field in ("observed_at", "pressure_drop_kpa", "sensor_value_ppm", "odor_reports"):
                if item.get(field) != new_map[key].get(field):
                    changes[field] = {"expected": item.get(field), "current": new_map[key].get(field)}
            if changes:
                diff["evidence_replaced"].append({"identity": {"source_type": key[0], "external_id": key[1]}, "changes": changes})
    return diff


def check_basis(current_payload, expected_snapshot, current_basis, expected_revision, expected_digest=None):
    current_revision = int(current_payload.get("basis_revision", 1))
    if expected_revision is None:
        raise DomainError("basis_version_required", "该操作需要携带依据版本 expected_basis_revision", 400)
    expected_revision = int(expected_revision)
    digest_match = expected_digest is None or expected_digest == current_payload.get("basis_digest")
    if expected_revision == current_revision and digest_match:
        return current_revision
    reason = "basis_digest_mismatch" if expected_revision == current_revision else "basis_revision_stale"
    raise ConflictError(
        "basis_conflict",
        "提交依据版本为 %s，当前有效依据版本为 %s，请按新依据重新处置" % (expected_revision, current_revision),
        {"reason": reason, "basis_diff": basis_diff(
            expected_snapshot, current_basis, expected_revision, current_revision)},
    )


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
    basis_revision = int(current.get("basis_revision", 1))
    basis_digest = current.get("basis_digest")

    if action == "verify":
        _need_status(item, {"reported", "verified"})
        if current.get("valve_status_conflict"):
            raise DomainError("valve_status_conflict", "阀门状态存在冲突，不能完成核验", 409)
        confirmed = bool(payload.get("field_confirmed"))
        if not confirmed:
            raise DomainError("field_confirmation_required", "需要现场确认", 409)
        assessment = item["basis_current"]
        current["assessment"] = {
            "score": assessment["score"],
            "level": assessment["level"],
            "basis_revision": basis_revision,
            "basis_digest": basis_digest,
        }
        current["verification"] = {
            "confirmed": True,
            "note": payload.get("note", ""),
            "basis_revision": basis_revision,
            "basis_digest": basis_digest,
        }
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

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
        return "isolated", current, {
            "valve_sequence": current["valve_sequence"],
            "basis_revision": basis_revision,
            "basis_digest": basis_digest,
        }

    if action == "repair":
        _need_status(item, {"isolated", "repaired"})
        work_order = _text(payload, "work_order")
        current["repair"] = {"work_order": work_order, "result": payload.get("result", "completed")}
        return "repaired", current, {
            "work_order": work_order,
            "basis_revision": basis_revision,
            "basis_digest": basis_digest,
        }

    if action == "pressure_test":
        _need_status(item, {"repaired", "tested"})
        if not payload.get("test_passed"):
            raise DomainError("pressure_test_failed", "压力测试未通过，不能恢复供气", 409)
        pressure = float(payload.get("pressure_kpa", 0))
        minimum = float(payload.get("minimum_pressure_kpa", 100))
        if pressure < minimum:
            raise DomainError("pressure_below_threshold", "试验压力低于最低要求", 409)
        current["pressure_test"] = {"passed": True, "pressure_kpa": pressure, "minimum_pressure_kpa": minimum}
        return "tested", current, {
            "pressure_test": current["pressure_test"],
            "basis_revision": basis_revision,
            "basis_digest": basis_digest,
        }

    if action == "restore":
        _need_status(item, {"tested"})
        if not payload.get("hazards_clear"):
            raise DomainError("hazards_not_clear", "现场危险条件尚未解除", 409)
        if not current.get("pressure_test", {}).get("passed"):
            raise DomainError("pressure_test_missing", "缺少通过的压力测试", 409)
        current["hazards_clear"] = True
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {
            "restoration": current["restoration"],
            "basis_revision": basis_revision,
            "basis_digest": basis_digest,
        }

    if action == "cancel":
        _need_status(item, {"reported", "verified"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
