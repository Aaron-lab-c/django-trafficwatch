"""View decorators. The middleware reads these attributes in ``process_view``.

Both work on function views and on class-based views (``as_view()`` copies attributes
set with ``method_decorator``; applying the decorator to the class itself also works
because the middleware looks at ``view_func.view_class``).
"""

from __future__ import annotations

from collections.abc import Iterable

from .rules import Rule

EXEMPT_ATTR = "trafficwatch_exempt"
RULES_ATTR = "trafficwatch_rules"


def trafficwatch_exempt(view):
    """Never count or block this view."""
    setattr(view, EXEMPT_ATTR, True)
    return view


def trafficwatch_rule(
    window_seconds: int,
    max_requests: int,
    *,
    methods: Iterable[str] | None = None,
    name: str | None = None,
    block: bool | None = None,
    key_func=None,
):
    """Attach a view-specific rule that overrides PATH_RULES and the global rule.

    Stack the decorator to enforce several limits at once::

        @trafficwatch_rule(60, 3)          # 3 per minute
        @trafficwatch_rule(86400, 50)      # and 50 per day
        def send_otp(request): ...
    """
    if window_seconds <= 0 or max_requests <= 0:
        raise ValueError("window_seconds and max_requests must be positive")

    def decorator(view):
        existing = tuple(getattr(view, RULES_ATTR, ()))
        rule_name = name or f"view:{view.__module__}.{view.__qualname__}"
        if existing and name is None:
            rule_name = f"{rule_name}#{len(existing)}"
        rule = Rule(
            name=rule_name,
            window=window_seconds,
            limit=max_requests,
            methods=frozenset(m.upper() for m in methods) if methods else None,
            block=block,
            key_func=key_func,
        )
        # Decorators apply bottom-up; keep declaration order (top first) for readability.
        setattr(view, RULES_ATTR, (rule, *existing))
        return view

    return decorator


def view_rules(view_func) -> tuple[Rule, ...]:
    """Rules attached to a resolved view callable (function or ``as_view()`` wrapper)."""
    rules = getattr(view_func, RULES_ATTR, None)
    if rules is None:
        view_class = getattr(view_func, "view_class", None)
        rules = getattr(view_class, RULES_ATTR, ()) if view_class is not None else ()
    return tuple(rules)


def is_exempt_view(view_func) -> bool:
    if getattr(view_func, EXEMPT_ATTR, False):
        return True
    view_class = getattr(view_func, "view_class", None)
    return bool(view_class is not None and getattr(view_class, EXEMPT_ATTR, False))
