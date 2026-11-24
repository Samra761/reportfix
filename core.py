"""Business logic: routing, grouping, priority, permissions, verification and trust.

Everything here is deterministic code. The AI's output (triage.py) is only an input to it.
Privacy rule for confidential (harassment) issues: no function here returns, logs or stores in a log table
the reporter's identity to anyone except the reporter. Confidential reports also never create trust events,
because a timestamped trust event would let an admin link a student to the report.
"""
import time

import config
import db
import triage

OPEN_STATUSES = ("open", "in_progress", "pending_verification")


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


def now():
    return time.time()


# ------------------------------------------------------------------ users & trust

def get_user(uid):
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        raise ApiError(401, "pick a user in the top bar (X-User-Id header missing)")
    u = db.q1("SELECT * FROM users WHERE id=?", (uid,))
    if not u:
        raise ApiError(401, "unknown user")
    return u


def require(user, *roles):
    if user["role"] not in roles:
        raise ApiError(403, f"only {' / '.join(roles)} can do this")


def trust_score(sid):
    s = db.q1("SELECT COALESCE(SUM(delta),0) FROM trust_events WHERE student_id=?", (sid,))[0]
    return max(config.TRUST_MIN, min(config.TRUST_MAX, config.TRUST_START + s))


def on_probation(sid):
    u = db.q1("SELECT created_at FROM users WHERE id=?", (sid,))
    young = now() - u["created_at"] < config.PROBATION_HOURS * 3600
    earned = db.q1("SELECT COUNT(*) FROM trust_events WHERE student_id=? AND delta>0", (sid,))[0]
    return young and not earned


def weight(sid):
    """(weight, on_probation). weight = clamp(trust/50, 0.2, 2.0); brand-new accounts get PROBATION_WEIGHT."""
    w = max(config.WEIGHT_MIN, min(config.WEIGHT_MAX, trust_score(sid) / 50))
    if on_probation(sid):
        return min(w, config.PROBATION_WEIGHT), True
    return round(w, 2), False


def add_trust(sid, delta, reason, issue_id=None, report_id=None):
    db.ex("INSERT INTO trust_events(student_id, delta, reason, issue_id, report_id, created_at) VALUES (?,?,?,?,?,?)",
          (sid, delta, reason, issue_id, report_id, now()))


def user_view(u):
    d = {"id": u["id"], "name": u["name"], "role": u["role"], "department": u["department"]}
    if u["role"] == "student":
        d["trust"] = round(trust_score(u["id"]), 1)
        d["weight"], d["probation"] = weight(u["id"])
    return d


def list_users():
    return [user_view(u) for u in db.q("SELECT * FROM users ORDER BY role DESC, id")]


def new_student():
    with db.LOCK:
        n = db.q1("SELECT COUNT(*) FROM users WHERE role='student'")[0] + 1
        uid = db.ex("INSERT INTO users(name, role, created_at) VALUES (?, 'student', ?)", (f"New student {n}", now()))
        return user_view(db.q1("SELECT * FROM users WHERE id=?", (uid,)))


def trust_log(viewer, sid):
    u = db.q1("SELECT * FROM users WHERE id=? AND role='student'", (sid,))
    if not u:
        raise ApiError(404, "no such student")
    events = db.q("SELECT delta, reason, issue_id, report_id, created_at FROM trust_events WHERE student_id=? "
                  "ORDER BY id", (sid,))
    w, prob = weight(sid)
    return {"student": {"id": u["id"], "name": u["name"]}, "start": config.TRUST_START,
            "sum_of_deltas": sum(e["delta"] for e in events), "score": trust_score(sid), "weight": w,
            "probation": prob, "formula": f"score = {config.TRUST_START} + sum(deltas), clamped 0-100; "
                                          f"weight = clamp(score/50, {config.WEIGHT_MIN}, {config.WEIGHT_MAX})"
                                          + (f"; on probation (new account) -> weight {config.PROBATION_WEIGHT}"
                                             if prob else ""),
            "events": [dict(e) for e in events]}


# ------------------------------------------------------------------ priority

def crowd(issue_id):
    """Credible crowd = DISTINCT affected students, weighted by trust. Probation accounts are capped in total."""
    rows = db.q("SELECT student_id FROM supporters WHERE issue_id=?", (issue_id,))
    est_w, prob_w, est_n, prob_n = 0.0, 0.0, 0, 0
    for r in rows:
        w, prob = weight(r["student_id"])
        if prob:
            prob_w, prob_n = prob_w + w, prob_n + 1
        else:
            est_w, est_n = est_w + w, est_n + 1
    prob_capped = min(prob_w, config.PROBATION_TOTAL_CAP)
    return {"distinct": len(rows), "established": est_n, "probation": prob_n, "established_weight": round(est_w, 2),
            "probation_weight": round(prob_capped, 2), "credible_weight": round(est_w + prob_capped, 2)}


def priority(issue):
    cat = issue["category"]
    base = config.SEVERITY[cat]
    c = crowd(issue["id"])
    lines = [f"Base severity of '{cat}' = {config.TIERS[base]} (fixed table; wording/CAPS/'urgent' ignored)"]
    if issue["is_safety"]:
        tier = 4
        lines.append("Safety hazard -> always critical, regardless of crowd or wording")
    else:
        bumps = 0
        for i, b in enumerate(config.CROWD_BUMPS, 1):
            ok = c["distinct"] >= b["min_students"] and c["credible_weight"] >= b["min_weight"]
            bumps += ok
            lines.append(f"Crowd bump {i}: needs >= {b['min_students']} distinct students AND credible weight >= "
                         f"{b['min_weight']} -> {'MET' if ok else 'not met'}")
        tier = min(base + bumps, max(base, config.MAX_CROWD_TIER))
        if base + bumps > tier:
            lines.append(f"Capped at {config.TIERS[tier]}: crowds never make a non-safety issue critical")
    factor = 1 + min(c["credible_weight"], config.CROWD_FACTOR_WEIGHT_CAP) / config.CROWD_FACTOR_WEIGHT_CAP
    importance = round(tier * factor, 2)
    lines.append(f"Crowd: {c['distinct']} distinct students ({c['established']} established, weight "
                 f"{c['established_weight']}; {c['probation']} new accounts, weight capped at {c['probation_weight']})")
    lines.append(f"Importance = tier {tier} x crowd factor {factor:.2f} = {importance}")
    return {"tier": tier, "label": config.TIERS[tier], "base": config.TIERS[base], "importance": importance,
            "crowd": c, "lines": lines}


# ------------------------------------------------------------------ views

def _is_supporter(issue_id, sid):
    return db.q1("SELECT * FROM supporters WHERE issue_id=? AND student_id=?", (issue_id, sid))


def can_see(issue, user):
    if user["role"] == "admin":
        return True
    if user["role"] == "staff":
        return not issue["confidential"] and issue["department"] == user["department"]
    if issue["confidential"]:
        return bool(_is_supporter(issue["id"], user["id"]))
    return True  # non-confidential issues are on the public board


def get_issue_row(issue_id, user):
    i = db.q1("SELECT * FROM issues WHERE id=?", (issue_id,))
    if not i or not can_see(i, user):
        raise ApiError(404, "issue not found")  # same answer whether it doesn't exist or you may not see it
    return i


def issue_view(i, viewer):
    p = priority(i)
    d = {"id": i["id"], "department": i["department"], "location": i["location"] or "(not given)",
         "category": i["category"], "category_label": triage.LABELS[i["category"]], "priority": p["label"],
         "priority_tier": p["tier"], "importance": p["importance"], "why_priority": p["lines"],
         "reason": i["reason"], "summary": i["summary"], "status": i["status"], "confidential": bool(i["confidential"]),
         "is_safety": bool(i["is_safety"]), "reopen_count": i["reopen_count"], "created_at": i["created_at"],
         "affected_students": p["crowd"]["distinct"], "me_too_count": max(0, p["crowd"]["distinct"] - 1)}
    if i["confidential"]:
        d["reporter"] = "Anonymous"
        d["me_too_count"] = d["affected_students"] = None  # never grouped, nothing to count
        d["why_priority"] = [p["lines"][0]] + (["Safety hazard -> critical"] if i["is_safety"] else []) + \
                            ["Confidential: routed to Admin only, never grouped or shown publicly"]
    if viewer["role"] == "student":
        sup = _is_supporter(i["id"], viewer["id"])
        d["is_mine"] = bool(sup)
        if i["status"] == "pending_verification" and not i["confidential"]:
            d["can_vote"], d["vote_block_reason"] = _vote_eligibility(i, viewer["id"])
    if i["status"] == "pending_verification" and not i["confidential"]:
        d["verification"] = tally(i)
    return d


# ------------------------------------------------------------------ reporting

def _route_department(category, location):
    return config.CATEGORY_DEPARTMENT_OVERRIDE.get(category) or config.LOCATIONS[location]["department"]


def _candidates(location):
    if not location:
        return []
    return [{"id": r["id"], "summary": r["summary"]} for r in db.q(
        "SELECT id, summary FROM issues WHERE location=? AND confidential=0 AND status IN (?,?,?) ORDER BY id DESC "
        "LIMIT 20", (location, *OPEN_STATUSES))]


def _log_status(issue_id, role, frm, to, note=None):
    db.ex("INSERT INTO status_log(issue_id, actor_role, from_status, to_status, note, created_at) VALUES (?,?,?,?,?,?)",
          (issue_id, role, frm, to, note, now()))


def submit_report(user, text):
    require(user, "student")
    text = (text or "").strip()
    if not text:
        raise ApiError(400, "report text is empty")
    if len(text) > 2000:
        raise ApiError(400, "report is too long (max 2000 characters)")

    found, unknown = triage.find_locations(text)
    hard_unknown = [u for u in unknown if any(ch.isdigit() for ch in u)]  # e.g. "lab 99", "room 204"
    pre_loc = next(iter(found)) if len(found) == 1 and not hard_unknown else None
    candidates = _candidates(pre_loc)

    ai = triage.llm_triage(text, candidates)   # one AI call per report (None => keyword fallback)
    rules = triage.rules_triage(text)          # always run: it is also the safety / harassment / injection net
    t = ai or rules
    source = "ai" if ai else "rules"

    category = t["category"]
    is_harassment = t["is_harassment"] or rules["is_harassment"] or category == "harassment"
    is_safety = t["is_safety"] or rules["is_safety"] or category in config.SAFETY_CATEGORIES
    injection = t["injection_attempt"] or rules["injection_attempt"]
    if is_harassment:
        category = "harassment"
    elif rules["is_safety"] and category not in config.SAFETY_CATEGORIES:
        category = rules["category"]  # keyword safety net overrides a non-safety AI category

    # --- location: exact aliases (code) first; AI may resolve paraphrases; anything unknown/ambiguous => ask.
    ai_raw = ai["location"] if ai else None
    ai_loc = triage.canonical_location(ai_raw)
    ai_named_unknown = bool(ai_raw) and not ai_loc
    if hard_unknown or len(found) > 1 or (ai_named_unknown and found):
        location = None
    elif found:
        location = next(iter(found))
    else:
        location = ai_loc  # None if the AI found nothing or named an unlisted place
    mentioned = ", ".join(hard_unknown) or (ai_raw if ai_named_unknown else None) or \
        (", ".join(unknown) if unknown else None) or (", ".join(sorted(found)) if len(found) > 1 else None)

    summary = (ai["summary"] if ai and ai["summary"] else rules["summary"])
    reason = t["reason"]

    with db.LOCK:
        if is_harassment:
            # Confidential: Admin only, never grouped, never "me too", location optional (don't block a victim).
            iid = db.ex("INSERT INTO issues(location, category, department, is_safety, confidential, status, summary, "
                        "reason, created_at, updated_at) VALUES (?,?,?,?,1,'open',?,?,?,?)",
                        (location, "harassment", config.ADMIN_QUEUE, int(is_safety),
                         summary or "Confidential harassment/ragging report",
                         "Harassment/ragging -> confidential, Admin only", now(), now()))
            rid = _insert_report(user, text, iid, "linked", mentioned, category, is_safety, True, injection, reason,
                                 summary, source)
            db.ex("INSERT INTO supporters(issue_id, student_id, via, report_id, created_at) VALUES (?,?,?,?,?)",
                  (iid, user["id"], "report", rid, now()))
            _log_status(iid, "system", None, "open", "confidential report received")
            return {"report_id": rid, "status": "confidential", "triage_source": source,
                    "issue": issue_view(db.q1("SELECT * FROM issues WHERE id=?", (iid,)), user),
                    "message": "Your report is confidential. Only the Admin sees it, and never your name."}

        status = "needs_clarification" if location is None else "linked"
        rid = _insert_report(user, text, None, status, mentioned, category, is_safety, False, injection, reason,
                             summary, source)
        if injection:
            add_trust(user["id"], config.TRUST_INJECTION_ATTEMPT,
                      "report contained instructions to the system (ignored)", report_id=rid)
        if location is None:
            return {"report_id": rid, "status": "needs_clarification", "triage_source": source,
                    "injection_attempt": injection, "category": category, "is_safety": is_safety,
                    "mentioned_place": mentioned, "valid_locations": list(config.LOCATIONS),
                    "message": ("We couldn't match " + (f"'{mentioned}'" if mentioned else "any place")
                                + " to a campus location. Which of these did you mean?")}
        result = _attach(user, rid, category, location, is_safety, summary, reason, ai["matches_issue_id"] if ai
                         else None, source)
        result.update({"triage_source": source, "injection_attempt": injection})
        return result


def _insert_report(user, text, iid, status, mentioned, category, is_safety, is_h, inj, reason, summary, source):
    return db.ex("INSERT INTO reports(student_id, text, issue_id, status, location_mentioned, category, is_safety, "
                 "is_harassment, injection_attempt, reason, summary, triage_source, created_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (user["id"], text, iid, status, mentioned, category, int(is_safety), int(is_h), int(inj), reason,
                  summary, source, now()))


def _attach(user, rid, category, location, is_safety, summary, reason, ai_match, source):
    """Group into an existing issue (same location + same problem) or create a new one. Code verifies everything."""
    match = None
    if ai_match:  # the AI's semantic match is only accepted if code confirms it
        match = db.q1(f"SELECT * FROM issues WHERE id=? AND location=? AND category=? AND confidential=0 AND status IN "
                      f"(?,?,?)", (ai_match, location, category, *OPEN_STATUSES))
    if not match and (category in config.SINGLETON_CATEGORIES or source != "ai"):
        # one WiFi / AC / projector per room: same place + same category is the same problem.
        # Without the AI we cannot compare meaning, so the rules fallback also merges on place + category.
        match = db.q1("SELECT * FROM issues WHERE location=? AND category=? AND confidential=0 AND status IN (?,?,?) "
                      "ORDER BY id LIMIT 1", (location, category, *OPEN_STATUSES))
    merged = bool(match)
    if match:
        iid = match["id"]
        if is_safety and not match["is_safety"]:
            db.ex("UPDATE issues SET is_safety=1, updated_at=? WHERE id=?", (now(), iid))
        if _is_supporter(iid, user["id"]):
            db.ex("UPDATE reports SET issue_id=?, status='duplicate' WHERE id=?", (iid, rid))
            add_trust(user["id"], config.TRUST_DUPLICATE_SPAM, f"reported issue #{iid} again (duplicate)",
                      issue_id=iid, report_id=rid)
            return {"report_id": rid, "status": "duplicate", "merged": True,
                    "issue": issue_view(db.q1("SELECT * FROM issues WHERE id=?", (iid,)), user),
                    "message": f"You already reported or joined issue #{iid}. Duplicate reports cost trust."}
    else:
        dept = _route_department(category, location)
        iid = db.ex("INSERT INTO issues(location, category, department, is_safety, confidential, status, summary, "
                    "reason, created_at, updated_at) VALUES (?,?,?,?,0,'open',?,?,?,?)",
                    (location, category, dept, int(is_safety), summary, reason, now(), now()))
        _log_status(iid, "system", None, "open", f"routed to {dept}")
    db.ex("UPDATE reports SET issue_id=?, status='linked' WHERE id=?", (iid, rid))
    db.ex("INSERT INTO supporters(issue_id, student_id, via, report_id, created_at) VALUES (?,?,?,?,?)",
          (iid, user["id"], "report", rid, now()))
    return {"report_id": rid, "status": "linked", "merged": merged,
            "issue": issue_view(db.q1("SELECT * FROM issues WHERE id=?", (iid,)), user),
            "message": (f"Grouped with existing issue #{iid}" if merged else f"New issue #{iid} created")}


def clarify(user, report_id, location):
    require(user, "student")
    with db.LOCK:
        r = db.q1("SELECT * FROM reports WHERE id=? AND student_id=?", (report_id, user["id"]))
        if not r:
            raise ApiError(404, "report not found")
        if r["status"] != "needs_clarification":
            raise ApiError(409, "this report does not need clarification")
        if location not in config.LOCATIONS:  # exact canonical names only - picked from the list we showed
            raise ApiError(400, "pick one of the valid locations")
        return _attach(user, r["id"], r["category"], location, bool(r["is_safety"]), r["summary"], r["reason"],
                       None, "clarified")


# ------------------------------------------------------------------ me too

def me_too(user, issue_id):
    require(user, "student")
    with db.LOCK:
        i = get_issue_row(issue_id, user)
        if i["confidential"]:
            raise ApiError(404, "issue not found")
        if i["status"] not in OPEN_STATUSES:
            raise ApiError(409, f"issue is {i['status']}; report it again if the problem is back")
        if _is_supporter(issue_id, user["id"]):
            raise ApiError(409, "you already reported or joined this issue")
        db.ex("INSERT INTO supporters(issue_id, student_id, via, created_at) VALUES (?,?,?,?)",
              (issue_id, user["id"], "metoo", now()))
        note = " (joined after the fix was marked resolved, so you can't vote on this round)" \
            if i["status"] == "pending_verification" else ""
        return {"issue": issue_view(db.q1("SELECT * FROM issues WHERE id=?", (issue_id,)), user),
                "message": "Added your me-too" + note}


# ------------------------------------------------------------------ staff / admin actions

def _staff_issue(user, issue_id):
    require(user, "staff", "admin")
    return get_issue_row(issue_id, user)  # staff only ever see their own department's non-confidential issues


def set_status(user, issue_id, status):
    with db.LOCK:
        i = _staff_issue(user, issue_id)
        allowed = {"in_progress": ("open",), "resolved": ("open", "in_progress")}
        if status not in allowed:
            raise ApiError(400, "status must be in_progress or resolved")
        if i["status"] not in allowed[status]:
            raise ApiError(409, f"cannot go from {i['status']} to {status}")
        if status == "resolved":
            if i["confidential"]:
                new = "closed"  # confidential cases are closed by Admin directly (no public verification)
                db.ex("UPDATE issues SET status='closed', updated_at=? WHERE id=?", (now(), issue_id))
            else:
                new = "pending_verification"
                db.ex("UPDATE issues SET status=?, verify_round=verify_round+1, resolved_at=?, updated_at=? WHERE id=?",
                      (new, now(), now(), issue_id))
        else:
            new = status
            db.ex("UPDATE issues SET status=?, updated_at=? WHERE id=?", (new, now(), issue_id))
        _log_status(issue_id, user["role"], i["status"], new)
        return issue_view(db.q1("SELECT * FROM issues WHERE id=?", (issue_id,)), user)


def issue_reports(user, issue_id):
    """Report texts behind an issue, for staff/admin. Never includes who wrote them."""
    _staff_issue(user, issue_id)
    return [{"report_id": r["id"], "text": r["text"], "status": r["status"], "flagged_fake": bool(r["flagged_fake"]),
             "injection_attempt": bool(r["injection_attempt"]), "triage_source": r["triage_source"],
             "created_at": r["created_at"]}
            for r in db.q("SELECT * FROM reports WHERE issue_id=? ORDER BY id", (issue_id,))]


def flag_report(user, report_id):
    require(user, "staff", "admin")
    with db.LOCK:
        r = db.q1("SELECT * FROM reports WHERE id=?", (report_id,))
        if not r:
            raise ApiError(404, "report not found")
        issue = db.q1("SELECT * FROM issues WHERE id=?", (r["issue_id"],)) if r["issue_id"] else None
        if user["role"] == "staff" and (not issue or not can_see(issue, user)):
            raise ApiError(404, "report not found")
        if r["flagged_fake"]:
            raise ApiError(409, "already flagged")
        db.ex("UPDATE reports SET flagged_fake=1 WHERE id=?", (report_id,))
        if not r["is_harassment"]:
            add_trust(r["student_id"], config.TRUST_REPORT_FLAGGED_FAKE, f"report #{report_id} flagged fake/spam by "
                      f"{user['role']}", issue_id=r["issue_id"], report_id=report_id)
        if issue:  # a fake report no longer counts towards the crowd or the vote
            db.ex("DELETE FROM supporters WHERE issue_id=? AND report_id=?", (issue["id"], report_id))
            left = db.q1("SELECT COUNT(*) FROM supporters WHERE issue_id=?", (issue["id"],))[0]
            if left == 0 and issue["status"] in OPEN_STATUSES:
                db.ex("UPDATE issues SET status='rejected', updated_at=? WHERE id=?", (now(), issue["id"]))
                _log_status(issue["id"], user["role"], issue["status"], "rejected", "all reports flagged fake")
        return {"ok": True, "message": f"report #{report_id} flagged" +
                ("" if r["is_harassment"] else f"; reporter trust {config.TRUST_REPORT_FLAGGED_FAKE}")}


# ------------------------------------------------------------------ verification & voting

def _vote_eligibility(i, sid):
    sup = _is_supporter(i["id"], sid)
    if not sup:
        return False, "only students who reported or joined this issue may vote"
    if i["resolved_at"] is None or sup["created_at"] > i["resolved_at"]:
        return False, "you joined after the fix was marked resolved"
    if db.q1("SELECT 1 FROM votes WHERE issue_id=? AND round=? AND student_id=?", (i["id"], i["verify_round"], sid)):
        return False, "you already voted"
    return True, None


def tally(i):
    est = {"confirm": 0.0, "dispute": 0.0}
    prob = {"confirm": 0.0, "dispute": 0.0}
    n = {"confirm": 0, "dispute": 0}
    est_disputers = 0
    for v in db.q("SELECT * FROM votes WHERE issue_id=? AND round=?", (i["id"], i["verify_round"])):
        w, p = weight(v["student_id"])
        n[v["vote"]] += 1
        if p:
            prob[v["vote"]] += w
        else:
            est[v["vote"]] += w
            est_disputers += v["vote"] == "dispute"
    cw = round(est["confirm"] + min(prob["confirm"], config.PROBATION_TOTAL_CAP), 2)
    dw = round(est["dispute"] + min(prob["dispute"], config.PROBATION_TOTAL_CAP), 2)
    reopen = (dw >= config.REOPEN_MIN_DISPUTE_WEIGHT and est_disputers >= config.REOPEN_MIN_DISTINCT_DISPUTERS
              and dw > cw)
    return {"round": i["verify_round"], "confirms": n["confirm"], "disputes": n["dispute"], "confirm_weight": cw,
            "dispute_weight": dw, "established_disputers": est_disputers, "reopen": reopen,
            "rule": f"reopen if dispute weight >= {config.REOPEN_MIN_DISPUTE_WEIGHT} AND >= "
                    f"{config.REOPEN_MIN_DISTINCT_DISPUTERS} established disputers AND dispute weight > confirm "
                    f"weight; new accounts add at most {config.PROBATION_TOTAL_CAP} per side"}


def vote(user, issue_id, choice):
    require(user, "student")
    if choice not in ("confirm", "dispute"):
        raise ApiError(400, "vote must be confirm or dispute")
    with db.LOCK:
        i = get_issue_row(issue_id, user)
        if i["confidential"] or i["status"] != "pending_verification":
            raise ApiError(409, "this issue is not awaiting verification")
        ok, why = _vote_eligibility(i, user["id"])
        if not ok:
            raise ApiError(409 if why == "you already voted" else 403, why)
        db.ex("INSERT INTO votes(issue_id, round, student_id, vote, created_at) VALUES (?,?,?,?,?)",
              (issue_id, i["verify_round"], user["id"], choice, now()))
        t = tally(i)
        if t["reopen"]:
            _settle(i, t, fixed=False)
        return {"issue": issue_view(db.q1("SELECT * FROM issues WHERE id=?", (issue_id,)), user), "tally": t,
                "message": "Issue reopened by credible disputes" if t["reopen"] else "Vote recorded"}


def _settle(i, t, fixed):
    """Close or reopen an issue and write the trust consequences for this round."""
    iid, rnd = i["id"], i["verify_round"]
    votes = db.q("SELECT * FROM votes WHERE issue_id=? AND round=?", (iid, rnd))
    win, lose = ("confirm", "dispute") if fixed else ("dispute", "confirm")
    margin = (t["confirm_weight"] - t["dispute_weight"]) * (1 if fixed else -1)
    for v in votes:
        if v["vote"] == win:
            add_trust(v["student_id"], config.TRUST_VOTE_CORRECT, f"vote '{v['vote']}' matched final outcome of "
                      f"issue #{iid}", issue_id=iid)
        elif margin >= config.CLEAR_MARGIN:
            add_trust(v["student_id"], config.TRUST_VOTE_WRONG, f"vote '{v['vote']}' went against the final outcome "
                      f"of issue #{iid} by a clear margin", issue_id=iid)
    if fixed:
        db.ex("UPDATE issues SET status='closed', updated_at=? WHERE id=?", (now(), iid))
        _log_status(iid, "system", i["status"], "closed", "verified fixed")
        # Reporters are rewarded only if OTHER students' confirmations outweigh the disputes (no self-reward).
        for r in db.q("SELECT * FROM reports WHERE issue_id=? AND status='linked' AND flagged_fake=0", (iid,)):
            others = sum(weight(v["student_id"])[0] for v in votes
                         if v["vote"] == "confirm" and v["student_id"] != r["student_id"])
            if others > t["dispute_weight"]:
                add_trust(r["student_id"], config.TRUST_REPORT_CONFIRMED, f"report #{r['id']} led to a fix confirmed by "
                          f"other students (issue #{iid})", issue_id=iid, report_id=r["id"])
    else:
        db.ex("UPDATE issues SET status='open', reopen_count=reopen_count+1, resolved_at=NULL, updated_at=? WHERE id=?",
              (now(), iid))
        _log_status(iid, "system", i["status"], "open", "reopened by credible disputes")


def finalize(user, issue_id):
    require(user, "admin")
    with db.LOCK:
        i = get_issue_row(issue_id, user)
        if i["status"] != "pending_verification":
            raise ApiError(409, "only issues awaiting verification can be finalized")
        t = tally(i)
        _settle(i, t, fixed=not t["reopen"])
        return issue_view(db.q1("SELECT * FROM issues WHERE id=?", (issue_id,)), user)


def sweep():
    """Lazily auto-finalize issues whose verification window has passed."""
    cutoff = now() - config.VERIFICATION_WINDOW_MINUTES * 60
    with db.LOCK:
        for i in db.q("SELECT * FROM issues WHERE status='pending_verification' AND resolved_at < ?", (cutoff,)):
            t = tally(i)
            _settle(i, t, fixed=not t["reopen"])


# ------------------------------------------------------------------ lists

def _sorted(rows, viewer):
    views = [issue_view(r, viewer) for r in rows]
    active = {"open": 0, "in_progress": 0, "pending_verification": 1, "closed": 2, "rejected": 3}
    return sorted(views, key=lambda v: (active.get(v["status"], 9), -v["priority_tier"], -v["importance"], v["id"]))


def board(user):
    sweep()
    return _sorted(db.q("SELECT * FROM issues WHERE confidential=0 AND status!='rejected'"), user)


def my_items(user):
    require(user, "student")
    sweep()
    issues = db.q("SELECT i.* FROM issues i JOIN supporters s ON s.issue_id=i.id WHERE s.student_id=?", (user["id"],))
    pending = db.q("SELECT id, text, category, is_safety, location_mentioned, created_at FROM reports "
                   "WHERE student_id=? AND status='needs_clarification' ORDER BY id", (user["id"],))
    return {"issues": _sorted(issues, user), "needs_clarification": [dict(r) for r in pending],
            "valid_locations": list(config.LOCATIONS)}


def verifications(user):
    require(user, "student")
    sweep()
    rows = db.q("SELECT i.* FROM issues i JOIN supporters s ON s.issue_id=i.id WHERE s.student_id=? "
                "AND i.status='pending_verification' AND i.confidential=0", (user["id"],))
    return [v for v in (issue_view(r, user) for r in rows) if v.get("can_vote")]


def staff_queue(user):
    require(user, "staff")
    sweep()
    return _sorted(db.q("SELECT * FROM issues WHERE department=? AND confidential=0", (user["department"],)), user)


def admin_issues(user):
    require(user, "admin")
    sweep()
    return _sorted(db.q("SELECT * FROM issues"), user)


def admin_clarifications(user):
    require(user, "admin")
    return [{"report_id": r["id"], "text": r["text"], "category": r["category"], "is_safety": bool(r["is_safety"]),
             "mentioned_place": r["location_mentioned"], "created_at": r["created_at"], "reporter": "hidden"}
            for r in db.q("SELECT * FROM reports WHERE status='needs_clarification' ORDER BY is_safety DESC, id")]
