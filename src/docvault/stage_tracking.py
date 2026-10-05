"""Optional stage instrumentation; parsers work independently of job persistence."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar

StageFactory = Callable[[str], AbstractContextManager[None]]
_stage_factory: ContextVar[StageFactory] = ContextVar(
    "stage_factory", default=lambda _: nullcontext()
)


@contextmanager
def collect_processing_stages(factory: StageFactory) -> Iterator[None]:
    """Install a recorder for this execution context and restore it even after failure."""
    token = _stage_factory.set(factory)
    try:
        yield
    finally:
        _stage_factory.reset(token)


def processing_stage(stage: str) -> AbstractContextManager[None]:
    """Record a named unit of work when running inside an instrumented job."""
    return _stage_factory.get()(stage)
