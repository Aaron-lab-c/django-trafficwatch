"""View decorators. The middleware reads these attributes in ``process_view``.

Both work on function views and on class-based views: apply them to the class itself (the
middleware looks at ``view_func.view_class``) or to a method through
``method_decorator(trafficwatch_rule(...), name="post")`` (the middleware also looks at the
handler for the request's method and at ``dispatch``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import replace
from typing import Any, TypeVar

from .rules import Rule

EXEMPT_ATTR = "trafficwatch_exempt"
RULES_ATTR = "trafficwatch_rules"

# ``method_decorator`` applies the decorator to a throw-away function named ``dummy`` and
# copies its attributes onto the method, so an auto-generated rule name would read
# ``view:django.utils.decorators._update_method_wrapper.<locals>.dummy`` and be shared by
# every view decorated that way. Such names are rewritten when the rule is collected.
_METHOD_DECORATOR_PREFIX = "view:django.utils.decorators."

V = TypeVar("V", bound=Callable[..., Any])


def trafficwatch_exempt(view: V) -> V:
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
    key_func: Callable[..., str] | None = None,
) -> Callable[[V], V]:
    """Attach a view-specific rule that overrides PATH_RULES and the global rule.

    Stack the decorator to enforce several limits at once::

        @trafficwatch_rule(60, 3)          # 3 per minute
        @trafficwatch_rule(86400, 50)      # and 50 per day
        def send_otp(request): ...
    """
    if window_seconds <= 0 or max_requests <= 0:
        raise ValueError("window_seconds and max_requests must be positive")

    def decorator(view: V) -> V:
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


def _fix_names(rules: Iterable[Rule], view_class: type, handler_name: str) -> list[Rule]:
    fixed = []
    for rule in rules:
        if rule.name.startswith(_METHOD_DECORATOR_PREFIX):
            suffix = rule.name.rsplit("#", 1)[1] if "#" in rule.name else ""
            base = f"view:{view_class.__module__}.{view_class.__qualname__}.{handler_name}"
            rule = replace(rule, name=f"{base}#{suffix}" if suffix else base)
        fixed.append(rule)
    return fixed


def _handler_rules(view_class: type, handler_name: str) -> list[Rule]:
    handler = getattr(view_class, handler_name, None)
    rules = getattr(handler, RULES_ATTR, ()) if handler is not None else ()
    return _fix_names(rules, view_class, handler_name)


def _unique(rules: Iterable[Rule]) -> tuple[Rule, ...]:
    seen: set[Rule] = set()
    unique = []
    for rule in rules:
        if rule not in seen:
            seen.add(rule)
            unique.append(rule)
    return tuple(unique)


def class_rules(view_class: type, method: str | None = None) -> tuple[Rule, ...]:
    """Rules on a class-based view: the class itself, ``dispatch`` and the handler for
    ``method`` (so ``method_decorator(..., name="post")`` works)."""
    rules: list[Rule] = list(getattr(view_class, RULES_ATTR, ()))
    rules += _handler_rules(view_class, "dispatch")
    if method:
        rules += _handler_rules(view_class, method.lower())
    return _unique(rules)


def view_rules(view_func: Any, method: str | None = None) -> tuple[Rule, ...]:
    """Rules attached to a resolved view callable (function or ``as_view()`` wrapper).
    Pass the request ``method`` to include rules on that handler of a class-based view."""
    own = tuple(getattr(view_func, RULES_ATTR, ()))
    view_class = getattr(view_func, "view_class", None)
    if view_class is None:
        return own
    # as_view() copies dispatch.__dict__ onto the wrapper, so ``own`` may carry rules set
    # through method_decorator(name="dispatch") under the throw-away name.
    return _unique(_fix_names(own, view_class, "dispatch") + list(class_rules(view_class, method)))


def is_exempt_class(view_class: type, method: str | None = None) -> bool:
    if getattr(view_class, EXEMPT_ATTR, False):
        return True
    for name in ("dispatch", method.lower() if method else None):
        if name and getattr(getattr(view_class, name, None), EXEMPT_ATTR, False):
            return True
    return False


def is_exempt_view(view_func: Any, method: str | None = None) -> bool:
    if getattr(view_func, EXEMPT_ATTR, False):
        return True
    view_class = getattr(view_func, "view_class", None)
    return view_class is not None and is_exempt_class(view_class, method)
