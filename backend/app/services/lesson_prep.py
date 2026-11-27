"""集体备课领域服务：备课记录登记、签阅、存档，含跨模块触发同行评议。

共同专题整组审核后，本模块承载「备课重签待办」生命周期：
待签阅（重签请求已发出、等回执）→ 教师用专题审核通过的整套材料签阅 →
待办上落下正式回执（重签回执字段）→ 才能存档推进备课。
回执等待超时可标记超时，之后重新打开原待办继续核对状态。
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.services.topic_group import parse_revision_ids
from app.store import store


class LessonPrepService(BaseModuleService):
    MODULE = "lesson_prep"
    ENTITY = "备课记录"
    OPERATION_TERM = "签阅记录"
    LIST_FIELDS = ["备课章节", "主备教师", "参与教师", "备课日期", "研讨要点", "教学资源",
                   "课后反思", "专题标识", "材料版本", "重签回执", "备注说明"]
    REQUIRED_FIELDS = ["备课章节", "主备教师", "参与教师"]
    STATUS_ORDER = ["草稿", "待签阅", "已超时", "已签阅", "已存档"]
    ACTION_RULES = {
        "签阅记录": "已签阅",
        "补充意见": "草稿",
        "存档记录": "已存档",
        "标记超时": "已超时",
        "重开待办": "待签阅",
    }
    NEGATIVE_ACTIONS = ["标记超时"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """执行动作，含跨模块联动：签阅后可触发同行评议；重签待办签阅前校验专题材料。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        # 签阅记录的幂等保护
        if action == "签阅记录":
            if entry.get("status") == "已签阅":
                return None, f"{self.ENTITY} {entry_id} 已是已签阅状态，无需重复签阅"
            if entry.get("status") == "已存档":
                return None, f"{self.ENTITY} {entry_id} 已存档，不可再签阅"
            # 重签待办：必须用该专题审核通过的整套材料签阅
            if str(entry.get("专题标识", "")).strip():
                error = self._check_topic_materials(entry)
                if error:
                    return None, error

        # 重签待办须签阅成功、拿到正式回执后才能存档推进备课
        if action == "存档记录":
            if str(entry.get("专题标识", "")).strip() and entry.get("status") != "已签阅":
                return None, (
                    f"{self.ENTITY} {entry_id} 是专题重签待办，须签阅成功、"
                    "待办上落下正式回执后才能存档推进备课"
                )

        # 标记超时：仅在等待回执（待签阅）时有效
        if action == "标记超时" and entry.get("status") != "待签阅":
            return None, (
                f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，"
                "仅等待回执的待办可标记超时"
            )

        # 重开待办：仅超时后可重开，且重开的是原待办本身
        if action == "重开待办" and entry.get("status") != "已超时":
            return None, (
                f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，"
                "仅已超时的待办可重新打开"
            )

        result_entry, message = super().run_action(entry_id, action)

        # 签阅成功：为重签待办补上正式回执；之后检查是否触发同行评议
        if result_entry and action == "签阅记录":
            topic_key = str(entry.get("专题标识", "")).strip()
            if topic_key:
                self._stamp_resign_receipt(entry, topic_key)
                message = f"{self.ENTITY}已签阅，正式回执已写入备课待办"
            reflection = entry.get("课后反思", "")
            teacher = entry.get("主备教师", "")
            if reflection and ("改进" in str(reflection) or "评议" in str(reflection)):
                self._maybe_create_peer_review(teacher, entry_id)

        # 重开原待办：同步专题当前材料版本，避免混用新旧要求
        if result_entry and action == "重开待办":
            topic_key = str(entry.get("专题标识", "")).strip()
            group = self._approved_topic_group(topic_key) if topic_key else None
            if group is not None:
                entry["材料版本"] = group.get("材料版本", "")
            note = f"[原待办于{date.today()}超时后重开，继续等待回执]"
            if note not in str(entry.get("备注说明", "")):
                entry["备注说明"] = f"{entry.get('备注说明', '')} {note}".strip()
            message = f"{self.ENTITY} {entry_id} 已重新打开，可继续核对回执状态"

        return result_entry, message

    # ---- 专题重签待办的签阅校验与回执 --------------------------------------

    def _approved_topic_group(self, topic_key: str) -> dict[str, Any] | None:
        """按专题标识精确查找已整组审核通过的专题组（不认章节名称）。"""
        for row in store.rows("topic_group"):
            if (str(row.get("专题标识", "")).strip() == topic_key
                    and row.get("status") == "审核通过"):
                return row
        return None

    def _check_topic_materials(self, entry: dict[str, Any]) -> str | None:
        """签阅前置校验：专题已整组通过、整套材料到齐、版本不混新旧。"""
        topic_key = str(entry.get("专题标识", "")).strip()
        group = self._approved_topic_group(topic_key)
        if group is None:
            return (
                f"专题{topic_key}尚未整组审核通过，不能用该专题材料签阅"
            )
        # 整套材料到齐校验：组内每门课的修订都必须已通过
        for rid in parse_revision_ids(group.get("包含修订")):
            rev = store.find("syllabus_rev", rid)
            if rev is None or rev.get("status") != "已通过":
                course = str(rev.get("课程名称", "")) if rev else f"修订{rid}"
                return (
                    f"专题{topic_key}整套材料未到齐：{course}的修订尚未通过，"
                    "仍需等待该课材料，暂不能签阅"
                )
        # 不得混用新旧要求：待办版本必须与专题当前审核通过版本一致
        current_version = str(group.get("材料版本", ""))
        if str(entry.get("材料版本", "")) != current_version:
            return (
                f"待办材料版本'{entry.get('材料版本', '')}'与专题{topic_key}当前版本"
                f"'{current_version}'不一致，不得混用新旧要求，请重开待办刷新版本后再签阅"
            )
        return None

    def _stamp_resign_receipt(self, entry: dict[str, Any], topic_key: str) -> None:
        """签阅成功后，把这笔重签的正式回执写到备课待办上。"""
        group = self._approved_topic_group(topic_key) or {}
        version = group.get("材料版本", entry.get("材料版本", ""))
        entry["材料版本"] = version
        entry["重签回执"] = (
            f"正式回执：专题{topic_key}（{version}）整套材料已签阅，"
            f"依据修订{group.get('包含修订', '')}，签阅日期{date.today()}"
        )

    # ---- 原有联动：签阅后按课后反思触发同行评议 ----------------------------

    def _maybe_create_peer_review(self, teacher: str, prep_id: int) -> None:
        """根据备课课后反思创建同行评议请求。"""
        review_rows = store.rows("peer_review")
        # 幂等检查
        for row in review_rows:
            existing_source = str(row.get("评议意见", ""))
            if f"lesson_prep:{prep_id}" in existing_source:
                return

        new_id = max((int(r.get("id", 0)) for r in review_rows), default=0) + 1
        review_rows.append({
            "id": new_id,
            "被评教师": teacher,
            "评议维度": "备课教学质量",
            "得分": "",
            "评议意见": f"由备课记录 #{prep_id} 课后反思触发 [lesson_prep:{prep_id}]",
            "评议人员": "",
            "评议日期": "",
            "是否匿名": "否",
            "备注说明": "备课签阅后自动生成评议请求",
            "status": "待汇总",
            "pending": True,
            "abnormal": False,
        })
