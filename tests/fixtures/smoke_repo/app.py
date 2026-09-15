class TaskStore:
    def __init__(self) -> None:
        self.tasks: list[str] = []

    def add(self, task: str) -> None:
        self.tasks.append(task)


def handle_task(store: TaskStore, task: str) -> int:
    store.add(task)
    return len(store.tasks)
