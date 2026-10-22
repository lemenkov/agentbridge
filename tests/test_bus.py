# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

import unittest

from agentbridge.bus import fold_messages


def out(agent, task, body):
    return {"agent_id": agent, "task_id": task, "type": "output", "body": body}


def done(agent, task, rc=0):
    return {"agent_id": agent, "task_id": task, "type": "done", "rc": rc}


class FoldMessages(unittest.TestCase):
    def test_interleaved_tasks_fold_separately(self):
        msgs = [out("a", "t1", "a1"), out("b", "t2", "b1"), out("a", "t1", "a2"),
                done("b", "t2", 3), done("a", "t1")]
        self.assertEqual(fold_messages(msgs), [
            {"agent_id": "a", "task_id": "t1", "type": "result",
             "done": True, "rc": 0, "body": "a1\na2"},
            {"agent_id": "b", "task_id": "t2", "type": "result",
             "done": True, "rc": 3, "body": "b1"},
        ])

    def test_unfinished_task(self):
        [entry] = fold_messages([out("a", "t1", "x")])
        self.assertFalse(entry["done"])
        self.assertIsNone(entry["rc"])
        self.assertEqual(entry["body"], "x")

    def test_untagged_tasks_split_on_done(self):
        msgs = [out("a", None, "first"), done("a", None),
                out("a", None, "second"), done("a", None)]
        self.assertEqual([e["body"] for e in fold_messages(msgs)], ["first", "second"])

    def test_done_without_output(self):
        [entry] = fold_messages([done("a", "t1", 1)])
        self.assertEqual((entry["body"], entry["rc"]), ("", 1))

    def test_waiting_passes_through(self):
        msgs = [{"agent_id": "a", "type": "waiting_for_input", "body": "idle"}]
        self.assertEqual(fold_messages(msgs), [{"agent_id": "a", "type": "waiting"}])


if __name__ == "__main__":
    unittest.main()
