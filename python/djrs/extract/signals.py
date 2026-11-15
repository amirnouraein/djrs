"""Model signal receivers: side effects a view's code doesn't show."""

from __future__ import annotations

from .describe import dotted_path

SIGNALS = ["pre_init", "post_init", "pre_save", "post_save", "pre_delete", "post_delete", "m2m_changed"]


def extract_signals():
    from django.apps import apps
    from django.db.models import signals

    by_id = {id(m): m._meta.label for m in apps.get_models(include_auto_created=True)}
    any_sender = id(None)
    out = {}
    for name in SIGNALS:
        signal = getattr(signals, name)
        entries = []
        for item in list(signal.receivers):
            try:
                lookup_key, ref = item[0], item[1]
                sender_id = lookup_key[1]
            except Exception:
                continue
            receiver = ref() if callable(ref) and not hasattr(ref, "__code__") else ref
            if receiver is None:
                continue
            if sender_id == any_sender:
                sender = "*"
            else:
                sender = by_id.get(sender_id, "<non-model sender>")
            entries.append({"sender": sender, "receiver": dotted_path(receiver)})
        entries.sort(key=lambda e: (e["sender"], e["receiver"]))
        if entries:
            out[name] = entries
    return out
