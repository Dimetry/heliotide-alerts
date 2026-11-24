#!/usr/bin/env python3
"""Heliotide chat moderation. Runs on a schedule in Dimetry/heliotide-alerts (GitHub Actions).

Uses the CloudKit server-to-server keys. Their user has the Moderator role in both environments.
Rules (docs/CHAT-SERVER.md, section 5):
  1. A message reported by 3+ different people (its author does not count) -> deleted.
  2. A person with 3+ messages deleted by rule 1 in 7 days -> ChatBan for 7 days.
  3. Messages, reactions and reports older than 30 days -> deleted.
  4. ChatPresence ("here now" marks) not touched for 1 day -> deleted.

Memory for rule 2: before a message is deleted, this job writes its own ChatReport record
(message, room, reportedUser = author, reason = "removed"). Only the Moderator role can read it.
Only records whose CREATOR is this key are trusted as memory; users can make ChatReport records too.

THE LOG IS PUBLIC: counts and error codes only. Never texts, names, IDs or rooms.

Usage:  python3 moderator.py            dry run (reads and counts, changes nothing)
        python3 moderator.py --apply    makes the changes
Env: CK_KEY_ID + CK_PRIVATE_KEY (development), CK_PROD_KEY_ID + CK_PROD_PRIVATE_KEY (production).
An environment without its key is skipped.
"""
import base64, collections, datetime as dt, hashlib, json, os, subprocess, sys, tempfile
import urllib.request, urllib.error, uuid

CONTAINER = "iCloud.dev.spacecast.app"
REPORTS_TO_DELETE = 3
DELETES_TO_BAN = 3
BAN_DAYS = 7
KEEP_DAYS = 30
PRESENCE_DAYS = 1
DAY_MS = 86_400_000


class CloudKitError(Exception):
    """Carries a CloudKit error code only (safe for the public log)."""


class CloudKit:
    def __init__(self, env, key_id, pem):
        self.env, self.key_id, self.pem = env, key_id, pem

    def call(self, sub, payload=None):
        """payload None = GET."""
        path = f"/database/1/{CONTAINER}/{self.env}/public/{sub}"
        body = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode()
        date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha256(body).digest()).decode()
        with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as k:
            k.write(self.pem)
        try:
            sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", k.name],
                                 input=f"{date}:{digest}:{path}".encode(), capture_output=True, check=True).stdout
        finally:
            os.unlink(k.name)
        req = urllib.request.Request("https://api.apple-cloudkit.com" + path, data=None if payload is None else body,
                                     method="GET" if payload is None else "POST", headers={
            "Content-Type": "application/json",
            "X-Apple-CloudKit-Request-KeyID": self.key_id,
            "X-Apple-CloudKit-Request-ISO8601Date": date,
            "X-Apple-CloudKit-Request-SignatureV1": base64.b64encode(sig).decode()})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                code = json.loads(e.read()).get("serverErrorCode")
            except Exception:
                code = None
            raise CloudKitError(code or f"HTTP_{e.code}") from None

    def me(self):
        return self.call("users/current")["userRecordName"]

    def query(self, rtype, filters=()):
        """All records that match (follows continuation markers)."""
        out, marker = [], None
        while True:
            payload = {"query": {"recordType": rtype, "filterBy": list(filters)}, "resultsLimit": 200}
            if marker:
                payload["continuationMarker"] = marker
            r = self.call("records/query", payload)
            for rec in r.get("records", []):
                if "serverErrorCode" in rec:
                    raise CloudKitError(rec["serverErrorCode"])
                out.append(rec)
            marker = r.get("continuationMarker")
            if not marker:
                return out

    def lookup(self, names):
        out = []
        for i in range(0, len(names), 200):
            r = self.call("records/lookup", {"records": [{"recordName": n} for n in names[i:i + 200]]})
            out += [rec for rec in r.get("records", []) if "serverErrorCode" not in rec]
        return out

    def create(self, rtype, name, fields):
        r = self.call("records/modify", {"operations": [{"operationType": "create", "record": {
            "recordType": rtype, "recordName": name, "fields": fields}}]})
        rec = (r.get("records") or [{}])[0]
        if "serverErrorCode" in rec:
            raise CloudKitError(rec["serverErrorCode"])

    def delete(self, rtype, names):
        """Returns (deleted, refused)."""
        done = refused = 0
        for i in range(0, len(names), 200):
            ops = [{"operationType": "forceDelete", "record": {"recordType": rtype, "recordName": n}}
                   for n in names[i:i + 200]]
            r = self.call("records/modify", {"operations": ops})
            for rec in r.get("records", []):
                if "serverErrorCode" in rec and rec["serverErrorCode"] != "NOT_FOUND":
                    refused += 1
                else:
                    done += 1
        return done, refused


def field(rec, name):
    return (rec.get("fields", {}).get(name) or {}).get("value")


def creator(rec):
    return (rec.get("created") or {}).get("userRecordName")


def created(rec):
    return (rec.get("created") or {}).get("timestamp") or 0


def before(system_field, ms):
    return {"systemFieldName": system_field, "comparator": "LESS_THAN",
            "fieldValue": {"value": ms, "type": "TIMESTAMP"}}


def text(v):
    return {"value": v, "type": "STRING"}


def moderate(ck, now, apply):
    """One pass over one environment. Returns counts (safe to print)."""
    c = collections.OrderedDict()
    me = ck.me()
    everything = ck.query("ChatReport")
    log = [r for r in everything if creator(r) == me and field(r, "reason") == "removed"]
    reports = [r for r in everything if creator(r) != me]
    c["reports"] = len(reports)

    # 1. Messages reported by enough different people. The author and the record type come from the
    #    server: the fields of a report are only what its maker wrote (it could name any record).
    who = collections.defaultdict(set)
    for r in reports:
        if field(r, "message") and creator(r):
            who[field(r, "message")].add(creator(r))
    candidates = [m for m, people in who.items() if len(people) >= REPORTS_TO_DELETE]
    remove = [r for r in (ck.lookup(candidates) if candidates else [])
              if r.get("recordType") == "ChatMessage" and creator(r) and creator(r) != me
              and len(who[r["recordName"]] - {creator(r)}) >= REPORTS_TO_DELETE]
    c["messages to delete"] = len(remove)
    c["deleted"] = c["refused"] = 0
    logged = {(field(r, "message"), field(r, "reportedUser")) for r in log}
    strikes = collections.defaultdict(dict)   # author -> {message: time}
    for r in log:
        strikes[field(r, "reportedUser")][field(r, "message")] = created(r)
    for r in remove:
        name, author = r["recordName"], creator(r)
        strikes[author].setdefault(name, now)
        if apply:
            # The note first: if the job dies here, the next run finds the note and deletes the message.
            if (name, author) not in logged:
                ck.create("ChatReport", "removed-" + uuid.uuid4().hex, {
                    "message": text(name), "room": text(field(r, "room") or ""),
                    "reportedUser": text(author), "reason": text("removed")})
            done, refused = ck.delete("ChatMessage", [name])
            c["deleted"] += done
            c["refused"] += refused

    # 2. Bans. Messages already counted for an earlier ban do not count again.
    c["bans"] = 0
    for author, messages in strikes.items():
        recent = [t for t in messages.values() if t > now - BAN_DAYS * DAY_MS]
        if not author or author == me or len(recent) < DELETES_TO_BAN:
            continue
        bans = ck.query("ChatBan", [{"fieldName": "user", "comparator": "EQUALS", "fieldValue": text(author)}])
        if any((field(b, "until") or 0) > now for b in bans):
            continue
        last = max([created(b) for b in bans], default=0)
        if sum(t > last for t in recent) < DELETES_TO_BAN:
            continue
        c["bans"] += 1
        if apply:
            ck.create("ChatBan", "ban-" + uuid.uuid4().hex, {
                "user": text(author), "until": {"value": now + BAN_DAYS * DAY_MS, "type": "TIMESTAMP"}})

    # 3 and 4. Old records.
    for rtype, f, days in (("ChatMessage", "createdTimestamp", KEEP_DAYS),
                           ("ChatReaction", "createdTimestamp", KEEP_DAYS),
                           ("ChatReport", "createdTimestamp", KEEP_DAYS),
                           ("ChatPresence", "modifiedTimestamp", PRESENCE_DAYS)):
        names = [r["recordName"] for r in ck.query(rtype, [before(f, now - days * DAY_MS)])]
        c["old " + rtype] = len(names)
        if apply and names:
            done, refused = ck.delete(rtype, names)
            c["old " + rtype] = f"{len(names)} (deleted {done}, refused {refused})"
    return c


def main():
    apply = "--apply" in sys.argv
    now = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    failed = False
    for env, prefix in (("development", "CK_"), ("production", "CK_PROD_")):
        key_id, pem = os.environ.get(prefix + "KEY_ID"), os.environ.get(prefix + "PRIVATE_KEY")
        if not (key_id and pem):
            print(f"{env}: no key, skipped")
            continue
        try:
            c = moderate(CloudKit(env, key_id, pem), now, apply)
            print(f"{env} [{'apply' if apply else 'dry run'}] " + "; ".join(f"{k}: {v}" for k, v in c.items()))
        except CloudKitError as e:
            failed = True
            print(f"{env}: CloudKit error {e}")
        except Exception as e:   # never the message: it may hold an ID
            failed = True
            print(f"{env}: {type(e).__name__}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
