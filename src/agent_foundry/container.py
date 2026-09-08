from .builder import Builder, Evaluator
from .catalog import Catalog
from .commands import Commands
from .db import Database
from .deployment import Deployment
from .executor import Executor
from .llm import LLM
from .profiles import ProfileStore
from .queue import JobQueue
from .registry import Registry
from .router import Router
from .sandbox import Sandbox
from .search import ProgramSearch
from .service import AgentService
from .web_auth import WebSessions
from .worker import Worker


class Container:
    def __init__(self, settings):
        self.settings = settings
        self.db = Database(settings)
        self.registry = Registry(self.db)
        self.profiles = ProfileStore(self.db, settings)
        self.web_sessions = WebSessions(self.db, settings)
        self.llm = LLM(settings, self.db, profiles=self.profiles)
        self.search = ProgramSearch(self.db, settings)
        self.router = Router(self.llm, settings)
        self.queue = JobQueue(self.db, settings)
        self.commands = Commands(self.db, settings)
        self.sandbox = Sandbox(self.commands, settings)
        self.executor = Executor(self.registry, self.sandbox, settings)
        self.deployment = Deployment(self.registry, self.sandbox, self.commands, settings)
        self.catalog = Catalog(self.db, self.commands, self.deployment, self.router, settings)
        self.evaluator = Evaluator(self.llm, self.search, self.queue, settings)
        self.builder = Builder(self.llm, self.search, self.registry, self.deployment, self.commands, settings)
        self.evaluator.catalog = self.catalog
        self.builder.catalog = self.catalog
        self.worker = Worker(
            self.queue, self.evaluator, self.builder, self.deployment, settings, self.catalog
        )
        self.service = AgentService(
            self.search, self.router, self.executor, self.llm, self.queue, self.db, settings
        )
        self.service.catalog = self.catalog

    async def open(self):
        await self.db.open()

    async def close(self):
        await self.llm.close()
        await self.db.close()
