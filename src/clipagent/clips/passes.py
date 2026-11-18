"""The two independent editorial passes (ranking, standalone) over the reviewed candidates, plus the duplicate/reuse policy.

Rules come from CONTENT_EDITING_RULES.md and are enforced by `plan.validate`: ranking = one narrow theme, honest count (minimum 3, never padded),
standalone = one complete viewing experience of any length, a source moment appears in both only as a materially different edit (recorded reuse),
no near-identical videos. Runtime is metadata: nothing here reads it as quality; the platform's technical upload limit is the only duration check.
"""
from collections import defaultdict

from .. import plan as plan_mod
from .. import style
from ..contentflow import _partition
from . import events as events_mod
from .common import ClipsError, read_json, run_dir, slug, write_json


def _moment(c: dict, rv: dict, rank: int, tight: bool, title: str) -> dict:
    s, e = (c["tight_start"], c["tight_end"]) if tight else (rv.get("start", c["start"]), rv.get("end", c["end"]))
    return {"rank": rank, "title": title, "start": round(s, 2), "end": round(e, 2), "verified": True, "candidate_id": c["id"], "peak_t": c["peak_t"],
            "hook": rv.get("hook", c["hook"]), "setup": rv.get("setup", c["setup"]), "payoff": rv.get("payoff", c["payoff"]),
            "treatment": rv.get("treatment", "tightened_continuous"), "trim_reason": rv.get("trim_reason")}


def build(source_id: str, root=None) -> dict:
    d = run_dir(source_id, root)
    led = read_json(d / "candidates" / "ledger.json")
    reviews = {r["id"]: r for r in read_json(d / "candidates" / "reviews.effective.json")}
    S = style.load()["limits"]
    cands = {c["id"]: c for c in led["candidates"]}
    accepted = [c for c in led["candidates"] if reviews[c["id"]]["verdict"] == "accept"]
    videos, ledger_rows = [], {c["id"]: {"candidate": c["id"], "status": "rejected", "reason": reviews[c["id"]].get("reason", ""), "outputs": []} for c in led["candidates"]}
    # ---- ranking pass
    by_theme = defaultdict(list)
    for c in accepted:
        if not reviews[c["id"]].get("standalone_only"):
            by_theme[reviews[c["id"]]["theme"]].append(c)
    ranking_none = []
    for theme, items in sorted(by_theme.items()):
        if len(items) < S["min_ranks"]:
            ranking_none.append(f"theme {theme!r} has {len(items)} honest entr{'y' if len(items) == 1 else 'ies'} (< {S['min_ranks']}); never padded, so no ranking")
            continue
        items = sorted(items, key=lambda c: c["score"])                           # ascending: the strongest ends up as #1 (played last)
        sizes = _partition(len(items)) if len(items) > S.get("layout_max_ranks", 10) else [len(items)]
        pos = 0
        for part, n in enumerate(sizes, 1):
            chunk = items[pos:pos + n]
            pos += n
            rid = f"R{len([v for v in videos if v['format'] == 'ranking']) + 1:02d}"
            title = f"{theme}" + (f" Part {part}" if len(sizes) > 1 else "")
            videos.append({"id": rid, "format": "ranking", "category": title, "name": slug(title), "layout_title": title,
                           "moments": [_moment(c, reviews[c["id"]], rank, True, reviews[c["id"]]["title"]) for rank, c in zip(range(len(chunk), 0, -1), chunk)]})
            for c in chunk:
                ledger_rows[c["id"]].update(status="ranking", reason=f"entry in {title}")
                ledger_rows[c["id"]]["outputs"].append(rid)
    # ---- standalone pass
    standalone_none = []
    ranking_of = {m["candidate_id"]: v for v in videos if v["format"] == "ranking" for m in v["moments"]}
    for c in sorted(accepted, key=lambda c: c["start"]):
        rv = reviews[c["id"]]
        full = [rv.get("start", c["start"]), rv.get("end", c["end"])]
        rk = ranking_of.get(c["id"])
        reuse = None
        if rk is not None:
            tight = next(m for m in rk["moments"] if m["candidate_id"] == c["id"])
            added = (full[1] - full[0]) - (tight["end"] - tight["start"])
            sim = plan_mod.footage_similarity([{"start": full[0], "end": full[1]}], [tight])
            if added < max(2.0, 0.25 * (tight["end"] - tight["start"])) or sim >= plan_mod.NEAR_IDENTICAL:
                standalone_none.append(f"{c['id']}: standalone would repeat its ranking entry ({sim:.0%} of the same footage, +{added:.1f}s of extra context); kept in {rk['id']} only")
                continue
            reuse = {"of": rk["id"], "reason": f"ranking entry is the payoff-focused cut ({tight['end'] - tight['start']:.1f}s); the standalone adds {added:.1f}s of setup/aftermath "
                                               f"with its own headline, so the viewing experience differs"}
        sid = f"S{len([v for v in videos if v['format'] == 'standalone']) + 1:02d}"
        m = _moment(c, rv, 1, False, rv["headline"])
        m["start"], m["end"] = round(full[0], 2), round(full[1], 2)
        v = {"id": sid, "format": "standalone", "name": slug(rv["headline"]), "moments": [m]}
        if reuse:
            v["reuse"] = reuse
        videos.append(v)
        ledger_rows[c["id"]]["status"] = "ranking+standalone" if rk else "standalone"
        ledger_rows[c["id"]]["outputs"].append(sid)
        if reuse:
            ledger_rows[c["id"]]["reuse"] = reuse
        else:
            ledger_rows[c["id"]]["reason"] = "complete event as one standalone"
    # ---- platform limit (the only duration check): split only at natural boundaries
    res = plan_mod.validate(videos)
    if res.errors:
        raise ClipsError("plan failed policy checks: " + " | ".join(res.errors))
    for v in videos:
        for m in v["moments"]:
            m["runtime_sec"] = round(m["end"] - m["start"], 2)
        v["runtime_sec"] = round(sum(m["end"] - m["start"] for m in v["moments"]), 2)
        write_json(d / "projects" / ("rankings" if v["format"] == "ranking" else "standalone") / f"{v['id']}-{v['name']}.json", v)
    rows = list(ledger_rows.values())
    led["passes"] = {"ranking": {"videos": [v["id"] for v in videos if v["format"] == "ranking"], "none_because": ranking_none},
                     "standalone": {"videos": [v["id"] for v in videos if v["format"] == "standalone"], "none_because": standalone_none}}
    led["ledger"] = rows
    assert {r["candidate"] for r in rows} == set(cands), "every candidate must be accounted for exactly once"
    write_json(d / "candidates" / "ledger.json", led)
    write_json(d / "candidates" / "recovery-ledger.json", rows)
    return {"videos": videos, "ranking_none": ranking_none, "standalone_none": standalone_none, "ledger": rows}
