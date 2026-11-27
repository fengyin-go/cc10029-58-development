"""共同专题领域服务：把多门课程的大纲修订放成一组整组审核，并逐课签发备课重签待办。

专题身份只认「专题标识」，不认章节名称——同名但独立的章节（如丙课的同名章节）
不进组、不发待办、不混材料。整组审核通过后，为覆盖课程里的每门课各发一张
备课重签待办（lesson_prep），同一门课在同次更正里只存在一张可办重签单。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 备课重签待办的幂等标记：topic_group:{组id}|课程:{课程名}
RESIGN_MARKER_PREFIX = "重签待办"


def split_list(raw: Any) -> list[str]:
    """把 '；' '、' ',' 等分隔的名单拆成去空白后的列表。"""
    return [part.strip() for part in re.split(r"[;；、,，/]", str(raw or "")) if part.strip()]


def parse_revision_ids(raw: Any) -> list[int]:
    """解析包含修订，兼容 '4,5'、'syllabus_rev:4；syllabus_rev:5'、'#4、#5' 等写法。"""
    ids: list[int] = []
    for part in split_list(raw):
        token = part.split(":", 1)[-1].strip().lstrip("#")
        if token.isdigit():
            ids.append(int(token))
    return ids


def resign_marker(group_id: int, course: str) -> str:
    """重签待办幂等键：同一专题组、同一门课只此一张。"""
    return f"{RESIGN_MARKER_PREFIX} topic_group:{group_id}|课程:{course}"


class TopicGroupService(BaseModuleService):
    MODULE = "topic_group"
    ENTITY = "专题组"
    OPERATION_TERM = "整组审核"
    LIST_FIELDS = ["专题标识", "专题名称", "覆盖课程", "牵头教师", "材料版本", "包含修订", "审核日期", "备注说明"]
    REQUIRED_FIELDS = ["专题标识", "专题名称", "覆盖课程", "包含修订"]
    STATUS_ORDER = ["组稿中", "待审核", "审核通过", "已否决"]
    ACTION_RULES = {"提交整组": "待审核", "整组审核": "审核通过", "否决整组": "已否决"}
    NEGATIVE_ACTIONS = ["否决整组"]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = self.get_entry(entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        if action == "整组审核":
            # 允许「待审核」以及「审核通过」状态下再次批审；再次批审走幂等刷新，
            # 同门课同次更正仍然只留一张可办重签单。
            if entry.get("status") not in ("待审核", "审核通过"):
                return None, (
                    f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，"
                    "须先提交整组再审核"
                )
            error = self._validate_members(entry)
            if error:
                return None, error

        if action == "否决整组" and entry.get("status") != "待审核":
            return None, (
                f"{self.ENTITY} {entry_id} 当前状态为'{entry.get('status')}'，不可否决整组"
            )

        result_entry, message = super().run_action(entry_id, action)

        if result_entry and action == "整组审核":
            approved = self._approve_member_revisions(entry)
            summary = self._issue_resign_todos(entry)
            entry["审核日期"] = str(date.today())
            message = (
                f"{self.ENTITY}已整组审核：{approved}条课程修订一次性通过，{summary}"
            )

        return result_entry, message

    # ---- 整组审核内部步骤 -------------------------------------------------

    def _validate_members(self, entry: dict[str, Any]) -> str | None:
        """校验组内修订：必须存在、课程必须在覆盖课程内、状态可审批。

        丙课那种同名但独立的章节修订不在覆盖课程内，硬塞进来会被拦下，
        防止把别课材料混进本专题组。
        """
        courses = set(split_list(entry.get("覆盖课程")))
        ids = parse_revision_ids(entry.get("包含修订"))
        if not ids:
            return f"{self.ENTITY} {entry['id']} 未登记任何包含修订，无法整组审核"
        for rid in ids:
            rev = store.find("syllabus_rev", rid)
            if rev is None:
                return f"包含修订 syllabus_rev:{rid} 不存在，无法整组审核"
            course = str(rev.get("课程名称", ""))
            if course not in courses:
                return (
                    f"修订{rid}的课程'{course}'不在本专题组覆盖课程内，"
                    "同名独立章节的材料不得混入本组"
                )
            if rev.get("status") not in ("草稿", "待审批", "已通过"):
                return (
                    f"修订{rid}当前状态为'{rev.get('status')}'，不能随整组审核通过"
                )
        return None

    def _approve_member_revisions(self, entry: dict[str, Any]) -> int:
        """把组内修订逐条置为已通过。

        故意不走 syllabus_rev 的逐门审批联动（那条路按章节名模糊匹配备课记录，
        会误标丙课同名章节的备课）；共同专题只按专题标识精确发待办。
        """
        count = 0
        for rid in parse_revision_ids(entry.get("包含修订")):
            rev = store.find("syllabus_rev", rid)
            if rev is None:
                continue
            note = f"[专题组{entry['id']}整组审核通过]"
            if note not in str(rev.get("备注说明", "")):
                rev["备注说明"] = f"{rev.get('备注说明', '')} {note}".strip()
            if rev.get("status") != "已通过":
                rev["status"] = "已通过"
                rev["pending"] = False
                rev["abnormal"] = False
                count += 1
        return count

    def _issue_resign_todos(self, entry: dict[str, Any]) -> str:
        """整组审核通过后，为每门覆盖课程逐条发备课重签待办（幂等）。"""
        prep_rows = store.rows("lesson_prep")
        created, reopened, refreshed, done = 0, 0, 0, 0
        for course in split_list(entry.get("覆盖课程")):
            marker = resign_marker(int(entry["id"]), course)
            existing = next(
                (row for row in prep_rows if marker in str(row.get("备注说明", ""))),
                None,
            )
            if existing is None:
                prep_rows.append(self._new_resign_todo(entry, course, marker, prep_rows))
                created += 1
            elif existing.get("status") in ("草稿", "已超时"):
                # 再次批审时把未办结的原待办重开，而不是另发新单
                existing["status"] = "待签阅"
                existing["pending"] = True
                existing["abnormal"] = False
                existing["材料版本"] = entry.get("材料版本", "")
                existing["重签回执"] = ""
                reopened += 1
            elif existing.get("status") == "待签阅":
                existing["材料版本"] = entry.get("材料版本", "")
                refreshed += 1
            else:
                # 已签阅/已存档：同次更正只此一单，归档后仍是它，不再发新单
                done += 1
        parts = [f"已逐课发出{created}张备课重签待办"]
        if reopened:
            parts.append(f"重开原待办{reopened}张")
        if refreshed:
            parts.append(f"刷新在办待办{refreshed}张")
        if done:
            parts.append(f"{done}张已办结保持不动")
        return "，".join(parts)

    def _new_resign_todo(
        self,
        entry: dict[str, Any],
        course: str,
        marker: str,
        prep_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        topic_key = entry.get("专题标识", "")
        new_id = max((int(row.get("id", 0)) for row in prep_rows), default=0) + 1
        return {
            "id": new_id,
            "备课章节": f"{entry.get('专题名称', '')}（{course}）",
            "主备教师": self._course_teacher(entry, course),
            "参与教师": entry.get("牵头教师", ""),
            "备课日期": "",
            "研讨要点": "",
            "教学资源": f"专题{topic_key}整套材料（修订{entry.get('包含修订', '')}）",
            "课后反思": "",
            "专题标识": topic_key,
            "材料版本": entry.get("材料版本", ""),
            "重签回执": "",
            "备注说明": (
                f"[{marker}] 重签请求已发出，待教师用该专题审核通过的整套材料签阅"
            ),
            "status": "待签阅",
            "pending": True,
            "abnormal": False,
        }

    def _course_teacher(self, entry: dict[str, Any], course: str) -> str:
        """主备教师取该课程在组内修订稿的提出教师，缺省用牵头教师。"""
        for rid in parse_revision_ids(entry.get("包含修订")):
            rev = store.find("syllabus_rev", rid)
            if rev and str(rev.get("课程名称", "")) == course and rev.get("提出教师"):
                return str(rev["提出教师"])
        return str(entry.get("牵头教师", ""))
