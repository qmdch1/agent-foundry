from .builder import Builder, Evaluator
from .commands import Commands
from .db import Database
from .deployment import Deployment
from .executor import Executor
from .llm import LLM
from .queue import JobQueue
from .registry import Registry
from .router import Router
from .sandbox import Sandbox
from .search import ProgramSearch
from .service import AgentService
from .worker import Worker


class Container:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings)
        self.registry = Registry(self.db)
        self.llm = LLM(settings, self.db)
        self.search = ProgramSearch(self.db, settings)
        self.router = Router(self.llm, settings)
        self.queue = JobQueue(self.db, settings)
        self.commands = Commands(self.db, settings)
        self.sandbox = Sandbox(self.commands, settings)
        self.executor = Executor(self.registry, self.sandbox, settings)
        self.deployment = Deployment(self.registry, self.sandbox, self.commands, settings)
        self.evaluator = Evaluator(self.llm, self.search, self.queue, settings)
        self.builder = Builder(self.llm, self.search, self.registry, self.deployment, self.commands, settings)
        self.worker = Worker(self.queue, self.evaluator, self.builder, self.deployment, settings)
        self.service = AgentService(
            self.search, self.router, self.executor, self.llm, self.queue, self.db, settings
        )

    async def open(self):
        await self.db.open()

    async def close(self):
        await self.llm.close()
        await self.db.close()
