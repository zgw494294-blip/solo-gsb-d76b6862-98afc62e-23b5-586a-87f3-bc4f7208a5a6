"""自动发出后端回归测试（FastAPI TestClient + 独立临时数据库 + 可控时钟）。

可控时钟：测试把 main.auto_clock 替换为手动推进的假时钟，并关闭后台节拍
（main.auto_background = False），改为手动调用 main.auto_tick() 驱动定时
任务，从而精确验证计时与手工操作的交错时序。

覆盖：默认关闭旧调用不变；设置规则（仅演出中、携带版本号、受控制席保护、
成功版本 +1、非法间隔 400、非演出中/过期 409、同版本竞争）；等满间隔自动
发出一条（沿用发出快照、版本推进与换轮规则）、发出后重新计满间隔、每次
只发一条不补发积压；暂停冻结剩余时间、恢复续计、暂停中开启冻结在完整
间隔；核验门槛（到期未就绪继续等、两工种就绪后立即发出）；手工确认 /
撤回 / 改写当前提示 / 换队首重排重新计时，队首不变的重排与编辑其他提示
不重置；接管 / 归档 / 重置 / 完成 / 服务重启关闭自动模式；与手工操作竞争
不重复发出、不跳条；写入失败不推进、不重置计时、不改版本，恢复后立即
重试；状态视图（剩余秒数、等待就绪、关闭视图）。

运行：python3 tests/auto.test.py
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="cue-auto-test-")
os.environ["DB_PATH"] = os.path.join(_tmp, "cues.db")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402

failures = 0


def check(cond, msg):
    global failures
    if cond:
        print("  ✓ " + msg)
    else:
        failures += 1
        print("  ✗ " + msg)


class FakeClock:
    """可控时钟：单调秒，测试手动推进。"""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


clock = FakeClock()
main.auto_clock = clock  # 注入可控时钟
main.auto_background = False  # 关闭后台节拍，测试手动驱动 auto_tick

CUES3 = [
    {"number": "A1", "content": "灯光渐亮"},
    {"number": "A2", "content": "音乐起"},
    {"number": "A3", "content": "幕落"},
]
CUES4 = CUES3 + [{"number": "A4", "content": "谢幕"}]
CUES5 = CUES4 + [{"number": "A5", "content": "散场灯"}]


def fresh_db(name):
    main.DB_PATH = os.path.join(_tmp, name)
    main._auto_timer_stop()  # 隔离上一用例的内存计时
    clock.t = 1000.0


def setup_show(c, cues=None):
    r = c.post("/api/setup", json={"cues": cues or CUES3})
    assert r.status_code == 200, r.text
    return r.json()


def set_auto(c, version, enabled, interval, headers=None):
    return c.post(
        "/api/auto",
        json={"version": version, "enabled": enabled, "interval": interval},
        headers=headers,
    )


def enable_auto(c, state, interval=5, headers=None):
    r = set_auto(c, state["version"], True, interval, headers)
    assert r.status_code == 200, r.text
    return r.json()


def tick():
    return main.auto_tick()


def state(c):
    return c.get("/api/state").json()


# ------------------------------------------------------------- 默认与设置规则

def test_default_off():
    fresh_db("a01.db")
    with TestClient(main.app) as c:
        print("A1 默认关闭：状态含关闭视图，后台节拍不发出，旧调用不变")
        s = state(c)
        check(
            s["auto"] == {
                "enabled": False,
                "interval": 10,
                "remaining_seconds": None,
                "waiting_ready": False,
            },
            "初始 auto 为固定关闭视图（默认间隔 10 秒）",
        )
        s = setup_show(c)
        check(s["auto"]["enabled"] is False, "开场后自动模式仍关闭（默认关闭）")
        clock.advance(3600)
        check(tick() is False, "未开启时后台节拍不发出")
        s = state(c)
        check(s["remaining"] == 3 and not s["issued"], "未开启时节拍不推进任何提示")
        # 关闭时手工确认等旧接口行为不变
        r = c.post("/api/confirm", json={"version": s["version"]})
        check(r.status_code == 200, "关闭自动时手工确认不变（旧调用不变）")


def test_configure_rules():
    fresh_db("a02.db")
    with TestClient(main.app) as c:
        print("A2 设置规则：仅演出中、版本 +1、非法间隔 400、过期 409、同版本竞争")
        v = state(c)["version"]
        r = set_auto(c, v, True, 5)
        check(r.status_code == 409, "开场前（非演出中）设置返回 409")
        check(r.json()["state"]["auto"]["enabled"] is False, "409 附最新状态")
        s = setup_show(c)
        v = s["version"]
        bad = [0, 61, -5, 1.5, "5", True, None]
        codes = [set_auto(c, v, True, b).status_code for b in bad]
        check(codes == [400] * len(bad),
              "非法间隔（0/61/负数/小数/字符串/布尔/缺失）一律 400")
        s = state(c)
        check(s["version"] == v and s["auto"]["enabled"] is False,
              "非法间隔不改版本、不开启")
        r = set_auto(c, v + 99, True, 5)
        check(r.status_code == 409, "过期版本设置返回 409 附最新状态")
        check(state(c)["auto"]["enabled"] is False, "过期设置不生效")
        r = set_auto(c, v, True, 5)
        check(r.status_code == 200, "演出中合法设置成功")
        s = r.json()
        check(s["version"] == v + 1, "成功版本 +1")
        check(s["auto"]["enabled"] is True and s["auto"]["interval"] == 5,
              "状态回报开关与间隔")
        check(4.9 < s["auto"]["remaining_seconds"] <= 5.0,
              "开启后从完整间隔开始计时")
        r = set_auto(c, s["version"], True, 1)
        check(r.status_code == 200, "间隔 1 秒合法（下边界）")
        r = set_auto(c, r.json()["version"], True, 60)
        check(r.status_code == 200, "间隔 60 秒合法（上边界）")
        v2 = r.json()["version"]
        r1 = set_auto(c, v2, True, 7)
        r2 = set_auto(c, v2, True, 9)
        check(sorted([r1.status_code, r2.status_code]) == [200, 409],
              "同版本两个设置只有一个生效")
        v3 = (r1 if r1.status_code == 200 else r2).json()["version"]
        r = set_auto(c, v3, False, 9)
        check(r.status_code == 200 and r.json()["auto"]["enabled"] is False,
              "关闭成功")
        check(r.json()["auto"]["remaining_seconds"] is None, "关闭后无剩余秒数")
        clock.advance(100)
        check(tick() is False, "关闭后后台不发出")


def test_control_seat_and_takeover():
    fresh_db("a03.db")
    with TestClient(main.app) as c:
        print("A3 控制席保护：无凭据 403；接管关闭自动模式")
        s = setup_show(c)
        r = c.post("/api/control/enable", json={"version": s["version"]})
        token = r.json()["control_token"]
        v = r.json()["version"]
        r = set_auto(c, v, True, 5)
        check(r.status_code == 403, "启用控制席后无凭据设置返回 403")
        check(state(c)["auto"]["enabled"] is False, "403 不开启、数据不变")
        r = set_auto(c, v, True, 5, headers={"X-Control-Token": token})
        check(r.status_code == 200, "当前控制席凭据可设置")
        v = r.json()["version"]
        clock.advance(3)
        r = c.post("/api/control/takeover", json={"version": v})
        check(r.status_code == 200, "接管成功")
        check(r.json()["auto"]["enabled"] is False, "接管关闭自动模式")
        clock.advance(100)
        check(tick() is False, "接管后后台不再发出")
        check(not state(c)["issued"], "接管后无任何提示被发出")


# ------------------------------------------------------------- 计时与发出

def test_auto_issue_timing():
    fresh_db("a04.db")
    with TestClient(main.app) as c:
        print("A4 等满间隔自动发出：快照/版本/重新计时，每次一条不补发，完成关闭")
        s = setup_show(c, CUES4)
        first, second = s["current"], s["next"]
        s = enable_auto(c, s, 5)
        v = s["version"]
        check(tick() is False, "刚开启未到期间隔不发出")
        clock.advance(4.9)
        check(tick() is False, "等满前不发出")
        clock.advance(0.1)
        check(tick() is True, "等满间隔自动发出一条")
        s = state(c)
        check(s["version"] == v + 1, "自动发出版本 +1")
        check(len(s["issued"]) == 1 and s["issued"][0]["id"] == first["id"],
              "发出的是原队首")
        check(s["issued"][0]["content"] == first["content"], "沿用发出快照规则")
        check(bool(s["issued"][0]["issued_at"]), "记录发出时间")
        check(s["current"]["id"] == second["id"], "推进到下一条")
        clock.advance(4.9)
        check(tick() is False, "发出后重新从完整间隔计时")
        clock.advance(0.2)
        check(tick() is True, "再次等满后发出第二条")
        # 每次只发一条，不补发积压：长时间停滞后一拍只发一条
        clock.advance(3600)
        check(tick() is True, "停滞一小时后到期发出一条")
        s = state(c)
        check(len(s["issued"]) == 3, "停滞后一拍只发一条，不补发积压")
        check(tick() is False, "紧接着的一拍不再发（重新计满间隔）")
        clock.advance(5)
        check(tick() is True, "等满后发出最后一条")
        s = state(c)
        check(s["status"] == "done", "全部发出后完成")
        check(s["auto"]["enabled"] is False, "完成关闭自动模式")
        check(s["auto"]["remaining_seconds"] is None, "完成后无剩余秒数")
        clock.advance(100)
        check(tick() is False, "完成后后台不再发出")


def test_pause_freeze_resume():
    fresh_db("a05.db")
    with TestClient(main.app) as c:
        print("A5 暂停冻结剩余时间，恢复续计")
        s = setup_show(c)
        s = enable_auto(c, s, 5)
        clock.advance(3)
        r = c.post("/api/pause", json={"version": s["version"], "paused": True})
        s = r.json()
        clock.advance(3600)
        check(tick() is False, "暂停期间到期也不发出")
        s2 = state(c)
        check(1.9 < s2["auto"]["remaining_seconds"] <= 2.0,
              "暂停冻结剩余时间（约 2 秒）")
        r = c.post("/api/pause", json={"version": s["version"], "paused": False})
        s = r.json()
        clock.advance(1.9)
        check(tick() is False, "恢复后未续满不发出")
        clock.advance(0.2)
        check(tick() is True, "恢复后续满剩余时间发出")


def test_enable_while_paused():
    fresh_db("a06.db")
    with TestClient(main.app) as c:
        print("A6 暂停中开启自动：冻结在完整间隔，恢复后走满才发")
        s = setup_show(c)
        r = c.post("/api/pause", json={"version": s["version"], "paused": True})
        s = enable_auto(c, r.json(), 5)
        check(4.9 < s["auto"]["remaining_seconds"] <= 5.0,
              "暂停中开启：冻结在完整间隔")
        clock.advance(100)
        check(tick() is False, "暂停中开启不发出")
        r = c.post("/api/pause", json={"version": s["version"], "paused": False})
        clock.advance(4.9)
        check(tick() is False, "恢复后从完整间隔计")
        clock.advance(0.2)
        check(tick() is True, "恢复后走满完整间隔发出")


def test_verify_gate():
    fresh_db("a07.db")
    with TestClient(main.app) as c:
        print("A7 核验门槛：到期未就绪继续等，两工种就绪后立即发出，沿用换轮")
        v = state(c)["version"]
        r = c.post("/api/verify/enable", json={"version": v})
        assert r.status_code == 200, r.text
        s = setup_show(c)
        cur = s["current"]
        s = enable_auto(c, s, 5)
        clock.advance(30)
        check(tick() is False, "到期但未就绪：继续等，不发出")
        s2 = state(c)
        check(s2["auto"]["waiting_ready"] is True, "状态显示等待就绪")
        check(s2["auto"]["remaining_seconds"] == 0.0, "剩余秒数归零")
        rnd = s2["verify"]["round"]
        r = c.post("/api/verify/report", json={
            "department": "lighting", "cue_id": cur["id"], "round": rnd,
            "ready": True,
        })
        assert r.status_code == 200, r.text
        check(tick() is False, "只有一个工种就绪仍等待")
        r = c.post("/api/verify/report", json={
            "department": "sound", "cue_id": cur["id"], "round": rnd,
            "ready": True,
        })
        assert r.status_code == 200, r.text
        check(tick() is True, "两工种就绪后立即发出")
        s3 = state(c)
        check(s3["verify"]["round"] == rnd + 1, "自动发出沿用换轮规则")
        check(s3["verify"]["lighting"] is None and s3["verify"]["sound"] is None,
              "新轮次清空就绪")
        check(s3["auto"]["waiting_ready"] is False, "发出后不再等待就绪")
        clock.advance(30)
        check(tick() is False, "新轮次未就绪继续等")


# ------------------------------------------------------------- 手工操作与计时

def test_manual_ops_reset_timer():
    fresh_db("a08.db")
    with TestClient(main.app) as c:
        print("A8 手工确认 / 撤回 / 改写当前 / 换队首后重新计时")
        s = setup_show(c, CUES5)
        s = enable_auto(c, s, 5)
        clock.advance(4)
        r = c.post("/api/confirm", json={"version": s["version"]})
        s = r.json()
        clock.advance(4.9)
        check(tick() is False, "手工确认后重新计满间隔")
        clock.advance(0.2)
        check(tick() is True, "手工确认后满间隔发出")
        # 撤回末条 → 重新计时
        clock.advance(2)
        st = state(c)
        last = st["issued"][-1]
        r = c.post("/api/withdraw",
                   json={"version": st["version"], "cue_id": last["id"]})
        assert r.status_code == 200, r.text
        clock.advance(4.9)
        check(tick() is False, "撤回后重新计满间隔")
        clock.advance(0.2)
        check(tick() is True, "撤回后满间隔发出")
        # 改写当前提示内容 → 重新计时
        clock.advance(4)
        st = state(c)
        cur = st["current"]
        r = c.post("/api/edit", json={
            "version": st["version"], "cue_id": cur["id"],
            "content": "改写后的内容",
        })
        assert r.status_code == 200, r.text
        clock.advance(4.9)
        check(tick() is False, "改写当前提示后重新计满间隔")
        clock.advance(0.2)
        check(tick() is True, "改写当前提示后满间隔发出")
        st = state(c)
        check(st["issued"][-1]["content"] == "改写后的内容",
              "自动发出使用改写后的最新内容快照")
        # 换队首的重排 → 重新计时
        clock.advance(4)
        st = state(c)
        ids = [p["id"] for p in st["pending"]]
        r = c.post("/api/reorder", json={
            "version": st["version"], "order": [ids[1], ids[0]] + ids[2:],
        })
        assert r.status_code == 200, r.text
        clock.advance(4.9)
        check(tick() is False, "换队首的重排后重新计满间隔")
        clock.advance(0.2)
        check(tick() is True, "换队首后满间隔发出")


def test_no_reset_ops():
    fresh_db("a09.db")
    with TestClient(main.app) as c:
        print("A9 队首不变的重排与编辑其他提示不重置计时")
        s = setup_show(c, CUES4)
        s = enable_auto(c, s, 5)
        clock.advance(4)
        other = s["pending"][1]
        r = c.post("/api/edit", json={
            "version": s["version"], "cue_id": other["id"],
            "content": "其他提示改写",
        })
        assert r.status_code == 200, r.text
        clock.advance(1.1)  # 累计 5.1 秒
        check(tick() is True, "编辑其他提示不重置计时，累计满间隔发出")
        clock.advance(3)
        st = state(c)
        ids = [p["id"] for p in st["pending"]]
        r = c.post("/api/reorder", json={
            "version": st["version"], "order": [ids[0], ids[2], ids[1]],
        })
        assert r.status_code == 200, r.text
        clock.advance(2.1)  # 累计 5.1 秒
        check(tick() is True, "队首不变的重排不重置计时")
        # 记现场问题也不重置
        clock.advance(3)
        st = state(c)
        r = c.post("/api/issues", json={
            "version": st["version"], "cue_id": st["current"]["id"],
            "text": "现场小问题",
        })
        assert r.status_code == 200, r.text
        clock.advance(2.1)
        check(tick() is True, "记现场问题不重置计时")


def test_interval_change_resets():
    fresh_db("a10.db")
    with TestClient(main.app) as c:
        print("A10 改间隔 / 重新开启后从完整间隔计时")
        s = setup_show(c)
        s = enable_auto(c, s, 5)
        clock.advance(4)
        r = set_auto(c, s["version"], True, 10)
        s = r.json()
        check(9.9 < s["auto"]["remaining_seconds"] <= 10.0,
              "改间隔后从完整新间隔计时")
        clock.advance(9.9)
        check(tick() is False, "改间隔后未走满新间隔不发出")
        clock.advance(0.2)
        check(tick() is True, "改间隔后走满新间隔发出")
        clock.advance(2)
        st = state(c)
        r = set_auto(c, st["version"], False, 10)
        assert r.status_code == 200, r.text
        clock.advance(100)
        check(tick() is False, "关闭后不再发出")
        r = set_auto(c, r.json()["version"], True, 3)
        s = r.json()
        clock.advance(2.9)
        check(tick() is False, "重新开启后从完整间隔计")
        clock.advance(0.2)
        check(tick() is True, "重新开启后走满间隔发出")


# ------------------------------------------------------------- 关闭路径

def test_disable_paths():
    fresh_db("a11.db")
    with TestClient(main.app) as c:
        print("A11 归档 / 重置 / 服务重启关闭自动模式")
        s = setup_show(c)
        s = enable_auto(c, s, 5)
        r = c.post("/api/archive",
                   json={"version": s["version"], "name": "场次一"})
        check(r.status_code == 200 and r.json()["auto"]["enabled"] is False,
              "归档关闭自动模式")
        clock.advance(100)
        check(tick() is False, "归档后后台不发出")
        s = setup_show(c)
        s = enable_auto(c, s, 5)
        r = c.post("/api/reset")
        check(r.status_code == 200 and r.json()["auto"]["enabled"] is False,
              "重置关闭自动模式")
        clock.advance(100)
        check(tick() is False, "重置后后台不发出")
        # 服务重启：同一数据库重新建应用
        s = setup_show(c)
        s = enable_auto(c, s, 5)
        check(s["auto"]["enabled"] is True, "重启前自动模式已开启")
    with TestClient(main.app) as c2:
        s2 = c2.get("/api/state").json()
        check(s2["auto"]["enabled"] is False, "服务重启关闭自动模式")
        check(s2["status"] == "live" and s2["remaining"] == 3,
              "重启后演出进度保留")
        clock.advance(100)
        check(tick() is False, "重启后后台不发出")


# ------------------------------------------------------------- 竞争与失败

def test_race_with_manual():
    fresh_db("a12.db")
    with TestClient(main.app) as c:
        print("A12 与手工操作竞争：不重复发出、不跳条")
        s = setup_show(c)
        first, second = s["current"], s["next"]
        s = enable_auto(c, s, 5)
        clock.advance(10)  # 已到期
        # 手工确认抢先：到期的节拍不得重复发出
        r = c.post("/api/confirm", json={"version": s["version"]})
        s = r.json()
        check(tick() is False, "手工确认抢先后，到期的节拍不重复发出")
        st = state(c)
        check(len(st["issued"]) == 1, "只发出一条")
        check(st["current"]["id"] == second["id"], "未跳条")
        # 反向：节拍先发出，手工确认用旧版本 → 409
        clock.advance(5)
        v = st["version"]
        check(tick() is True, "到期节拍发出")
        r = c.post("/api/confirm", json={"version": v})
        check(r.status_code == 409, "节拍发出后旧版本确认被 409")
        st = state(c)
        check(len(st["issued"]) == 2, "仍只发出两条，不重复")
        check(st["current"]["id"] != second["id"], "推进顺序正确")


def test_write_failure():
    fresh_db("a13.db")
    with TestClient(main.app) as c:
        print("A13 写入失败：不推进、不重置计时、不改版本，恢复后重试")
        s = setup_show(c)
        s = enable_auto(c, s, 5)
        v = s["version"]
        clock.advance(10)
        with main.db() as conn:
            conn.execute(
                "CREATE TRIGGER fail_cue_update BEFORE UPDATE ON cues "
                "BEGIN SELECT RAISE(ABORT, 'injected cue failure'); END"
            )
        raised = False
        try:
            main.auto_tick()
        except Exception:
            raised = True
        check(raised, "写入失败时节拍抛出异常（后台循环捕获，不吞掉）")
        st = state(c)
        check(st["version"] == v, "写入失败不改版本")
        check(st["remaining"] == 3 and not st["issued"], "写入失败不推进")
        check(st["auto"]["enabled"] is True, "写入失败不关闭自动模式")
        check(st["auto"]["remaining_seconds"] == 0.0,
              "写入失败不重置计时（仍到期）")
        with main.db() as conn:
            conn.execute("DROP TRIGGER fail_cue_update")
        check(tick() is True, "故障恢复后立即重试发出")
        st = state(c)
        check(len(st["issued"]) == 1 and st["version"] == v + 1,
              "恢复后只发一条、版本 +1")


def test_state_view_countdown():
    fresh_db("a14.db")
    with TestClient(main.app) as c:
        print("A14 状态视图：剩余秒数随时间减少，等待就绪与关闭视图")
        s = setup_show(c)
        s = enable_auto(c, s, 6)
        check(5.9 < s["auto"]["remaining_seconds"] <= 6.0,
              "开启后剩余为完整间隔")
        clock.advance(2)
        st = state(c)
        check(3.9 < st["auto"]["remaining_seconds"] <= 4.0,
              "剩余秒数随时间减少")
        check(st["auto"]["waiting_ready"] is False, "未到期不显示等待就绪")
        # 完成后为关闭视图
        clock.advance(4)
        tick()
        clock.advance(6)
        tick()
        clock.advance(6)
        tick()
        st = state(c)
        check(st["status"] == "done", "三条全部自动发出")
        check(st["auto"] == {
            "enabled": False,
            "interval": 6,
            "remaining_seconds": None,
            "waiting_ready": False,
        }, "完成后自动视图回到关闭（间隔保留）")


test_default_off()
test_configure_rules()
test_control_seat_and_takeover()
test_auto_issue_timing()
test_pause_freeze_resume()
test_enable_while_paused()
test_verify_gate()
test_manual_ops_reset_timer()
test_no_reset_ops()
test_interval_change_resets()
test_disable_paths()
test_race_with_manual()
test_write_failure()
test_state_view_countdown()

print("\n全部通过" if failures == 0 else f"\n失败 {failures} 处断言")
sys.exit(0 if failures == 0 else 1)
