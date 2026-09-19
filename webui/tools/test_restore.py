#!/usr/bin/env python3
"""test_restore.py -- 「作品库 -> 恢复完整会话」的后端契约自检（纯离线：不跑生成、不联网）。

要钉住的是三件事，任何一件断了「恢复参数」都会退化成「只有一个英文孤本」：

  1. 提交时把中文原文 / 优化结果 / 编辑模式写进作业记录，且 clean_optimize 只留必要字段、
     逐个封顶（system / user / 思考全文有自己的 LLM 调用记录，不进 job.json）；
  2. 记录落盘、服务重启后仍能原样读回 —— 用户看到的「作品库里有记录」正是这条路径；
  3. to_dict() 把完整 request 交给前端（GET /api/jobs/<id> 返回的就是它）。

用法：
    cd <repo> && python3 webui/tools/test_restore.py -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI = os.path.dirname(HERE)
ROOT = os.path.dirname(WEBUI)
for p in (WEBUI, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from backend import jobs                        # noqa: E402
from backend.jobs import JobManager             # noqa: E402
from backend.runlog import RunLog               # noqa: E402


def _cfg(tmp: str) -> dict:
    return {
        "root": ROOT,
        "paths": {
            "cache_dir": os.path.join(tmp, "cache"),
            "jobs_dir": os.path.join(tmp, "jobs"),
            "outputs_dir": os.path.join(tmp, "out"),
        },
        "limits": {"max_num_frames": 400, "max_steps": 80, "max_prompt_chars": 12000,
                   "allowed_ref_ext": {}},
        "jobs": {"max_concurrent": 1, "log_tail_lines": 50},
    }


SPEC = {"measured": []}


def _raw(**extra) -> dict:
    raw = {"prompt": "EN BODY", "width": 640, "height": 384, "num_frames": 22,
           "steps": 4, "seed": 42, "preset": "blitz"}
    raw.update(extra)
    return raw


class MakeRequestContextTest(unittest.TestCase):
    """提交请求 -> 作业记录：恢复会话需要的上下文必须在这里就存下来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)

    def _req(self, **extra):
        req, errors, _warnings = jobs.make_request(_raw(**extra), self.cfg, SPEC)
        self.assertEqual(errors, [])
        return req

    def test_context_is_persisted(self):
        req = self._req(
            prompt_zh="中文原文", prompt_source="en", mode="ref2va", mode_tab="edit",
            optimize={"en": "EN BODY", "zh": "回译", "notes": "结构说明", "mode": "ref2va",
                      "source": "中文原文", "runId": "llm-1", "useZh": False,
                      "system": "S" * 100, "user": "U" * 100, "thinking": "T" * 100})
        self.assertEqual(req["prompt"], "EN BODY")
        self.assertEqual(req["prompt_zh"], "中文原文")
        self.assertEqual(req["prompt_source"], "en")
        self.assertEqual(req["mode"], "ref2va")
        self.assertEqual(req["mode_tab"], "edit")
        self.assertEqual(req["optimize"],
                         {"en": "EN BODY", "zh": "回译", "notes": "结构说明",
                          "mode": "ref2va", "source": "中文原文", "runId": "llm-1"})

    def test_debug_blobs_do_not_enter_job_json(self):
        req = self._req(optimize={"en": "EN", "system": "S" * 100, "user": "U" * 100,
                                  "thinking": "T" * 100})
        for k in ("system", "user", "thinking"):
            self.assertNotIn(k, req["optimize"])

    def test_usezh_flag_is_kept(self):
        req = self._req(optimize={"en": "EN", "useZh": True})
        self.assertTrue(req["optimize"]["useZh"])
        self.assertEqual(req["optimize"]["en"], "EN")

    def test_defaults_keep_old_clients_working(self):
        # 老客户端不传这些字段：记录仍然可用，恢复出来是同一份文本
        req = self._req()
        self.assertEqual(req["prompt_zh"], "")
        self.assertEqual(req["prompt_source"], "zh")
        self.assertEqual(req["mode"], "auto")
        self.assertEqual(req["mode_tab"], "generate")
        self.assertEqual(req["optimize"], {})

    def test_fields_are_capped(self):
        long = "x" * (jobs.CTX_TEXT_CAP + 500)
        req = self._req(prompt_zh=long, optimize={"en": long, "zh": long, "notes": long})
        self.assertEqual(len(req["prompt_zh"]), jobs.CTX_TEXT_CAP)
        self.assertEqual(len(req["optimize"]["en"]), jobs.CTX_TEXT_CAP)
        self.assertEqual(len(req["optimize"]["zh"]), jobs.CTX_TEXT_CAP)
        self.assertEqual(len(req["optimize"]["notes"]), jobs.CTX_TEXT_CAP)

    def test_clean_optimize_tolerates_garbage(self):
        self.assertEqual(jobs.clean_optimize(None), {})
        self.assertEqual(jobs.clean_optimize("not a dict"), {})
        self.assertEqual(jobs.clean_optimize({"en": 123, "zh": ""}), {})


class JobContextSurvivesRestartTest(unittest.TestCase):
    """作业记录落盘 + 服务重启读回：作品库里那条记录到底是完整的。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = _cfg(self.tmp.name)
        os.makedirs(self.cfg["paths"]["jobs_dir"])
        os.makedirs(self.cfg["paths"]["cache_dir"])
        self.log = RunLog(os.path.join(self.tmp.name, "logs"), level="info",
                          console=False, capacity=100)
        self.addCleanup(self.log.close)

    def _manager(self):
        m = JobManager(self.cfg, self.log)
        m._start = lambda j: None          # 只测记录，绝不起真实生成子进程
        return m

    def test_context_survives_a_service_restart(self):
        raw = _raw(prompt_zh="中文原文", prompt_source="en", mode="ref2va", mode_tab="edit",
                   optimize={"en": "EN BODY", "zh": "回译", "notes": "结构说明",
                             "mode": "ref2va", "source": "中文原文", "runId": "llm-1"})
        req, errors, _ = jobs.make_request(raw, self.cfg, SPEC)
        self.assertEqual(errors, [])
        m = self._manager()
        j = m.submit(req, ["bash", "run_h3.sh", "gen"])

        got = m.get(j.id).to_dict()        # GET /api/jobs/<id> 返回的就是 to_dict()
        self.assertEqual(got["request"]["prompt_zh"], "中文原文")
        self.assertEqual(got["request"]["prompt_source"], "en")
        self.assertEqual(got["request"]["optimize"]["en"], "EN BODY")
        self.assertEqual(got["request"]["mode_tab"], "edit")

        # 列表视图（作品库表格）仍带形状/步数/种子，但不背几万字上下文
        listed = [x for x in m.list(10) if x["id"] == j.id][0]
        self.assertEqual(listed["request"]["width"], 640)
        self.assertNotIn("optimize", listed["request"])

        # 模拟服务重启：新的 JobManager 从磁盘读回同一条记录
        m2 = self._manager()
        j2 = m2.get(j.id)
        self.assertIsNotNone(j2, "重启后应能从磁盘读回作业记录")
        self.assertEqual(j2.status, "interrupted")
        self.assertEqual(j2.request["prompt_zh"], "中文原文")
        self.assertEqual(j2.request["optimize"]["zh"], "回译")
        self.assertEqual(j2.request["optimize"]["runId"], "llm-1")
        self.assertEqual(j2.request["mode"], "ref2va")
        self.assertEqual(j2.request["mode_tab"], "edit")
        # 参数也要在（恢复后要能照着重跑）
        self.assertEqual(j2.request["width"], 640)
        self.assertEqual(j2.request["num_frames"], 22)
        self.assertEqual(j2.request["steps"], 4)

    def test_timestamps_survive_a_service_restart(self):
        """重启后每条历史记录必须还是它**自己**的时间。

        老实现 _load_from_disk() 里写的是 j.created = now()、j.started/finished 直接丢掉：
        一次重启就让作品库里所有记录的「时间」列一起变成本次服务启动时间（本次事故里
        22 条记录全变成了 09:20:21），「用时」还跟着 now() 一直涨。
        """
        req, errors, _ = jobs.make_request(_raw(prompt_zh="甲"), self.cfg, SPEC)
        self.assertEqual(errors, [])
        m = self._manager()
        j = m.submit(req, ["bash", "run_h3.sh", "gen"])
        t0 = 1_700_000_000.0                      # 一个固定的过去时间：不许被 now() 顶掉
        j.created, j.started, j.finished = t0, t0 + 5, t0 + 65
        j.status, j.stage = "done", "done"
        j.snapshot()

        j2 = self._manager().get(j.id)
        self.assertIsNotNone(j2)
        self.assertEqual(jobs.iso(j2.created), jobs.iso(t0))
        self.assertEqual(jobs.iso(j2.started), jobs.iso(t0 + 5))
        self.assertEqual(jobs.iso(j2.finished), jobs.iso(t0 + 65))
        d = j2.to_dict()
        self.assertEqual(d["created"], jobs.iso(t0))
        self.assertEqual(d["finished"], jobs.iso(t0 + 65))
        self.assertEqual(d["elapsed_s"], 60.0)

    def test_interrupted_job_keeps_its_own_clock(self):
        """跑着的时候服务被杀：时间列是当初开工的时间，用时是它实际跑过的时长。"""
        req, errors, _ = jobs.make_request(_raw(prompt_zh="甲"), self.cfg, SPEC)
        self.assertEqual(errors, [])
        m = self._manager()
        j = m.submit(req, ["bash", "run_h3.sh", "gen"])
        t_now = jobs.now()
        j.created = j.started = t_now - 300
        j.finished = None
        j.status, j.stage = "running", "denoise"
        j.snapshot()
        # 假装最后一份快照写在 200 秒前（服务在那之后被杀，没来得及写 finished）
        past = t_now - 200
        for p in (j.paths["job"], j.paths["log"]):
            if os.path.exists(p):
                os.utime(p, (past, past))

        j2 = self._manager().get(j.id)
        self.assertEqual(j2.status, "interrupted")
        self.assertEqual(jobs.iso(j2.created), jobs.iso(t_now - 300))
        self.assertEqual(jobs.iso(j2.started), jobs.iso(t_now - 300))
        self.assertIsNotNone(j2.finished, "中断的作业没有结束时间的话，「用时」会一直涨")
        self.assertLess(abs(j2.finished - past), 2.0, "结束时间应该取最后写盘时间")
        self.assertLess(abs(j2.to_dict()["elapsed_s"] - 100.0), 2.0)

    def test_pending_dirs_do_not_evict_records_on_restart(self):
        """提交时落提示词会留下 pending-* 目录（没有 job.json、名字排在最前）。

        它们在磁盘上会越积越多；如果「先切片再过滤」，作业记录会被挤出 50 条窗口，
        重启后作品库莫名变空，恢复参数也就无从谈起。
        """
        req, errors, _ = jobs.make_request(_raw(prompt_zh="甲"), self.cfg, SPEC)
        self.assertEqual(errors, [])
        m = self._manager()
        j = m.submit(req, ["bash", "run_h3.sh", "gen"])
        for i in range(60):
            d = os.path.join(self.cfg["paths"]["jobs_dir"], "pending-20990101-%06d" % i)
            os.makedirs(d)
            with open(os.path.join(d, "prompt.txt"), "w", encoding="utf-8") as fp:
                fp.write("x\n")

        m2 = self._manager()
        got = m2.get(j.id)
        self.assertIsNotNone(got, "pending-* 目录把作业记录挤出了重启加载窗口")
        self.assertEqual(got.request["prompt_zh"], "甲")


if __name__ == "__main__":
    unittest.main()
