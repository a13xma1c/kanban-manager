"""
Model-based (stateful) testing of the database service.

Hypothesis drives random sequences of real operations - create, move (across
columns, into WIP-limited and done columns), reorder, complete, archive,
restore, delete, add and remove dependencies - against both the service and a
tiny in-memory model of what the board *should* look like.  After every step
the two must agree on:

* the order of the open cards in every column (what the board shows),
* which tasks are completed, archived, blocked,
* the dependency graph, which must stay acyclic,
* WIP limits (a full column refuses cards moved into it).

When something disagrees Hypothesis shrinks the run to a minimal sequence of
steps that reproduces it.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, Phase, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from hypothesis.stateful import (  # noqa: E402
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    precondition,
    rule,
)

from kanbanmanager.database.service import DatabaseService  # noqa: E402
from support import seeded_manager  # noqa: E402

WIP_LIMIT = 2
titles = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1, max_size=16).filter(
    lambda s: s.strip())


class BoardModel(RuleBasedStateMachine):
    tasks = Bundle("tasks")

    @initialize()
    def setup(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="kanban-model-"))
        self.service = DatabaseService(seeded_manager(self.tmp / "model.db", seed=False), actor="Model")
        ws = self.service.create_workspace("Model")
        board = self.service.create_board(ws.id, "Board", labels_def=[], columns_def=[
            {"name": "Todo", "type": "TODO"},
            {"name": "Doing", "type": "IN_PROGRESS", "wip_limit": WIP_LIMIT},
            {"name": "Review", "type": "REVIEW"},
            {"name": "Done", "type": "DONE", "is_done_column": True},
        ])
        self.board_id = board.id
        self.columns = [c.id for c in self.service.get_columns_for_board(board.id)]
        self.wip = {self.columns[1]: WIP_LIMIT}
        self.done = self.columns[3]
        # the model
        self.order = {c: [] for c in self.columns}   # open cards per column, top to bottom
        self.archived = {}                            # task id -> column
        self.completed = {}                           # task id -> bool (every existing task)
        self.deps = set()                             # (task, prerequisite)

    def teardown(self):
        if hasattr(self, "service"):
            self.service.db.engine.dispose()
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- model helpers --------------------------------------------------------------
    def _column_of(self, task_id):
        return next((c for c, ids in self.order.items() if task_id in ids), None)

    def _exists(self, task_id):
        return task_id in self.completed

    def _reaches(self, start, target):
        """Can we get from ``start`` to ``target`` following task -> prerequisite edges?"""
        seen, stack = set(), [start]
        while stack:
            n = stack.pop()
            if n == target:
                return True
            if n in seen:
                continue
            seen.add(n)
            stack.extend(p for t, p in self.deps if t == n)
        return False

    # -- rules ---------------------------------------------------------------------------
    @rule(target=tasks, col=st.integers(0, 2), title=titles)
    def create(self, col, title):
        column = self.columns[col]
        task = self.service.create_task(self.board_id, column, title)
        self.order[column].append(task.id)
        self.completed[task.id] = False
        return task.id

    @rule(task=tasks, col=st.integers(0, 3), pos=st.integers(0, 8))
    def move(self, task, col, pos):
        if task in self.archived:
            return  # archived cards aren't on the board, so they can't be dragged
        source, target = self._column_of(task), self.columns[col]
        ok, err = self.service.move_task(task, target, pos)
        if not self._exists(task):
            assert not ok
            return
        if source != target and target in self.wip and len(self.order[target]) >= self.wip[target]:
            assert not ok and "WIP" in err, err
            return
        assert ok, err
        self.order[source].remove(task)
        ids = self.order[target]
        ids.insert(max(0, min(pos, len(ids))), task)
        if target == self.done:
            self.completed[task] = True
        elif source == self.done:
            self.completed[task] = False

    @rule(task=tasks)
    def toggle(self, task):
        result = self.service.toggle_task_completion(task)
        if not self._exists(task):
            assert result is None
            return
        self.completed[task] = not self.completed[task]

    @rule(task=tasks)
    def archive(self, task):
        ok = self.service.archive_task(task)
        column = self._column_of(task)
        if column is None:
            return  # deleted (ok False) or already archived (either answer is fine)
        assert ok
        self.order[column].remove(task)
        self.archived[task] = column

    @rule(task=tasks)
    def restore(self, task):
        ok = self.service.restore_task(task)
        if task not in self.archived:
            return
        assert ok
        column = self.archived.pop(task)
        self.order[column].append(task)

    @rule(task=tasks)
    def delete(self, task):
        ok = self.service.delete_task(task)
        assert ok == self._exists(task)
        if not ok:
            return
        column = self._column_of(task)
        if column is not None:
            self.order[column].remove(task)
        self.archived.pop(task, None)
        del self.completed[task]
        self.deps = {(t, p) for t, p in self.deps if task not in (t, p)}

    @rule(task=tasks, prereq=tasks)
    def depend(self, task, prereq):
        ok, message = self.service.add_dependency(task, prereq)
        if not (self._exists(task) and self._exists(prereq)):
            assert not ok, "a dependency on a task that no longer exists is refused"
            return
        if task == prereq:
            assert not ok
            return
        if (task, prereq) in self.deps:
            assert ok  # already there
            return
        creates_cycle = self._reaches(prereq, task)
        assert ok == (not creates_cycle), message
        if ok:
            self.deps.add((task, prereq))

    @precondition(lambda self: bool(self.deps))
    @rule(data=st.data())
    def undepend(self, data):
        task, prereq = data.draw(st.sampled_from(sorted(self.deps)))
        assert self.service.remove_dependency(task, prereq)
        self.deps.discard((task, prereq))

    # -- invariants --------------------------------------------------------------------------
    @invariant()
    def service_agrees_with_the_model(self):
        if not hasattr(self, "service"):
            return
        tasks = self.service.get_tasks_for_board(self.board_id, include_archived=True)
        # the open cards, column by column, in the order the board shows them
        shown = {c: [] for c in self.columns}
        for t in tasks:
            if not t.is_archived:
                shown[t.column_id].append(t.id)
        assert shown == self.order
        # archived, completed and blocked flags
        assert {t.id for t in tasks if t.is_archived} == set(self.archived)
        assert {t.id: bool(t.is_completed) for t in tasks} == self.completed
        for t in tasks:
            blocked = any(not self.completed[p] for task, p in self.deps if task == t.id)
            assert t.is_blocked == blocked, t.id
        # the dependency graph, which stays acyclic
        edges = {(t.id, d.depends_on_task_id) for t in tasks for d in t.prerequisites}
        assert edges == self.deps
        assert not any(self._reaches(p, t) for t, p in edges), "acyclic"

    # WIP limits gate *moves* (asserted in ``move``): a full column refuses cards dragged in.
    # Creating a card in a full column is allowed - imports, recurring tasks and calendar sync
    # create cards too - and the column header shows the overflow in red.


_phases = [p for p in Phase if p is not Phase.shrink or not os.environ.get("KANBAN_MODEL_NO_SHRINK")]
BoardModel.TestCase.settings = settings(
    max_examples=int(os.environ.get("KANBAN_MODEL_EXAMPLES", "25")), stateful_step_count=30, deadline=None,
    phases=_phases, suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much])
test_board_model = pytest.mark.slow(BoardModel.TestCase)
