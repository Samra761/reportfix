"""ReportFix HTTP API. Run with:  python app.py   (then open http://127.0.0.1:8000)

Identity: the UI sends the selected user's id in the X-User-Id header (judge mode, no passwords).
"""
import asyncio
import logging
import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

import config
import core
import db

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
app = FastAPI(title="ReportFix")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "index.html")  # Vercel serves public/ itself


async def _body(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


async def run(fn, *args):
    """Run blocking core logic (SQLite, LLM call) off the event loop and map ApiError -> HTTP error."""
    try:
        return await asyncio.to_thread(fn, *args)
    except core.ApiError as e:
        raise HTTPException(status_code=e.status, detail=e.message)


def who(request):
    return core.get_user(request.headers.get("x-user-id"))


async def as_user(request, fn, *args):
    def go():
        return fn(who(request), *args)
    return await run(go)


@app.get("/")
async def index():
    return FileResponse(STATIC)


# ---------------- judge access
@app.get("/api/config")
async def get_config():
    return {"locations": list(config.LOCATIONS), "departments": config.DEPARTMENTS, "categories": config.CATEGORIES,
            "ai_enabled": bool(os.environ.get("ANTHROPIC_API_KEY")), "model": config.LLM_MODEL}


@app.get("/api/users")
async def users():
    return await run(core.list_users)


@app.post("/api/users/new_student")
async def new_student():
    return await run(core.new_student)


@app.post("/api/reset")
async def reset():
    await run(db.reset)
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    return await run(lambda: core.user_view(who(request)))


# ---------------- students
@app.post("/api/reports")
async def create_report(request: Request):
    data = await _body(request)
    return await as_user(request, core.submit_report, data.get("text"))


@app.post("/api/reports/{report_id}/clarify")
async def clarify(request: Request, report_id: int):
    data = await _body(request)
    return await as_user(request, core.clarify, report_id, data.get("location"))


@app.get("/api/my")
async def my_items(request: Request):
    return await as_user(request, core.my_items)


@app.get("/api/board")
async def board(request: Request):
    return await as_user(request, core.board)


@app.get("/api/verifications")
async def verifications(request: Request):
    return await as_user(request, core.verifications)


@app.post("/api/issues/{issue_id}/metoo")
async def me_too(request: Request, issue_id: int):
    return await as_user(request, core.me_too, issue_id)


@app.post("/api/issues/{issue_id}/vote")
async def vote(request: Request, issue_id: int):
    data = await _body(request)
    return await as_user(request, core.vote, issue_id, data.get("vote"))


@app.get("/api/issues/{issue_id}")
async def issue(request: Request, issue_id: int):
    return await as_user(request, lambda u, i: core.issue_view(core.get_issue_row(i, u), u), issue_id)


@app.get("/api/trust/{student_id}")
async def trust(request: Request, student_id: int):
    return await as_user(request, core.trust_log, student_id)


# ---------------- staff / admin
@app.get("/api/staff/queue")
async def staff_queue(request: Request):
    return await as_user(request, core.staff_queue)


@app.post("/api/issues/{issue_id}/status")
async def set_status(request: Request, issue_id: int):
    data = await _body(request)
    return await as_user(request, core.set_status, issue_id, data.get("status"))


@app.get("/api/issues/{issue_id}/reports")
async def issue_reports(request: Request, issue_id: int):
    return await as_user(request, core.issue_reports, issue_id)


@app.post("/api/reports/{report_id}/flag")
async def flag(request: Request, report_id: int):
    return await as_user(request, core.flag_report, report_id)


@app.get("/api/admin/issues")
async def admin_issues(request: Request):
    return await as_user(request, core.admin_issues)


@app.get("/api/admin/clarifications")
async def admin_clarifications(request: Request):
    return await as_user(request, core.admin_clarifications)


@app.post("/api/issues/{issue_id}/finalize")
async def finalize(request: Request, issue_id: int):
    return await as_user(request, core.finalize, issue_id)


if __name__ == "__main__":
    import uvicorn
    db.conn()
    print("AI triage:", "ON (" + config.LLM_MODEL + ")" if os.environ.get("ANTHROPIC_API_KEY")
          else "OFF - keyword rules only (set ANTHROPIC_API_KEY to enable)")
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", 8000)))
