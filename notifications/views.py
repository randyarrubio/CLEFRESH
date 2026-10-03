import json
import time

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, StreamingHttpResponse
from django.views.decorators.http import require_POST

from .models import Notification


def _serialize_notifications(qs):
    return [
        {
            'id': n.id,
            'message': n.message,
            'is_read': n.is_read,
            'created_at': n.created_at.isoformat(),
            'link': n.link,
        }
        for n in qs
    ]


@login_required
def notifications_api_view(request):
    qs = Notification.objects.filter(user=request.user).order_by('-created_at')
    data = {
        'unread_count': qs.filter(is_read=False).count(),
        'notifications': _serialize_notifications(qs[:20]),
    }
    return JsonResponse(data)


@login_required
@require_POST
def mark_notifications_read_view(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return JsonResponse({'status': 'ok'})


# ── Server-Sent Events stream ──────────────────────────────────────────────
# Each authenticated client holds one thread for the duration of the
# connection (WSGI model). Run gunicorn with --worker-class gthread and
# enough --threads to support concurrent users, or use an ASGI server.

def _sse_event(event_id, payload):
    """Format a single SSE message frame."""
    return f"id: {event_id}\ndata: {json.dumps(payload)}\n\n"


def _sse_generator(user_id, resume_after_id):
    """
    Generator that yields SSE frames for the given user.
    Polls the DB every 3 s; releases the DB connection while sleeping
    so it isn't held for the full lifetime of the stream.
    Sends a keepalive comment every 15 s to prevent proxy timeouts.
    Terminates after ~30 min (600 iterations) so the client reconnects
    and the thread is recycled.
    """
    from django.db import connection as _db

    try:
        # ── Initial snapshot ──────────────────────────────────────
        qs = Notification.objects.filter(user_id=user_id).order_by('-created_at')[:20]
        notif_list = list(qs)
        last_id = notif_list[0].id if notif_list else 0
        last_id = max(last_id, resume_after_id)
        unread = Notification.objects.filter(user_id=user_id, is_read=False).count()
        first_event = _sse_event(last_id, {
            'unread_count': unread,
            'notifications': _serialize_notifications(notif_list),
        })
        _db.close()   # release before suspending on first yield
        yield first_event

        # ── Poll loop ────────────────────────────────────────────
        tick = 0
        for _ in range(600):
            _db.close()       # release connection before blocking sleep
            time.sleep(3)
            tick += 1

            new = list(
                Notification.objects
                .filter(user_id=user_id, id__gt=last_id)
                .order_by('id')
            )

            if new:
                last_id = new[-1].id
                unread = Notification.objects.filter(user_id=user_id, is_read=False).count()
                recent = list(
                    Notification.objects
                    .filter(user_id=user_id)
                    .order_by('-created_at')[:20]
                )
                yield _sse_event(last_id, {
                    'unread_count': unread,
                    'notifications': _serialize_notifications(recent),
                })
            elif tick % 5 == 0:
                # keepalive comment every 15 s (5 × 3 s)
                yield ": keepalive\n\n"

    except GeneratorExit:
        _db.close()


@login_required
def notifications_stream_view(request):
    # Honor Last-Event-ID for reconnect — avoids re-toasting already-seen events
    try:
        resume_after_id = int(request.headers.get('Last-Event-ID', 0))
    except (TypeError, ValueError):
        resume_after_id = 0

    response = StreamingHttpResponse(
        _sse_generator(request.user.id, resume_after_id),
        content_type='text/event-stream; charset=utf-8',
    )
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'   # tell nginx not to buffer the stream
    return response
