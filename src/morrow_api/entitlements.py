from datetime import datetime, timezone

from fastapi import HTTPException

MODULES = {
    "profile": "My profile",
    "applications": "Applications",
    "files": "Private files",
    "resumes": "Resume studio",
    "research": "Research",
    "learning": "Learning path",
    "automations": "Automations",
    "outreach": "Outreach",
    "analytics": "Insights",
}


def access_for(conn, user):
    expired = user["plan_expires_at"] and user["plan_expires_at"] <= datetime.now(timezone.utc)
    slug = "free" if expired else user["plan_slug"]
    plan = conn.execute(
        "SELECT slug,name,modules FROM plans WHERE slug=%s AND active", (slug,)
    ).fetchone()
    if plan is None:
        plan = conn.execute("SELECT slug,name,modules FROM plans WHERE slug='free'").fetchone()
    return {
        "effective_plan": plan["slug"],
        "plan_name": plan["name"],
        "modules": list(MODULES) if user["role"] == "admin" else plan["modules"],
    }


def require_module(user, module):
    if module not in user["modules"]:
        raise HTTPException(
            403,
            {
                "code": "plan_required",
                "module": module,
                "message": "Your plan does not include this module",
            },
        )


def record_module(collection):
    return {
        "research": "research",
        "learning": "learning",
        "runs": "automations",
        "outreach": "outreach",
        "resumes": "resumes",
        "analytics": "analytics",
    }.get(collection, "files")
