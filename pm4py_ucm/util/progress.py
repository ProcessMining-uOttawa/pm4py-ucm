"""Optional progress reporting for long-running pipelines.

Every long loop in the package (variant replay, per-cell family
mining, family statistics) accepts an optional ``progress_callback``
— a plain callable::

    def progress_callback(stage: str, done: int, total: int) -> None: ...

``stage`` is a short human label ("Replaying cases"), ``done`` /
``total`` the work counts. The callback fires at stage start
(``done=0``), on completion (``done=total``), and at throttled
intervals in between — never more than ~200 times per stage, so a
callback that repaints a UI cannot slow down the work it is
measuring. Passing ``None`` (the default everywhere) costs nothing.

Consumers decide the presentation: the V3 Streamlit app renders a
progress bar with an elapsed-based remaining-time estimate; a script
can pass ``lambda s, d, t: print(f"{s}: {d}/{t}")`` or adapt a
``tqdm`` bar.
"""

from __future__ import annotations

from typing import Callable, Optional


#: ``callback(stage, done, total)`` — see the module docstring.
ProgressCallback = Callable[[str, int, int], None]


class Ticker:
    """Throttled emitter for one stage of work.

    Wraps an optional :data:`ProgressCallback`: ``tick()`` per work
    item, ``finish()`` when the stage ends (also emitted automatically
    when the tick count reaches ``total``). With ``callback=None``
    every method is a no-op cheap enough for tight loops.
    """

    __slots__ = ("_callback", "stage", "total", "done", "_every", "_next")

    def __init__(
        self,
        callback: Optional[ProgressCallback],
        stage: str,
        total: int,
        report_every: Optional[int] = None,
    ) -> None:
        self._callback = callback
        self.stage = stage
        self.total = max(0, int(total))
        self.done = 0
        #: Emit roughly 200 updates per stage regardless of size.
        self._every = int(report_every) if report_every else max(
            1, self.total // 200,
        )
        self._next = self._every
        if callback is not None:
            callback(stage, 0, self.total)

    def tick(self, n: int = 1) -> None:
        """Advance by ``n`` items."""
        self.done += n
        if self._callback is None:
            return
        if self.done >= self._next or self.done >= self.total:
            self._callback(
                self.stage, min(self.done, self.total), self.total,
            )
            self._next = self.done + self._every

    def finish(self) -> None:
        """Emit the final ``done == total`` update (idempotent)."""
        if self._callback is not None and self.done < self.total:
            self.done = self.total
            self._callback(self.stage, self.total, self.total)
