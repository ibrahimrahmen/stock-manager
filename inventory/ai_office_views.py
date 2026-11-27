"""Views for the AI office (3D office page + its small JSON API).

Owner-only (superuser): the finance employee sees all the money figures.
The daily cron endpoint is unauthenticated like the other Railway crons, so it
is made idempotent (at most one automatic report per day) to cap AI cost.
"""
import json
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import ai_office
from .models import AIConversation, AIEmployee, AITask

# A task still "working" after this long was killed (worker restart): show it as failed.
STALE_AFTER = timedelta(minutes=5)
PERIOD_KEYS = ("yesterday", "last7", "last14", "month", "last_month")


def _owner_only(view):
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden("Accès réservé au propriétaire.")
        return view(request, *args, **kwargs)
    wrapped.__name__ = view.__name__
    wrapped.__doc__ = view.__doc__
    return login_required(login_url="/login/")(wrapped)


def _expire_stale():
    AITask.objects.filter(
        status__in=[AITask.WORKING, AITask.QUEUED],
        created_at__lt=timezone.now() - STALE_AFTER,
    ).update(status=AITask.FAILED, error="Interrompu (le serveur a redémarré).",
             finished_at=timezone.now())


def _task_json(t, full=False):
    d = {
        "id": t.id, "title": t.title, "status": t.status,
        "status_label": t.get_status_display(),
        "period_from": t.period_from.isoformat() if t.period_from else None,
        "period_to": t.period_to.isoformat() if t.period_to else None,
        "triggered_by": t.triggered_by,
        "created_at": t.created_at.isoformat(),
        "finished_at": t.finished_at.isoformat() if t.finished_at else None,
        # Headline figure for the history list: net profit (finance) or the
        # ads' estimated profit (marketing).
        "net_profit": (t.result_data or {}).get(
            "net_profit", ((t.result_data or {}).get("totals") or {}).get("ad_est_profit")),
        "error": t.error,
    }
    if full:
        d.update({"instruction": t.instruction, "result_text": t.result_text,
                  "result_data": t.result_data})
    return d


def _employee_json(e):
    current = e.tasks.filter(status__in=[AITask.QUEUED, AITask.WORKING]).first()
    recent = list(e.tasks.all()[:8])
    return {
        "slug": e.slug, "name": e.name, "role": e.role,
        "role_label": e.get_role_display(), "title": e.title or e.get_role_display(),
        "job_description": e.job_description, "color": e.color,
        "desk_index": e.desk_index,
        "default_period": ai_office.ROLE_DAILY_PERIOD.get(e.role, "yesterday"),
        "state": "working" if current else "idle",
        "current_task": _task_json(current) if current else None,
        "recent_tasks": [_task_json(t) for t in recent],
    }


@_owner_only
def office(request):
    return render(request, "inventory/ai_office.html", {"period_keys": PERIOD_KEYS})


@_owner_only
def api_state(request):
    _expire_stale()
    emps = AIEmployee.objects.filter(is_active=True)
    return JsonResponse({"employees": [_employee_json(e) for e in emps],
                         "now": timezone.now().isoformat()})


@_owner_only
def api_task_detail(request, pk):
    return JsonResponse(_task_json(get_object_or_404(AITask, pk=pk), full=True))


@_owner_only
@require_POST
def api_give_task(request, slug):
    """Give the employee a task. Body: {"period": "yesterday|last7|month|last_month",
    "instruction": "optional question"}. Runs now and returns the finished task."""
    emp = get_object_or_404(AIEmployee, slug=slug, is_active=True)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        body = {}
    period = body.get("period") or "yesterday"
    if period not in PERIOD_KEYS:
        return JsonResponse({"error": "période inconnue"}, status=400)
    _expire_stale()
    if emp.tasks.filter(status__in=[AITask.QUEUED, AITask.WORKING]).exists():
        return JsonResponse({"error": f"{emp.name} travaille déjà sur une tâche."}, status=409)
    d_from, d_to, label = ai_office.period_for(period)
    task = ai_office.create_task(
        emp, d_from, d_to, label, instruction=body.get("instruction", ""),
        user=request.user)
    ai_office.run_task(task)
    return JsonResponse(_task_json(task, full=True))


@_owner_only
@require_POST
def api_update_employee(request, slug):
    """Owner edits name / title / job description / shirt colour."""
    emp = get_object_or_404(AIEmployee, slug=slug)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "JSON invalide"}, status=400)
    fields = []
    for f, maxlen in (("name", 60), ("title", 80), ("job_description", 8000)):
        if f in body:
            setattr(emp, f, str(body[f])[:maxlen].strip())
            fields.append(f)
    color = str(body.get("color", ""))
    if color.startswith("#") and len(color) in (4, 7):
        emp.color = color
        fields.append("color")
    if not emp.name:
        return JsonResponse({"error": "Le nom est obligatoire."}, status=400)
    if fields:
        emp.save(update_fields=fields)
    return JsonResponse(_employee_json(emp))


@csrf_exempt
def cron_ai_office_daily(request):
    """Railway cron, every morning: each active employee writes its report
    (finance: yesterday, marketing: last 7 days). At most one automatic report
    per employee per day, whoever calls this URL (caps the AI cost).
    Each call runs ONE employee (the next one not done today) so a call stays
    under the gunicorn timeout; railway.toml schedules a call per employee."""
    today = timezone.localdate()
    for emp in AIEmployee.objects.filter(is_active=True):
        period = ai_office.ROLE_DAILY_PERIOD.get(emp.role)
        if period is None:
            continue
        if emp.tasks.filter(triggered_by="cron", created_at__date=today).exists():
            continue
        d_from, d_to, label = ai_office.period_for(period, today)
        task = ai_office.create_task(emp, d_from, d_to, label, triggered_by="cron")
        ai_office.run_task(task)
        return JsonResponse({"status": task.status, "employee": emp.slug, "task": task.id})
    return JsonResponse({"status": "skipped", "reason": "all employees already done today"})


def _conversation_json(conv):
    return {"id": conv.id if conv else None,
            "messages": (conv.messages if conv else [])}


@_owner_only
def api_chat(request, slug):
    """GET: the current conversation. POST {"message": str} -> reply;
    POST {"new": true} -> start a fresh conversation."""
    emp = get_object_or_404(AIEmployee, slug=slug, is_active=True)
    conv = emp.conversations.first()
    if request.method == "GET":
        return JsonResponse(_conversation_json(conv))
    if request.method != "POST":
        return JsonResponse({"error": "méthode non autorisée"}, status=405)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"error": "JSON invalide"}, status=400)
    if body.get("new"):
        conv = AIConversation.objects.create(employee=emp)
        return JsonResponse(_conversation_json(conv))
    text = str(body.get("message", "")).strip()
    if not text:
        return JsonResponse({"error": "Message vide."}, status=400)
    if conv is None:
        conv = AIConversation.objects.create(employee=emp)
    reply, err = ai_office.chat(emp, conv, text)
    data = _conversation_json(conv)
    data["reply"] = reply
    if err:
        data["error"] = "L'IA n'a pas pu répondre : " + err[:300]
    return JsonResponse(data, status=200 if reply else 502)
