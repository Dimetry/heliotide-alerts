#!/usr/bin/env python3
"""Tests of moderator.py against a CloudKit in memory. Run: python3 moderator_test.py"""
import moderator as m, unittest
DAY = m.DAY_MS
NOW = 1_800_000_000_000
ME = "_key"


class Fake:
    def __init__(self): self.r = {}; self.n = NOW
    def put(self, t, name, by, at=NOW, mod=None, **f):
        self.r[name] = {"recordType": t, "recordName": name, "created": {"userRecordName": by, "timestamp": at},
                        "modified": {"timestamp": mod or at}, "fields": {k: {"value": v} for k, v in f.items()}}
    def me(self): return ME
    def query(self, t, filters=()):
        out = [r for r in self.r.values() if r["recordType"] == t]
        for f in filters:
            v = f["fieldValue"]["value"]
            if f.get("systemFieldName"):
                k = "created" if f["systemFieldName"] == "createdTimestamp" else "modified"
                out = [r for r in out if r[k]["timestamp"] < v]
            else: out = [r for r in out if m.field(r, f["fieldName"]) == v]
        return out
    def lookup(self, names): return [self.r[n] for n in names if n in self.r]
    def create(self, t, name, fields):
        self.put(t, name, ME, NOW); self.r[name]["fields"] = fields
    def delete(self, t, names):
        for n in names: self.r.pop(n, None)
        return len(names), 0
    def of(self, t): return [r for r in self.r.values() if r["recordType"] == t]


def chat(ck, msg, author, reporters, at=NOW):
    ck.put("ChatMessage", msg, author, at, room="world", text="x")
    for p in reporters: ck.put("ChatReport", f"rep-{msg}-{p}", p, at, message=msg, reason="hidden")


class T(unittest.TestCase):
    def run_(self, ck, apply=True, now=NOW): return m.moderate(ck, now, apply)

    def test_three_reports_delete(self):
        ck = Fake(); chat(ck, "a", "_bad", ["_1", "_2", "_3"]); chat(ck, "b", "_bad", ["_1", "_2"])
        c = self.run_(ck)
        self.assertEqual((c["deleted"], "a" in ck.r, "b" in ck.r), (1, False, True))

    def test_dry_run_changes_nothing(self):
        ck = Fake(); chat(ck, "a", "_bad", ["_1", "_2", "_3"]); ck.put("ChatPresence", "p", "_1", NOW - 3 * DAY)
        size = len(ck.r); c = self.run_(ck, apply=False)
        self.assertEqual((c["messages to delete"], c["deleted"], len(ck.r), c["old ChatPresence"]), (1, 0, size, 1))

    def test_same_person_many_reports(self):
        ck = Fake(); ck.put("ChatMessage", "a", "_bad")
        for i in range(5): ck.put("ChatReport", f"r{i}", "_1", message="a")
        self.assertEqual(self.run_(ck)["messages to delete"], 0)

    def test_author_does_not_count(self):
        ck = Fake(); chat(ck, "a", "_bad", ["_1", "_2", "_bad"])
        self.assertEqual(self.run_(ck)["messages to delete"], 0)

    def test_report_naming_another_record_type(self):
        ck = Fake(); ck.put("Alert", "alert1", ME)
        for p in ("_1", "_2", "_3"): ck.put("ChatReport", "r" + p, p, message="alert1")
        self.assertEqual(self.run_(ck)["messages to delete"], 0); self.assertIn("alert1", ck.r)

    def test_ban_after_three_over_several_runs(self):
        ck = Fake()
        for i, d in enumerate((4, 2, 0)):
            chat(ck, f"m{i}", "_bad", ["_1", "_2", "_3"], NOW - d * DAY)
            c = self.run_(ck)
        self.assertEqual(c["bans"], 1); b = ck.of("ChatBan")
        self.assertEqual((len(b), m.field(b[0], "user"), m.field(b[0], "until")), (1, "_bad", NOW + 7 * DAY))
        self.assertEqual(self.run_(ck)["bans"], 0)                         # not twice
        self.assertEqual(self.run_(ck, now=NOW + 8 * DAY)["bans"], 0)      # not again for the same messages

    def test_old_strikes_do_not_count(self):
        ck = Fake()
        for i in range(2): ck.put("ChatReport", f"removed-{i}", ME, NOW - 8 * DAY, message=f"o{i}", reportedUser="_bad", reason="removed")
        chat(ck, "a", "_bad", ["_1", "_2", "_3"])
        self.assertEqual(self.run_(ck)["bans"], 0)

    def test_forged_strikes_do_not_ban(self):
        ck = Fake()
        for i in range(5): ck.put("ChatReport", f"removed-{i}", "_troll", message=f"x{i}", reportedUser="_victim", reason="removed")
        self.assertEqual(self.run_(ck)["bans"], 0)

    def test_second_ban_needs_three_new(self):
        ck = Fake()
        for i in range(3): ck.put("ChatReport", f"removed-{i}", ME, NOW - 6 * DAY, message=f"o{i}", reportedUser="_bad", reason="removed")
        ck.put("ChatBan", "old", ME, NOW - 5 * DAY, user="_bad", until=NOW - 1)
        chat(ck, "a", "_bad", ["_1", "_2", "_3"])
        self.assertEqual(self.run_(ck)["bans"], 0)

    def test_cleanup(self):
        ck = Fake()
        ck.put("ChatMessage", "old", "_1", NOW - 31 * DAY); ck.put("ChatMessage", "new", "_1", NOW - 29 * DAY)
        ck.put("ChatPresence", "gone", "_1", NOW - 9 * DAY, mod=NOW - 2 * DAY)
        ck.put("ChatPresence", "here", "_2", NOW - 9 * DAY, mod=NOW - 1000)
        self.run_(ck); self.assertEqual(set(ck.r), {"new", "here"})

    def test_message_already_gone(self):
        ck = Fake(); chat(ck, "a", "_bad", ["_1", "_2", "_3"]); del ck.r["a"]
        self.assertEqual(self.run_(ck)["messages to delete"], 0)


unittest.main()
